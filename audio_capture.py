# audio_capture.py
import os
import sys
import time
import shutil
import functools
import threading
import subprocess
import collections
import config

import logging
logger = logging.getLogger(__name__)

_bytes_lock = threading.Lock()
_bytes_total = 0

_proc_lock = threading.Lock()
_active_procs = set()


def _add_bytes(n):
    global _bytes_total
    with _bytes_lock:
        _bytes_total += n


def get_bytes_received():
    with _bytes_lock:
        return _bytes_total


def stop_active_ffmpeg():
    """Принудительно завершает активные процессы FFmpeg (разблокирует stdout.read на Windows)."""
    with _proc_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass


@functools.lru_cache(maxsize=1)
def _find_ffmpeg():
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates += [
            os.path.join(exe_dir, "ffmpeg.exe"),
            os.path.join(exe_dir, "ffmpeg"),
        ]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "ffmpeg.exe"),
        os.path.join(here, "ffmpeg"),
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("ffmpeg")


def _find_ca_bundle():
    """Путь к cacert.pem для -ca_file.

    FFmpeg с GnuTLS не видит хранилище сертификатов Windows, поэтому без
    этого файла HTTPS-поток падает с «Peer certificate failed
    verification». Ищем рядом с .exe (ручная раскладка), внутри _internal
    (туда PyInstaller 6 кладёт datas), в assets и рядом с исходниками.
    """
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.getcwd())

    for d in dirs:
        for name in ("cacert.pem", "ca-bundle.crt",
                     os.path.join("assets", "cacert.pem")):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p

    logger.warning(
        "[FFmpeg] cacert.pem не найден — HTTPS-потоки могут не открыться. "
        "Положите cacert.pem рядом с .exe."
    )
    return None


def _ffmpeg_cmd(ffmpeg_path, url, extra_args=None):
    cmd = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", url, "-vn",
        "-ac", str(config.CHANNELS),
        "-ar", str(config.SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]
    if extra_args:
        idx = cmd.index("-i")
        cmd = cmd[:idx] + extra_args + cmd[idx:]
    return cmd


def stream_audio_chunks(url=None, block_size=None, read_timeout=None):
    """
    Генератор PCM-чанков из интернет-потока через FFmpeg.
    При завершении FFmpeg печатает последние строки stderr — это
    позволяет увидеть настоящую причину (403, таймаут, неверный кодек).
    """
    if url is None:
        url = config.RADIO_STREAM_URL
    if block_size is None:
        block_size = config.BLOCK_SIZE
    if read_timeout is None:
        read_timeout = 20

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg не найден. Положите ffmpeg.exe рядом с программой "
            "или установите FFmpeg и добавьте его в PATH."
        )
    extra = [
        "-rw_timeout", str(int(read_timeout * 1_000_000)),
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_delay_max", "5",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "-icy", "1",
    ]
    ca_path = _find_ca_bundle()
    if ca_path:
        extra += ["-ca_file", ca_path]

    cmd = _ffmpeg_cmd(ffmpeg, url, extra_args=extra)
    chunk_bytes = block_size * config.CHANNELS * 2

    creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

    logger.info("[FFmpeg] Запуск: %s", url)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=chunk_bytes * 4,
        creationflags=creation_flags,
    )

    with _proc_lock:
        _active_procs.add(proc)

    stderr_lines = collections.deque(maxlen=30)

    def _drain_stderr():
        try:
            for line in iter(proc.stderr.readline, b""):
                if not line:
                    break
                try:
                    text = line.decode("utf-8", "replace").rstrip()
                except Exception:
                    text = repr(line)
                if text:
                    stderr_lines.append(text)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True, name="ffmpeg-stderr").start()

    try:
        while True:
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break
            _add_bytes(len(data))
            if len(data) < chunk_bytes:
                data = data + b"\x00" * (chunk_bytes - len(data))
            yield data
    finally:
        with _proc_lock:
            _active_procs.discard(proc)
        try:
            proc.terminate()
            rc = proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                rc = proc.wait(timeout=1)
            except Exception:
                rc = -999

        if rc != 0:
            logger.warning("[FFmpeg] завершился с кодом %s, URL: %s", rc, url)
            if stderr_lines:
                print("[FFmpeg] Последние сообщения stderr:")
                for line in stderr_lines:
                    print(f"  {line}")
            else:
                print("[FFmpeg] stderr пуст — возможно, поток закрыт сервером по EOF.")
        else:
            print(f"[FFmpeg] штатно завершён (код 0), URL: {url}")


def _wasapi_index(sd):
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api["name"]:
                return i
    except Exception:
        pass
    return None


def stream_from_device(device=None, block_size=None):
    import sounddevice as sd
    if block_size is None:
        block_size = config.BLOCK_SIZE

    extra = None
    try:
        if _wasapi_index(sd) is not None:
            extra = sd.WasapiSettings(exclusive=False)
    except Exception:
        pass

    stream = None
    last_err = None
    for settings in ([extra, None] if extra is not None else [None]):
        try:
            stream = sd.InputStream(
                device=device,
                channels=config.CHANNELS,
                samplerate=config.SAMPLE_RATE,
                blocksize=block_size,
                dtype="int16",
                latency="low",
                extra_settings=settings,
            )
            break
        except Exception as e:
            last_err = e
            stream = None

    if stream is None:
        raise RuntimeError(f"Не удалось открыть InputStream: {last_err}")

    last_ovf = 0.0
    stream.start()
    try:
        while True:
            data, overflowed = stream.read(block_size)
            if overflowed:
                now = time.time()
                if now - last_ovf > 2.0:
                    print("[CAP] overflow при чтении устройства")
                    last_ovf = now
            yield data.tobytes()
    finally:
        try:
            stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass


def stream_audio(source_type="stream", url=None, device=None,
                 block_size=None, read_timeout=None):
    if source_type == "device":
        return stream_from_device(device=device, block_size=block_size)
    return stream_audio_chunks(url=url, block_size=block_size,
                               read_timeout=read_timeout)


def list_input_devices():
    import sounddevice as sd
    try:
        wasapi = _wasapi_index(sd)
        default_idx = sd.default.device[0]
        all_devs = sd.query_devices()

        def _collect(hostapi_filter):
            items = []
            for idx, dev in enumerate(all_devs):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                if hostapi_filter is not None and dev.get("hostapi") != hostapi_filter:
                    continue
                items.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": int(dev["max_input_channels"]),
                    "default_samplerate": float(dev["default_samplerate"]),
                    "hostapi": dev.get("hostapi"),
                    "is_default": idx == default_idx,
                })
            return items

        # Предпочитаем WASAPI; если на Windows 7 / Server их 0 — берём все доступные API
        result = _collect(wasapi) if wasapi is not None else []
        if not result:
            result = _collect(None)
        return result
    except Exception as e:
        print(f"Ошибка получения устройств: {e}")
        return []