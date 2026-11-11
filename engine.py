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
from audio_capture import stream_audio, stream_audio_chunks
from tone_detector import detect_all, scan_ctcss_frequencies
from dtmf_detector import DTMFDetector
from mixer import load_announcement
from audio_output import HardwareOutput
import announcement_store
import settings_store
import config

logger = logging.getLogger(__name__)

SPECTRUM_BANDS = 24


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
        self._lock = threading.RLock()

        self._last_play_time = 0.0
        self._ann_active = False
        self._ann_pos = 0

        self._playing_data = None
        self._playing_id = None
        self._playing_name = None

        self._sip_active = False
        self._sip_call = None

        # --- Fade out/in при SIP (по времени, не по итерациям) ---
        self._sip_gain_current = 1.0
        self._chime_pump_thread = None
        self._sip_gain_target = 1.0
        self._sip_gain_start = 1.0  # gain на момент старта fade
        self._sip_fade_start_time = 0.0  # когда начался fade

        # --- Chime (сигнал внимания перед SIP) ---
        self._chime_data = None
        self._chime_active = False
        self._chime_pos = 0
        self._chime_call = None

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

        self._output_queue = queue.Queue(maxsize=config.STREAM_QUEUE_SIZE)

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
        }

    # --- SIP ---
    def enter_sip_mode(self):
        with self._lock:
            was_active = self._sip_active
            self._sip_active = True
            self._sip_call = None
            self._state["sip_active"] = True
            # Если уже в SIP-режиме (после chime) — не сбрасываем fade
            if not was_active:
                self._sip_gain_start = self._sip_gain_current
                self._sip_gain_target = config.SIP_DUCKING_LEVEL
                self._sip_fade_start_time = time.time()
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
            self._sip_gain_start = self._sip_gain_current
            self._sip_gain_target = 1.0
            self._sip_fade_start_time = time.time()
        self._set_event("streaming", force=True)
        self._update_state(
            output_type="idle",
            output_name=None,
            output_level_db=-90.0,
            output_pos=None,
            output_total=None,
        )
        logger.info("Режим SIP выключен.")

    def start_sip_chime(self, call=None):
        """Возвращает True если можно играть/ответить, False если busy."""
        with self._lock:
            already_busy = self._sip_active or self._chime_active
            chime_enabled = self._settings.get("sip_chime_enabled", True)

        print(f"[SIP] start_sip_chime: busy={already_busy}, "
              f"enabled={chime_enabled}")

        if already_busy:
            logger.warning("[SIP] Второй звонок — отклоняем")
            return False

        chime = self._load_chime() if chime_enabled else None
        print(f"[SIP] chime data: "
              f"{'загружен' if chime else 'НЕТ'} "
              f"({len(chime) if chime else 0} байт)")

        if chime is None:
            # Без chime — sip_client сразу ответит 200
            return True

        with self._lock:
            # ← ВАЖНО: запускаем SIP-режим УЖЕ СЕЙЧАС.
            # Radio начнёт гаснуть со следующего чанка _run,
            # ДО chime (chime играет через _apply_envelope с fade).
            self._sip_active = True
            self._sip_call = None
            self._state["sip_active"] = True
            self._sip_gain_start = self._sip_gain_current
            self._sip_gain_target = config.SIP_DUCKING_LEVEL
            self._sip_fade_start_time = time.time()

            # Chime
            self._chime_data = chime
            self._chime_pos = 0
            self._chime_active = True
            self._chime_call = call
            if self._ann_active:
                self._ann_active = False
                self._ann_pos = 0

        # Сбрасываем накопленную очередь HW — чтобы chime пошёл СРАЗУ,

        self._clear_output_queues()
        self._spawn_chime_pump()
        self._set_event("🔔 Входящий звонок — сигнал внимания", force=True)
        self._update_state(
            output_type="chime",
            output_name="Сигнал внимания",
            output_level_db=-90.0,
            output_pos=0.0,
            output_total=round(len(chime) / 2 / config.SAMPLE_RATE, 2),
        )
        return True

    def is_chime_active(self):
        with self._lock:
            return self._chime_active

    def cancel_chime(self):
        """Отмена chime (например, звонок сброшен до конца chime)."""
        was_active = False
        with self._lock:
            if self._chime_active:
                was_active = True
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
        if was_active:
            self._set_event("📞 Звонок отменён", force=True)
            self._update_state(output_type="idle", output_name=None, output_level_db=-90.0)

    def reset_sip_state(self):
        """Принудительно сбрасывает SIP-состояние в idle.
        Вызывается при старте SIP-клиента и при старте движка,
        чтобы исключить «залипание» после сбоев."""
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

    # --- Chime / сигнал внимания ---
    def _load_chime(self):
        """Загружает chime-файл (WAV → PCM bytes). Кэширует."""
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

    def test_chime(self):
        """Проигрывает chime без SIP-звонка. Возвращает dict {"ok", "error"}."""
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
            self._chime_call = None
            if self._ann_active:
                self._ann_active = False
                self._ann_pos = 0

        # ← Очищаем накопленный radio — chime пойдёт СРАЗУ
            self._clear_output_queues()
            self._spawn_chime_pump()
            self._set_event("🔔 Тест сигнала внимания", force=True)
            self._update_state(
            output_type="chime",
            output_name="Тест сигнала",
            output_level_db=-90.0,
            output_pos=0.0,
            output_total=round(len(chime) / 2 / config.SAMPLE_RATE, 2),
        )
        return {"ok": True}
    def _spawn_chime_pump(self):
        """Запускает отдельный поток, который гонит chime в HW queue,
        НЕ дожидаясь, пока _run проснётся от read() FFmpeg.
        Только если ещё не запущен."""
        t = self._chime_pump_thread
        if t is not None and t.is_alive():
            return
        chunk_len = config.BLOCK_SIZE * config.CHANNELS * 2
        silence = b"\x00" * chunk_len
        sleep_s = chunk_len / 2 / config.SAMPLE_RATE  # ≈21 мс

        def _pump():
            while True:
                with self._lock:
                    if not self._chime_active:
                        break
                try:
                    self._play_chime_chunk(silence)
                except Exception as e:
                    print(f"[CHIME-PUMP] error: {e}")
                    break
                time.sleep(sleep_s)

        self._chime_pump_thread = threading.Thread(
            target=_pump, daemon=True, name="chime-pump"
        )
        self._chime_pump_thread.start()

    def _play_chime_chunk(self, radio_chunk):
        """Играет chime вместо радио. Радио плавно гаснет до SIP_DUCKING_LEVEL."""
        with self._lock:
            chime = self._chime_data
            pos = self._chime_pos

        if pos == 0:
            print(f"[CHIME] t={time.time():.3f} first chunk, "
                  f"hw_q={self._hw_output._queue.qsize() if hasattr(self._hw_output, '_queue') else '?'}") #диагностика chime

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
            config.SIP_DUCKING_LEVEL,   # ← было 0.0, теперь 0.05
        )

        # Обновляем _sip_gain_current синхронно по времени,
        # чтобы после chime не было скачка radio с 5% обратно до 100%
        if self._sip_active:
            fade_s = config.SIP_FADE_MS / 1000.0
            elapsed = time.time() - self._sip_fade_start_time
            if fade_s <= 0 or elapsed >= fade_s:
                self._sip_gain_current = self._sip_gain_target
            else:
                t = elapsed / fade_s
                self._sip_gain_current = (
                    self._sip_gain_start
                    + (self._sip_gain_target - self._sip_gain_start) * t
                )

        with self._lock:
            self._chime_pos = pos + take

        self._push_output(
            out, "chime",
            {
                "output_name": "Сигнал внимания",
                "output_pos": round(self._chime_pos / 2 / config.SAMPLE_RATE, 2),
                "output_total": round(len(chime) / 2 / config.SAMPLE_RATE, 2),
            },
        )

        if self._chime_pos >= len(chime):
            self._finish_chime()

    def _finish_chime(self):
        """Chime закончился. sip_client сам отправит answer 200."""
        with self._lock:
            self._chime_active = False
            self._chime_pos = 0
            self._chime_call = None
            sip_active = self._sip_active

        if sip_active:
            self._update_state(
                output_type="sip",
                output_name="SIP-звонок",
                output_level_db=-90.0,
                output_pos=None,
                output_total=None,
            )
        self._update_state(last_event="streaming")

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

    def get_hw_output(self):
        return self._hw_output

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
            # Предзагрузка chime (ускоряет реакцию на первый звонок)
            try:
                self._load_chime()
            except Exception:
                pass
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


    # --- Внутреннее ---
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
    def _compute_spectrum(chunk, prev_spectrum=None):
        try:
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            if len(samples) < 512:
                return [0.0] * SPECTRUM_BANDS
            n_fft = len(samples)
            window = np.hanning(n_fft)
            fft = np.fft.rfft(samples * window)
            mag = np.abs(fft)
            sr = config.SAMPLE_RATE
            nyquist = sr / 2.0
            freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
            f_min = 40.0
            f_max = min(nyquist, 16000.0)
            if f_max <= f_min:
                return [0.0] * SPECTRUM_BANDS
            log_ratio = np.log(f_max / f_min)
            bands = []
            for i in range(SPECTRUM_BANDS):
                f_lo = f_min * np.exp(log_ratio * i / SPECTRUM_BANDS)
                f_hi = f_min * np.exp(log_ratio * (i + 1) / SPECTRUM_BANDS)
                idx_lo = int(np.searchsorted(freqs, f_lo, side="left"))
                idx_hi = int(np.searchsorted(freqs, f_hi, side="right"))
                if idx_hi <= idx_lo:
                    idx_hi = min(idx_lo + 1, len(mag))
                band_mag = float(np.mean(mag[idx_lo:idx_hi])) if idx_hi > idx_lo else 0.0
                tilt = 1.0 + (i / SPECTRUM_BANDS) * 4.0
                bands.append(band_mag * tilt)
            mx = max(bands) if max(bands) > 0 else 1.0
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

    def _update_spectrum(self, chunk):
        now = time.time()
        if now - self._spectrum_last < 0.08:
            return
        self._spectrum_last = now
        with self._lock:
            prev = list(self._state.get("spectrum") or [])
        spec = self._compute_spectrum(chunk, prev)
        self._update_state(spectrum=spec)

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

    def _clear_output_queues(self):
        """Очищает output_queue и HW queue перед chime — чтобы chime
        не встал в конец 4-6-секундного backlog'а."""
        print(f"[CLEAR] t={time.time():.3f} clearing queues")
        try:
            self._hw_output.clear()
        except Exception:
            pass
        try:
            while True:
                self._output_queue.get_nowait()
        except queue.Empty:
            pass

    def _push_output(self, chunk, output_type="radio", extra=None):
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

        # Метрики исходящего потока (не чаще 10 раз/сек)
        now = time.time()
        if now - self._output_last_update < 0.1:
            return
        self._output_last_update = now
        try:
            _rms, db = self._level(chunk)
        except Exception:
            db = -90.0
        upd = {
            "output_type": output_type,
            "output_level_db": round(db, 1),
        }
        if extra:
            upd.update(extra)
        self._update_state(**upd)

    # --- Выбор объявления и расписания ---
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

    # --- DTMF ---
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

    # --- Триггеры ---
    def _handle_trigger(self, from_timeout=False, dtmf_label=None):
        now = time.time()
        with self._lock:
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
        self._handle_trigger(from_timeout=True)

    def _play_chunk(self, chunk):
        with self._lock:
            ann_data = self._playing_data
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

    def _stream_manager(self):
        s = self.get_settings()
        if s["source_type"] == "device":
            self._update_state(current_source="Устройство", using_reserve=False)
            try:
                from audio_capture import stream_from_device
                yield from stream_from_device(device=s["input_device"])
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
                        break
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
        self._update_state(running=True, error=None)
        self._set_event("starting", force=True)
        self._net_last_bytes = audio_capture.get_bytes_received()
        self._net_last_time = time.time()

        try:
            for chunk in self._stream_manager():
                if self._stop_event.is_set():
                    break
                if self._chime_active:
                    # chime играет chime_pump — независимо от read() FFmpeg.
                    # _run только пропускает входящие чанки, чтобы не дублировать.
                    time.sleep(0.005)
                    # ← ОБЯЗАТЕЛЬНО: иначе chime
                    # генерируется 50x быстрее, чем
                    # HW успевает воспроизвести
                    if self._chime_pos == 0:
                        print(f"[CHIME] t={time.time():.3f} run-loop started chime") #диагностика chime
                    continue

                # --- Плавное затухание/возврат радио при SIP (по времени) ---
                if self._sip_active or self._sip_gain_current != self._sip_gain_target:
                    fade_s = config.SIP_FADE_MS / 1000.0
                    elapsed = time.time() - self._sip_fade_start_time
                    if fade_s <= 0 or elapsed >= fade_s:
                        self._sip_gain_current = self._sip_gain_target
                    else:
                        t = elapsed / fade_s
                        self._sip_gain_current = (
                                self._sip_gain_start
                                + (self._sip_gain_target - self._sip_gain_start) * t
                        )

                    if self._sip_gain_current <= 0.005:
                        silence = b"\x00" * len(chunk)
                        self._push_output(silence, "sip",
                                          {"output_name": "SIP-звонок"})
                    else:
                        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                        arr = arr * self._sip_gain_current
                        arr = np.clip(arr, -32768, 32767).astype(np.int16)
                        self._push_output(arr.tobytes(), "sip",
                                          {"output_name": "SIP-звонок"})
                    time.sleep(0.005)
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
                    self._check_timeout_trigger(s)
                    if not self._ann_active:
                        self._set_event("streaming")

                with self._lock:
                    src_name = self._state.get("current_source") or "Основной"
                self._push_output(chunk, "radio", {"output_name": src_name})
                time.sleep(0.005)

        except Exception as e:
            logger.exception("Ошибка движка")
            self._update_state(error=str(e))
        finally:
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