# audio_output.py
import sounddevice as sd
import numpy as np
import threading
import queue
import time
import config


class HardwareOutput:
    """Потоковый вывод микшированного аудио на выбранное устройство."""

    def __init__(self):
        self._device = None
        self._stream = None
        self._queue = queue.Queue(maxsize=100)
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

    @staticmethod
    def list_output_devices():
        result = []
        try:
            default_out = sd.default.device[1]
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_output_channels", 0) > 0:
                    result.append({
                        "index": idx,
                        "name": dev["name"],
                        "channels": int(dev["max_output_channels"]),
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
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self._stream = None

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
        silence = np.zeros(block, dtype=np.int16)
        buffer = np.array([], dtype=np.int16)

        try:
            stream = sd.OutputStream(
                device=self._device,
                samplerate=config.SAMPLE_RATE,
                channels=config.CHANNELS,
                dtype="int16",
                blocksize=block,
            )
            stream.start()
            with self._lock:
                self._stream = stream

            while self._running:
                drained = False
                while True:
                    try:
                        raw = self._queue.get_nowait()
                        buffer = np.concatenate([buffer, np.frombuffer(raw, dtype=np.int16)])
                        drained = True
                    except queue.Empty:
                        break

                while len(buffer) >= block:
                    stream.write(buffer[:block])
                    buffer = buffer[block:]

                if len(buffer) < block and not drained:
                    if len(buffer) < block // 2:
                        stream.write(silence)
                    else:
                        time.sleep(0.005)
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