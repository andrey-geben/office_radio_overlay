# config.py
import os

# --- Настройки аудио ---
SAMPLE_RATE = 44100
CHANNELS = 1
BLOCK_SIZE = 1024

# --- Источник входящего сигнала ---
SOURCE_TYPE = "stream"
INPUT_DEVICE = None

# --- Настройки источника (по умолчанию) ---
RADIO_STREAM_URL = "http://icecast.radiofrance.fr/fip-midfi.mp3"

# --- CTCSS ---
CTCSS_TONE_FREQ = 131.8
DETECTION_THRESHOLD = 0.1

# --- DCS (упрощённо: второй частотный детектор) ---
DCS_TONE_FREQ = 100.0
DCS_THRESHOLD = 0.1

# --- Микширование ---
ANNOUNCEMENT_FILE = "assets/announcement.wav"
DUCKING_LEVEL = 0.2
ANNOUNCEMENT_LEVEL = 1.0

# --- Fade (плавное затухание/нарастание) ---
FADE_OUT_MS = 500
FADE_IN_MS = 800

# --- Антиспам: минимальный интервал между объявлениями, сек ---
MIN_ANNOUNCE_INTERVAL = 3

# --- Таймаут ожидания субтона (fallback-триггер) ---
TONE_TIMEOUT_ENABLED = False
TONE_TIMEOUT_SECONDS = 600   # 10 минут по умолчанию

# --- Авторизация ---
AUTH_USERNAME = os.environ.get("AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("AUTH_PASS", "admin")
SECRET_KEY = os.environ.get("SECRET_KEY", "CHANGE-ME-PLEASE-32-CHARS-MINIMUM-!!")
SESSION_LIFETIME_HOURS = 12

# --- Трансляция (Icecast) ---
ICECAST_HOST = "http://localhost:8000"
ICECAST_MOUNT = "/overlay"
ICECAST_USER = "source"
ICECAST_PASSWORD = "hackme"

# --- Аппаратный выход ---
HW_OUTPUT_ENABLED = False
HW_OUTPUT_DEVICE = None

# --- HTTP-стрим для браузера ---
STREAM_QUEUE_SIZE = 20

# --- SIP-клиент (начальные значения; далее хранится в assets/settings.json) ---
SIP_DOMAIN = ""              # пусто = пользователь введёт через UI
SIP_USERNAME = ""
SIP_PASSWORD = ""
SIP_PORT = 5060
SIP_TRANSPORT = "udp"

# --- Fallback (резервные источники) ---
FALLBACK_ENABLED = False
FALLBACK_TIMEOUT_SECONDS = 10        # сколько секунд без данных = простой
FALLBACK_RETRY_MAIN_SECONDS = 60     # как часто пробовать вернуть основной
FALLBACK_SOURCES = []                # [{"name": "...", "url": "..."}]