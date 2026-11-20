# engine.py
import os
import threading
import time
import math
import logging
import collections
import queue
import numpy as np
import psutil

import audio_capture
from audio_capture import stream_audio_chunks, stop_active_ffmpeg
from tone_detector import detect_all, scan_ctcss_frequencies
from dtmf_detector import DTMFDetector
from mixer import load_announcement
from audio_output import HardwareOutput
import announcement_store
import settings_store
import config

logger = logging.getLogger(__name__)

SPECTRUM_BANDS = 24
_SPECTRUM_CACHE = {}


def _get_spectrum_plan(n_fft, sr):
    key = (n_fft, sr)
    plan = _SPECTRUM_CACHE.get(key)
    if plan is not None:
        return plan
    window = np.hanning(n_fft).astype(np.float32)
    nyquist = sr / 2.0
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    f_min = 40.0
    f_max = min(nyquist, 16000.0)
    if f_max <= f_min:
        return None
    log_ratio = math.log(f_max / f_min)
    bins = []
    for i in range(SPECTRUM_BANDS):
        f_lo = f_min * math.exp(log_ratio * i / SPECTRUM_BANDS)
        f_hi = f_min * math.exp(log_ratio * (i + 1) / SPECTRUM_BANDS)
        idx_lo = int(np.searchsorted(freqs, f_lo, side="left"))
        idx_hi = int(np.searchsorted(freqs, f_hi, side="right"))
        if idx_hi <= idx_lo:
            idx_hi = min(idx_lo + 1, len(freqs))
        tilt = 1.0 + (i / SPECTRUM_BANDS) * 4.0
        bins.append((idx_lo, idx_hi, tilt))
    plan = (window, bins)
    _SPECTRUM_CACHE[key] = plan
    return plan


def _drain_queue(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


def _apply_envelope(radio_chunk, ann_chunk, pos_samples, total_samples,
                    fade_out_samples, fade_in_samples, ducking,
                    ann_level=1.0):
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

    ann_gain = ann_gain * float(max(0.0, min(1.0, ann_level)))
    mixed = radio * radio_gain + ann * ann_gain  # ann_level ниже
    mixed = np.clip(mixed, -32768, 32767)
    return mixed.astype(np.int16).tobytes()


class RadioOverlayEngine:
    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        self._last_play_time = 0.0
        self._ann_active = False
        self._ann_pos = 0

        self._playing_data = None
        self._playing_id = None
        self._playing_name = None

        # --- SIP / chime ---
        self._sip_active = False
        self._sip_call = None

        self._chime_data = None
        self._chime_active = False
        self._chime_pos = 0
        self._chime_call = None

        # --- Статистика сработок ---
        self._tone_events = collections.deque(maxlen=5000)
        self._total_ctcss = 0
        self._total_dcs = 0
        self._prev_ctcss = False
        self._prev_dcs = False
        self._session_start = 0.0

        self._last_activity_time = time.time()
        self._last_event_time = 0.0

        self._stats_last = 0.0
        self._net_last_bytes = 0
        self._net_last_time = 0.0
        self._spectrum_last = 0.0
        self._output_last_update = 0.0

        # --- Очереди ---
        self._output_queue = queue.Queue(maxsize=config.STREAM_QUEUE_SIZE)
        # Каждый браузер-слушатель получает свою копию эфира.
        self._subscribers = []

        # --- Буфер для сканера субтонов ---
        self._scan_buffer = collections.deque()
        self._scan_buffer_size = 0
        self._scan_buffer_max = int(config.SAMPLE_RATE * 3)

        self._hw_output = HardwareOutput()

        # --- DTMF ---
        self._dtmf = DTMFDetector(
            sample_rate=config.SAMPLE_RATE,
            min_digit_ms=config.DTMF_MIN_DIGIT_MS,
        )
        self._dtmf_sequence = ""
        self._dtmf_last_digit = ""
        self._dtmf_last_digit_time = 0.0
        self._dtmf_last_match = ""

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
            "playing_name": None,
            "playing_id": None,
            "last_dtmf_digit": "",
            "dtmf_sequence": "",
            "dtmf_last_match": "",
            "output_type": "idle",
            "output_level_db": -90.0,
            "output_name": None,
            "output_pos": None,
            "output_total": None,
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
            "dtmf_labels": saved.get("dtmf_labels", config.DTMF_DEFAULT_LABELS),
            "sip_chime_enabled": saved.get("sip_chime_enabled", config.SIP_CHIME_ENABLED_DEFAULT),
            "sip_answer_delay_ms": saved.get(
                "sip_answer_delay_ms", getattr(config, "SIP_ANSWER_DELAY_MS", 400)),
        }

    # ==================== SIP ====================

    def get_hw_output(self):
        return self._hw_output

    def is_chime_active(self):
        with self._lock:
            return self._chime_active

    def get_chime_state(self):
        """Прогресс сигнала внимания: (active, pos_samples, total_samples)."""
        with self._lock:
            total = len(self._chime_data) if self._chime_data else 0
            return bool(self._chime_active), int(self._chime_pos), int(total)

    def play_chime_direct(self):
        """Играет сигнал прямо в звуковую карту, не дожидаясь потока.

        Нужен, если радио в этот момент переподключается и обычный chime
        (микшируемый в потоке) не успевает начаться: иначе трубка
        снимается раньше сигнала и chime звучит уже в разговоре.
        Возвращает длительность сигнала в секундах (0, если играть некуда).
        """
        chime = self._load_chime()
        if not chime:
            return 0.0
        hw = self._hw_output
        if not hw.is_running():
            return 0.0
        block = config.BLOCK_SIZE * 2
        block_sec = config.BLOCK_SIZE / float(config.SAMPLE_RATE)

        def feeder():
            for off in range(0, len(chime), block):
                hw.push(chime[off:off + block])
                time.sleep(block_sec)

        threading.Thread(target=feeder, daemon=True).start()
        return len(chime) / 2.0 / config.SAMPLE_RATE

    def get_chime_seconds(self):
        """Длительность сигнала внимания в секундах (0 — если нет)."""
        with self._lock:
            data = self._chime_data
        if data is None:
            return 0.0
        try:
            # байты -> сэмплы (2 байта) -> каналы -> секунды
            return len(data) / 2.0 / max(1, config.CHANNELS) / float(config.SAMPLE_RATE)
        except Exception:
            return 0.0

    def enter_sip_mode(self, sip_call=None):
        with self._lock:
            self._sip_active = True
            self._sip_call = sip_call
            self._state["sip_active"] = True
            chime_playing = self._chime_active
        self._set_event("📞 SIP-звонок в эфире", force=True)
        if not chime_playing:
            self._update_state(
                output_type="sip",
                output_name="SIP-звонок",
                output_level_db=-90.0,
                output_pos=None,
                output_total=None,
            )
        logger.info("Режим SIP включён.")

    def exit_sip_mode(self):
        with self._lock:
            self._sip_active = False
            self._sip_call = None
            self._state["sip_active"] = False
        self._set_event("streaming", force=True)
        self._update_state(
            output_type="idle",
            output_name=None,
            output_level_db=-90.0,
        )
        logger.info("Режим SIP выключен.")

    def reset_sip_state(self):
        """Принудительно сбрасывает SIP-состояние в idle."""
        with self._lock:
            self._sip_active = False
            self._sip_call = None
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
            self._state["sip_active"] = False
        self._update_state(
            output_type="idle",
            output_name=None,
            output_level_db=-90.0,
            output_pos=None,
            output_total=None,
        )
        logger.info("[SIP] Состояние сброшено в idle.")

    def is_sip_active(self):
        with self._lock:
            return self._sip_active

    # ==================== Chime ====================

    def _load_chime(self):
        """Загружает chime-файл (WAV -> PCM bytes). Кэширует."""
        with self._lock:
            if self._chime_data is not None:
                return self._chime_data
        path = config.SIP_CHIME_FILE
        if not os.path.isabs(path):
            path = os.path.join(settings_store.BASE_DIR, path)
        if not os.path.exists(path):
            logger.warning(f"Chime file not found: {path}")
            return None
        try:
            import wave
            with wave.open(path, "rb") as wf:
                n_ch = wf.getnchannels()
                sw = wf.getsampwidth()
                sr = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            if sw == 2 and sr == config.SAMPLE_RATE and n_ch == config.CHANNELS:
                data = frames
            else:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(path)
                audio = audio.set_frame_rate(config.SAMPLE_RATE).set_channels(config.CHANNELS)
                data = audio.raw_data
            with self._lock:
                self._chime_data = data
            return data
        except Exception as e:
            logger.error(f"Failed to load chime: {e}")
            return None

    def start_sip_chime(self, call=None):
        """Запускает сигнал внимания перед SIP-звонком. Возвращает True, если звонок принят."""
        with self._lock:
            already_busy = self._sip_active or self._chime_active
            chime_enabled = self._settings.get("sip_chime_enabled", True)

        if already_busy:
            logger.warning("[SIP] Второй звонок во время активного — отклоняем")
            if call is not None:
                try:
                    call.hangup()
                except Exception:
                    pass
            return False

        engine_running = bool(self._state.get("running"))
        try:
            hw_ready = self._hw_output.is_running()
        except Exception:
            hw_ready = False
        chime = self._load_chime() if (chime_enabled and engine_running and hw_ready) else None
        if not engine_running:
            logger.info("Эфир остановлен — SIP соединяем без сигнала внимания")
        elif not hw_ready:
            logger.info("Аппаратный выход не запущен — SIP соединяем без сигнала внимания")

        if chime is None:
            with self._lock:
                self._sip_active = True
                self._state["sip_active"] = True
            self._update_state(
                output_type="sip",
                output_name="SIP-звонок",
                output_level_db=-90.0,
            )
            self._set_event("📞 SIP-звонок в эфире", force=True)
            return True

        with self._lock:
            self._chime_data = chime
            self._chime_pos = 0
            self._chime_active = True
            # Сигнал играем напрямую в карту: не зависит от темпа источника
            # и не растягивается, если поток радио подтормаживает.
            self._chime_direct = True
            self._chime_call = call
            if self._ann_active:
                self._ann_active = False
                self._ann_pos = 0

        feeder = getattr(self, "_feed_chime_direct", None)
        if feeder is None:
            # Смешанный/старый билд: откатываемся к микшированию в потоке,
            # чтобы не отклонять звонок из-за отсутствующего метода.
            logger.warning("Нет _feed_chime_direct — chime пойдёт через поток")
            with self._lock:
                self._chime_direct = False
        else:
            threading.Thread(target=feeder, args=(chime,), daemon=True).start()

        self._set_event("🔔 Входящий звонок — сигнал внимания", force=True)
        self._update_state(
            output_type="chime",
            output_name="Сигнал внимания",
            output_level_db=-90.0,
            output_pos=0.0,
            output_total=round(len(chime) / 2 / config.SAMPLE_RATE, 2),
        )
        return True

    def cancel_chime(self):
        was_active = False
        with self._lock:
            if self._chime_active:
                was_active = True
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
            self._chime_direct = False
        if was_active:
            self._set_event("📞 Звонок отменён", force=True)
            self._update_state(output_type="idle", output_name=None, output_level_db=-90.0)

    def test_chime(self):
        """Проигрывает chime без SIP. Возвращает dict {"ok", "error"}."""
        if not self._thread or not self._thread.is_alive():
            return {"ok": False, "error": "Движок не запущен. Запустите трансляцию."}
        chime = self._load_chime()
        if chime is None:
            return {"ok": False, "error": "Файл chime.wav не найден или повреждён."}
        with self._lock:
            if self._chime_active:
                return {"ok": False, "error": "Сигнал уже воспроизводится."}
            self._chime_data = chime
            self._chime_pos = 0
            self._chime_active = True
            self._chime_direct = False
            self._chime_call = None
            if self._ann_active:
                self._ann_active = False
                self._ann_pos = 0
        self._set_event("🔔 Тест сигнала внимания", force=True)
        self._update_state(
            output_type="chime",
            output_name="Тест сигнала",
            output_level_db=-90.0,
            output_pos=0.0,
            output_total=round(len(chime) / 2 / config.SAMPLE_RATE, 2),
        )
        return {"ok": True}

    def _feed_chime_direct(self, chime):
        """Подаёт chime прямо в звуковую карту с реальным темпом."""
        block = config.BLOCK_SIZE * config.CHANNELS * 2
        block_sec = config.BLOCK_SIZE / float(config.SAMPLE_RATE)
        try:
            for off in range(0, len(chime), block):
                with self._lock:
                    if not self._chime_active or not self._chime_direct:
                        return
                self._hw_output.push(chime[off:off + block])
                with self._lock:
                    self._chime_pos = min(len(chime), off + block)
                time.sleep(block_sec)

            deadline = time.time() + 3.0
            while time.time() < deadline:
                try:
                    if self._hw_output.pending_duration() < 0.05:
                        break
                except Exception:
                    break
                time.sleep(0.02)
        finally:
            with self._lock:
                still = self._chime_active and self._chime_direct
            if still:
                self._finish_chime()

    def _play_chime_chunk(self, radio_chunk):
        with self._lock:
            chime = self._chime_data
            pos = self._chime_pos

        if chime is None or pos >= len(chime):
            self._finish_chime()
            return

        take = min(len(radio_chunk), len(chime) - pos)
        chime_chunk = chime[pos:pos + take]
        if take < len(radio_chunk):
            chime_chunk = chime_chunk + b"\x00" * (len(radio_chunk) - take)

        fade_out_s = int(config.SIP_CHIME_FADE_OUT_MS * config.SAMPLE_RATE / 1000)
        out = _apply_envelope(
            radio_chunk, chime_chunk,
            pos // 2, len(chime) // 2,
            fade_out_s, 0,
            0.0,
        )

        with self._lock:
            self._chime_pos = pos + take
            new_pos = self._chime_pos

        self._push_output(
            out, "chime",
            {
                "output_name": "Сигнал внимания",
                "output_pos": round(new_pos / 2 / config.SAMPLE_RATE, 2),
                "output_total": round(len(chime) / 2 / config.SAMPLE_RATE, 2),
            },
        )

        if new_pos >= len(chime):
            self._finish_chime()

    def _finish_chime(self):
        with self._lock:
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
            self._chime_direct = False
        self._update_state(last_event="streaming")

    # ==================== Публичное API ====================
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
            now_timeout_enabled = self._settings.get("tone_timeout_enabled", False)
            to_save = {k: v for k, v in kwargs.items()
                       if k in self._settings and k != "simulate_tone"}
            snapshot = dict(self._settings)

        if now_timeout_enabled and not was_timeout_enabled:
            with self._lock:
                self._last_activity_time = time.time()

        if to_save:
            try:
                settings_store.update(**to_save)
            except Exception as e:
                logger.warning(f"Не удалось сохранить настройки: {e}")

        return snapshot

    def set_fallback_sources(self, sources):
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

    def set_dtmf_labels(self, labels):
        cleaned = []
        for label in labels:
            if not isinstance(label, str):
                continue
            s = label.strip().upper()
            if 2 <= len(s) <= 10 and all(c in "0123456789*#ABCD" for c in s):
                cleaned.append(s)
        with self._lock:
            self._settings["dtmf_labels"] = cleaned
        try:
            settings_store.update(dtmf_labels=cleaned)
        except Exception:
            pass
        return cleaned

    def subscribe_output(self):
        """Отдельная очередь эфира для одного слушателя (браузера)."""
        q = queue.Queue(maxsize=config.STREAM_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe_output(self, q):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def get_output_chunk(self, timeout=1.0):
        """Совместимость: общая очередь (не использовать для веб-плеера)."""
        try:
            return self._output_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def is_alive(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    def is_hw_output_running(self):
        try:
            return self._hw_output.is_running()
        except Exception:
            return False

    def get_announcement_seconds_left(self):
        with self._lock:
            if not self._ann_active or not self._playing_data:
                return 0.0
            left = len(self._playing_data) - self._ann_pos
            return max(0.0, left / 2.0 / config.SAMPLE_RATE)

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
            buf_sec = round(self._scan_buffer_size / config.SAMPLE_RATE, 2)
        try:
            combined = np.concatenate(chunks).astype(np.int16).tobytes()
        except Exception as e:
            return {"ok": False, "error": f"Ошибка объединения буфера: {e}"}
        candidates = scan_ctcss_frequencies(combined)
        return {
            "ok": True,
            "buffer_seconds": buf_sec,
            "candidates": candidates,
        }

    def reload_announcement(self):
        with self._lock:
            ann_id = self._settings.get("active_announcement_id")
        if not ann_id:
            return False
        path = announcement_store.get_path(ann_id)
        if not path:
            return False
        try:
            load_announcement(path=path)
        except Exception:
            logger.exception("Не удалось загрузить %s", path)
            return False
        return True

    # ==================== Start / Stop ====================
    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        with self._lock:
            self._ann_active = False
            self._ann_pos = 0
            self._playing_data = None
            self._playing_id = None
            self._playing_name = None
            self._sip_active = False
            self._sip_call = None
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
            self._tone_events.clear()
            self._total_ctcss = 0
            self._total_dcs = 0
            self._prev_ctcss = False
            self._prev_dcs = False
            self._session_start = time.time()
            self._last_activity_time = time.time()
            self._last_event_time = 0.0
            self._scan_buffer.clear()
            self._scan_buffer_size = 0
            self._dtmf_sequence = ""
            self._dtmf_last_digit = ""
            self._dtmf_last_match = ""
            self._dtmf_last_digit_time = 0.0
            self._dtmf.reset()
            self._output_last_update = 0.0
            self._state["output_type"] = "idle"
            self._state["output_level_db"] = -90.0
            self._state["output_name"] = None
            self._state["output_pos"] = None
            self._state["output_total"] = None
            hw_dev = self._settings.get("hw_output_device")

        if hw_dev is not None and not self._hw_output.is_running():
            try:
                self._hw_output.start(hw_dev)
            except Exception as e:
                logger.warning(f"Не удалось восстановить аппаратный выход: {e}")

        try:
            self._load_chime()
        except Exception:
            pass

        _drain_queue(self._output_queue)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        stop_active_ffmpeg()
        # Гасим и хвост, уже отправленный в звуковую карту: иначе после
        # СТОПа ещё полсекунды звучит радио из буфера.
        try:
            self._hw_output.clear()
        except Exception:
            pass
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("Поток движка не завершился за 5 c — добиваю ffmpeg")
                stop_active_ffmpeg()
                self._thread.join(timeout=2)
        self._thread = None
        with self._lock:
            self._state["running"] = False
            self._state["announcement_active"] = False
            self._state["playing_name"] = None
            self._state["playing_id"] = None
            self._state["output_type"] = "idle"
            self._state["output_level_db"] = -90.0
            self._state["output_name"] = None
            self._state["output_pos"] = None
            self._state["output_total"] = None
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
        self._set_event("stopped", force=True)

    def force_stop(self):
        """Жёсткая остановка: движок + все ffmpeg-процессы приложения."""
        self.stop()
        killed = 0
        try:
            me = psutil.Process()
            for child in me.children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                    if name.startswith("ffmpeg") or name.startswith("ffprobe"):
                        child.kill()
                        killed += 1
                except Exception:
                    continue
        except Exception:
            pass
        try:
            self._hw_output.clear()
        except Exception:
            pass
        logger.info("force_stop: движок остановлен, добито ffmpeg: %d", killed)
        return killed

    # ==================== Утилиты ====================
    def _update_state(self, **kwargs):
        with self._lock:
            self._state.update(kwargs)

    def _set_event(self, text, force=False, min_interval=3.0):
        now = time.time()
        with self._lock:
            if not force and now - self._last_event_time < min_interval:
                return
            self._state["last_event"] = text
            self._last_event_time = now

    @staticmethod
    def _level(samples_f32):
        if len(samples_f32) == 0:
            return 0.0, -90.0
        rms = float(np.sqrt(np.mean(samples_f32 * samples_f32)))
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
    def _compute_spectrum(samples_f32, prev_spectrum=None):
        try:
            n_fft = len(samples_f32)
            if n_fft < 512:
                return [0.0] * SPECTRUM_BANDS
            plan = _get_spectrum_plan(n_fft, config.SAMPLE_RATE)
            if plan is None:
                return [0.0] * SPECTRUM_BANDS
            window, bins = plan
            mag = np.abs(np.fft.rfft(samples_f32 * window))
            bands = []
            for idx_lo, idx_hi, tilt in bins:
                band_mag = float(np.mean(mag[idx_lo:idx_hi])) if idx_hi > idx_lo else 0.0
                bands.append(band_mag * tilt)
            mx = max(bands)
            if mx <= 0.0:
                mx = 1.0
            norm = [b / mx for b in bands]
            if prev_spectrum and len(prev_spectrum) == len(norm):
                result = []
                for p, n in zip(prev_spectrum, norm):
                    if n > p:
                        result.append(n)
                    else:
                        result.append(p * 0.82 + n * 0.18)
                norm = result
            return norm
        except Exception as e:
            logger.debug(f"spectrum error: {e}")
            return [0.0] * SPECTRUM_BANDS

    def _update_spectrum(self, samples_f32):
        now = time.time()
        if now - self._spectrum_last < 0.08:
            return
        self._spectrum_last = now
        with self._lock:
            prev = list(self._state.get("spectrum") or [])
        spec = self._compute_spectrum(samples_f32, prev)
        self._update_state(spectrum=spec)

    def _update_scan_buffer(self, arr_i16):
        if len(arr_i16) == 0:
            return
        with self._lock:
            self._scan_buffer.append(arr_i16)
            self._scan_buffer_size += len(arr_i16)
            while self._scan_buffer_size > self._scan_buffer_max and self._scan_buffer:
                removed = self._scan_buffer.popleft()
                self._scan_buffer_size -= len(removed)

    def _push_output(self, chunk, output_type="radio", extra=None):
        if chunk is None:
            return
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass
        if self._hw_output.is_running():
            self._hw_output.push(chunk)

        now = time.time()
        if now - self._output_last_update < 0.1:
            return
        self._output_last_update = now
        try:
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * (1.0 / 32768.0)
            _rms, db = self._level(samples)
        except Exception:
            db = -90.0
        upd = {
            "output_type": output_type,
            "output_level_db": round(db, 1),
        }
        if extra:
            upd.update(extra)
        self._update_state(**upd)

    # ==================== Выбор объявления ====================
    def _pick_announcement_for_trigger(self):
        """Возвращает (ann_id, schedule_id) или None."""
        items = announcement_store.list_announcements()
        candidates = []
        for it in items:
            ok, _reason, best_sched = announcement_store.check_playable(it)
            if ok and best_sched:
                candidates.append({
                    "ann_id": it["id"],
                    "sched_id": best_sched["schedule_id"],
                    "last_played": best_sched.get("last_played") or "",
                })
        if not candidates:
            return None

        with self._lock:
            active_id = self._settings.get("active_announcement_id")

        if active_id:
            active_cands = [c for c in candidates if c["ann_id"] == active_id]
            if active_cands:
                active_cands.sort(key=lambda c: c["last_played"])
                return active_cands[0]["ann_id"], active_cands[0]["sched_id"]

        candidates.sort(key=lambda c: c["last_played"])
        return candidates[0]["ann_id"], candidates[0]["sched_id"]

    # ==================== DTMF ====================
    def _handle_dtmf_digits(self, digits):
        now = time.time()
        with self._lock:
            if now - self._dtmf_last_digit_time > config.DTMF_SEQUENCE_TIMEOUT_SECONDS:
                self._dtmf_sequence = ""
            for d in digits:
                self._dtmf_sequence += d
                self._dtmf_last_digit = d
                self._dtmf_last_digit_time = now
            if len(self._dtmf_sequence) > 20:
                self._dtmf_sequence = self._dtmf_sequence[-20:]
            seq = self._dtmf_sequence
            labels = list(self._settings.get("dtmf_labels", []))

        self._update_state(last_dtmf_digit=self._dtmf_last_digit, dtmf_sequence=seq)

        matched = None
        for label in sorted(labels, key=len, reverse=True):
            if label and seq.endswith(label):
                matched = label
                break

        if matched:
            with self._lock:
                self._dtmf_sequence = ""
                self._dtmf_last_match = matched
            self._update_state(dtmf_sequence="", dtmf_last_match=matched)
            logger.info(f"[DTMF] Обнаружена метка '{matched}'")
            self._handle_trigger(dtmf_label=matched)

    def _check_dtmf_timeout(self):
        need_clear = False
        with self._lock:
            if self._dtmf_sequence and (time.time() - self._dtmf_last_digit_time) > config.DTMF_SEQUENCE_TIMEOUT_SECONDS:
                self._dtmf_sequence = ""
                need_clear = True
        if need_clear:
            self._update_state(dtmf_sequence="")

    def trigger_dtmf_test(self, label):
        return self._handle_trigger(dtmf_label=f"test:{label}")

    # ==================== Триггеры ====================
    def _handle_trigger(self, from_timeout=False, dtmf_label=None):
        now = time.time()
        with self._lock:
            if self._sip_active or self._chime_active:
                logger.info("Объявление пропущено: идёт SIP-звонок или сигнал внимания")
                return False
            if now - self._last_play_time < config.MIN_ANNOUNCE_INTERVAL:
                return False

        picked = self._pick_announcement_for_trigger()
        if not picked:
            if dtmf_label:
                self._set_event(f"📟 DTMF {dtmf_label} — нет объявления в окне", force=True)
            elif not from_timeout:
                self._set_event("⏸ все объявления на паузе (расписание / лимит / интервал)", force=True)
            return False

        ann_id, sched_id = picked

        path = announcement_store.get_path(ann_id)
        if not path:
            self._set_event("объявление удалено", force=True)
            self._update_state(error=f"Файл объявления {ann_id} не найден")
            return False

        try:
            data = load_announcement(path=path)
        except Exception:
            logger.exception("Не удалось загрузить %s", path)
            self._set_event("ошибка загрузки объявления", force=True)
            return False

        meta = announcement_store.get_announcement(ann_id)
        name = (meta.get("original_name") if meta else None) or ann_id

        with self._lock:
            self._last_play_time = now
            self._last_activity_time = now
            self._playing_data = data
            self._playing_id = ann_id
            self._playing_name = name
            self._ann_pos = 0
            self._ann_active = True
            self._state["announcements_played"] += 1
            self._state["announcement_active"] = True
            self._state["playing_name"] = name
            self._state["playing_id"] = ann_id

        announcement_store.increment_play(ann_id, sched_id)

        if dtmf_label:
            self._set_event(f"📟 {name} (DTMF {dtmf_label})", force=True)
        elif from_timeout:
            self._set_event(f"⏰ {name} (по таймауту)", force=True)
        else:
            self._set_event(f"🔔 {name} в эфире", force=True)
        return True

    def _check_timeout_trigger(self, enabled, timeout_val):
        if not enabled:
            return
        try:
            timeout = float(timeout_val)
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
        self._handle_trigger(from_timeout=True)

    def _play_chunk(self, chunk):
        with self._lock:
            ann_data = self._playing_data
            pos = self._ann_pos
            fade_out_ms = self._settings["fade_out_ms"]
            fade_in_ms = self._settings["fade_in_ms"]
            ducking = self._settings["ducking_level"]
            ann_level = self._settings.get("announcement_level", 1.0)

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

        fade_out_s = int(fade_out_ms * config.SAMPLE_RATE / 1000)
        fade_in_s = int(fade_in_ms * config.SAMPLE_RATE / 1000)
        pos_samples = pos // 2
        total_samples = len(ann_data) // 2

        mixed = _apply_envelope(
            chunk, ann_chunk, pos_samples, total_samples,
            fade_out_s, fade_in_s, ducking, ann_level,
        )
        with self._lock:
            self._ann_pos = pos + take
            new_pos = self._ann_pos
        if new_pos >= len(ann_data):
            self._finish_announcement()
        return mixed

    def _finish_announcement(self):
        with self._lock:
            self._ann_active = False
            self._ann_pos = 0
            self._playing_data = None
            self._playing_id = None
            self._playing_name = None
        self._update_state(announcement_active=False,
                           playing_name=None, playing_id=None)
        self._set_event("streaming", force=True)

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

    # ==================== Менеджер источников ====================
    def _stream_manager(self):
        s = self.get_settings()

        if s["source_type"] == "device":
            self._update_state(current_source="Устройство", using_reserve=False)
            while not self._stop_event.is_set():
                try:
                    from audio_capture import stream_from_device
                    got_any = False
                    for chunk in stream_from_device(device=s["input_device"]):
                        if self._stop_event.is_set():
                            break
                        got_any = True
                        yield chunk
                    if not got_any and not self._stop_event.is_set():
                        print("[ENGINE] Устройство завершилось без данных, переподключение...")
                        time.sleep(2)
                except Exception as e:
                    logger.warning(f"Ошибка устройства: {e}")
                    if self._stop_event.is_set():
                        break
                    time.sleep(2)
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

        # --- Один источник: бесконечное переподключение ---
        if len(sources) == 1:
            self._update_state(current_source="Основной", using_reserve=False)
            while not self._stop_event.is_set():
                try:
                    for chunk in stream_audio_chunks(url=main_url, read_timeout=timeout):
                        if self._stop_event.is_set():
                            break
                        yield chunk
                except Exception as e:
                    logger.warning(f"Источник упал: {e}")
                if self._stop_event.is_set():
                    break
                print("[ENGINE] Источник завершился, переподключение через 3 сек...")
                self._set_event("⚠ источник отключился, переподключение", force=True)
                time.sleep(3)
            return

        # --- Несколько источников: fallback + автовозврат ---
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
                got_any = False
                for chunk in stream_audio_chunks(url=src["url"], read_timeout=timeout):
                    if self._stop_event.is_set():
                        break
                    got_any = True
                    yield chunk
                    if not src["is_main"] and time.time() - last_try_main > retry_main:
                        break
                if not got_any and not self._stop_event.is_set():
                    logger.warning(f"Источник '{src['name']}' завершился без данных")
                    if idx < len(sources) - 1:
                        idx += 1
                    else:
                        idx = 0
                        time.sleep(2)
                    continue
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

    # ==================== Главный цикл ====================
    def _run(self):
        self._update_state(running=True, error=None)
        self._set_event("starting", force=True)
        self._net_last_bytes = audio_capture.get_bytes_received()
        self._net_last_time = time.time()

        try:
            for chunk in self._stream_manager():
                if self._stop_event.is_set():
                    break

                if self._chime_active:
                    if self._chime_direct:
                        # Сигнал играет отдельный поток прямо в карту,
                        # радио в это время молчит.
                        continue
                    self._play_chime_chunk(chunk)
                    continue

                if self._sip_active:
                    # Во время SIP-разговора голос идёт напрямую через VB-Cable из sip_worker,
                    # а радио приглушается (отдаём тишину в выходной поток).
                    silence = b"\x00" * len(chunk)
                    self._push_output(silence, "sip", {"output_name": "SIP-звонок"})
                    continue

                arr_i16 = np.frombuffer(chunk, dtype=np.int16)
                samples_f32 = arr_i16.astype(np.float32) * (1.0 / 32768.0)

                with self._lock:
                    src_type = self._settings["source_type"]
                    tone_freq = self._settings["tone_freq"]
                    det_thresh = self._settings["detection_threshold"]
                    dcs_freq = self._settings["dcs_freq"]
                    dcs_thresh = self._settings["dcs_threshold"]
                    sim_tone = self._settings["simulate_tone"]
                    timeout_en = self._settings["tone_timeout_enabled"]
                    timeout_sec = self._settings["tone_timeout_seconds"]
                    src_name = self._state.get("current_source") or "Основной"

                self._update_scan_buffer(arr_i16)
                self._update_perf(src_type)
                self._update_spectrum(samples_f32)

                rms, db = self._level(samples_f32)
                self._update_state(signal_rms=round(rms, 4), signal_db=round(db, 1))

                tones = detect_all(chunk, [
                    {"name": "ctcss", "freq": tone_freq, "threshold": det_thresh},
                    {"name": "dcs",   "freq": dcs_freq, "threshold": dcs_thresh},
                ])
                ctcss = bool(tones["ctcss"]["detected"]) or bool(sim_tone)
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
                    ctcss_power=round(1.0 if sim_tone else tones["ctcss"]["power"], 4),
                    dcs_detected=dcs,
                    dcs_power=round(tones["dcs"]["power"], 4),
                )

                dtmf_digits = self._dtmf.feed(chunk)
                if dtmf_digits:
                    self._handle_dtmf_digits(dtmf_digits)
                else:
                    self._check_dtmf_timeout()

                if self._ann_active:
                    mixed = self._play_chunk(chunk)
                    with self._lock:
                        pos_bytes = self._ann_pos
                        playing = self._playing_data
                        ann_name = self._playing_name
                    pos_s = (pos_bytes / 2 / config.SAMPLE_RATE) if pos_bytes else 0.0
                    total_s = (len(playing) / 2 / config.SAMPLE_RATE) if playing else 0.0
                    self._push_output(
                        mixed if mixed is not None else chunk,
                        "announcement",
                        {
                            "output_name": ann_name,
                            "output_pos": round(pos_s, 2),
                            "output_total": round(total_s, 2),
                        },
                    )
                    continue

                if ctcss or dcs:
                    self._handle_trigger()
                else:
                    self._check_timeout_trigger(timeout_en, timeout_sec)
                    if not self._ann_active:
                        self._set_event("streaming")

                self._push_output(chunk, "radio", {"output_name": src_name})

        except Exception as e:
            logger.exception("Ошибка движка")
            self._update_state(error=str(e))
        finally:
            if not self._stop_event.is_set():
                print("[ENGINE] _stream_manager завершился без команды Стоп — источник закрылся")
            with self._lock:
                self._chime_active = False
                self._chime_pos = 0
                self._chime_call = None
            self._update_state(
                running=False,
                ctcss_detected=False,
                dcs_detected=False,
                announcement_active=False,
                playing_name=None,
                playing_id=None,
                output_type="idle",
                output_level_db=-90.0,
                output_name=None,
                output_pos=None,
                output_total=None,
            )
