# audio_output.py
import collections
import threading
import queue
import numpy as np
import sounddevice as sd
import config


def _wasapi_index():
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api["name"]:
                return i
    except Exception:
        pass
    return None


try:
    import soxr as _soxr
except Exception:
    _soxr = None


class _SampleRing:
    """Лёгкий буфер int16-сэмплов на deque без постоянных реаллокаций np.concatenate."""

    def __init__(self):
        self._chunks = collections.deque()
        self._head_offset = 0
        self._total = 0

    def __len__(self):
        return self._total

    def clear(self):
        self._chunks.clear()
        self._head_offset = 0
        self._total = 0

    def append(self, arr):
        if arr is not None and len(arr) > 0:
            self._chunks.append(arr)
            self._total += len(arr)

    def pop(self, n):
        if n <= 0 or self._total < n:
            return None
        out = np.empty(n, dtype=np.int16)
        written = 0
        while written < n and self._chunks:
            head = self._chunks[0]
            avail = len(head) - self._head_offset
            take = min(avail, n - written)
            out[written:written + take] = head[self._head_offset:self._head_offset + take]
            written += take
            self._head_offset += take
            self._total -= take
            if self._head_offset >= len(head):
                self._chunks.popleft()
                self._head_offset = 0
        return out


class HardwareOutput:
    """Потоковый вывод микшированного аудио на выбранное устройство."""

    def __init__(self):
        self._device = None
        self._stream = None
        self._queue = queue.Queue(maxsize=30)  # ~0.6 сек буфер
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._actual_sr = config.SAMPLE_RATE
        self._actual_ch = config.CHANNELS
        self._flush_requested = False

    @staticmethod
    def list_output_devices():
        try:
            wasapi = _wasapi_index()
            default_out = sd.default.device[1]
            all_devs = sd.query_devices()

            def _collect(hostapi_filter):
                items = []
                for idx, dev in enumerate(all_devs):
                    if dev.get("max_output_channels", 0) <= 0:
                        continue
                    if hostapi_filter is not None and dev.get("hostapi") != hostapi_filter:
                        continue
                    items.append({
                        "index": idx,
                        "name": dev["name"],
                        "channels": int(dev["max_output_channels"]),
                        "hostapi": dev.get("hostapi"),
                        "is_default": idx == default_out,
                    })
                return items

            # Предпочитаем WASAPI; если на Windows 7 / Server их 0 — берём все API
            result = _collect(wasapi) if wasapi is not None else []
            if not result:
                result = _collect(None)
            return result
        except Exception as e:
            print(f"Ошибка получения выходных устройств: {e}")
            return []

    def start(self, device_index=None):
        self.stop()
        with self._lock:
            self._device = device_index
            self._running = True
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hw-output"
        )
        self._thread.start()

    def stop(self):
        with self._lock:
            was_running = self._running
            self._running = False
            stream = self._stream
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout=5)
            if t.is_alive():
                print("[HW] WARNING: _run thread did not exit within 5s")
            self._thread = None
        with self._lock:
            s = self._stream
            self._stream = None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        if was_running:
            print("[HW] OutputStream остановлен")

    def clear(self):
        """Очищает очередь воспроизведения (thread-safe)."""
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._flush_requested = True

    def pending_duration(self):
        """Приблизительное время (сек) до полного опустошения HW-буфера."""
        with self._lock:
            q = self._queue.qsize()
            stream = self._stream
        q_sec = q * config.BLOCK_SIZE / config.SAMPLE_RATE
        try:
            drv = float(stream.latency) if stream is not None else 0.0
        except Exception:
            drv = 0.0
        return q_sec + drv

    def push(self, chunk_bytes):
        if chunk_bytes is None:
            return
        try:
            self._queue.put_nowait(chunk_bytes)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(chunk_bytes)
            except queue.Full:
                pass

    def is_running(self):
        with self._lock:
            return self._running

    def get_device(self):
        with self._lock:
            return self._device

    def _open_stream_with_fallback(self, block):
        """Пробует открыть поток: WASAPI (auto_convert) -> WASAPI -> MME/DirectSound."""
        settings_candidates = []
        if _wasapi_index() is not None:
            try:
                settings_candidates.append(
                    sd.WasapiSettings(exclusive=False, auto_convert=True)
                )
            except Exception:
                pass
            try:
                settings_candidates.append(sd.WasapiSettings(exclusive=False))
            except Exception:
                pass
        settings_candidates.append(None)

        # 48000 первым — CABLE Input (VB-Audio) и большинство карт работают на 48 кГц
        attempts = [
            (48000, config.CHANNELS),
            (48000, 2),
            (config.SAMPLE_RATE, config.CHANNELS),
            (config.SAMPLE_RATE, 2),
            (None, config.CHANNELS),
            (None, 2),
        ]

        for extra in settings_candidates:
            for sr, ch in attempts:
                if not self._running:
                    return None
                try:
                    stream = sd.OutputStream(
                        device=self._device,
                        samplerate=sr,
                        channels=ch,
                        dtype="int16",
                        blocksize=block,
                        latency=0.05,
                        extra_settings=extra,
                    )
                    stream.start()
                    return stream
                except Exception:
                    continue
        return None

    def _run(self):
        block = config.BLOCK_SIZE
        stream = None

        try:
            stream = self._open_stream_with_fallback(block)
            if stream is None:
                print("[HW] Не удалось открыть ни одну комбинацию аудиовыхода")
                return

            with self._lock:
                self._stream = stream
                self._actual_sr = int(stream.samplerate)
                self._actual_ch = int(stream.channels)

            try:
                dev_name = (
                    sd.query_devices(self._device)["name"]
                    if self._device is not None
                    else sd.query_devices(kind="output")["name"]
                )
            except Exception:
                dev_name = "?"

            print(
                f"[HW] OutputStream открыт: {self._actual_sr}Гц, "
                f"{self._actual_ch}ch -> '{dev_name}'"
            )

            out_sr = self._actual_sr
            out_ch = self._actual_ch
            needs_resample = (out_sr != config.SAMPLE_RATE)

            resampler = None
            if needs_resample:
                if _soxr is None:
                    print(
                        f"[HW] WARNING: SAMPLE_RATE={config.SAMPLE_RATE} != "
                        f"device {out_sr}. Установите `pip install soxr` "
                        f"ИЛИ смените SAMPLE_RATE=48000 в config.py."
                    )
                else:
                    resampler = _soxr.ResampleStream(
                        config.SAMPLE_RATE, out_sr, 1, dtype="int16"
                    )

            if needs_resample:
                block_in = max(1, int(round(block * config.SAMPLE_RATE / out_sr)))
            else:
                block_in = block

            silence_in = np.zeros(block_in, dtype=np.int16)
            ring_in = _SampleRing()
            ring_out = _SampleRing()

            while self._running:
                if self._flush_requested:
                    self._flush_requested = False
                    ring_in.clear()
                    ring_out.clear()

                while True:
                    try:
                        raw = self._queue.get_nowait()
                        arr = np.frombuffer(raw, dtype=np.int16)
                        ring_in.append(arr)
                    except queue.Empty:
                        break

                while len(ring_out) < block and self._running:
                    if len(ring_in) < block_in:
                        ring_in.append(silence_in[: block_in - len(ring_in)])

                    chunk_in = ring_in.pop(block_in)
                    if chunk_in is None:
                        break

                    if needs_resample and resampler is not None:
                        rs = resampler.resample_chunk(chunk_in)
                        rs = np.clip(rs, -32768, 32767).astype(np.int16)
                    else:
                        rs = chunk_in

                    if len(rs) < block:
                        rs = np.pad(rs, (0, block - len(rs)))
                    elif len(rs) > block:
                        rs = rs[:block]

                    ring_out.append(rs)

                while len(ring_out) >= block and self._running:
                    chunk = ring_out.pop(block)
                    if chunk is None:
                        break
                    if out_ch == 2 and chunk.ndim == 1:
                        chunk = np.repeat(chunk, 2)
                    try:
                        stream.write(chunk)
                    except Exception as e:
                        if not self._running:
                            return
                        print(f"[HW] write error: {e}")
                        return

        except Exception as e:
            print(f"Ошибка аппаратного выхода: {e}")
        finally:
            with self._lock:
                self._running = False
                s = self._stream
                self._stream = None
            if s is not None:
                try:
                    s.stop()
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass