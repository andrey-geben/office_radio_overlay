# audio_output.py
import sounddevice as sd
import numpy as np
import threading
import queue
import config


def _wasapi_index():
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api["name"]:
                return i
    except Exception:
        pass
    return None


# Попытка импортировать stateful-ресемплер (если понадобится)
try:
    import soxr as _soxr
except Exception:
    _soxr = None


class HardwareOutput:
    """Потоковый вывод микшированного аудио на выбранное устройство."""

    def __init__(self):
        self._device = None
        self._stream = None
        self._queue = queue.Queue(maxsize=30) # ~0.6 сек буфер (было 200 = 4.2 сек)
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._actual_sr = config.SAMPLE_RATE
        self._actual_ch = config.CHANNELS
        self._flush_requested = False

    @staticmethod
    def list_output_devices():
        result = []
        try:
            wasapi = _wasapi_index()
            default_out = sd.default.device[1]
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_output_channels", 0) <= 0:
                    continue
                if wasapi is not None and dev.get("hostapi") != wasapi:
                    continue
                result.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": int(dev["max_output_channels"]),
                    "hostapi": dev.get("hostapi"),
                    "is_default": idx == default_out,
                })
        except Exception as e:
            print(f"Ошибка получения выходных устройств: {e}")
        return result

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

    def _run(self):
        block = config.BLOCK_SIZE
        stream = None

        # --- WASAPI settings: shared mode + auto convert ---
        wasapi_settings = None
        try:
            wasapi_settings = sd.WasapiSettings(
                exclusive=False,
                auto_convert=True,
            )
        except Exception as e:
            print(f"[HW] WasapiSettings: {e}")

        # ВАЖНО: 48000 первым — CABLE Input (VB-Audio) не принимает 44100.
        attempts = [
            (48000, config.CHANNELS),
            (48000, 2),
            (config.SAMPLE_RATE, config.CHANNELS),
            (config.SAMPLE_RATE, 2),
            (None, config.CHANNELS),
            (None, 2),
        ]

        try:
            for sr, ch in attempts:
                if not self._running:
                    return
                try:
                    stream = sd.OutputStream(
                        device=self._device,
                        samplerate=sr,
                        channels=ch,
                        dtype="int16",
                        blocksize=block,
                        latency=0.05,
                        extra_settings=wasapi_settings,
                    )
                    stream.start()
                    try:
                        print(f"[HW] stream.latency={stream.latency:.3f} сек")
                    except Exception:
                        pass
                    with self._lock:
                        self._stream = stream
                        self._actual_sr = int(stream.samplerate)
                        self._actual_ch = int(stream.channels)
                    print(f"[HW] OutputStream открыт: "
                          f"{self._actual_sr}Гц, {self._actual_ch}ch")
                    try:
                        dev_name = (sd.query_devices(self._device)["name"]
                                    if self._device is not None
                                    else sd.query_devices(kind="output")["name"])
                    except Exception:
                        dev_name = "?"
                    print(f"[HW]   → устройство: '{dev_name}'")
                    break
                except Exception as e:
                    print(f"[HW] Попытка {sr}Гц / {ch}ch не удалась: {e}")
                    stream = None
                    continue

            if stream is None:
                print("[HW] Не удалось открыть ни одну комбинацию")
                return

            out_sr = self._actual_sr
            out_ch = self._actual_ch
            needs_resample = (out_sr != config.SAMPLE_RATE)

            # Правильный потоковый ресемплер (stateful). soxr предпочтителен.
            resampler = None
            if needs_resample:
                if _soxr is None:
                    print(f"[HW] WARNING: SAMPLE_RATE={config.SAMPLE_RATE} != "
                          f"device {out_sr}. Установите `pip install soxr` "
                          f"ИЛИ смените SAMPLE_RATE=48000 в config.py. "
                          f"Без soxr будут щелчки на каждом блоке.")
                else:
                    resampler = _soxr.ResampleStream(
                        config.SAMPLE_RATE, out_sr, 1, dtype='int16'
                    )

            # Ожидаемое количество входных сэмплов на один выходной блок
            if needs_resample:
                block_in = max(1, int(round(
                    block * config.SAMPLE_RATE / out_sr
                )))
            else:
                block_in = block

            silence_in = np.zeros(block_in, dtype=np.int16)
            pending_in = np.array([], dtype=np.int16)
            pending_out = np.array([], dtype=np.int16)

            while self._running:
                # 1) Забираем из очереди
                if self._flush_requested:
                    self._flush_requested = False
                    pending_in = np.array([], dtype=np.int16)
                    pending_out = np.array([], dtype=np.int16)
                while True:
                    try:
                        raw = self._queue.get_nowait()
                        arr = np.frombuffer(raw, dtype=np.int16)
                        if len(arr) > 0:
                            if len(pending_in):
                                pending_in = np.concatenate([pending_in, arr])
                            else:
                                pending_in = arr
                    except queue.Empty:
                        break

                # 2) Производим блоки выхода
                while len(pending_out) < block and self._running:
                    if len(pending_in) < block_in:
                        pad = silence_in[:block_in - len(pending_in)]
                        if len(pending_in):
                            pending_in = np.concatenate([pending_in, pad])
                        else:
                            pending_in = pad

                    chunk_in = pending_in[:block_in]
                    pending_in = pending_in[block_in:]

                    if needs_resample and resampler is not None:
                        # Stateful — без фейдов на границах
                        rs = resampler.resample_chunk(chunk_in)
                        rs = np.clip(rs, -32768, 32767).astype(np.int16)
                    else:
                        rs = chunk_in

                    if len(rs) < block:
                        rs = np.pad(rs, (0, block - len(rs)))
                    elif len(rs) > block:
                        rs = rs[:block]

                    if len(pending_out):
                        pending_out = np.concatenate([pending_out, rs])
                    else:
                        pending_out = rs

                # 3) Отдаём в устройство
                while len(pending_out) >= block and self._running:
                    chunk = pending_out[:block]
                    pending_out = pending_out[block:]
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