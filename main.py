# main.py
import os
import queue
import sys

# --- Инициализация логирования (до всех остальных импортов) ---
from logging_setup import setup_logging
setup_logging()

# --- Frozen mode: этот же .exe запущен как SIP-worker ---
if "--sip-worker" in sys.argv:
    from sip_worker import main as sip_worker_main
    sip_worker_main()
    sys.exit(0)

import atexit
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
print(f"[boot] direct chime: {hasattr(engine, '_feed_chime_direct')}")
sip_client = None

users_store.ensure_default_admin()


def _cleanup_on_exit():
    global sip_client
    try:
        if sip_client is not None:
            sip_client.stop()
            sip_client = None
    except Exception:
        pass
    try:
        engine.stop()
    except Exception:
        pass
    try:
        engine._hw_output.stop()
    except Exception:
        pass
    try:
        import sounddevice as sd
        sd._terminate()
    except Exception:
        pass


atexit.register(_cleanup_on_exit)


def _validate_sip_domain(domain):
    """Проверяет IP-адрес или hostname. Возвращает (ok, error_message)."""
    if not domain:
        return False, "SIP-домен не указан"
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
    return True, None


def _ensure_chime():
    """Генерирует assets/chime.wav, если его нет."""
    try:
        chime_path = config.SIP_CHIME_FILE
        if not os.path.isabs(chime_path):
            chime_path = os.path.join(settings_store.BASE_DIR, chime_path)
        if os.path.exists(chime_path):
            return
        os.makedirs(os.path.dirname(chime_path), exist_ok=True)
        from make_chime import generate_chime
        n = generate_chime(chime_path, sample_rate=config.SAMPLE_RATE)
        print(f"[boot] Сгенерирован сигнал внимания: {chime_path} ({n / config.SAMPLE_RATE:.2f}с)")
    except Exception as e:
        print(f"[boot] Не удалось сгенерировать chime: {e}")


_net_speed_cache = {"ts": 0.0, "mbps": 0}


def _net_link_speed_mbps(ttl: float = 60.0) -> int:
    """Скорость активной сетевой карты в Мбит/с (для шкалы NET).

    Берём максимальную скорость среди физических интерфейсов, которые
    подняты и имеют IPv4. Виртуальные (Hyper-V, VMware, loopback) и
    туннели пропускаем, иначе шкала врёт.
    """
    import time as _time
    now = _time.time()
    if now - _net_speed_cache["ts"] < ttl and _net_speed_cache["mbps"]:
        return _net_speed_cache["mbps"]

    skip = ("loopback", "isatap", "teredo", "vethernet", "hyper-v",
            "vmware", "virtualbox", "vbox", "tap-", "tun", "ppp")
    best = 0
    try:
        import psutil
        import socket as _socket
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for name, st in stats.items():
            lname = name.lower()
            if not st.isup or st.speed <= 0:
                continue
            if any(k in lname for k in skip):
                continue
            has_ipv4 = any(
                a.family == _socket.AF_INET and not a.address.startswith("127.")
                for a in addrs.get(name, [])
            )
            if not has_ipv4:
                continue
            best = max(best, int(st.speed))
    except Exception:
        best = 0

    _net_speed_cache["mbps"] = best
    _net_speed_cache["ts"] = now
    if best:
        print(f"[net] Скорость сетевой карты: {best} Мбит/с")
    return best


def _server_addresses():
    """IP-адреса самой серверной машины (чтобы узнать «свой» клиент)."""
    import socket
    addrs = {"127.0.0.1", "::1"}
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            addrs.add(ip)
    except Exception:
        pass
    return addrs


def _client_is_server():
    """Панель открыта на самой серверной машине?"""
    ra = (request.remote_addr or "").strip()
    if not ra:
        return False
    if ra in ("127.0.0.1", "::1"):
        return True
    return ra in _server_addresses()


def _web_player_state(settings=None):
    """Разрешён ли веб-плеер: (allowed, reason)."""
    settings = settings or engine.get_settings()
    # На сервере плеер запрещён (двойной звук), на удалённом ПК — всегда
    # разрешён: только там можно проверить, что сервер реально звучит.
    if _client_is_server() and not settings.get("web_player_allow_local", False):
        return False, "server"
    return True, ""


def _hw_output_device_name():
    """Имя выбранного серверного устройства вывода (для PJSIP по имени)."""
    try:
        dev = engine.get_settings().get("hw_output_device")
    except Exception:
        dev = None
    if dev is None:
        return None
    try:
        for it in HardwareOutput.list_output_devices():
            if int(it["index"]) == int(dev):
                return it["name"]
    except Exception:
        pass
    return None


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

            # SIP всегда звучит на том же устройстве, что и серверный эфир
            playback_device = saved.get("hw_output_device")
            playback_name = _hw_output_device_name()

            sip_client = SipClient(
                engine,
                domain=saved["sip_domain"],
                username=saved["sip_username"],
                password=saved["sip_password"],
                port=saved.get("sip_port", 5060),
                transport=saved.get("sip_transport", "udp"),
                playback_device=playback_device,
                playback_device_name=playback_name,
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
@login_required
def audio_stream():
    """Поток эфира для браузера оператора (только после входа).

    На самой серверной машине поток закрыт: браузер там играет в то же
    устройство, что и эфир, и получается двойной звук. Разрешается
    настройкой web_player_allow_local.
    """
    _settings = engine.get_settings()
    if _client_is_server() and not _settings.get("web_player_allow_local", False):
        return jsonify({
            "ok": False,
            "error": "Веб-плеер отключён на сервере: звук пойдёт в серверное устройство. "
                     "Разрешить можно в «Выход аудио» → «Разрешить веб-плеер на сервере».",
        }), 403

    def generate():
        sub = engine.subscribe_output()
        try:
            yield _wav_header(config.SAMPLE_RATE, config.CHANNELS, 16)
            silence = bytes(config.BLOCK_SIZE * 2)
            while True:
                try:
                    chunk = sub.get(timeout=0.5)
                except queue.Empty:
                    chunk = None
                yield chunk if chunk is not None else silence
        finally:
            engine.unsubscribe_output(sub)

    return Response(
        stream_with_context(generate()),
        mimetype="audio/wav",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== Статус ====================
# ------------ подсказка -----------------
# list_output_devices() каждый раз инициализирует PortAudio — на опросе
# раз в секунду это лишняя нагрузка. Кэшируем на 10 секунд.
_outputs_cache = {"ts": 0.0, "items": []}


def _outputs_cached(ttl: float = 10.0):
    import time as _time
    now = _time.time()
    if now - _outputs_cache["ts"] > ttl:
        try:
            _outputs_cache["items"] = HardwareOutput.list_output_devices()
        except Exception:
            _outputs_cache["items"] = []
        _outputs_cache["ts"] = now
    return _outputs_cache["items"]


@app.route("/api/status")
@login_required
def api_status():
    """
    Состояние + флаг, можно ли показывать веб-плеер.

    Веб-плеер играет в браузере оператора. Если на сервере всего одно
    устройство вывода, легко случайно получить двойной звук — в этом
    случае плеер в панели скрываем.
    """
    outputs = _outputs_cached()
    settings = engine.get_settings()
    allowed, reason = _web_player_state(settings)
    state = engine.get_state()
    if state.get("running") and not engine.is_alive():
        # Поток движка умер, а флаг остался — показываем правду.
        state["running"] = False
        logger_ = app.logger
        logger_.warning("Движок числился запущенным, но поток не жив — сбрасываю статус")
    return jsonify({
        "state": state,
        "engine_alive": engine.is_alive(),
        "hw_output_active": engine.is_hw_output_running(),
        "settings": settings,
        "outputs_count": len(outputs),
        "client_is_server": _client_is_server(),
        "net_speed_mbps": _net_link_speed_mbps(),
        "web_player_allowed": allowed,
        "web_player_blocked_reason": reason,
    })


@app.route("/api/about")
@login_required
def api_about():
    readme_path = _resource_path("README.md")
    if not os.path.exists(readme_path):
        readme_path = os.path.join(settings_store.BASE_DIR, "README.md")
    readme_md = ""
    if os.path.exists(readme_path):
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                readme_md = f.read()
        except Exception:
            pass
    return jsonify({
        "version": "1.2.4",
        "author": "Andrey Geben",
        "github": "https://github.com/andrey-geben/office_radio_overlay",
        "license": "MIT",
        "year": 2026,
        "readme_md": readme_md,
    })

@app.route("/api/logs")
@admin_required
def api_logs():
    """Возвращает последние N строк лога (только для админа)."""
    import os
    from logging_setup import LOG_FILE

    n = int(request.args.get("n", 200))
    if not os.path.exists(LOG_FILE):
        return jsonify({"ok": False, "error": "Файл лога не найден"})

    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n:]
        return jsonify({"ok": True, "lines": tail, "total": len(lines)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
    """Остановка эфира. ?force=1 — дополнительно убить все ffmpeg процесса."""
    import time as _time
    force = bool(request.args.get("force"))
    if force:
        killed = engine.force_stop()
    else:
        engine.stop()
        killed = 0

    _time.sleep(0.3)
    state = engine.get_state()
    running = bool(state.get("running"))
    # сколько ffmpeg осталось у нашего процесса
    left = 0
    try:
        import psutil
        for child in psutil.Process().children(recursive=True):
            try:
                if (child.name() or "").lower().startswith("ffmpeg"):
                    left += 1
            except Exception:
                continue
    except Exception:
        pass

    app.logger.info("СТОП: force=%s, running=%s, ffmpeg=%d, убито=%d",
                    force, running, left, killed)
    return jsonify({"ok": True, "running": running, "ffmpeg_left": left})


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
    if ok and sip_client is not None:
        # SIP обязан звучать на том же устройстве, что и серверный эфир
        try:
            sip_client.set_playback_device(_hw_output_device_name())
        except Exception as e:
            print(f"[output] не удалось переключить SIP: {e}")
    return jsonify({"ok": ok, "hw_output": engine.get_state().get("hw_output")})


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_settings():
    data = request.get_json(silent=True) or {}
    numeric = {"tone_freq", "detection_threshold", "dcs_freq", "dcs_threshold",
               "ducking_level", "announcement_level", "fade_out_ms", "fade_in_ms",
               "tone_timeout_seconds", "fallback_timeout_seconds", "fallback_retry_main_seconds"}
    boolean = {"simulate_tone", "tone_timeout_enabled", "fallback_enabled",
               "web_player_allow_local"}
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
def _sip_common_settings(data):
    """Общие настройки SIP из запроса: сигнал внимания и пауза перед ответом."""
    chime_enabled = bool(data.get("sip_chime_enabled", True))
    try:
        answer_delay_ms = int(data.get("sip_answer_delay_ms", 400))
    except (TypeError, ValueError):
        answer_delay_ms = 400
    answer_delay_ms = max(0, min(3000, answer_delay_ms))
    return chime_enabled, answer_delay_ms


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
    try:
        answer_delay_ms = int(data.get("sip_answer_delay_ms", 400))
    except (TypeError, ValueError):
        answer_delay_ms = 400
    answer_delay_ms = max(0, min(3000, answer_delay_ms))

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
        sip_answer_delay_ms=answer_delay_ms,
    )
    engine.update_settings(sip_chime_enabled=chime_enabled,
                           sip_answer_delay_ms=answer_delay_ms)
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

    chime_enabled, answer_delay_ms = _sip_common_settings(data)

    settings_store.update(
        sip_domain=domain, sip_username=username, sip_password=password,
        sip_port=port, sip_transport=transport,
        sip_chime_enabled=chime_enabled, sip_answer_delay_ms=answer_delay_ms,
    )
    engine.update_settings(sip_chime_enabled=chime_enabled,
                           sip_answer_delay_ms=answer_delay_ms)

    if sip_client is not None and sip_client.is_running():
        return jsonify({
            "ok": True,
            "already_running": True,
            "message": "SIP уже подключён. Для смены настроек нажмите «Отключить».",
        })

    try:
        engine.reset_sip_state()
    except Exception:
        pass

    playback_device = saved.get("hw_output_device")
    playback_name = _hw_output_device_name()

    if sip_client is not None:
        sip_client.domain = domain
        sip_client.username = username
        sip_client.password = password
        sip_client.port = port
        sip_client.transport = transport
        sip_client.playback_device = playback_device
        sip_client.playback_device_name = playback_name
        ok = sip_client.start()
        return jsonify({"ok": ok, "reused": True})

    try:
        from sip_client import SipClient
    except ImportError as e:
        return jsonify({"ok": False, "error": f"pjsua2 не установлен: {e}"}), 500

    sip_client = SipClient(
        engine, domain=domain, username=username,
        password=password, port=port, transport=transport,
        playback_device=playback_device,
        playback_device_name=playback_name,
    )
    ok = sip_client.start()
    return jsonify({"ok": ok, "settings": engine.get_settings()})


@app.route("/api/sip/stop", methods=["POST"])
@admin_required
def api_sip_stop():
    global sip_client
    if sip_client is not None:
        sip_client.stop()
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
        "sip_answer_delay_ms": saved.get("sip_answer_delay_ms", 400),
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

# ==================== Автооткрытие браузера ====================
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "5000"))


def _browser_url():
    """Адрес, который открываем: BROWSER_URL → env APP_URL → localhost."""
    url = os.environ.get("APP_URL") or getattr(config, "BROWSER_URL", None)
    if url:
        return url
    return f"http://127.0.0.1:{APP_PORT}/"


def _browser_autostart_enabled():
    """Приоритет: переменная окружения → settings.json → config.py."""
    env = os.environ.get("AUTO_OPEN_BROWSER", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        saved = settings_store.load()
        if "auto_open_browser" in saved:
            return bool(saved["auto_open_browser"])
    except Exception:
        pass
    return bool(getattr(config, "AUTO_OPEN_BROWSER", True))


def _wait_for_server(port=APP_PORT, timeout=20.0):
    """Ждём, пока Flask реально начнёт принимать соединения."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _open_browser_when_ready():
    if not _wait_for_server():
        print("[BOOT] сервер не поднялся — браузер не открываю")
        return
    url = _browser_url()
    try:
        import webbrowser
        webbrowser.open(url)
        print(f"[BOOT] открыт браузер: {url}")
    except Exception as e:
        print(f"[BOOT] не удалось открыть браузер: {e}")


_single_instance_handle = None


def _acquire_single_instance():
    """Не даём запустить второй exe: иначе порт занят и путаница."""
    global _single_instance_handle
    lock_path = os.path.join(settings_store.BASE_DIR, "assets", "app.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+")
    except Exception:
        return True
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except Exception:
            pass
        return False
    _single_instance_handle = fh
    return True


if __name__ == "__main__":
    if not _acquire_single_instance():
        preflight.show_messagebox(
            "Office Radio Overlay уже запущен.\n\n"
            "Закройте уже работающее окно (или завершите процесс "
            "OfficeRadioOverlay.exe) и запустите снова.",
            is_error=True,
        )
        sys.exit(0)
    _ensure_chime()
    results = preflight.run_checks()
    saved = settings_store.load()
    first_run = not saved.get("preflight_done", False)
    ok = preflight.report_to_user(results, first_run=first_run)
    if not ok:
        sys.exit(1)
    if first_run:
        settings_store.update(preflight_done=True)

    _boot_autostart()

    # Автооткрытие браузера — в отдельном потоке, чтобы не блокировать Flask
    if _browser_autostart_enabled():
        threading.Timer(1.0, _open_browser_when_ready).start()

    try:
        app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup_on_exit()
