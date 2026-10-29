import json
import os
import unittest
import urllib.request
from core.execution_control import (
    CONTROL_SIGNAL_FILE,
    write_control_signal,
    read_control_signal,
    read_and_clear_control_signal,
    ensure_control_server,
    render_live_control_bar_html,
    get_control_auth_token,
)


class TestExecutionControl(unittest.TestCase):
    def setUp(self):
        if os.path.exists(CONTROL_SIGNAL_FILE):
            try:
                os.remove(CONTROL_SIGNAL_FILE)
            except Exception:
                pass

    def tearDown(self):
        if os.path.exists(CONTROL_SIGNAL_FILE):
            try:
                os.remove(CONTROL_SIGNAL_FILE)
            except Exception:
                pass

    def test_write_and_read_control_signals(self):
        """Validates that pause, resume, and stop signals are accurately written and read."""
        self.assertIsNone(read_control_signal())

        write_control_signal("PAUSE")
        self.assertEqual(read_control_signal(), "PAUSE")

        write_control_signal("resume")
        self.assertEqual(read_control_signal(), "RESUME")

        write_control_signal("  STOP  ")
        self.assertEqual(read_control_signal(), "STOP")

    def test_read_and_clear_control_signal(self):
        """Ensures read_and_clear_control_signal atomically retrieves and deletes the signal."""
        write_control_signal("PAUSE")
        sig = read_and_clear_control_signal()
        self.assertEqual(sig, "PAUSE")

        # After clearing, file must not exist and subsequent call returns None
        self.assertIsNone(read_control_signal())
        self.assertIsNone(read_and_clear_control_signal())

    def test_render_live_control_bar_html(self):
        """Verifies HTML generation includes control buttons, badges, and the assigned port."""
        html = render_live_control_bar_html(port=8503, total_tests=25, step_pause=True)
        self.assertIn("8503", html)
        self.assertIn("25 Tests", html)
        self.assertIn("Step-Pause", html)
        self.assertIn("Pause After Step", html)
        self.assertIn("Stop Execution", html)

    def test_ensure_control_server_http_lifecycle(self):
        """Tests that the background HTTP daemon accepts control signals over REST endpoints with auth tokens."""
        port = ensure_control_server(start_port=8510)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 8500)

        base_url = f"http://127.0.0.1:{port}"
        token = get_control_auth_token()

        # 1. GET status
        req = urllib.request.Request(f"{base_url}/status")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))

        # 2. Unauthorized POST should return HTTP 401
        req_unauth = urllib.request.Request(f"{base_url}/pause", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_unauth, timeout=3)
        self.assertEqual(ctx.exception.code, 401)

        # 3. POST /pause with valid X-Control-Token header
        req_pause = urllib.request.Request(
            f"{base_url}/pause",
            data=b"",
            headers={"X-Control-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req_pause, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("signal"), "PAUSE")
        self.assertEqual(read_control_signal(), "PAUSE")

        # 4. POST /stop with valid token in query param
        req_stop = urllib.request.Request(
            f"{base_url}/stop?token={token}",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req_stop, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("signal"), "STOP")
        self.assertEqual(read_control_signal(), "STOP")


if __name__ == "__main__":
    unittest.main()
