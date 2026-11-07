# announcement_store.py
import os
import json
import uuid
import time
import threading
from datetime import date, datetime
from pydub import AudioSegment

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "assets", "announcements")
META_FILE = os.path.join(BASE_DIR, "assets", "announcements.json")
ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
MAX_SIZE_MB = 20

_write_lock = threading.Lock()


def _ensure_dirs():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    if not os.path.exists(META_FILE):
        with open(META_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def _read_meta():
    _ensure_dirs()
    with open(META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_meta(data):
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_announcements():
    with _write_lock:
        data = _read_meta()
        today = date.today().isoformat()
        changed = False
        for item in data:
            if item.get("last_reset_date") != today:
                item["plays_today"] = 0
                item["last_reset_date"] = today
                changed = True
        if changed:
            _write_meta(data)
        return data


def get_announcement(ann_id):
    for item in _read_meta():
        if item["id"] == ann_id:
            return item
    return None


def get_path(ann_id):
    item = get_announcement(ann_id)
    if not item:
        return None
    return os.path.join(AUDIO_DIR, item["filename"])


def add_announcement(file_storage, description, start_time=None, end_time=None, repeat_limit=0):
    _ensure_dirs()
    original = file_storage.filename or "audio"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Неподдерживаемый формат: {ext}")

    ann_id = uuid.uuid4().hex[:10]
    filename = f"{ann_id}{ext}"
    path = os.path.join(AUDIO_DIR, filename)
    file_storage.save(path)

    if os.path.getsize(path) > MAX_SIZE_MB * 1024 * 1024:
        os.remove(path)
        raise ValueError(f"Файл больше {MAX_SIZE_MB} МБ")

    try:
        duration = round(len(AudioSegment.from_file(path)) / 1000.0, 2)
    except Exception:
        duration = None

    def _norm_time(v):
        if not v:
            return None
        v = str(v).strip()
        if len(v) == 5 and v[2] == ":":
            hh, mm = v.split(":")
            if hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60:
                return v
        return None

    start_time = _norm_time(start_time)
    end_time = _norm_time(end_time)
    try:
        repeat_limit = int(repeat_limit) if repeat_limit not in (None, "", "0") else 0
        repeat_limit = max(0, repeat_limit)
    except (TypeError, ValueError):
        repeat_limit = 0

    item = {
        "id": ann_id,
        "original_name": original,
        "description": (description or "").strip(),
        "filename": filename,
        "size": os.path.getsize(path),
        "duration": duration,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "start_time": start_time,
        "end_time": end_time,
        "repeat_limit": repeat_limit,
        "plays_today": 0,
        "plays_total": 0,
        "last_played": None,
        "last_reset_date": date.today().isoformat(),
    }
    with _write_lock:
        data = _read_meta()
        data.append(item)
        _write_meta(data)
    return item


def delete_announcement(ann_id):
    with _write_lock:
        data = _read_meta()
        found, rest = None, []
        for item in data:
            if item["id"] == ann_id:
                found = item
            else:
                rest.append(item)
        if not found:
            return False
        path = os.path.join(AUDIO_DIR, found["filename"])
        if os.path.exists(path):
            os.remove(path)
        _write_meta(rest)
        return True


def update_schedule(ann_id, start_time, end_time, repeat_limit):
    with _write_lock:
        data = _read_meta()
        for item in data:
            if item["id"] == ann_id:
                item["start_time"] = start_time or None
                item["end_time"] = end_time or None
                try:
                    item["repeat_limit"] = max(0, int(repeat_limit or 0))
                except (TypeError, ValueError):
                    item["repeat_limit"] = 0
                _write_meta(data)
                return item
    return None


def check_playable(item, now=None):
    if now is None:
        now = datetime.now()
    today = now.date().isoformat()

    plays_today = item.get("plays_today", 0) if item.get("last_reset_date") == today else 0
    cur = now.strftime("%H:%M")

    start = item.get("start_time")
    end = item.get("end_time")
    if start and cur < start:
        return False, f"до {start}"
    if end and cur > end:
        return False, f"после {end}"

    limit = item.get("repeat_limit", 0) or 0
    if limit > 0 and plays_today >= limit:
        return False, f"лимит {limit}/день"

    return True, "ok"


def increment_play(ann_id):
    today = date.today().isoformat()
    with _write_lock:
        data = _read_meta()
        for item in data:
            if item["id"] == ann_id:
                if item.get("last_reset_date") != today:
                    item["plays_today"] = 0
                    item["last_reset_date"] = today
                item["plays_today"] = item.get("plays_today", 0) + 1
                item["plays_total"] = item.get("plays_total", 0) + 1
                item["last_played"] = datetime.now().isoformat(timespec="seconds")
                _write_meta(data)
                return item
    return None