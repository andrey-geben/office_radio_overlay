# audio_capture.py
import io
import threading
import requests
from pydub import AudioSegment
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


def stream_audio_chunks(url=None, block_size=None, read_timeout=None):
    """Генератор аудио-чанков из интернет-потока. read_timeout — таймаут между чанками."""
    if url is None:
        url = config.RADIO_STREAM_URL
    if block_size is None:
        block_size = config.BLOCK_SIZE
    if read_timeout is None:
        read_timeout = 15

    response = requests.get(url, stream=True, timeout=(5, read_timeout))
    response.raise_for_status()
    buffer = io.BytesIO()

    for chunk in response.iter_content(chunk_size=block_size * 4):
        if chunk:
            _add_bytes(len(chunk))
            buffer.write(chunk)
            if buffer.tell() > block_size * 4:
                buffer.seek(0)
                try:
                    audio = AudioSegment.from_file(buffer, format="mp3")
                    audio = audio.set_frame_rate(config.SAMPLE_RATE).set_channels(config.CHANNELS)
                    yield audio.raw_data
                except Exception as e:
                    print(f"Ошибка декодирования: {e}")
                finally:
                    buffer.seek(0)
                    buffer.truncate()


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