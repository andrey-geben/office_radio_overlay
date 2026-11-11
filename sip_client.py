# sip_client.py
# Обёртка над subprocess sip_worker.py.
# pjsua2 живёт в subprocess — не пересекается с WASAPI HardwareOutput.

import os
import sys
import json
import subprocess
import threading
import time
from queue import Queue, Empty


class SipClient:
    def __init__(self, engine, domain, username, password,
                 port=5060, transport="udp", playback_device=None):
        self.engine = engine
        self.domain = domain
        self.username = username
        self.password = password
        self.port = int(port)
        self.transport = transport or "udp"
        self.playback_device = playback_device

        self._proc = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._running = False
        self._lock = threading.Lock()

        self._ready_evt = threading.Event()
        self._registered_evt = threading.Event()

    def is_running(self):
        with self._lock:
            return self._running

    # --- Жизненный цикл ---

    def start(self):
        with self._lock:
            if self._running:
                return False
            self._running = True

        worker_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sip_worker.py",
        )

        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-u", worker_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            print(f"[SIP] subprocess start error: {e}")
            with self._lock:
                self._running = False
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
        })

        # register
        self._send({
            "cmd": "register",
            "domain": self.domain,
            "username": self.username,
            "password": self.password,
        })

        if not self._registered_evt.wait(timeout=10):
            print("[SIP] WARNING: регистрация не завершилась за 10 сек")

        print(f"[SIP] Клиент запущен "
              f"({self.username}@{self.domain}). Ожидание звонков...")
        return True

    def stop(self):
        with self._lock:
            self._running = False

        if self._proc is not None:
            try:
                self._send({"cmd": "stop"})
            except Exception:
                pass
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            self._proc = None

        print("[SIP] Клиент остановлен.")

    # --- Отправка/чтение ---

    def _send(self, msg):
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            line = json.dumps(msg, ensure_ascii=False) + "\n"
            proc.stdin.write(line)
            proc.stdin.flush()
        except Exception as e:
            print(f"[SIP] send error: {e}")

    def _read_stdout(self):
        proc = self._proc
        if proc is None:
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
        proc = self._proc
        if proc is None:
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
            # ждём reg_state для 200

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
            # В отдельном потоке — чтобы не блокировать чтение stdout
            threading.Thread(
                target=self._handle_incoming,
                daemon=True, name="sip-incoming"
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
            print("[SIP] SIP-аудио → CABLE In (subprocess)")

        elif ev == "answered":
            pass  # ответ на INVITE отправлен

        elif ev == "error":
            where = msg.get("where", "?")
            error = msg.get("error", "?")
            print(f"[SIP] ERROR [{where}]: {error}")

    # --- Обработка входящего ---

    def _handle_incoming(self):
        """Запускает chime, ждёт окончания в ЭФИРЕ, потом отвечает."""
        t0 = time.time()
        print(f"[SIP] [{t0:.3f}] incoming: start_sip_chime")

        try:
            ok = self.engine.start_sip_chime()
        except Exception as e:
            print(f"[SIP] start_sip_chime error: {e}")
            ok = False
        print(f"[SIP] [{time.time():.3f}] start_sip_chime → {ok}")

        if not ok:
            print("[SIP] Занято — отклоняем (486)")
            self._send({"cmd": "answer", "status": 486})
            return

        # 1) Ждём, пока chime сгенерирован (engine → HW queue)
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                if not self.engine.is_chime_active():
                    break
            except Exception:
                break
            time.sleep(0.02)
        t_gen = time.time()
        print(f"[SIP] [{t_gen:.3f}] chime generated "
              f"(elapsed={t_gen - t0:.2f}s)")

        # 2) Ждём, пока HW queue РЕАЛЬНО опустеет в эфире
        hw = None
        try:
            hw = self.engine.get_hw_output()
        except Exception as e:
            print(f"[SIP] get_hw_output: {e}")

        if hw is not None:
            deadline2 = time.time() + 5.0
            while time.time() < deadline2:
                try:
                    remain = hw.pending_duration()
                except Exception:
                    remain = 0.0
                if remain < 0.15:
                    break
                time.sleep(0.02)

        dt = time.time() - t0
        print(f"[SIP] [{time.time():.3f}] chime done IN AIR "
              f"(elapsed={dt:.2f}s) → send answer 200")
        self._send({"cmd": "answer", "status": 200})