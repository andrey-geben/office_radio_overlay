# audio_output.py
import sounddevice as sd
import numpy as np
import threading
import queue
import config


class HardwareOutput:
    """Потоковый вывод микшированного аудио на выбранное устройство."""

    def __init__(self):
        self._device = None
        self._stream = None
        self._queue = queue.Queue(maxsize=200)
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
        # чистим очередь
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
            # выкидываем самый старый, чтобы не копить задержку
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
        pending = np.array([], dtype=np.int16)

        try:
            stream = sd.OutputStream(
                device=self._device,
                samplerate=config.SAMPLE_RATE,
                channels=config.CHANNELS,
                dtype="int16",
                blocksize=block,
                latency='low',
            )
            stream.start()
            with self._lock:
                self._stream = stream

            while self._running:
                # забираем всё, что есть в очереди
                while True:
                    try:
                        raw = self._queue.get_nowait()
                        arr = np.frombuffer(raw, dtype=np.int16)
                        if len(arr) > 0:
                            pending = np.concatenate([pending, arr]) if len(pending) else arr
                    except queue.Empty:
                        break

                # отдаём полные блоки
                while len(pending) >= block:
                    stream.write(pending[:block])
                    pending = pending[block:]

                # недобор — дополняем тишиной (без sleep!)
                if len(pending) > 0:
                    # оставшиеся сэмплы дополняем тишиной до полного блока
                    pad = block - len(pending)
                    padded = np.concatenate([pending, np.zeros(pad, dtype=np.int16)])
                    stream.write(padded)
                    pending = np.array([], dtype=np.int16)
                elif len(pending) == 0:
                    # очередь пуста — пишем блок тишины, чтобы не было underrun
                    stream.write(silence)

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