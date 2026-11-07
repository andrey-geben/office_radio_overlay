# settings_store.py
import os
import json
import threading
import config

import sys
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "assets", "settings.json")

_lock = threading.Lock()


def _defaults():
    return {
        "source_type": config.SOURCE_TYPE,
        "radio_url": config.RADIO_STREAM_URL,
        "input_device": config.INPUT_DEVICE,
        "tone_freq": config.CTCSS_TONE_FREQ,
        "detection_threshold": config.DETECTION_THRESHOLD,
        "dcs_freq": config.DCS_TONE_FREQ,
        "dcs_threshold": config.DCS_THRESHOLD,
        "ducking_level": config.DUCKING_LEVEL,
        "announcement_level": config.ANNOUNCEMENT_LEVEL,
        "fade_out_ms": config.FADE_OUT_MS,
        "fade_in_ms": config.FADE_IN_MS,
        "hw_output_device": config.HW_OUTPUT_DEVICE,
        "tone_timeout_enabled": config.TONE_TIMEOUT_ENABLED,
        "tone_timeout_seconds": config.TONE_TIMEOUT_SECONDS,
        "active_announcement_id": None,
        "fallback_enabled": config.FALLBACK_ENABLED,
        "fallback_timeout_seconds": config.FALLBACK_TIMEOUT_SECONDS,
        "fallback_retry_main_seconds": config.FALLBACK_RETRY_MAIN_SECONDS,
        "fallback_sources": config.FALLBACK_SOURCES,
        "sip_domain": "",
        "sip_username": "",
        "sip_password": "",
        "sip_port": 5060,
        "sip_transport": "udp",
        "auto_start_engine": False,
        "auto_start_sip": False,
    }


def _ensure_dir():
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)


def _write_json(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load():
    _ensure_dir()
    defaults = _defaults()
    if not os.path.exists(SETTINGS_FILE):
        with _lock:
            if not os.path.exists(SETTINGS_FILE):
                _write_json(defaults)
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = dict(defaults)
    merged.update(data)
    return merged


def save(data):
    with _lock:
        _ensure_dir()
        _write_json(data)


def update(**kwargs):
    current = load()
    current.update(kwargs)
    save(current)
    return current