# sip_worker.py
# Запускается как subprocess основным sip_client.py.
# Общается через stdin/stdout — JSON-lines.
# pjsua2 живёт ЗДЕСЬ, не в основном процессе.

import sys
import json
import time
import threading
from queue import Queue, Empty
import os

# C-логи pjsua2 (printf) идут в fd 1 (stdout). Перенаправим fd 1 в stderr,
# а наш JSON — через сохранённый dup fd 1.
_ORIG_STDOUT_FD = os.dup(1)
os.dup2(2, 1)   # теперь C-level stdout → stderr

# UTF-8 для русских символов
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import pjsua2 as pj


# --- Состояние ---
_state = {
    "ep": None,
    "account": None,
    "call": None,
    "playback_dev": None,
    "call_state": "idle",   # idle | incoming | confirmed
}

_cmd_queue = Queue()


def send(event, **kwargs):
    """Пишет JSON-строку в оригинальный stdout (fd, не буферизованный)."""
    msg = {"event": event, **kwargs}
    try:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        os.write(_ORIG_STDOUT_FD, data)
    except Exception:
        pass


def log(text):
    try:
        sys.stderr.write(f"[WORKER] {text}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _list_devices(ep, max_devices=20):
    result = []
    for idx in range(max_devices):
        try:
            info = ep.audDevManager().getDevInfo(idx)
        except Exception:
            break
        try:
            result.append((
                idx,
                info.name or "",
                int(info.inputCount),
                int(info.outputCount),
            ))
        except Exception:
            pass
    return result


def _find_output_device(ep, name_substrings):
    subs = [s.lower() for s in name_substrings]
    for idx, name, _in_c, out_c in _list_devices(ep):
        if out_c <= 0:
            continue
        nl = name.lower()
        for s in subs:
            if s in nl:
                return idx
    return None


class MyCall(pj.Call):
    def __init__(self, acc, call_id=pj.PJSUA_INVALID_ID):
        pj.Call.__init__(self, acc, call_id)
        self._connected = False
        self._media_transmitting = False
        self._play_dev_med = None
        self._call_media = None

    def onCallState(self, prm):
        ci = self.getInfo()
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            if not self._connected:
                self._connected = True
                _state["call_state"] = "confirmed"
                send("call_confirmed")
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            reason = getattr(ci, "lastReason", "") or "unknown"
            send("call_ended", reason=reason)
            self._connected = False
            self._media_transmitting = False
            self._play_dev_med = None
            self._call_media = None
            _state["call_state"] = "idle"
            _state["call"] = None

    def onCallMediaState(self, prm):
        if self._media_transmitting:
            return
        try:
            ci = self.getInfo()
            for media_info in ci.media:
                if media_info.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    if media_info.type == pj.PJMEDIA_TYPE_AUDIO:
                        call_media = pj.AudioMedia.typecastFromMedia(
                            self.getMedia(media_info.index)
                        )
                        adm = pj.Endpoint.instance().audDevManager()
                        play_dev_med = adm.getPlaybackDevMedia()
                        self._call_media = call_media
                        self._play_dev_med = play_dev_med
                        call_media.startTransmit(play_dev_med)
                        self._media_transmitting = True
                        send("media_active")
        except Exception as e:
            send("error", where="onCallMediaState", error=str(e))


class MyAccount(pj.Account):
    def __init__(self):
        pj.Account.__init__(self)
        self._calls = []

    def onRegState(self, prm):
        send("reg_state", code=int(prm.code), reason=str(prm.reason))

    def onIncomingCall(self, iprm):
        call = MyCall(self, iprm.callId)
        self._calls.append(call)
        _state["call"] = call
        _state["call_state"] = "incoming"
        try:
            ci = call.getInfo()
            remote = ci.remoteUri or "unknown"
        except Exception:
            remote = "unknown"
        send("incoming_call", remote=remote)
        # НЕ отвечаем — ждём команду "answer" от основного процесса


# --- Команды из stdin ---

def cmd_init(msg):
    if _state["ep"] is not None:
        return
    log("init endpoint")
    ep = pj.Endpoint()
    ep.libCreate()

    cfg = pj.EpConfig()
    cfg.uaConfig.threadCnt = 0
    cfg.logConfig.level = 3
    cfg.logConfig.consoleLevel = 3
    try:
        cfg.medConfig.clockRate = 48000
        cfg.medConfig.sndClockRate = 48000
        cfg.medConfig.channelCount = 1
        cfg.medConfig.noVad = True
        cfg.medConfig.ecTailLen = 0
        cfg.medConfig.audioFramePtime = 40
    except Exception as e:
        log(f"medConfig: {e}")

    ep.libInit(cfg)

    tcfg = pj.TransportConfig()
    tcfg.port = int(msg.get("port", 5060))
    transport = (msg.get("transport") or "udp").lower()
    ttype = (pj.PJSIP_TRANSPORT_UDP if transport == "udp"
             else pj.PJSIP_TRANSPORT_TCP)
    ep.transportCreate(ttype, tcfg)
    ep.libStart()

    # Список устройств для диагностики
    log("devices:")
    for idx, name, in_c, out_c in _list_devices(ep):
        log(f"  dev_id={idx} '{name}' in={in_c} out={out_c}")

    # Выбор playback: CABLE In → VoiceMeeter Aux
    target = _find_output_device(ep, ["cable in"])
    if target is None:
        target = _find_output_device(ep, ["voicemeeter aux",
                                          "voicemeeter vaio3"])
    if target is None and msg.get("playback_device") is not None:
        try:
            target = int(msg["playback_device"])
        except Exception:
            target = None

    if target is not None:
        try:
            ep.audDevManager().setPlaybackDev(int(target))
            actual = ep.audDevManager().getPlaybackDev()
            _state["playback_dev"] = target
            log(f"playback set={target} actual={actual}")
            send("playback_set", dev_id=target, actual=actual)
        except Exception as e:
            send("error", where="setPlaybackDev", error=str(e))
    else:
        log("playback: default (CABLE In / VM не найдены)")

    # SPEAKER_ONLY — без микрофона (Ducking не срабатывает)
    try:
        ep.audDevManager().setSndDevMode(pj.PJSUA_SND_DEV_SPEAKER_ONLY)
        log("SPEAKER_ONLY")
    except Exception as e:
        log(f"setSndDevMode: {e}")

    _state["ep"] = ep


def cmd_register(msg):
    ep = _state["ep"]
    if ep is None:
        send("error", where="register", error="endpoint not initialized")
        return
    domain = msg.get("domain")
    username = msg.get("username")
    password = msg.get("password")
    try:
        acc = MyAccount()
        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{username}@{domain}"
        acc_cfg.regConfig.registrarUri = f"sip:{domain}"
        cred = pj.AuthCredInfo("digest", "*", username, 0, password)
        acc_cfg.sipConfig.authCreds.append(cred)
        acc.create(acc_cfg)
        _state["account"] = acc
        send("registered")
    except Exception as e:
        send("error", where="register", error=str(e))


def cmd_answer(msg):
    call = _state.get("call")
    if call is None:
        send("error", where="answer", error="no call")
        return
    try:
        prm = pj.CallOpParam()
        prm.statusCode = int(msg.get("status", 200))
        call.answer(prm)
        send("answered")
    except Exception as e:
        send("error", where="answer", error=str(e))


def cmd_hangup(msg):
    call = _state.get("call")
    if call is None:
        return
    try:
        pj.Call.hangup(call, pj.CallOpParam())
        send("hangup_sent")
    except Exception as e:
        send("error", where="hangup", error=str(e))


def cmd_stop(msg):
    log("stop")
    acc = _state.get("account")
    if acc is not None:
        try:
            acc.setRegistration(False)
            acc.delete()
        except Exception:
            pass
    send("stopped")
    sys.exit(0)


COMMANDS = {
    "init": cmd_init,
    "register": cmd_register,
    "answer": cmd_answer,
    "hangup": cmd_hangup,
    "stop": cmd_stop,
}


# --- Main loop ---

def main():
    send("ready")

    def _stdin_reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                _cmd_queue.put(json.loads(line))
            except Exception as e:
                send("error", where="parse", error=str(e))

    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    while True:
        # Разгребаем команды
        try:
            while True:
                msg = _cmd_queue.get_nowait()
                cmd = msg.get("cmd")
                handler = COMMANDS.get(cmd)
                if handler is None:
                    send("error", where="dispatch", error=f"unknown: {cmd}")
                    continue
                try:
                    handler(msg)
                except Exception as e:
                    send("error", where=cmd, error=str(e))
        except Empty:
            pass

        # Крутим события pjsua2 в главном потоке
        ep = _state.get("ep")
        if ep is not None:
            try:
                ep.libHandleEvents(20)
            except Exception as e:
                log(f"libHandleEvents: {e}")
                time.sleep(0.05)
        else:
            time.sleep(0.05)


if __name__ == "__main__":
    main()