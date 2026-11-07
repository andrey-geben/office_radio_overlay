# sip_client.py
import threading
import pjsua2 as pj


class MyAudioMediaPort(pj.AudioMediaPort):
    def __init__(self):
        super().__init__()

    def onFrameReceived(self, frame):
        pass


class MyCall(pj.Call):
    def __init__(self, engine, acc, call_id=pj.PJSUA_INVALID_ID):
        pj.Call.__init__(self, acc, call_id)
        self.engine = engine
        self._connected = False

    def onCallState(self, prm):
        ci = self.getInfo()
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            if not self._connected:
                self._connected = True
                print("[SIP] Звонок подтверждён.")
                self.engine.enter_sip_mode(self)
        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print("[SIP] Звонок завершён.")
            self._connected = False
            self.engine.exit_sip_mode()

    def onCallMediaState(self, prm):
        try:
            ci = self.getInfo()
            for media_info in ci.media:
                if media_info.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    if media_info.type == pj.PJMEDIA_TYPE_AUDIO:
                        call_media = pj.AudioMedia.typecastFromMedia(
                            self.getMedia(media_info.index)
                        )
                        play_dev_med = pj.Endpoint.instance().audDevManager().getPlaybackDevMedia()
                        call_media.startTransmit(play_dev_med)
                        print("[SIP] Аудио звонка направлено в устройство вывода.")
        except Exception as e:
            print(f"[SIP] onCallMediaState: {e}")


class MyAccount(pj.Account):
    def __init__(self, engine):
        pj.Account.__init__(self)
        self.engine = engine
        self._calls = []

    def onRegState(self, prm):
        print(f"[SIP] Регистрация: {prm.reason} (код {prm.code})")

    def onIncomingCall(self, iprm):
        print(f"[SIP] Входящий звонок от: {iprm.remoteUri}")
        call = MyCall(self.engine, self, iprm.callId)
        self._calls.append(call)
        prm = pj.CallOpParam()
        prm.statusCode = 200
        call.answer(prm)


class SipClient:
    """SIP-клиент. Параметры передаются явно — не из config."""

    def __init__(self, engine, domain, username, password, port=5060, transport="udp"):
        self.engine = engine
        self.domain = domain
        self.username = username
        self.password = password
        self.port = int(port)
        self.transport = transport or "udp"
        self.ep = None
        self.account = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

    def is_running(self):
        with self._lock:
            return self._running

    def start(self):
        with self._lock:
            if self._running:
                return False
            self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        try:
            self.ep = pj.Endpoint()
            self.ep.libCreate()
            ep_cfg = pj.EpConfig()
            ep_cfg.uaConfig.threadCnt = 1
            self.ep.libInit(ep_cfg)

            transport_cfg = pj.TransportConfig()
            transport_cfg.port = self.port
            ttype = (
                pj.PJSIP_TRANSPORT_UDP if self.transport.lower() == "udp"
                else pj.PJSIP_TRANSPORT_TCP
            )
            self.ep.transportCreate(ttype, transport_cfg)
            self.ep.libStart()

            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = f"sip:{self.username}@{self.domain}"
            acc_cfg.regConfig.registrarUri = f"sip:{self.domain}"
            cred = pj.AuthCredInfo("digest", "*", self.username, 0, self.password)
            acc_cfg.sipConfig.authCreds.append(cred)

            self.account = MyAccount(self.engine)
            self.account.create(acc_cfg)

            print(f"[SIP] Клиент запущен ({self.username}@{self.domain}). Ожидание звонков...")
            while self.is_running():
                self.ep.libHandleEvents(50)
        except Exception as e:
            print(f"[SIP] Ошибка: {e}")
        finally:
            if self.ep:
                try:
                    self.ep.libDestroy()
                except Exception:
                    pass
            with self._lock:
                self._running = False
            print("[SIP] Клиент остановлен.")

    def stop(self):
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=3)