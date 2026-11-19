# config.py
import os
import sys
import secrets


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _load_secret_key():
    """Ключ сессий: env -> assets/secret.key -> сгенерировать и сохранить."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(_app_dir(), "assets", "secret.key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if len(key) >= 32:
                return key
    except Exception:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
    except Exception:
        pass
    return key

# --- Настройки аудио ---
SAMPLE_RATE = 48000
CHANNELS = 1
BLOCK_SIZE = 1024

# Плавное затухание радио при SIP-звонке
SIP_DUCKING_LEVEL = 0.05   # 0.0 = полное выключение радио, 0.3 = смешение (радио тише)
SIP_FADE_MS = 300         # плавное затухание за 300 мс

# --- Источник входящего сигнала (по умолчанию) ---
SOURCE_TYPE = "stream"                # "stream" или "device"
INPUT_DEVICE = None                   # None = по умолчанию, int = индекс устройства
RADIO_STREAM_URL = "http://icecast.radiofrance.fr/fip-midfi.mp3"

# --- CTCSS ---
CTCSS_TONE_FREQ = 131.8
DETECTION_THRESHOLD = 0.1

# --- DCS (упрощённый второй частотный детектор) ---
DCS_TONE_FREQ = 100.0
DCS_THRESHOLD = 0.1

# --- Микширование ---
ANNOUNCEMENT_FILE = "assets/announcement.wav"
DUCKING_LEVEL = 0.05   # радио на время объявления: 0.05 = почти тишина
ANNOUNCEMENT_LEVEL = 1.0

# --- Fade (плавное затухание / нарастание) ---
FADE_OUT_MS = 500
FADE_IN_MS = 800

# --- Антиспам: минимальный интервал между объявлениями (сек) ---
MIN_ANNOUNCE_INTERVAL = 3

# --- Таймаут ожидания субтона (fallback-триггер) ---
TONE_TIMEOUT_ENABLED = False
TONE_TIMEOUT_SECONDS = 600

# --- DTMF-метки (автоматизация вещания) ---
DTMF_DEFAULT_LABELS = []              # ["3957"] — пример для Русского Радио
DTMF_SEQUENCE_TIMEOUT_SECONDS = 3.0   # сколько помнить накопленную последовательность
DTMF_MIN_DIGIT_MS = 40                # минимальная длительность одной цифры

# --- Авторизация ---
AUTH_USERNAME = os.environ.get("AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASS", "admin")
SECRET_KEY = _load_secret_key()
SESSION_LIFETIME_HOURS = 12

# --- Icecast (на будущее) ---
ICECAST_HOST = "http://localhost:8000"
ICECAST_MOUNT = "/overlay"
ICECAST_USER = "source"
ICECAST_PASSWORD = "hackme"

# --- Аппаратный выход ---
HW_OUTPUT_ENABLED = False
HW_OUTPUT_DEVICE = None

# --- HTTP-стрим для браузера ---
STREAM_QUEUE_SIZE = 20

# --- Fallback (резервные источники) ---
FALLBACK_ENABLED = False
FALLBACK_TIMEOUT_SECONDS = 10
FALLBACK_RETRY_MAIN_SECONDS = 60
FALLBACK_SOURCES = []

# --- Автооткрытие браузера при старте ---
# True  — после запуска сервера открыть http://127.0.0.1:5000
# False — не открывать (или задать AUTO_OPEN_BROWSER=0 в окружении)
AUTO_OPEN_BROWSER = True
# Свой адрес вместо локального (например, http://192.168.1.10:5000)
BROWSER_URL = None

# --- SIP: сигнал внимания перед соединением ---
SIP_CHIME_FILE = "assets/chime.wav"
SIP_CHIME_ENABLED_DEFAULT = True
# Пауза после окончания сигнала, прежде чем ответить на звонок.
# Нужна, чтобы хвост chime не накладывался на голос вызывающего.
SIP_ANSWER_DELAY_MS = 400
SIP_CHIME_FADE_OUT_MS = 300     # затухание радио перед chime