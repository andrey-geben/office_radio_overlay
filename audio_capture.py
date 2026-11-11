# audio_capture.py
import os
import sys
import shutil
import threading
import subprocess
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
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates += [os.path.join(exe_dir, "ffmpeg.exe"),
                       os.path.join(exe_dir, "ffmpeg")]
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [os.path.join(here, "ffmpeg.exe"),
                   os.path.join(here, "ffmpeg")]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("ffmpeg")


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
    """Генератор PCM-чанков из интернет-потока через FFmpeg."""
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
    chunk_bytes = block_size * config.CHANNELS * 2

    creation_flags = 0
    if os.name == "nt":
        creation_flags = 0x08000000  # CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,   # ← не копим, не блокируем
        bufsize=chunk_bytes * 4,
        creationflags=creation_flags,
    )

    try:
        while True:
            data = proc.stdout.read(chunk_bytes)
            if not data:
                print("[FFMPEG] поток завершён (EOF)")
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

    extra = None
    try:
        for api in sd.query_hostapis():
            if "WASAPI" in api["name"]:
                extra = sd.WasapiSettings(exclusive=False)
                break
    except Exception:
        pass

    try:
        stream = sd.InputStream(
            device=device,
            channels=config.CHANNELS,
            samplerate=config.SAMPLE_RATE,
            blocksize=block_size,
            dtype='int16',
            latency='low',
            extra_settings=extra,
        )
    except Exception as e:
        raise RuntimeError(f"Не удалось открыть InputStream: {e}")

    stream.start()
    try:
        while True:
            data, overflowed = stream.read(block_size)
            if overflowed:
                # не принтуем на каждый — только раз в ~2 сек
                if not hasattr(stream_from_device, "_last_ovf"):
                    stream_from_device._last_ovf = 0.0
                import time as _t
                now = _t.time()
                if now - stream_from_device._last_ovf > 2.0:
                    print("[CAP] overflow при чтении устройства")
                    stream_from_device._last_ovf = now
            yield data.tobytes()
    finally:
        stream.stop()
        stream.close()


def stream_audio(source_type="stream", url=None, device=None,
                 block_size=None, read_timeout=None):
    if source_type == "device":
        return stream_from_device(device=device, block_size=block_size)
    return stream_audio_chunks(url=url, block_size=block_size,
                               read_timeout=read_timeout)


def list_input_devices():
    import sounddevice as sd
    result = []
    try:
        wasapi = None
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api["name"]:
                wasapi = i
                break
        default_idx = sd.default.device[0]
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if wasapi is not None and dev.get("hostapi") != wasapi:
                continue
            result.append({
                "index": idx,
                "name": dev["name"],
                "channels": int(dev["max_input_channels"]),
                "default_samplerate": float(dev["default_samplerate"]),
                "hostapi": dev.get("hostapi"),
                "is_default": idx == default_idx,
            })
    except Exception as e:
        print(f"Ошибка получения устройств: {e}")
    return result