# preflight.py
# Проверка компонентов при запуске.
# Показывает нативное Windows-окно с проблемами — без tkinter.

import os
import sys
import ctypes
import platform
import traceback

import config
import settings_store


CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

_last_results = []


def get_last_results():
    return _last_results


def _check_os():
    try:
        rel = platform.release()
        ver = platform.version()
        if os.name == "nt" and hasattr(sys, "getwindowsversion"):
            wv = sys.getwindowsversion()
            if wv.major == 10 and wv.build >= 22000:
                rel = "11"
            ver = f"{wv.major}.{wv.minor}.{wv.build}"
        return INFO, f"Windows: {rel} ({ver})"
    except Exception:
        return INFO, "Windows (версия не определена)"


def _check_vc_runtime():
    """Проверяет наличие VC++ Redistributable и UCRT на чистых Windows 7/8/10 LTSC."""
    if os.name != "nt":
        return INFO, "Runtime: non-Windows"
    missing = []
    for dll in ("vcruntime140.dll", "ucrtbase.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            missing.append(dll)
    if missing:
        return CRITICAL, (
            f"В системе отсутствуют системные библиотеки: {', '.join(missing)}. "
            "Установите Microsoft Visual C++ Redistributable 2015–2022 (x64) "
            "и обновление Universal C Runtime (KB2999226 для Windows 7/8.1)."
        )
    return INFO, "Windows C/C++ Runtime: OK"


def _check_portaudio():
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        n_out = sum(1 for d in devs if d["max_output_channels"] > 0)
        n_in = sum(1 for d in devs if d["max_input_channels"] > 0)
        if n_out == 0:
            return CRITICAL, (
                "Не найдено ни одного устройства вывода звука. "
                "Проверьте, что в Windows подключены колонки или наушники "
                "и они не отключены в Панели управления → Звук."
            )
        return INFO, f"PortAudio OK: устройств вывода {n_out}, ввода {n_in}"
    except Exception as e:
        return CRITICAL, f"PortAudio не инициализируется: {e}"


def _check_resampler():
    try:
        import soxr  # noqa: F401
        return INFO, "Ресемплер soxr: OK"
    except Exception:
        if config.SAMPLE_RATE != 48000:
            return WARNING, (
                f"Библиотека soxr не установлена, а SAMPLE_RATE={config.SAMPLE_RATE} Гц. "
                "При выводе на устройства 48000 Гц (включая VB-Cable) возможны искажения звука. "
                "Установите: pip install soxr"
            )
        return INFO, "Ресемплер soxr: не требуется (48000 Гц)"


def _check_pjsua2():
    try:
        import pjsua2  # noqa: F401
        return INFO, "pjsua2: OK"
    except Exception as e:
        return WARNING, (
            f"pjsua2 не загружается: {e}. "
            "SIP-звонки работать не будут (радио — будет). "
            "Установите pjsua2-wheel или проверьте DLL."
        )


def _check_ffmpeg():
    try:
        from audio_capture import _find_ffmpeg
        p = _find_ffmpeg()
    except Exception:
        p = None
    if p:
        return INFO, f"FFmpeg: {p}"
    return WARNING, (
        "FFmpeg не найден. Радио из интернета работать не будет. "
        "Скачайте ffmpeg.exe (https://ffmpeg.org/download.html) "
        "и положите его рядом с программой."
    )


def _check_vb_cable():
    try:
        import sounddevice as sd
        names = [d["name"].lower() for d in sd.query_devices()]
        has_in = any("cable in" in n for n in names)
        has_out = any("cable output" in n for n in names)
        if has_in and has_out:
            return INFO, "VB-Audio Cable: CABLE In / CABLE Output найдены"
        if not has_in and not has_out:
            return WARNING, (
                "VB-Audio Cable не установлен. "
                "SIP-звонки работать не будут. "
                "Скачайте: https://vb-audio.com/Cable/"
            )
        return WARNING, (
            "VB-Audio Cable установлен частично (найден только один "
            "из CABLE In / CABLE Output). Проверьте установку."
        )
    except Exception as e:
        return WARNING, f"Не удалось проверить VB-Audio Cable: {e}"


def _check_chime():
    path = config.SIP_CHIME_FILE
    if not os.path.isabs(path):
        path = os.path.join(settings_store.BASE_DIR, path)
    if not os.path.isfile(path):
        try:
            from make_chime import generate_chime
            os.makedirs(os.path.dirname(path), exist_ok=True)
            generate_chime(path, sample_rate=config.SAMPLE_RATE)
        except Exception:
            pass
    if not os.path.isfile(path):
        return WARNING, (
            f"chime.wav не найден: {path}. "
            "Сигнал внимания перед звонком играть не будет."
        )
    try:
        size = os.path.getsize(path)
        if size < 1000:
            return WARNING, (
                f"chime.wav слишком мал ({size} байт) — возможно повреждён."
            )
        return INFO, f"chime.wav: {size} байт"
    except Exception as e:
        return WARNING, f"chime.wav не читается: {e}"


def _check_writable():
    try:
        os.makedirs(settings_store.BASE_DIR, exist_ok=True)
        test_file = os.path.join(settings_store.BASE_DIR, ".write_test")
        with open(test_file, "w") as f:
            f.write("x")
        os.remove(test_file)
        return INFO, f"Папка {settings_store.BASE_DIR} доступна для записи"
    except Exception as e:
        return CRITICAL, (
            f"Папка {settings_store.BASE_DIR} недоступна для записи: {e}. "
            "Переместите программу в другую папку (например, "
            "C:\\OfficeRadio) или запустите от администратора."
        )


def _check_path_characters():
    """Кириллица в пути может сломать FFmpeg или PJSIP."""
    try:
        path = settings_store.BASE_DIR
        if any(ord(c) > 127 for c in path):
            return WARNING, (
                f"Путь содержит кириллицу: {path}. "
                "Это может вызвать проблемы с FFmpeg. "
                "Рекомендуется переместить программу в путь "
                "без русских букв (например, C:\\OfficeRadio)."
            )
        return INFO, "Путь программы: OK"
    except Exception:
        return INFO, "Путь программы: не проверен"


def run_checks():
    checks = [
        _check_os,
        _check_vc_runtime,
        _check_writable,
        _check_portaudio,
        _check_resampler,
        _check_pjsua2,
        _check_ffmpeg,
        _check_vb_cable,
        _check_chime,
        _check_path_characters,
    ]
    global _last_results
    results = []
    for fn in checks:
        try:
            sev, msg = fn()
        except Exception as e:
            results.append((WARNING, f"{fn.__name__}: {e}"))
            traceback.print_exc()
            continue
        results.append((sev, msg))

    _last_results = results

    # Логируем всё в файл (важно для --windowed сборки без консоли)
    try:
        log_path = os.path.join(settings_store.BASE_DIR, "preflight.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("Preflight checks\n\n")
            for sev, msg in results:
                f.write(f"[{sev}] {msg}\n")
    except Exception:
        pass

    return results


def has_critical(results):
    return any(sev == CRITICAL for sev, _ in results)


def show_messagebox(text, title="Office Radio Overlay", is_error=False):
    """Нативный Windows MessageBox — работает без tkinter."""
    try:
        flags = 0x10 if is_error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        print(f"[PREFLIGHT] {title}: {text}")


def report_to_user(results, first_run=False):
    """Показывает окно, если есть critical/warning (или если first_run)."""
    critical = [m for s, m in results if s == CRITICAL]
    warnings = [m for s, m in results if s == WARNING]

    if critical:
        text = "Не удалось запустить программу.\n\n"
        text += "\n\n".join(f"• {m}" for m in critical)
        if warnings:
            text += "\n\nДополнительно:\n"
            text += "\n\n".join(f"? {m}" for m in warnings)
        show_messagebox(text, "Office Radio Overlay — ошибка", is_error=True)
        return False

    if warnings:
        text = "Программа запущена, но есть предупреждения:\n\n"
        text += "\n\n".join(f"• {m}" for m in warnings)
        show_messagebox(text, "Office Radio Overlay")
        return True

    if first_run:
        text = "Все компоненты в порядке. Программа готова к работе."
        show_messagebox(text, "Office Radio Overlay")
    return True