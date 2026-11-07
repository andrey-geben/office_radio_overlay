# main.py
import threading
import struct
from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, session, Response, stream_with_context)
from datetime import timedelta
from engine import RadioOverlayEngine
from audio_capture import list_input_devices
from audio_output import HardwareOutput
from auth import login_required
import announcement_store
import settings_store
import config

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(hours=config.SESSION_LIFETIME_HOURS)
engine = RadioOverlayEngine()

# SIP-клиент — глобальный, создаётся/останавливается через API
sip_client = None


# --- Авторизация ---
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == config.AUTH_USERNAME and p == config.AUTH_PASSWORD:
            session.permanent = True
            session["logged_in"] = True
            session["username"] = u
            nxt = request.args.get("next") or "/"
            return redirect(nxt)
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", username=session.get("username", "?"))


# --- HTTP-стрим для браузера ---
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
        },
    )


# --- Статус / управление ---
@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"state": engine.get_state(), "settings": engine.get_settings()})


@app.route("/api/devices")
@login_required
def api_devices():
    return jsonify({"items": list_input_devices()})


@app.route("/api/outputs")
@login_required
def api_outputs():
    return jsonify({"items": HardwareOutput.list_output_devices()})


@app.route("/api/settings/output", methods=["POST"])
@login_required
def api_set_output():
    data = request.get_json(silent=True) or {}
    if "hw_output_device" not in data:
        return jsonify({"ok": False, "error": "hw_output_device не передан"}), 400
    ok = engine.set_hw_output(data.get("hw_output_device"))
    return jsonify({"ok": ok, "hw_output": engine.get_state().get("hw_output")})


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    return jsonify({"ok": engine.start()})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    engine.stop()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    data = request.get_json(silent=True) or {}
    numeric = {"tone_freq", "detection_threshold", "dcs_freq", "dcs_threshold",
               "ducking_level", "announcement_level", "fade_out_ms", "fade_in_ms",
               "tone_timeout_seconds"}
    boolean = {"simulate_tone", "tone_timeout_enabled"}
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
            engine.stop()
            engine.start()
        threading.Thread(target=restart, daemon=True).start()
        return jsonify({"ok": True, "restarting": True, "settings": engine.get_settings()})

    return jsonify({"ok": True, "settings": engine.get_settings()})


# --- Объявления ---
@app.route("/api/announcements", methods=["GET"])
@login_required
def api_list_announcements():
    return jsonify({"items": announcement_store.list_announcements()})


@app.route("/api/announcements", methods=["POST"])
@login_required
def api_upload_announcement():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "Файл не передан"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"ok": False, "error": "Пустое имя"}), 400
    try:
        item = announcement_store.add_announcement(
            f,
            request.form.get("description", ""),
            start_time=request.form.get("start_time") or None,
            end_time=request.form.get("end_time") or None,
            repeat_limit=request.form.get("repeat_limit") or 0,
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


@app.route("/api/announcements/<ann_id>/schedule", methods=["POST"])
@login_required
def api_update_schedule(ann_id):
    data = request.get_json(silent=True) or {}
    item = announcement_store.update_schedule(
        ann_id,
        data.get("start_time") or None,
        data.get("end_time") or None,
        data.get("repeat_limit") or 0,
    )
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    if engine.get_settings().get("active_announcement_id") == ann_id:
        engine.reload_announcement()
    return jsonify({"ok": True, "item": item})


# --- Сканирование субтонов ---
@app.route("/api/scan_tone", methods=["POST"])
@login_required
def api_scan_tone():
    return jsonify(engine.scan_tone())


@app.route("/api/apply_tone", methods=["POST"])
@login_required
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


# --- SIP / ЛСО ---
def _get_sip_settings():
    saved = settings_store.load()
    return {
        "sip_domain": saved.get("sip_domain", ""),
        "sip_username": saved.get("sip_username", ""),
        "sip_password": saved.get("sip_password", ""),
        "sip_port": saved.get("sip_port", 5060),
        "sip_transport": saved.get("sip_transport", "udp"),
    }


@app.route("/api/sip/save", methods=["POST"])
@login_required
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

    saved = settings_store.load()
    # если пароль не введён — сохраняем ранее сохранённый
    if not password:
        password = saved.get("sip_password", "")

    settings_store.update(
        sip_domain=domain,
        sip_username=username,
        sip_password=password,
        sip_port=port,
        sip_transport=transport,
    )
    return jsonify({"ok": True})


@app.route("/api/sip/start", methods=["POST"])
@login_required
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

    saved = settings_store.load()
    if not password:
        password = saved.get("sip_password", "")
    if not password:
        return jsonify({"ok": False, "error": "Введите пароль"}), 400

    settings_store.update(
        sip_domain=domain,
        sip_username=username,
        sip_password=password,
        sip_port=port,
        sip_transport=transport,
    )

    if sip_client is not None and sip_client.is_running():
        sip_client.stop()
        sip_client = None

    try:
        from sip_client import SipClient
    except ImportError as e:
        return jsonify({"ok": False, "error": f"pjsua2 не установлен: {e}"}), 500

    sip_client = SipClient(engine, domain=domain, username=username,
                           password=password, port=port, transport=transport)
    ok = sip_client.start()
    return jsonify({"ok": ok})


@app.route("/api/sip/stop", methods=["POST"])
@login_required
def api_sip_stop():
    global sip_client
    if sip_client is not None:
        sip_client.stop()
        sip_client = None
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
    })


@app.route("/api/autostart", methods=["POST"])
@login_required
def api_autostart():
    data = request.get_json(silent=True) or {}
    settings_store.update(
        auto_start_engine=bool(data.get("auto_start_engine")),
        auto_start_sip=bool(data.get("auto_start_sip")),
    )
    return jsonify({"ok": True})


# --- Автозапуск при старте сервера ---
def _boot_autostart():
    saved = settings_store.load()
    if saved.get("auto_start_engine"):
        print("[boot] Автозапуск движка...")
        try:
            engine.start()
        except Exception as e:
            print(f"[boot] Ошибка автозапуска движка: {e}")

    if saved.get("auto_start_sip") and saved.get("sip_domain") and saved.get("sip_username") and saved.get("sip_password"):
        print("[boot] Автозапуск SIP...")
        try:
            from sip_client import SipClient
            global sip_client
            sip_client = SipClient(
                engine,
                domain=saved["sip_domain"],
                username=saved["sip_username"],
                password=saved["sip_password"],
                port=saved.get("sip_port", 5060),
                transport=saved.get("sip_transport", "udp"),
            )
            sip_client.start()
        except Exception as e:
            print(f"[boot] Ошибка автозапуска SIP: {e}")

# --- Fallback-источники ---
@app.route("/api/fallback/sources", methods=["GET"])
@login_required
def api_get_fallback_sources():
    return jsonify({"items": engine.get_settings().get("fallback_sources", [])})


@app.route("/api/fallback/sources", methods=["POST"])
@login_required
def api_set_fallback_sources():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    ok = engine.set_fallback_sources(items)
    return jsonify({"ok": ok, "items": engine.get_settings().get("fallback_sources", [])})

if __name__ == "__main__":
    _boot_autostart()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)