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


if __name__ == "__main__":
    unittest.main()
