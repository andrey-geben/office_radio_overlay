# main.py
import os
import sys
import preflight
import time
import threading
import struct
import faulthandler
from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, session, Response, stream_with_context)
from datetime import timedelta

faulthandler.enable()

from engine import RadioOverlayEngine
from audio_capture import list_input_devices
from audio_output import HardwareOutput
from auth import login_required, admin_required
import announcement_store
import settings_store
import users_store
import config


def _resource_path(relative):
    """Работает и из исходников, и из .exe PyInstaller."""
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.abspath(".")
    return os.path.join(base, relative)


app = Flask(
    __name__,
    template_folder=_resource_path("templates"),
    static_folder=_resource_path("static"),
)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)

engine = RadioOverlayEngine()
sip_client = None

users_store.ensure_default_admin()


def _validate_sip_domain(domain):
    """Проверяет IP-адрес или hostname. Возвращает (ok, error_message)."""
    if not domain:
        return False, "SIP-домен не указан"
    # Похоже на IP? Тогда проверяем строго
    if domain.count(".") >= 2 and domain.replace(".", "").isdigit():
        try:
            import ipaddress
            ipaddress.ip_address(domain)
            return True, None
        except ValueError:
            return False, (
                f"'{domain}' похоже на IP-адрес, но не является валидным. "
                f"Каждый октет должен быть 0–255. Проверьте: ping {domain}"
            )
    # Hostname — пропускаем
    return True, None


def _ensure_chime():
    """Генерирует assets/chime.wav, если его нет."""
    try:
        chime_path = config.SIP_CHIME_FILE
        if not os.path.isabs(chime_path):
            chime_path = os.path.join(settings_store.BASE_DIR, chime_path)
        if os.path.exists(chime_path):
            return
        from make_chime import generate_chime
        n = generate_chime(chime_path, sample_rate=config.SAMPLE_RATE)
        print(f"[boot] Сгенерирован сигнал внимания: {chime_path} ({n/44100:.2f}с)")
    except Exception as e:
        print(f"[boot] Не удалось сгенерировать chime: {e}")


def _boot_autostart():
    global sip_client
    saved = settings_store.load()

    if saved.get("auto_start_engine"):
        print("[boot] Автозапуск движка...")
        try:
            engine.start()
        except Exception as e:
            print(f"[boot] Ошибка автозапуска движка: {e}")

    if saved.get("auto_start_sip") and saved.get("sip_domain") and \
       saved.get("sip_username") and saved.get("sip_password"):
        print("[boot] Автозапуск SIP...")
        try:
            from sip_client import SipClient

            try:
                engine.reset_sip_state()
            except Exception:
                pass

            playback_device = saved.get("sip_playback_device")
            if playback_device is None:
                playback_device = 6

            sip_client = SipClient(
                engine,
                domain=saved["sip_domain"],
                username=saved["sip_username"],
                password=saved["sip_password"],
                port=saved.get("sip_port", 5060),
                transport=saved.get("sip_transport", "udp"),
                playback_device=playback_device,
            )
            ok = sip_client.start()
            print(f"[boot] SIP-клиент создан: start()={ok}")
        except Exception as e:
            print(f"[boot] Ошибка автозапуска SIP: {e}")
            sip_client = None


# ==================== Авторизация ====================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        user = users_store.verify(u, p)
        if user:
            session.permanent = True
            session["logged_in"] = True
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_change_password"] = bool(user.get("must_change_password", False))
            if user.get("must_change_password"):
                return redirect(url_for("change_password"))
            nxt = request.args.get("next") or "/"
            return redirect(nxt)
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    must_change = session.get("must_change_password", False)

    if request.method == "POST":
        new_p = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        current = request.form.get("current_password", "")

        if not must_change:
            if not users_store.verify(session.get("username"), current):
                error = "Неверный текущий пароль"

        if not error:
            if len(new_p) < 4:
                error = "Пароль должен быть минимум 4 символа"
            elif new_p != confirm:
                error = "Пароли не совпадают"
            elif must_change and new_p.lower() == "admin":
                error = "Нельзя использовать пароль по умолчанию"

        if not error:
            try:
                users_store.update_password(session["username"], new_p, must_change=False)
                session["must_change_password"] = False
                return redirect(url_for("index"))
            except Exception as e:
                error = str(e)

    return render_template(
        "change_password.html",
        username=session.get("username"),
        must_change=must_change,
        error=error,
    )


# ==================== UI ====================
@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        username=session.get("username", "?"),
        role=session.get("role", "operator"),
    )
# ==================== HTTP-стрим ====================
def _wav_header(sample_rate, channels, bits):
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE" +
        b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                              byte_rate, block_align, bits) +
        b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


@app.route("/stream")
def audio_stream():
    """Публичный поток — без @login_required, чтобы сотрудники могли слушать без пароля."""
    def generate():
        yield _wav_header(config.SAMPLE_RATE, config.CHANNELS, 16)
        silence = bytes(config.BLOCK_SIZE * 2)
        while True:
            chunk = engine.get_output_chunk(timeout=0.5)
            yield chunk if chunk is not None else silence

    return Response(
        stream_with_context(generate()),
        mimetype="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ==================== Статус ====================
@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"state": engine.get_state(), "settings": engine.get_settings()})


@app.route("/api/about")
@login_required
def api_about():
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")
    readme_md = ""
    if os.path.exists(readme_path):
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                readme_md = f.read()
        except Exception:
            pass
    return jsonify({
        "version": "1.1.0",
        "author": "Andrey Geben",
        "github": "https://github.com/andrey-geben/office_radio_overlay",
        "license": "MIT",
        "year": 2026,
        "readme_md": readme_md,
    })


@app.route("/api/stream-url")
@login_required
def api_stream_url():
    """Список URL, по которым можно слушать поток."""
    import socket
    urls = [
        {"label": "Локально (этот ПК)", "url": "http://127.0.0.1:5000/stream"}
    ]
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip.startswith(("192.", "10.", "172.")) and not ip.startswith("127."):
                urls.append({
                    "label": "В офисе (LAN)",
                    "url": f"http://{ip}:5000/stream",
                })
                break
    except Exception:
        pass
    try:
        urls.append({
            "label": "По имени хоста",
            "url": f"http://{socket.gethostname()}:5000/stream",
        })
    except Exception:
        pass
    return jsonify({"items": urls})


# ==================== Управление движком ====================
@app.route("/api/start", methods=["POST"])
@admin_required
def api_start():
    return jsonify({"ok": engine.start()})


@app.route("/api/stop", methods=["POST"])
@admin_required
def api_stop():
    engine.stop()
    return jsonify({"ok": True})


@app.route("/api/devices")
@admin_required
def api_devices():
    return jsonify({"items": list_input_devices()})


@app.route("/api/outputs")
@admin_required
def api_outputs():
    return jsonify({"items": HardwareOutput.list_output_devices()})


@app.route("/api/settings/output", methods=["POST"])
@admin_required
def api_set_output():
    data = request.get_json(silent=True) or {}
    if "hw_output_device" not in data:
        return jsonify({"ok": False, "error": "hw_output_device не передан"}), 400
    ok = engine.set_hw_output(data.get("hw_output_device"))
    return jsonify({"ok": ok, "hw_output": engine.get_state().get("hw_output")})


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_settings():
    data = request.get_json(silent=True) or {}
    numeric = {"tone_freq", "detection_threshold", "dcs_freq", "dcs_threshold",
               "ducking_level", "announcement_level", "fade_out_ms", "fade_in_ms",
               "tone_timeout_seconds", "fallback_timeout_seconds", "fallback_retry_main_seconds"}
    boolean = {"simulate_tone", "tone_timeout_enabled", "fallback_enabled"}
    source_keys = {"radio_url", "source_type", "input_device"}

    filtered = {}
    for k, v in data.items():
        if k in numeric:
            try:
                filtered[k] = float(v)
            except (TypeError, ValueError):
                continue
        elif k in boolean:
            filtered[k] = bool(v)
        elif k == "radio_url" and isinstance(v, str):
            filtered[k] = v
        elif k == "source_type" and v in ("stream", "device"):
            filtered[k] = v
        elif k == "input_device":
            if v is None or v == "":
                filtered[k] = None
            else:
                try:
                    filtered[k] = int(v)
                except (TypeError, ValueError):
                    continue
        elif k == "active_announcement_id" and (v is None or isinstance(v, str)):
            filtered[k] = v

    was_running = engine.get_state()["running"]
    source_changed = any(k in filtered for k in source_keys)
    engine.update_settings(**filtered)

    if source_changed and was_running:
        def restart():
            time.sleep(0.3)
            engine.stop()
            engine.start()
        threading.Thread(target=restart, daemon=True).start()
        return jsonify({"ok": True, "restarting": True, "settings": engine.get_settings()})

    return jsonify({"ok": True, "settings": engine.get_settings()})
# ==================== Объявления ====================
@app.route("/api/announcements", methods=["GET"])
@login_required
def api_list_announcements():
    return jsonify({"items": announcement_store.list_with_progress()})


@app.route("/api/announcements", methods=["POST"])
@login_required
def api_upload_announcement():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Файл не передан"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Пустое имя файла"}), 400
    dow_raw = request.form.getlist("days_of_week")
    try:
        item = announcement_store.add_announcement(
            f,
            request.form.get("description", ""),
            start_time=request.form.get("start_time") or None,
            end_time=request.form.get("end_time") or None,
            repeat_limit=request.form.get("repeat_limit") or 0,
            min_interval_minutes=request.form.get("min_interval_minutes") or 0,
            days_of_week=dow_raw if dow_raw else None,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not engine.get_settings().get("active_announcement_id"):
        engine.update_settings(active_announcement_id=item["id"])
    return jsonify({"ok": True, "item": item})


@app.route("/api/announcements/<ann_id>", methods=["DELETE"])
@login_required
def api_delete_announcement(ann_id):
    ok = announcement_store.delete_announcement(ann_id)
    if ok and engine.get_settings().get("active_announcement_id") == ann_id:
        engine.update_settings(active_announcement_id=None)
    return jsonify({"ok": ok})


@app.route("/api/announcements/<ann_id>/activate", methods=["POST"])
@login_required
def api_activate_announcement(ann_id):
    if not announcement_store.get_announcement(ann_id):
        return jsonify({"ok": False, "error": "not found"}), 404
    engine.update_settings(active_announcement_id=ann_id)
    return jsonify({"ok": True})


@app.route("/api/announcements/<ann_id>/schedules", methods=["POST"])
@login_required
def api_upsert_schedule(ann_id):
    data = request.get_json(silent=True) or {}
    try:
        item, sched = announcement_store.upsert_schedule(
            ann_id,
            data.get("schedule_id") or None,
            data.get("start_time") or None,
            data.get("end_time") or None,
            data.get("repeat_limit") or 0,
            data.get("min_interval_minutes") or 0,
            data.get("days_of_week"),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not item or not sched:
        return jsonify({"ok": False, "error": "not found"}), 404
    if engine.get_settings().get("active_announcement_id") == ann_id:
        engine.reload_announcement()
    return jsonify({"ok": True, "item": item, "schedule": sched})


@app.route("/api/announcements/<ann_id>/schedules/<sched_id>", methods=["DELETE"])
@login_required
def api_delete_schedule(ann_id, sched_id):
    ok = announcement_store.delete_schedule(ann_id, sched_id)
    return jsonify({"ok": ok})

# ==================== Субтоны ====================
@app.route("/api/scan_tone", methods=["POST"])
@admin_required
def api_scan_tone():
    return jsonify(engine.scan_tone())


@app.route("/api/apply_tone", methods=["POST"])
@admin_required
def api_apply_tone():
    data = request.get_json(silent=True) or {}
    if "freq" not in data:
        return jsonify({"ok": False, "error": "freq не передан"}), 400
    try:
        freq = float(data["freq"])
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "freq не число"}), 400
    engine.update_settings(tone_freq=freq)
    return jsonify({"ok": True, "settings": engine.get_settings()})


# ==================== Fallback ====================
@app.route("/api/fallback/sources", methods=["GET"])
@admin_required
def api_get_fallback_sources():
    return jsonify({"items": engine.get_settings().get("fallback_sources", [])})


@app.route("/api/fallback/sources", methods=["POST"])
@admin_required
def api_set_fallback_sources():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    ok = engine.set_fallback_sources(items)
    return jsonify({"ok": ok, "items": engine.get_settings().get("fallback_sources", [])})


# ==================== DTMF ====================
@app.route("/api/dtmf/labels", methods=["GET"])
@admin_required
def api_dtmf_labels_get():
    return jsonify({"items": engine.get_settings().get("dtmf_labels", [])})


@app.route("/api/dtmf/labels", methods=["POST"])
@admin_required
def api_dtmf_labels_set():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    cleaned = engine.set_dtmf_labels(items)
    return jsonify({"ok": True, "items": cleaned})


@app.route("/api/dtmf/test", methods=["POST"])
@admin_required
def api_dtmf_test():
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip().upper()
    if not label:
        return jsonify({"ok": False, "error": "label не передан"}), 400
    ok = engine.trigger_dtmf_test(label)
    return jsonify({"ok": ok, "label": label})


# ==================== SIP ====================
@app.route("/api/sip/save", methods=["POST"])
@admin_required
def api_sip_save():
    data = request.get_json(silent=True) or {}
    domain = (data.get("sip_domain") or "").strip()
    username = (data.get("sip_username") or "").strip()
    password = data.get("sip_password") or ""
    try:
        port = int(data.get("sip_port", 5060))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "sip_port должен быть числом"}), 400
    transport = data.get("sip_transport", "udp")
    if transport not in ("udp", "tcp"):
        transport = "udp"
    chime_enabled = bool(data.get("sip_chime_enabled", True))

    ok, err = _validate_sip_domain(domain)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    saved = settings_store.load()
    if not password:
        password = saved.get("sip_password", "")

    settings_store.update(
        sip_domain=domain, sip_username=username, sip_password=password,
        sip_port=port, sip_transport=transport,
        sip_chime_enabled=chime_enabled,
    )
    engine.update_settings(sip_chime_enabled=chime_enabled)
    return jsonify({"ok": True})


@app.route("/api/sip/start", methods=["POST"])
@admin_required
def api_sip_start():
    global sip_client

    data = request.get_json(silent=True) or {}
    domain = (data.get("sip_domain") or "").strip()
    username = (data.get("sip_username") or "").strip()
    password = data.get("sip_password") or ""
    try:
        port = int(data.get("sip_port", 5060))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "sip_port должен быть числом"}), 400
    transport = data.get("sip_transport", "udp")
    if transport not in ("udp", "tcp"):
        transport = "udp"

    if not domain or not username:
        return jsonify({"ok": False, "error": "Заполните домен и логин"}), 400

    ok, err = _validate_sip_domain(domain)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    saved = settings_store.load()
    if not password:
        password = saved.get("sip_password", "")
    if not password:
        return jsonify({"ok": False, "error": "Введите пароль"}), 400

    settings_store.update(
        sip_domain=domain, sip_username=username, sip_password=password,
        sip_port=port, sip_transport=transport,
    )

    # Уже работает — не перезапускаем (pjsua2 не любит повторную инициализацию)
    if sip_client is not None and sip_client.is_running():
        return jsonify({
            "ok": True,
            "already_running": True,
            "message": "SIP уже подключён. Для смены настроек нажмите «Отключить».",
        })

    # Сброс «залипшего» SIP-состояния
    try:
        engine.reset_sip_state()
    except Exception:
        pass

    # Переиспользуем существующий клиент (Endpoint singleton в sip_client.py)
    if sip_client is not None:
        sip_client.domain = domain
        sip_client.username = username
        sip_client.password = password
        sip_client.port = port
        sip_client.transport = transport
        ok = sip_client.start()
        return jsonify({"ok": ok, "reused": True})

    # Первый запуск — создаём клиент
    try:
        from sip_client import SipClient
    except ImportError as e:
        return jsonify({"ok": False, "error": f"pjsua2 не установлен: {e}"}), 500

    # WMME dev_id для CABLE In 16ch (VB-Audio) — для микса SIP в линии.
    # Из вашего list_wmme.py: CABLE In 16ch = WMME dev_id 6.
    # Если у вас другой индекс — измените в панели «Устройство вывода PJSIP».
    saved = settings_store.load()
    playback_device = saved.get("sip_playback_device")
    if playback_device is None:
        playback_device = 6   # fallback: CABLE In 16ch по умолчанию

    sip_client = SipClient(
        engine, domain=domain, username=username,
        password=password, port=port, transport=transport,
        playback_device=playback_device,
    )
    ok = sip_client.start()
    return jsonify({"ok": ok, "settings": engine.get_settings()})


@app.route("/api/sip/stop", methods=["POST"])
@admin_required
def api_sip_stop():
    global sip_client
    if sip_client is not None:
        sip_client.stop()
        # ВАЖНО: не обнуляем sip_client — Endpoint остаётся жив,
        # при следующем start() переиспользуется.
    engine.reset_sip_state()
    return jsonify({"ok": True})


@app.route("/api/sip/status")
@login_required
def api_sip_status():
    running = bool(sip_client is not None and sip_client.is_running())
    saved = settings_store.load()
    return jsonify({
        "running": running,
        "engine_sip_active": engine.is_sip_active(),
        "sip_domain": saved.get("sip_domain", ""),
        "sip_username": saved.get("sip_username", ""),
        "sip_password_set": bool(saved.get("sip_password")),
        "sip_port": saved.get("sip_port", 5060),
        "sip_transport": saved.get("sip_transport", "udp"),
        "auto_start_sip": saved.get("auto_start_sip", False),
        "auto_start_engine": saved.get("auto_start_engine", False),
        "sip_chime_enabled": saved.get("sip_chime_enabled", True),
    })


@app.route("/api/sip/test-chime", methods=["POST"])
@admin_required
def api_sip_test_chime():
    result = engine.test_chime()
    return jsonify(result)


@app.route("/api/autostart", methods=["POST"])
@admin_required
def api_autostart():
    data = request.get_json(silent=True) or {}
    settings_store.update(
        auto_start_engine=bool(data.get("auto_start_engine")),
        auto_start_sip=bool(data.get("auto_start_sip")),
    )
    return jsonify({"ok": True})
# ==================== Пользователи ====================
@app.route("/api/users", methods=["GET"])
@admin_required
def api_list_users():
    return jsonify({"items": users_store.list_users()})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    data = request.get_json(silent=True) or {}
    try:
        user = users_store.create_user(
            (data.get("username") or "").strip(),
            data.get("password") or "",
            role=data.get("role", "operator"),
            must_change=bool(data.get("must_change_password", True)),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({
        "ok": True,
        "user": {k: v for k, v in user.items() if k != "password_hash"},
    })


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def api_delete_user(username):
    if username == session.get("username"):
        return jsonify({"ok": False, "error": "Нельзя удалить самого себя"}), 400
    try:
        users_store.delete_user(username)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/users/<username>/reset-password", methods=["POST"])
@admin_required
def api_reset_user_password(username):
    if username == session.get("username"):
        return jsonify({"ok": False, "error": "Свой пароль меняйте через «🔑 Пароль»"}), 400
    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    try:
        users_store.update_password(username, new_password, must_change=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})

@app.route("/api/preflight")
def api_preflight():
    from preflight import get_last_results, CRITICAL, WARNING
    results = get_last_results()
    return jsonify({
        "critical": [m for s, m in results if s == CRITICAL],
        "warnings": [m for s, m in results if s == WARNING],
    })

# ==================== Запуск ====================
if __name__ == '__main__':
    # 1) Проверка компонентов
    results = preflight.run_checks()
    # 2) Показать пользователю (только critical/warning или при первом запуске)
    saved = settings_store.load()
    first_run = not saved.get("preflight_done", False)
    ok = preflight.report_to_user(results, first_run=first_run)
    if not ok:
        # Critical — выходим
        sys.exit(1)
    # Запомнить, что первая проверка пройдена
    if first_run:
        settings_store.update(preflight_done=True)
    # ... ваш запуск Flask ...
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            engine._hw_output.stop()
        except Exception:
            pass
        try:
            import sounddevice as sd
            sd._terminate()
        except Exception:
            pass