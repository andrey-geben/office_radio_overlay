# sip_client.py
# Обёртка над subprocess sip_worker.py.
# pjsua2 живёт в subprocess — не пересекается с WASAPI HardwareOutput.

import os
import sys
import json
import subprocess
import threading
import time
import config


class SipClient:
    def __init__(self, engine, domain, username, password,
                 port=5060, transport="udp", playback_device=None,
                 playback_device_name=None):
        self.engine = engine
        self.domain = domain
        self.username = username
        self.password = password
        self.port = int(port)
        self.transport = transport or "udp"
        self.playback_device = playback_device
        self.playback_device_name = playback_device_name

        self._proc = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._running = False
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()

        self._ready_evt = threading.Event()
        self._registered_evt = threading.Event()

    def is_running(self):
        with self._lock:
            if not self._running or self._proc is None:
                return False
            return self._proc.poll() is None

    # --- Жизненный цикл ---

    def start(self):
        with self._lock:
            if self._running and self._proc is not None and self._proc.poll() is None:
                return False
            self._running = True
            self._ready_evt.clear()
            self._registered_evt.clear()

        # В dev: python -u sip_worker.py
        # В .exe: тот же .exe с флагом --sip-worker (перехват в main.py)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--sip-worker"]
        else:
            worker_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "sip_worker.py",
            )
            cmd = [sys.executable, "-u", worker_path]

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        creation_flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creation_flags,
            )
        except Exception as e:
            print(f"[SIP] subprocess start error: {e}")
            with self._lock:
                self._running = False
                self._proc = None
            return False

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="sip-stdout"
        )
        self._stdout_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="sip-stderr"
        )
        self._stderr_thread.start()

        if not self._ready_evt.wait(timeout=5):
            print("[SIP] WARNING: subprocess 'ready' не пришёл за 5 сек")

        # init
        self._send({
            "cmd": "init",
            "port": self.port,
            "transport": self.transport,
            "playback_device": self.playback_device,
            "playback_device_name": self.playback_device_name,
        })

        # register
        self._send({
            "cmd": "register",
            "domain": self.domain,
            "username": self.username,
            "password": self.password,
        })

        if not self._registered_evt.wait(timeout=5):
            print("[SIP] WARNING: регистрация не завершилась за 5 сек (ожидаем в фоне)")

        print(f"[SIP] Клиент запущен ({self.username}@{self.domain}). Ожидание звонков...")
        return True

    def set_playback_device(self, name=None, dev_id=None):
        """Переключить устройство вывода SIP (то же, что у серверного эфира)."""
        self.playback_device_name = name
        self.playback_device = dev_id
        if not self.is_running():
            return False
        return self._send({
            "cmd": "set_playback",
            "playback_device_name": name,
            "playback_device": dev_id,
        })

    def stop(self):
        with self._lock:
            self._running = False
            proc = self._proc
            self._proc = None

        if proc is not None:
            try:
                self._send_to_proc(proc, {"cmd": "stop"})
            except Exception:
                pass
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        print("[SIP] Клиент остановлен.")

    # --- Отправка/чтение ---

    def _send_to_proc(self, proc, msg):
        if proc is None or proc.stdin is None:
            return
        with self._send_lock:
            try:
                line = json.dumps(msg, ensure_ascii=False) + "\n"
                proc.stdin.write(line)
                proc.stdin.flush()
            except Exception as e:
                print(f"[SIP] send error: {e}")

    def _send(self, msg):
        with self._lock:
            proc = self._proc
        self._send_to_proc(proc, msg)

    def _read_stdout(self):
        with self._lock:
            proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    print(f"[SIP] bad json: {line[:100]}")
                    continue
                try:
                    self._handle_event(msg)
                except Exception as e:
                    print(f"[SIP] event handler error: {e}")
        except Exception as e:
            print(f"[SIP] stdout read error: {e}")
        finally:
            with self._lock:
                self._running = False

    def _read_stderr(self):
        with self._lock:
            proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    print(line)
        except Exception:
            pass

    # --- Диспетчер событий ---

    def _handle_event(self, msg):
        ev = msg.get("event")

        if ev == "ready":
            self._ready_evt.set()

        elif ev == "registered":
            print("[SIP] Account created")

        elif ev == "reg_state":
            code = int(msg.get("code", 0))
            reason = str(msg.get("reason", ""))
            print(f"[SIP] Регистрация: {reason} (код {code})")
            if code == 200:
                self._registered_evt.set()

        elif ev == "playback_set":
            dev_id = msg.get("dev_id")
            actual = msg.get("actual")
            print(f"[SIP] PJSIP playback: set={dev_id}, actual={actual}")

        elif ev == "incoming_call":
            remote = msg.get("remote", "unknown")
            print(f"[SIP] Входящий звонок от: {remote}")
            threading.Thread(
                target=self._handle_incoming,
                daemon=True,
                name="sip-incoming",
            ).start()

        elif ev == "call_confirmed":
            print("[SIP] Звонок подтверждён.")
            try:
                self.engine.enter_sip_mode()
            except Exception as e:
                print(f"[SIP] enter_sip_mode error: {e}")

        elif ev == "call_ended":
            reason = msg.get("reason", "unknown")
            print(f"[SIP] Звонок завершён (reason: {reason}).")
            try:
                self.engine.cancel_chime()
            except Exception:
                pass
            try:
                self.engine.exit_sip_mode()
            except Exception as e:
                print(f"[SIP] exit_sip_mode error: {e}")

        elif ev == "media_active":
            print("[SIP] SIP-аудио -> CABLE In (subprocess)")

        elif ev == "answered":
            pass

        elif ev == "error":
            where = msg.get("where", "?")
            error = msg.get("error", "?")
            print(f"[SIP] ERROR [{where}]: {error}")

    # --- Обработка входящего ---

    def _handle_incoming(self):
        """Запускает chime, ждёт окончания в ЭФИРЕ, потом отвечает 200 OK."""
        t0 = time.time()
        print(f"[SIP] [{t0:.3f}] incoming: start_sip_chime")

        # Если звучит объявление — даём ему доиграть, чтобы chime
        # не обрывал фразу на полуслове.
        try:
            ann_left = float(self.engine.get_announcement_seconds_left() or 0.0)
        except Exception:
            ann_left = 0.0
        if ann_left > 0:
            print(f"[SIP] Ждём окончания объявления ({ann_left:.1f} c)")
            time.sleep(min(ann_left + 0.2, 15.0))

        try:
            ok = self.engine.start_sip_chime()
        except Exception as e:
            print(f"[SIP] start_sip_chime error: {e}")
            ok = False

        print(f"[SIP] [{time.time():.3f}] start_sip_chime -> {ok}")

        if not ok:
            print("[SIP] Занято — отклоняем (486)")
            self._send({"cmd": "answer", "status": 486})
            return

        # Эфир остановлен: сигнала внимания нет, соединяем сразу.
        try:
            engine_running = bool(self.engine.get_state().get("running"))
        except Exception:
            engine_running = True
        if not engine_running:
            print("[SIP] Эфир остановлен — отвечаем без ожидания сигнала")
            time.sleep(0.2)
            self._send({"cmd": "answer", "status": 200})
            return

        # 1) Ждём окончание сигнала строго по длительности файла.
        #    Буфер карты не опрашиваем: после сигнала радио уже возвращается,
        #    и «дренаж» мог ждать вечно (был ответ на 9-й секунде).
        try:
            chime_secs = float(self.engine.get_chime_seconds() or 0.0)
        except Exception:
            chime_secs = 0.0
        t_chime = time.time()
        if chime_secs > 0:
            time.sleep(chime_secs)
            print(f"[SIP] chime timer done: elapsed={time.time() - t_chime:.2f}s "
                  f"(expect={chime_secs:.2f}s)")
        # короткий хвост, чтобы последние сэмплы успели уйти в устройство
        time.sleep(0.15)

        # 3) Небольшая пауза после сигнала: хвост затухания не должен
        #    накладываться на первые слова вызывающего.
        try:
            _settings = self.engine.get_settings() or {}
            gap_ms = int(_settings.get("sip_answer_delay_ms",
                                       getattr(config, "SIP_ANSWER_DELAY_MS", 400)))
        except Exception:
            gap_ms = 400
        gap_ms = max(0, min(3000, gap_ms))
        if gap_ms:
            time.sleep(gap_ms / 1000.0)

        dt = time.time() - t0
        print(f"[SIP] [{time.time():.3f}] chime done IN AIR (elapsed={dt:.2f}s, "
              f"gap={gap_ms}ms) -> send answer 200")
        self._send({"cmd": "answer", "status": 200})