# audio_capture.py
import os
import shutil
import subprocess
import threading
import time
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
    """Ищет ffmpeg в PATH. Если нет — вернёт None."""
    return shutil.which("ffmpeg")


def _ffmpeg_cmd(url, extra_args=None):
    cmd = [
        "ffmpeg",
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
        # вставляем перед -i
        idx = cmd.index("-i")
        cmd = cmd[:idx] + extra_args + cmd[idx:]
    return cmd


def stream_audio_chunks(url=None, block_size=None, read_timeout=None):
    """
    Генератор PCM-чанков из интернет-потока через долгоживущий процесс FFmpeg.
    Отдаёт чанки ровно по block_size сэмплов, без рывков.
    """
    if url is None:
        url = config.RADIO_STREAM_URL
    if block_size is None:
        block_size = config.BLOCK_SIZE
    if read_timeout is None:
        read_timeout = 20

    if _find_ffmpeg() is None:
        raise RuntimeError("ffmpeg не найден в PATH. Установите FFmpeg: https://ffmpeg.org/")

    # Доп. аргументы для сетевого источника: таймауты, reconnect
    extra = [
        "-rw_timeout", str(int(read_timeout * 1_000_000)),
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]

    cmd = _ffmpeg_cmd(url, extra_args=extra)
    bytes_per_sample = 2  # int16
    chunk_bytes = block_size * config.CHANNELS * bytes_per_sample

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=chunk_bytes * 4,
    )

    try:
        while True:
            # читаем ровно chunk_bytes (или меньше в самом конце)
            data = proc.stdout.read(chunk_bytes)
            if not data:
                break
            _add_bytes(len(data))
            # на случай, если ffmpeg вернул неполный блок — добиваем тишиной
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
        dtype='int16',
        latency='low',
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