# audio_capture.py
import io
import os
import sys
import shutil
import threading
import subprocess
import requests
import config

_bytes_lock = threading.Lock()
_bytes_total = 0


def _add_bytes(n):
    global _bytes_total
    with _bytes_lock:
        _bytes_total += n


def get_bytes_received():
    with _bytes_lock:
        return _bytes_total


def _find_ffmpeg():
    """
    Ищет ffmpeg:
      1. Рядом с exe (для сборки PyInstaller).
      2. Рядом с текущим файлом.
      3. В PATH.
    Возвращает путь или None.
    """
    candidates = []

    # 1) рядом с exe (PyInstaller)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "ffmpeg.exe"))
        candidates.append(os.path.join(exe_dir, "ffmpeg"))

    # 2) рядом с этим файлом
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "ffmpeg.exe"))
    candidates.append(os.path.join(here, "ffmpeg"))

    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    # 3) в PATH
    return shutil.which("ffmpeg")


def _ffmpeg_cmd(ffmpeg_path, url, extra_args=None):
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-i", url,
        "-vn",
        "-ac", str(config.CHANNELS),
        "-ar", str(config.SAMPLE_RATE),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "pipe:1",
    ]
    if extra_args:
        idx = cmd.index("-i")
        cmd = cmd[:idx] + extra_args + cmd[idx:]
    return cmd


def stream_audio_chunks(url=None, block_size=None, read_timeout=None):
    """
    Генератор PCM-чанков из интернет-потока через FFmpeg.
    Отдаёт ровные чанки по block_size сэмплов (int16).
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
        "-reconnect_delay_max", "5",
    ]

    cmd = _ffmpeg_cmd(ffmpeg, url, extra_args=extra)
    chunk_bytes = block_size * config.CHANNELS * 2  # int16

    creation_flags = 0
    if os.name == "nt":
        # Скрываем окно консоли FFmpeg в сборке без консоли
        creation_flags = 0x08000000  # CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=chunk_bytes * 4,
        creationflags=creation_flags,
    )

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
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def stream_from_device(device=None, block_size=None):
    import sounddevice as sd
    if block_size is None:
        block_size = config.BLOCK_SIZE

    stream = sd.InputStream(
        device=device,
        channels=config.CHANNELS,
        samplerate=config.SAMPLE_RATE,
        blocksize=block_size,
        dtype="int16",
        latency="low",
    )
    stream.start()
    try:
        while True:
            data, _overflowed = stream.read(block_size)
            yield data.tobytes()
    finally:
        stream.stop()
        stream.close()


def stream_audio(source_type="stream", url=None, device=None, block_size=None, read_timeout=None):
    """Диспетчер: поток или устройство."""
    if source_type == "device":
        return stream_from_device(device=device, block_size=block_size)
    return stream_audio_chunks(url=url, block_size=block_size, read_timeout=read_timeout)


def list_input_devices():
    import sounddevice as sd
    result = []
    try:
        default_idx = sd.default.device[0]
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) > 0:
                result.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": int(dev["max_input_channels"]),
                    "default_samplerate": float(dev["default_samplerate"]),
                    "is_default": idx == default_idx,
                })
    except Exception as e:
        print(f"Ошибка получения устройств: {e}")
    return result