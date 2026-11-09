# announcement_store.py
import os
import sys
import json
import uuid
import time
import threading
from datetime import date, datetime, timedelta
from pydub import AudioSegment

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

AUDIO_DIR = os.path.join(BASE_DIR, "assets", "announcements")
META_FILE = os.path.join(BASE_DIR, "assets", "announcements.json")
ALLOWED_EXT = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
MAX_SIZE_MB = 20

ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
DOW_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_write_lock = threading.RLock()


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


def _norm_time(v):
    if not v:
        return None
    v = str(v).strip()
    if len(v) == 5 and v[2] == ":":
        hh, mm = v.split(":")
        if hh.isdigit() and mm.isdigit():
            h, m = int(hh), int(mm)
            if 0 <= h < 24 and 0 <= m < 60:
                return f"{h:02d}:{m:02d}"
    return None


def _norm_limit(v):
    try:
        n = int(v) if v not in (None, "", "0") else 0
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


def _norm_days(v):
    if v is None:
        return list(ALL_DAYS)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = [int(c) for c in v if c.isdigit()]
    if not isinstance(v, list):
        return list(ALL_DAYS)
    cleaned = set()
    for x in v:
        try:
            n = int(x)
            if 0 <= n <= 6:
                cleaned.add(n)
        except (TypeError, ValueError):
            pass
    if not cleaned:
        return list(ALL_DAYS)
    return sorted(cleaned)


def format_days(days):
    days = sorted(days or ALL_DAYS)
    if len(days) == 7:
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "будни"
    if days == [5, 6]:
        return "выходные"
    return " ".join(DOW_SHORT[d] for d in days)


def _validate_schedule_fields(start_time, end_time, repeat_limit):
    start_time = _norm_time(start_time)
    end_time = _norm_time(end_time)
    repeat_limit = _norm_limit(repeat_limit)
    if not start_time:
        raise ValueError("Укажите время начала действия (start_time).")
    if not end_time and repeat_limit <= 0:
        raise ValueError("Укажите время окончания (end_time) или лимит повторов (repeat_limit).")


def _make_schedule(start_time, end_time, repeat_limit, min_interval_minutes, days_of_week):
    return {
        "schedule_id": uuid.uuid4().hex[:8],
        "start_time": _norm_time(start_time),
        "end_time": _norm_time(end_time),
        "repeat_limit": _norm_limit(repeat_limit),
        "min_interval_minutes": _norm_limit(min_interval_minutes),
        "days_of_week": _norm_days(days_of_week),
        "plays_today": 0,
        "plays_total": 0,
        "last_played": None,
        "last_reset_date": date.today().isoformat(),
    }


def _migrate_item(item):
    """Старый формат → новый (schedules: [...]). Возвращает True, если изменилось."""
    if "schedules" in item and isinstance(item["schedules"], list):
        return False
    sched = {
        "schedule_id": uuid.uuid4().hex[:8],
        "start_time": item.get("start_time"),
        "end_time": item.get("end_time"),
        "repeat_limit": item.get("repeat_limit", 0),
        "min_interval_minutes": item.get("min_interval_minutes", 0),
        "days_of_week": item.get("days_of_week", list(ALL_DAYS)),
        "plays_today": item.get("plays_today", 0),
        "plays_total": item.get("plays_total", 0),
        "last_played": item.get("last_played"),
        "last_reset_date": item.get("last_reset_date"),
    }
    item["schedules"] = [sched]
    for k in ("start_time", "end_time", "repeat_limit", "min_interval_minutes",
              "days_of_week", "plays_today", "plays_total", "last_played", "last_reset_date"):
        item.pop(k, None)
    return True


def list_announcements():
    with _write_lock:
        data = _read_meta()
        today = date.today().isoformat()
        changed = False
        for item in data:
            if _migrate_item(item):
                changed = True
            for s in item.get("schedules", []):
                if s.get("last_reset_date") != today:
                    s["plays_today"] = 0
                    s["last_reset_date"] = today
                    changed = True
        if changed:
            _write_meta(data)
        return data


def check_schedule(s, now=None):
    if now is None:
        now = datetime.now()
    today = now.date().isoformat()
    days = s.get("days_of_week", ALL_DAYS)
    if now.weekday() not in days:
        return False, f"не сегодня ({format_days(days)})"
    cur = now.strftime("%H:%M")
    start = s.get("start_time")
    end = s.get("end_time")
    if start and cur < start:
        return False, f"до {start}"
    if end and cur > end:
        return False, f"после {end}"
    plays_today = s.get("plays_today", 0) if s.get("last_reset_date") == today else 0
    limit = s.get("repeat_limit", 0) or 0
    if limit > 0 and plays_today >= limit:
        return False, f"лимит {limit}/день"
    interval_min = s.get("min_interval_minutes", 0) or 0
    if interval_min > 0:
        last_played = s.get("last_played")
        if last_played:
            try:
                last_dt = datetime.fromisoformat(last_played)
                delta = now - last_dt
                if delta < timedelta(minutes=interval_min):
                    next_dt = last_dt + timedelta(minutes=interval_min)
                    return False, f"интервал до {next_dt.strftime('%H:%M')}"
            except Exception:
                pass
    return True, "ok"


def check_playable(item, now=None):
    """Возвращает (ok, reason, best_schedule_or_None)."""
    if now is None:
        now = datetime.now()
    schedules = item.get("schedules", [])
    if not schedules:
        return False, "нет расписаний", None

    playable = []
    reasons = []
    for s in schedules:
        ok, reason = check_schedule(s, now)
        if ok:
            playable.append(s)
        else:
            if reason not in reasons:
                reasons.append(reason)

    if not playable:
        return False, " · ".join(reasons) if reasons else "нет окон", None

    playable.sort(key=lambda s: s.get("last_played") or "")
    return True, "ok", playable[0]


def _schedule_progress(s, now):
    """(progress, hint, state) для одного расписания."""
    if now.weekday() not in s.get("days_of_week", ALL_DAYS):
        return 0.0, "не сегодня", "blocked"
    today = now.date().isoformat()
    cur = now.strftime("%H:%M")
    start = s.get("start_time")
    end = s.get("end_time")
    if start and cur < start:
        return 0.0, f"до {start}", "blocked"
    if end and cur > end:
        return 0.0, f"после {end}", "blocked"
    plays_today = s.get("plays_today", 0) if s.get("last_reset_date") == today else 0
    limit = s.get("repeat_limit", 0) or 0
    if limit > 0 and plays_today >= limit:
        return 1.0, "лимит исчерпан", "blocked"
    interval_min = s.get("min_interval_minutes", 0) or 0
    last_played = s.get("last_played")
    if interval_min > 0 and last_played:
        try:
            last_dt = datetime.fromisoformat(last_played)
            total = interval_min * 60
            delta = (now - last_dt).total_seconds()
            if delta >= total:
                return 1.0, "готов", "ready"
            p = max(0.0, min(1.0, delta / total))
            remaining = max(1, int((total - delta) / 60 + 0.5))
            return p, f"через {remaining}м", "cooldown"
        except Exception:
            pass
    return 1.0, "готов", "ready"


def list_with_progress():
    items = list_announcements()
    now = datetime.now()
    today_iso = now.date().isoformat()
    result = []
    for it in items:
        schedules = it.get("schedules", [])
        n_sched = len(schedules)

        plays_today_sum = 0
        plays_total_sum = 0
        last_played_max = None
        for s in schedules:
            if s.get("last_reset_date") == today_iso:
                plays_today_sum += s.get("plays_today", 0)
            plays_total_sum += s.get("plays_total", 0)
            lp = s.get("last_played")
            if lp and (not last_played_max or lp > last_played_max):
                last_played_max = lp

        ok, reason, _best = check_playable(it, now)

        if ok:
            progress, state, hint = 1.0, "ready", "готов"
        elif not schedules:
            progress, state, hint = 0.0, "blocked", "нет расписаний"
        else:
            max_progress = 0.0
            hint_cooldown = None
            blocked_hints = []
            for s in schedules:
                p, h, st = _schedule_progress(s, now)
                if st == "cooldown" and p > max_progress:
                    max_progress = p
                    hint_cooldown = h
                elif st == "blocked" and h not in blocked_hints:
                    blocked_hints.append(h)
            if hint_cooldown:
                progress, state, hint = max_progress, "cooldown", hint_cooldown
            else:
                progress, state = 0.0, "blocked"
                hint = " · ".join(blocked_hints) if blocked_hints else "нет окон"

        result.append({
            **it,
            "playable": ok,
            "reason": reason,
            "progress": round(progress, 3),
            "queue_state": state,
            "queue_hint": hint,
            "schedules_count": n_sched,
            "plays_today": plays_today_sum,
            "plays_total": plays_total_sum,
            "last_played": last_played_max,
        })
    return result


def get_announcement(ann_id):
    for item in list_announcements():
        if item["id"] == ann_id:
            return item
    return None


def get_path(ann_id):
    item = get_announcement(ann_id)
    if not item:
        return None
    return os.path.join(AUDIO_DIR, item["filename"])


def add_announcement(file_storage, description, start_time=None, end_time=None,
                     repeat_limit=0, min_interval_minutes=0, days_of_week=None):
    _ensure_dirs()
    original = file_storage.filename or "audio"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Неподдерживаемый формат: {ext}")

    _validate_schedule_fields(start_time, end_time, repeat_limit)

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

    first_sched = _make_schedule(start_time, end_time, repeat_limit,
                                 min_interval_minutes, days_of_week)

    item = {
        "id": ann_id,
        "original_name": original,
        "description": (description or "").strip(),
        "filename": filename,
        "size": os.path.getsize(path),
        "duration": duration,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "schedules": [first_sched],
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


def upsert_schedule(ann_id, schedule_id, start_time, end_time,
                    repeat_limit, min_interval_minutes, days_of_week):
    """Создаёт новое расписание (если schedule_id=None) или обновляет существующее."""
    _validate_schedule_fields(start_time, end_time, repeat_limit)

    with _write_lock:
        data = _read_meta()
        for item in data:
            if item["id"] == ann_id:
                if not schedule_id:
                    new_sched = _make_schedule(start_time, end_time, repeat_limit,
                                               min_interval_minutes, days_of_week)
                    item.setdefault("schedules", []).append(new_sched)
                    _write_meta(data)
                    return item, new_sched
                for s in item.get("schedules", []):
                    if s["schedule_id"] == schedule_id:
                        s["start_time"] = _norm_time(start_time)
                        s["end_time"] = _norm_time(end_time)
                        s["repeat_limit"] = _norm_limit(repeat_limit)
                        s["min_interval_minutes"] = _norm_limit(min_interval_minutes)
                        s["days_of_week"] = _norm_days(days_of_week)
                        _write_meta(data)
                        return item, s
    return None, None


def delete_schedule(ann_id, schedule_id):
    with _write_lock:
        data = _read_meta()
        for item in data:
            if item["id"] == ann_id:
                before = len(item.get("schedules", []))
                item["schedules"] = [s for s in item.get("schedules", []) if s["schedule_id"] != schedule_id]
                if len(item["schedules"]) < before:
                    _write_meta(data)
                    return True
    return False


def increment_play(ann_id, schedule_id):
    today = date.today().isoformat()
    with _write_lock:
        data = _read_meta()
        for item in data:
            if item["id"] == ann_id:
                for s in item.get("schedules", []):
                    if s["schedule_id"] == schedule_id:
                        if s.get("last_reset_date") != today:
                            s["plays_today"] = 0
                            s["last_reset_date"] = today
                        s["plays_today"] = s.get("plays_today", 0) + 1
                        s["plays_total"] = s.get("plays_total", 0) + 1
                        s["last_played"] = datetime.now().isoformat(timespec="seconds")
                        _write_meta(data)
                        return item
    return None