# chime_store.py
"""Библиотека сигналов внимания (chime).

Файлы лежат в assets/chimes/*.wav (48 кГц, моно, 16 бит).
Активный сигнал хранится в настройках как chime_file.
"""
import os
import re
import shutil
import sys
import wave

import settings_store

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CHIMES_DIR = os.path.join(BASE_DIR, "assets", "chimes")
LEGACY_CHIME = os.path.join(BASE_DIR, "assets", "chime.wav")
LEGACY_VARIANTS = os.path.join(BASE_DIR, "assets", "chime_variants")
DEFAULT_FILE = "chime.wav"
TARGET_RATE = 48000
TARGET_CHANNELS = 1
TARGET_DBFS = -1.0
MAX_SECONDS = 5.0
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 МБ на файл сигнала


def _ensure():
    os.makedirs(CHIMES_DIR, exist_ok=True)
    # первичное наполнение: старый chime + готовые варианты
    target = os.path.join(CHIMES_DIR, DEFAULT_FILE)
    if not os.path.exists(target) and os.path.exists(LEGACY_CHIME):
        try:
            shutil.copy2(LEGACY_CHIME, target)
        except Exception:
            pass
    if os.path.isdir(LEGACY_VARIANTS):
        for name in os.listdir(LEGACY_VARIANTS):
            if not name.lower().endswith(".wav"):
                continue
            src = os.path.join(LEGACY_VARIANTS, name)
            dst = os.path.join(CHIMES_DIR, name)
            if not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass


def _duration(path):
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or TARGET_RATE
            return round(w.getnframes() / float(rate), 2)
    except Exception:
        return 0.0


def _display_name(filename):
    base = os.path.splitext(filename)[0]
    base = base.replace("_", " ").replace("-", " ").strip()
    return base[:1].upper() + base[1:] if base else filename


def list_chimes():
    """Список сигналов: [{file, name, seconds, size, active}]."""
    _ensure()
    active = settings_store.load().get("chime_file", DEFAULT_FILE)
    items = []
    for name in sorted(os.listdir(CHIMES_DIR)):
        if not name.lower().endswith(".wav"):
            continue
        path = os.path.join(CHIMES_DIR, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        items.append({
            "file": name,
            "name": _display_name(name),
            "seconds": _duration(path),
            "size": size,
            "active": name == active,
        })
    if not any(i["active"] for i in items) and items:
        items[0]["active"] = True
    return items


def active_file():
    name = settings_store.load().get("chime_file", DEFAULT_FILE)
    path = os.path.join(CHIMES_DIR, name) if name else ""
    if not name or not os.path.exists(path):
        _ensure()
        path = os.path.join(CHIMES_DIR, DEFAULT_FILE)
        if not os.path.exists(path):
            path = LEGACY_CHIME
    return path


def path_for(name):
    if not name or os.path.basename(name) != name:
        return None
    path = os.path.join(CHIMES_DIR, name)
    return path if os.path.exists(path) else None


def select(name):
    path = path_for(name)
    if not path:
        return False, "Файл не найден"
    settings_store.update(chime_file=name)
    return True, ""


def _safe_name(original):
    base = os.path.splitext(os.path.basename(original or "chime"))[0]
    slug = re.sub(r"[^A-Za-zА-Яа-я0-9_ -]+", "", base).strip().replace(" ", "_")
    return (slug or "chime")[:40]


def _normalize_segment(seg):
    from pydub import AudioSegment
    seg = seg.set_frame_rate(TARGET_RATE).set_channels(TARGET_CHANNELS)
    if seg.dBFS != float("-inf"):
        seg = seg.apply_gain(TARGET_DBFS - seg.dBFS)
    # обрезаем тишину по краям (оставляя 20 мс запаса)
    head = 0
    for ms in range(0, min(len(seg), 3000), 10):
        if seg[ms:ms + 10].dBFS > -50:
            head = max(0, ms - 20)
            break
    tail = len(seg)
    for ms in range(len(seg), max(0, len(seg) - 3000), -10):
        if seg[ms - 10:ms].dBFS > -50:
            tail = min(len(seg), ms + 80)
            break
    seg = seg[head:tail]
    seg = seg.fade_in(5).fade_out(150)
    return seg


def add_from_file(original_name, file_obj):
    """Конвертирует загруженный файл в chime WAV и добавляет в библиотеку.

    Поддерживаются MP3/WAV/OGG/FLAC/M4A (декодирование через ffmpeg/pydub).
    Ограничения: не больше 10 МБ и не длиннее 5 секунд.
    """
    _ensure()
    # контроль размера
    try:
        pos = file_obj.stream.tell()
        file_obj.stream.seek(0, os.SEEK_END)
        size = file_obj.stream.tell()
        file_obj.stream.seek(pos)
    except Exception:
        size = 0
    if size and size > MAX_UPLOAD_BYTES:
        return None, (f"Файл больше 10 МБ ({size / 1048576:.1f} МБ). "
                      "Загрузите сигнал короче или с меньшим битрейтом.")
    from pydub import AudioSegment
    try:
        seg = AudioSegment.from_file(file_obj)
    except Exception as e:
        return None, f"Не удалось прочитать файл: {e}"
    if len(seg) < 120:
        return None, "Файл слишком короткий (меньше 0.12 с)"
    if len(seg) > MAX_SECONDS * 1000 + 50:
        return None, (f"Сигнал длиннее {MAX_SECONDS:.0f} секунд "
                      f"({len(seg) / 1000:.1f} с). Обрежьте файл — "
                      "длинный сигнал задерживает соединение.")
    try:
        seg = _normalize_segment(seg)
    except Exception as e:
        return None, f"Ошибка обработки: {e}"
    slug = _safe_name(original_name)
    name = f"user_{slug}.wav"
    n = 0
    while os.path.exists(os.path.join(CHIMES_DIR, name)):
        n += 1
        name = f"user_{slug}_{n}.wav"
    path = os.path.join(CHIMES_DIR, name)
    try:
        seg.export(path, format="wav")
    except Exception as e:
        return None, f"Не удалось сохранить: {e}"
    return {"file": name, "name": _display_name(name), "seconds": _duration(path)}, ""


def delete(name):
    if not name or name == DEFAULT_FILE:
        return False, "Базовый сигнал удалить нельзя"
    path = path_for(name)
    if not path:
        return False, "Файл не найден"
    active = settings_store.load().get("chime_file", DEFAULT_FILE)
    try:
        os.remove(path)
    except Exception as e:
        return False, f"Не удалось удалить: {e}"
    if active == name:
        settings_store.update(chime_file=DEFAULT_FILE)
    return True, ""
