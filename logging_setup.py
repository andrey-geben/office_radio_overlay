# logging_setup.py
import os
import sys
import logging
import logging.handlers
import threading

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOGS_DIR, "app.log")

_initialized = False
_lock = threading.Lock()


def setup_logging(level=logging.INFO):
    """Настраивает логирование в файл и консоль. Вызывать один раз при старте."""
    global _initialized
    with _lock:
        if _initialized:
            return

    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
    except Exception:
        pass

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1) Файл с ротацией (5 МБ × 5 файлов). Если открыть не удалось
    #    (файл занят, папка только для чтения) — работаем без файла.
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except Exception as e:
        print(f"[LOGGING] Не удалось открыть {LOG_FILE}: {e} — только консоль")

    # 2) Консоль (для отладки в PyCharm / терминале)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    # 3) Перехват необработанных исключений
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("uncaught").critical(
            "Необработанное исключение",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _excepthook

    # 4) Перехват исключений в фоновых потоках
    def _thread_excepthook(args):
        logging.getLogger("uncaught.thread").critical(
            f"Необработанное исключение в потоке {args.thread.name}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook

    with _lock:
        _initialized = True
    logging.getLogger(__name__).info("Логирование инициализировано: %s", LOG_FILE)