# tests/test_smoke.py
"""Быстрые проверки API и очередей Office Radio Overlay.

Запуск:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _admin_session(client):
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["username"] = "admin"
        s["role"] = "admin"
        s["must_change_password"] = False


class ApiSmokeTest(unittest.TestCase):
    def setUp(self):
        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def test_login_page(self):
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_status_requires_login(self):
        self.assertIn(self.client.get("/api/status").status_code, (302, 401))

    def test_status_fields(self):
        _admin_session(self.client)
        data = self.client.get("/api/status").get_json()
        for key in ("state", "settings", "outputs_count", "web_player_allowed",
                    "client_is_server", "engine_alive", "hw_output_active"):
            self.assertIn(key, data)

    def test_stream_blocked_on_server(self):
        _admin_session(self.client)
        self.assertEqual(self.client.get("/stream", buffered=False).status_code, 403)

    def test_stream_allowed_remote(self):
        _admin_session(self.client)
        r = self.client.get("/stream", buffered=False,
                            environ_base={"REMOTE_ADDR": "192.168.50.10"})
        self.assertEqual(r.status_code, 200)

    def test_stop_endpoint(self):
        _admin_session(self.client)
        data = self.client.post("/api/stop").get_json()
        self.assertTrue(data.get("ok"))
        self.assertFalse(data.get("running"))


class OutputFanoutTest(unittest.TestCase):
    def test_each_subscriber_gets_copy(self):
        engine = main.engine
        a = engine.subscribe_output()
        b = engine.subscribe_output()
        try:
            for i in range(3):
                engine._push_output(bytes([i]) * 8)
            got_a = [a.get_nowait() for _ in range(3)]
            got_b = [b.get_nowait() for _ in range(3)]
            self.assertEqual(got_a, got_b)
        finally:
            engine.unsubscribe_output(a)
            engine.unsubscribe_output(b)




class ChimeImplementationTest(unittest.TestCase):
    def test_direct_chime_method_exists(self):
        """Регресс: вызов _feed_chime_direct без реализации ронял SIP (486)."""
        self.assertTrue(hasattr(main.engine, "_feed_chime_direct"))

    def test_chime_seconds_formula(self):
        data = main.engine._load_chime()
        if not data:
            self.skipTest("chime.wav отсутствует")
        import config
        expected = len(data) / 2.0 / config.CHANNELS / config.SAMPLE_RATE
        self.assertAlmostEqual(main.engine.get_chime_seconds(), expected, places=3)




class ChimeLibraryTest(unittest.TestCase):
    def test_list_and_select(self):
        main.app.config["TESTING"] = True
        with main.app.test_client() as c:
            _admin_session(c)
            items = c.get("/api/chimes").get_json()["items"]
            if not items:
                self.skipTest("Библиотека chime пуста (в CI нет assets/chimes)")
            first = items[0]["file"]
            r = c.post("/api/chimes/select", json={"file": first})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])
            main.engine._load_chime()
            self.assertGreater(main.engine.get_chime_seconds(), 0.1)




class TwoSipClientsTest(unittest.TestCase):
    def setUp(self):
        main.app.config["TESTING"] = True

    def test_status_both_kinds(self):
        with main.app.test_client() as c:
            _admin_session(c)
            for kind in ("lso", "pgs"):
                d = c.get(f"/api/sip/{kind}/status").get_json()
                self.assertEqual(d.get("kind"), kind)

    def test_engine_sip_kind(self):
        main.engine.enter_sip_mode("pgs")
        self.assertEqual(main.engine.get_sip_kind(), "pgs")
        main.engine.exit_sip_mode()
        self.assertIsNone(main.engine.get_sip_kind())


    def test_foreign_call_ended_ignored(self):
        from sip_client import SipClient

        class Eng:
            def __init__(self):
                self.kind = "lso"
                self.exited = 0
                self.cancelled = 0
            def get_sip_kind(self): return self.kind
            def exit_sip_mode(self): self.exited += 1
            def cancel_chime(self): self.cancelled += 1

        eng = Eng()
        pgs = SipClient(eng, "d", "u", "p", kind="pgs")
        pgs._handle_event({"event": "call_ended", "reason": "Normal"})
        self.assertEqual(eng.exited, 0)
        self.assertEqual(eng.cancelled, 0)

    def test_pgs_low_priority(self):
        from sip_client import SipClient

        class Eng:
            def is_sip_active(self): return True
            def is_chime_active(self): return False
            def get_sip_kind(self): return "lso"
            def preempt_for_lso(self): return True

        pgs = SipClient(Eng(), "d", "u", "p", kind="pgs")
        self.assertFalse(pgs._allow_incoming())
        lso = SipClient(Eng(), "d", "u", "p", kind="lso")
        self.assertTrue(lso._allow_incoming())



class ChannelsTest(unittest.TestCase):
    def test_channels_switch(self):
        old = main.engine.get_settings().get("fallback_sources", [])
        try:
            main.engine.set_fallback_sources([
                {"name": "Резерв А", "url": "https://example.com/a.mp3"},
                {"name": "Резерв Б", "url": "https://example.com/b.mp3"},
            ])
            items = main.engine.get_channels()
            self.assertEqual(len(items), 3)
            ok, name = main.engine.switch_channel(1)
            self.assertTrue(ok)
            self.assertEqual(name, "Резерв А")
            self.assertEqual(main.engine.get_channels()[1]["active"], True)
        finally:
            main.engine.set_fallback_sources(old)
            main.engine._manual_channel = None
            main.engine._channel_index = 0


if __name__ == "__main__":
    unittest.main()