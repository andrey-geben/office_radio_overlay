# engine.py
import threading
import time
import math
import logging
import collections
import queue
import numpy as np
import psutil

import audio_capture
from audio_capture import stream_audio, stream_audio_chunks
from tone_detector import detect_all, scan_ctcss_frequencies
from mixer import load_announcement
from audio_output import HardwareOutput
import announcement_store
import settings_store
import config

logger = logging.getLogger(__name__)

SPECTRUM_BANDS = 16


def _apply_envelope(radio_chunk, ann_chunk, pos_samples, total_samples,
                    fade_out_samples, fade_in_samples, ducking):
    radio = np.frombuffer(radio_chunk, dtype=np.int16).astype(np.float32)
    ann = np.frombuffer(ann_chunk, dtype=np.int16).astype(np.float32)
    n = min(len(radio), len(ann))
    if n == 0:
        return radio_chunk
    radio, ann = radio[:n], ann[:n]

    total = max(total_samples, 4)
    fade_out = max(2, min(fade_out_samples, total // 2))
    fade_in = max(2, min(fade_in_samples, total - fade_out))

    pos = np.arange(pos_samples, pos_samples + n)
    radio_gain = np.ones(n, dtype=np.float32)
    ann_gain = np.zeros(n, dtype=np.float32)

    in_start = pos < fade_out
    if in_start.any():
        t = np.clip(pos[in_start] / (fade_out - 1), 0.0, 1.0)
        radio_gain[in_start] = 1.0 - t * (1.0 - ducking)
        ann_gain[in_start] = t

    in_middle = (~in_start) & (pos < total - fade_in)
    if in_middle.any():
        radio_gain[in_middle] = ducking
        ann_gain[in_middle] = 1.0

    in_end = pos >= total - fade_in
    if in_end.any():
        t = np.clip((pos[in_end] - (total - fade_in)) / (fade_in - 1), 0.0, 1.0)
        radio_gain[in_end] = ducking + t * (1.0 - ducking)
        ann_gain[in_end] = 1.0 - t

    mixed = radio * radio_gain + ann * ann_gain
    mixed = np.clip(mixed, -32768, 32767)
    return mixed.astype(np.int16).tobytes()


class RadioOverlayEngine:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._last_play_time = 0.0
        self._ann_active = False
        self._ann_pos = 0

        self._sip_active = False
        self._sip_call = None

        self._tone_events = collections.deque(maxlen=5000)
        self._total_ctcss = 0
        self._total_dcs = 0
        self._prev_ctcss = False
        self._prev_dcs = False
        self._session_start = 0.0

        self._last_activity_time = time.time()

        self._stats_last = 0.0
        self._net_last_bytes = 0
        self._net_last_time = 0.0
        self._spectrum_last = 0.0

        self._output_queue = queue.Queue(maxsize=config.STREAM_QUEUE_SIZE)

        self._scan_buffer = collections.deque()
        self._scan_buffer_size = 0
        self._scan_buffer_max = int(config.SAMPLE_RATE * 3)

        self._hw_output = HardwareOutput()

        saved = settings_store.load()

        self._state = {
            "running": False,
            "ctcss_detected": False,
            "ctcss_power": 0.0,
            "dcs_detected": False,
            "dcs_power": 0.0,
            "signal_rms": 0.0,
            "signal_db": -90.0,
            "cpu_percent": 0.0,
            "network_bps": 0,
            "announcement_active": False,
            "sip_active": False,
            "announcements_played": 0,
            "last_event": "idle",
            "error": None,
            "source_info": "—",
            "seconds_since_tone": 0,
            "spectrum": [0.0] * SPECTRUM_BANDS,
            "current_source": "—",
            "using_reserve": False,
        }

        self._settings = {
            "source_type": saved.get("source_type", config.SOURCE_TYPE),
            "radio_url": saved.get("radio_url", config.RADIO_STREAM_URL),
            "input_device": saved.get("input_device", config.INPUT_DEVICE),
            "tone_freq": saved.get("tone_freq", config.CTCSS_TONE_FREQ),
            "detection_threshold": saved.get("detection_threshold", config.DETECTION_THRESHOLD),
            "dcs_freq": saved.get("dcs_freq", config.DCS_TONE_FREQ),
            "dcs_threshold": saved.get("dcs_threshold", config.DCS_THRESHOLD),
            "ducking_level": saved.get("ducking_level", config.DUCKING_LEVEL),
            "announcement_level": saved.get("announcement_level", config.ANNOUNCEMENT_LEVEL),
            "fade_out_ms": saved.get("fade_out_ms", config.FADE_OUT_MS),
            "fade_in_ms": saved.get("fade_in_ms", config.FADE_IN_MS),
            "simulate_tone": False,
            "active_announcement_id": saved.get("active_announcement_id"),
            "hw_output_device": saved.get("hw_output_device"),
            "tone_timeout_enabled": saved.get("tone_timeout_enabled", config.TONE_TIMEOUT_ENABLED),
            "tone_timeout_seconds": saved.get("tone_timeout_seconds", config.TONE_TIMEOUT_SECONDS),
            "fallback_enabled": saved.get("fallback_enabled", config.FALLBACK_ENABLED),
            "fallback_timeout_seconds": saved.get("fallback_timeout_seconds", config.FALLBACK_TIMEOUT_SECONDS),
            "fallback_retry_main_seconds": saved.get("fallback_retry_main_seconds", config.FALLBACK_RETRY_MAIN_SECONDS),
            "fallback_sources": saved.get("fallback_sources", []),
        }

        try:
            if self._settings.get("active_announcement_id"):
                self.reload_announcement()
        except Exception:
            logger.warning("Не удалось восстановить активное объявление")

    # --- SIP ---
    def enter_sip_mode(self, sip_call=None):
        with self._lock:
            self._sip_active = True
            self._sip_call = sip_call
            self._state["sip_active"] = True
        self._update_state(last_event="📞 SIP-звонок в эфире")
        logger.info("Режим SIP включён.")

    def exit_sip_mode(self):
        with self._lock:
            self._sip_active = False
            self._sip_call = None
            self._state["sip_active"] = False
        self._update_state(last_event="streaming")
        logger.info("Режим SIP выключен.")

    def is_sip_active(self):
        with self._lock:
            return self._sip_active

    # --- API ---
    def get_state(self):
        with self._lock:
            state = dict(self._state)
            events = list(self._tone_events)
            total_ctcss = self._total_ctcss
            total_dcs = self._total_dcs
            session_start = self._session_start
            elapsed = int(time.time() - self._last_activity_time)
            sip_active = self._sip_active
        state["tone_histogram"] = self._build_histogram(events)
        state["session_tones"] = {"ctcss": total_ctcss, "dcs": total_dcs, "started_at": session_start}
        state["hw_output"] = {"running": self._hw_output.is_running(), "device": self._hw_output.get_device()}
        state["seconds_since_tone"] = elapsed
        state["sip_active"] = sip_active
        return state

    def get_settings(self):
        with self._lock:
            return dict(self._settings)

    def update_settings(self, **kwargs):
        with self._lock:
            was_timeout_enabled = self._settings.get("tone_timeout_enabled", False)
            self._settings.update(kwargs)
            changed_active = "active_announcement_id" in kwargs
            now_timeout_enabled = self._settings.get("tone_timeout_enabled", False)

        if now_timeout_enabled and not was_timeout_enabled:
            with self._lock:
                self._last_activity_time = time.time()

        to_save = {k: v for k, v in kwargs.items() if k in self._settings and k != "simulate_tone"}
        if to_save:
            try:
                settings_store.update(**to_save)
            except Exception as e:
                logger.warning(f"Не удалось сохранить настройки: {e}")

        if changed_active:
            self.reload_announcement()
        return self.get_settings()

    def set_fallback_sources(self, sources):
        """sources — список [{"name": str, "url": str}]"""
        if not isinstance(sources, list):
            return False
        cleaned = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            url = (s.get("url") or "").strip()
            name = (s.get("name") or "").strip()
            if not url:
                continue
            cleaned.append({"name": name or f"Резерв {len(cleaned)+1}", "url": url})
        with self._lock:
            self._settings["fallback_sources"] = cleaned
        try:
            settings_store.update(fallback_sources=cleaned)
        except Exception:
            pass
        return True

    def get_output_chunk(self, timeout=1.0):
        try:
            return self._output_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def set_hw_output(self, device_index):
        if device_index is None or device_index == "":
            self._hw_output.stop()
            with self._lock:
                self._settings["hw_output_device"] = None
            try:
                settings_store.update(hw_output_device=None)
            except Exception:
                pass
            return True
        try:
            device_index = int(device_index)
        except (TypeError, ValueError):
            return False
        try:
            self._hw_output.start(device_index)
        except Exception as e:
            logger.error(f"Ошибка запуска аппаратного выхода: {e}")
            return False
        with self._lock:
            self._settings["hw_output_device"] = device_index
        try:
            settings_store.update(hw_output_device=device_index)
        except Exception:
            pass
        return True

    def scan_tone(self):
        with self._lock:
            if not self._scan_buffer or self._scan_buffer_size == 0:
                return {"ok": False, "error": "Нет данных. Запустите трансляцию и подождите 2-3 секунды."}
            chunks = list(self._scan_buffer)
        try:
            combined = np.concatenate(chunks).astype(np.int16).tobytes()
        except Exception as e:
            return {"ok": False, "error": f"Ошибка объединения буфера: {e}"}
        candidates = scan_ctcss_frequencies(combined)
        return {
            "ok": True,
            "buffer_seconds": round(self._scan_buffer_size / config.SAMPLE_RATE, 2),
            "candidates": candidates,
        }

    def reload_announcement(self):
        with self._lock:
            ann_id = self._settings.get("active_announcement_id")
        if not ann_id:
            with self._lock:
                self._announcement_data = None
            return False
        path = announcement_store.get_path(ann_id)
        if not path:
            with self._lock:
                self._announcement_data = None
            return False
        try:
            data = load_announcement(path=path)
        except Exception:
            logger.exception("Не удалось загрузить %s", path)
            with self._lock:
                self._announcement_data = None
            return False
        with self._lock:
            self._announcement_data = data
        return True

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self.reload_announcement()
        with self._lock:
            self._ann_active = False
            self._ann_pos = 0
            self._tone_events.clear()
            self._total_ctcss = 0
            self._total_dcs = 0
            self._prev_ctcss = False
            self._prev_dcs = False
            self._session_start = time.time()
            self._last_activity_time = time.time()
            self._scan_buffer.clear()
            self._scan_buffer_size = 0
            hw_dev = self._settings.get("hw_output_device")

        if hw_dev is not None and not self._hw_output.is_running():
            try:
                self._hw_output.start(hw_dev)
            except Exception as e:
                logger.warning(f"Не удалось восстановить аппаратный выход: {e}")

        while True:
            try:
                self._output_queue.get_nowait()
            except queue.Empty:
                break
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._update_state(running=False, last_event="stopped", announcement_active=False)

    # --- Внутреннее ---
    def _update_state(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    @staticmethod
    def _level(chunk):
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == 0:
            return 0.0, -90.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        db = 20.0 * math.log10(rms + 1e-9)
        return rms, max(-90.0, min(0.0, db))

    def _record_tone(self, kind):
        with self._lock:
            self._tone_events.append((time.time(), kind))
            if kind == "ctcss":
                self._total_ctcss += 1
            else:
                self._total_dcs += 1

    @staticmethod
    def _build_histogram(events, window_s=300, bucket_s=5):
        now = time.time()
        start = now - window_s
        n = window_s // bucket_s
        buckets = [{"ctcss": 0, "dcs": 0} for _ in range(n)]
        for ts, kind in events:
            if ts < start:
                continue
            idx = min(int((ts - start) / bucket_s), n - 1)
            if idx >= 0:
                buckets[idx][kind] += 1
        return {"window_s": window_s, "bucket_s": bucket_s, "buckets": buckets}

    @staticmethod
    def _compute_spectrum(chunk):
        try:
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            if len(samples) < 512:
                return [0.0] * SPECTRUM_BANDS
            n_fft = 1024
            if len(samples) > n_fft:
                samples = samples[-n_fft:]
            window = np.hanning(len(samples))
            fft = np.fft.rfft(samples * window)
            mag = np.abs(fft)
            total = len(mag)
            bands = []
            for i in range(SPECTRUM_BANDS):
                start = int(total * (i / SPECTRUM_BANDS) ** 1.4)
                end = int(total * ((i + 1) / SPECTRUM_BANDS) ** 1.4)
                if end <= start:
                    end = start + 1
                bands.append(float(np.mean(mag[start:end])))
            mx = max(bands) if max(bands) > 0 else 1.0
            return [b / mx for b in bands]
        except Exception:
            return [0.0] * SPECTRUM_BANDS

    def _update_spectrum(self, chunk):
        now = time.time()
        if now - self._spectrum_last < 0.08:
            return
        self._spectrum_last = now
        self._update_state(spectrum=self._compute_spectrum(chunk))

    def _update_scan_buffer(self, chunk):
        try:
            arr = np.frombuffer(chunk, dtype=np.int16)
        except Exception:
            return
        if len(arr) == 0:
            return
        with self._lock:
            self._scan_buffer.append(arr)
            self._scan_buffer_size += len(arr)
            while self._scan_buffer_size > self._scan_buffer_max and self._scan_buffer:
                removed = self._scan_buffer.popleft()
                self._scan_buffer_size -= len(removed)

    def _push_output(self, chunk):
        if chunk is None:
            return
        try:
            self._output_queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._output_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._output_queue.put_nowait(chunk)
            except queue.Full:
                pass
        if self._hw_output.is_running():
            self._hw_output.push(chunk)

    def _handle_trigger(self, from_timeout=False):
        with self._lock:
            active_id = self._settings.get("active_announcement_id")
            ann_data = self._announcement_data
            last_play = self._last_play_time

        if not active_id or ann_data is None:
            if not from_timeout:
                self._update_state(last_event="🔊 трансляция (нет активного объявления)")
            return False
        if time.time() - last_play < config.MIN_ANNOUNCE_INTERVAL:
            return False

        meta = announcement_store.get_announcement(active_id)
        if not meta:
            self._update_state(last_event="тон (объявление удалено)",
                               error="Активное объявление не найдено")
            return False
        ok, reason = announcement_store.check_playable(meta)
        if not ok:
            if not from_timeout:
                self._update_state(last_event=f"тон (вне расписания: {reason})")
            return False

        with self._lock:
            self._last_play_time = time.time()
            self._last_activity_time = time.time()
            self._ann_active = True
            self._ann_pos = 0
            self._state["announcements_played"] += 1
            self._state["announcement_active"] = True
        announcement_store.increment_play(active_id)

        if from_timeout:
            self._update_state(last_event="⏰ таймаут без тона — объявление в эфире")
        else:
            self._update_state(last_event="🔔 объявление в эфире")
        return True

    def _check_timeout_trigger(self, settings):
        if not settings.get("tone_timeout_enabled"):
            return
        try:
            timeout = float(settings.get("tone_timeout_seconds", 0))
        except (TypeError, ValueError):
            return
        if timeout <= 0:
            return
        with self._lock:
            if self._ann_active:
                return
            elapsed = time.time() - self._last_activity_time
        if elapsed < timeout:
            return
        with self._lock:
            active_id = self._settings.get("active_announcement_id")
            ann_data = self._announcement_data
        if not active_id or ann_data is None:
            return
        self._handle_trigger(from_timeout=True)

    def _play_chunk(self, chunk):
        with self._lock:
            ann_data = self._announcement_data
            pos = self._ann_pos
        if ann_data is None:
            self._finish_announcement()
            return None
        remaining = len(ann_data) - pos
        if remaining <= 0:
            self._finish_announcement()
            return None
        take = min(remaining, len(chunk))
        ann_chunk = ann_data[pos:pos + take]
        if take < len(chunk):
            ann_chunk = ann_chunk + b"\x00" * (len(chunk) - take)

        s = self.get_settings()
        fade_out_s = int(s["fade_out_ms"] * config.SAMPLE_RATE / 1000)
        fade_in_s = int(s["fade_in_ms"] * config.SAMPLE_RATE / 1000)
        pos_samples = pos // 2
        total_samples = len(ann_data) // 2

        mixed = _apply_envelope(
            chunk, ann_chunk, pos_samples, total_samples,
            fade_out_s, fade_in_s, s["ducking_level"],
        )
        with self._lock:
            self._ann_pos = pos + take
        if self._ann_pos >= len(ann_data):
            self._finish_announcement()
        else:
            self._update_state(last_event="🔔 объявление в эфире")
        return mixed

    def _finish_announcement(self):
        with self._lock:
            self._ann_active = False
            self._ann_pos = 0
        self._update_state(announcement_active=False, last_event="streaming")

    def _update_perf(self, source_type):
        now = time.time()
        if now - self._stats_last < 1.0:
            return
        self._stats_last = now
        cpu = psutil.cpu_percent(interval=None)
        net_bps = 0
        if source_type == "stream":
            cur = audio_capture.get_bytes_received()
            dt = now - self._net_last_time
            if self._net_last_time > 0 and dt > 0:
                net_bps = max(0, int((cur - self._net_last_bytes) / dt))
            self._net_last_bytes = cur
            self._net_last_time = now
        self._update_state(cpu_percent=round(cpu, 1), network_bps=net_bps)

    def _stream_manager(self):
        """Генератор чанков с логикой fallback-источников."""
        s = self.get_settings()

        if s["source_type"] == "device":
            self._update_state(current_source="Устройство", using_reserve=False)
            try:
                yield from stream_from_device_safe(s["input_device"])
            except Exception as e:
                logger.warning(f"Ошибка устройства: {e}")
            return

        main_url = (s.get("radio_url") or "").strip()
        if not main_url:
            return

        fallback_enabled = bool(s.get("fallback_enabled"))
        try:
            timeout = float(s.get("fallback_timeout_seconds", 10))
        except (TypeError, ValueError):
            timeout = 10
        try:
            retry_main = float(s.get("fallback_retry_main_seconds", 60))
        except (TypeError, ValueError):
            retry_main = 60

        reserves = s.get("fallback_sources", []) if fallback_enabled else []
        sources = [{"name": "Основной", "url": main_url, "is_main": True}]
        for i, r in enumerate(reserves):
            url = (r.get("url") or "").strip()
            if not url:
                continue
            sources.append({"name": r.get("name") or f"Резерв {i+1}", "url": url, "is_main": False})

        if len(sources) == 1:
            self._update_state(current_source="Основной", using_reserve=False)
            try:
                yield from stream_audio_chunks(url=main_url, read_timeout=timeout)
            except Exception as e:
                logger.warning(f"Источник упал: {e}")
            return

        idx = 0
        last_try_main = 0.0
        while not self._stop_event.is_set():
            src = sources[idx]
            self._update_state(
                current_source=src["name"],
                using_reserve=not src["is_main"],
                source_info=f"{src['name']}: {src['url']}",
            )
            try:
                for chunk in stream_audio_chunks(url=src["url"], read_timeout=timeout):
                    if self._stop_event.is_set():
                        break
                    yield chunk
                    if not src["is_main"] and time.time() - last_try_main > retry_main:
                        break  # пора проверить основной
            except Exception as e:
                logger.warning(f"Источник '{src['name']}' недоступен: {e}")
                if idx < len(sources) - 1:
                    idx += 1
                else:
                    idx = 0
                    time.sleep(2)
                continue

            if self._stop_event.is_set():
                break

            if not src["is_main"]:
                idx = 0
                last_try_main = time.time()
            else:
                if len(sources) > 1:
                    idx = 1
                time.sleep(1)

    def _run(self):
        self._update_state(running=True, last_event="starting", error=None)
        self._net_last_bytes = audio_capture.get_bytes_received()
        self._net_last_time = time.time()

        try:
            for chunk in self._stream_manager():
                if self._stop_event.is_set():
                    break
                if self._sip_active:
                    time.sleep(0.02)
                    continue

                self._update_scan_buffer(chunk)
                self._update_perf(self.get_settings()["source_type"])
                self._update_spectrum(chunk)

                rms, db = self._level(chunk)
                self._update_state(signal_rms=round(rms, 4), signal_db=round(db, 1))

                s = self.get_settings()
                tones = detect_all(chunk, [
                    {"name": "ctcss", "freq": s["tone_freq"], "threshold": s["detection_threshold"]},
                    {"name": "dcs",   "freq": s["dcs_freq"], "threshold": s["dcs_threshold"]},
                ])
                ctcss = bool(tones["ctcss"]["detected"]) or bool(s["simulate_tone"])
                dcs = bool(tones["dcs"]["detected"])

                if ctcss and not self._prev_ctcss:
                    self._record_tone("ctcss")
                if dcs and not self._prev_dcs:
                    self._record_tone("dcs")
                self._prev_ctcss = ctcss
                self._prev_dcs = dcs

                if ctcss or dcs:
                    with self._lock:
                        self._last_activity_time = time.time()

                self._update_state(
                    ctcss_detected=ctcss,
                    ctcss_power=round(1.0 if s["simulate_tone"] else tones["ctcss"]["power"], 4),
                    dcs_detected=dcs,
                    dcs_power=round(tones["dcs"]["power"], 4),
                )

                if self._ann_active:
                    mixed = self._play_chunk(chunk)
                    self._push_output(mixed if mixed is not None else chunk)
                    continue

                if ctcss or dcs:
                    self._handle_trigger()
                else:
                    self._check_timeout_trigger(s)
                    if not self._ann_active:
                        self._update_state(last_event="streaming")

                self._push_output(chunk)
                # Без sleep: ffmpeg отдаёт ровно по block_size, нет накопления задержки
        except Exception as e:
            logger.exception("Ошибка движка")
            self._update_state(error=str(e))
        finally:
            self._update_state(
                running=False,
                ctcss_detected=False,
                dcs_detected=False,
                announcement_active=False,
            )


def stream_from_device_safe(device):
    from audio_capture import stream_from_device
    yield from stream_from_device(device=device)