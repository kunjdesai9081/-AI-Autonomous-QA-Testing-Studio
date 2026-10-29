import io
import json
import unittest
from unittest.mock import MagicMock

from core.security_guardrails import (
    ActionSafetyCategory,
    classify_action_intent,
    validate_target_url_security,
)
from core.execution_control import (
    ExecutionControlHandler,
    get_control_auth_token,
)


class TestSecurityGuardrails(unittest.TestCase):
    """Verifies SSRF prevention, URL validation, and action intent classification."""

    def test_empty_and_whitespace_urls(self):
        ok, msg = validate_target_url_security("")
        self.assertFalse(ok)
        self.assertIn("cannot be empty", msg)

        ok, msg = validate_target_url_security("   ")
        self.assertFalse(ok)
        self.assertIn("cannot be empty", msg)

    def test_blocked_cloud_metadata_endpoints(self):
        # AWS / GCP link-local metadata exfiltration block
        ok, msg = validate_target_url_security("http://169.254.169.254/latest/meta-data")
        self.assertFalse(ok)
        self.assertIn("cloud metadata service", msg)

        # GCP internal metadata service
        ok, msg = validate_target_url_security("http://metadata.google.internal/computeMetadata/v1/")
        self.assertFalse(ok)
        self.assertIn("cloud metadata service", msg)

        # Alibaba cloud metadata
        ok, msg = validate_target_url_security("http://100.100.100.200/latest/meta-data")
        self.assertFalse(ok)
        self.assertIn("cloud metadata service", msg)

    def test_blocked_protocols(self):
        # File protocol injection
        ok, msg = validate_target_url_security("file:///etc/passwd")
        self.assertFalse(ok)
        self.assertIn("prohibited", msg)

        # FTP protocol
        ok, msg = validate_target_url_security("ftp://ftp.example.com/data")
        self.assertFalse(ok)
        self.assertIn("prohibited", msg)

        # Gopher protocol
        ok, msg = validate_target_url_security("gopher://example.com/test")
        self.assertFalse(ok)
        self.assertIn("prohibited", msg)

    def test_dangerous_infrastructure_ports(self):
        # Port 22 (SSH)
        ok, msg = validate_target_url_security("http://127.0.0.1:22")
        self.assertFalse(ok)
        self.assertIn("infrastructure port 22", msg)

        # Port 3306 (MySQL)
        ok, msg = validate_target_url_security("http://localhost:3306")
        self.assertFalse(ok)
        self.assertIn("infrastructure port 3306", msg)

        # Port 6379 (Redis)
        ok, msg = validate_target_url_security("http://example.com:6379")
        self.assertFalse(ok)
        self.assertIn("infrastructure port 6379", msg)

    def test_valid_http_and_https_targets(self):
        ok, msg = validate_target_url_security("https://google.com")
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        ok, msg = validate_target_url_security("http://localhost:8080/dashboard", allow_local_dev=True)
        self.assertTrue(ok)

        ok, msg = validate_target_url_security("http://127.0.0.1:3000", allow_local_dev=True)
        self.assertTrue(ok)

    def test_android_native_targets(self):
        ok, msg = validate_target_url_security("android://com.example.qaapp")
        self.assertTrue(ok)

        ok, msg = validate_target_url_security("com.android.settings")
        self.assertTrue(ok)

    def test_action_intent_classification(self):
        # Destructive actions
        self.assertEqual(classify_action_intent("Delete Account"), ActionSafetyCategory.DESTRUCTIVE)
        self.assertEqual(classify_action_intent("Purge All Cache"), ActionSafetyCategory.DESTRUCTIVE)
        self.assertEqual(classify_action_intent("Reset Database"), ActionSafetyCategory.DESTRUCTIVE)
        self.assertEqual(classify_action_intent("Pay Now"), ActionSafetyCategory.DESTRUCTIVE)

        # State change actions
        self.assertEqual(classify_action_intent("Submit Application"), ActionSafetyCategory.STATE_CHANGE)
        self.assertEqual(classify_action_intent("Save Profile"), ActionSafetyCategory.STATE_CHANGE)
        self.assertEqual(classify_action_intent("Update Password"), ActionSafetyCategory.STATE_CHANGE)
        self.assertEqual(classify_action_intent("Import Leads"), ActionSafetyCategory.STATE_CHANGE)

        # Safe interaction actions
        self.assertEqual(classify_action_intent("Close Modal"), ActionSafetyCategory.SAFE_INTERACTION)
        self.assertEqual(classify_action_intent("Cancel"), ActionSafetyCategory.SAFE_INTERACTION)
        self.assertEqual(classify_action_intent("Expand Accordion"), ActionSafetyCategory.SAFE_INTERACTION)

        # Read only actions
        self.assertEqual(classify_action_intent("View Analytics"), ActionSafetyCategory.READ_ONLY)
        self.assertEqual(classify_action_intent("Dashboard Overview"), ActionSafetyCategory.READ_ONLY)
        self.assertEqual(classify_action_intent("Search Customers"), ActionSafetyCategory.READ_ONLY)


class TestExecutionControlSecurity(unittest.TestCase):
    """Verifies that ExecutionControlHandler enforces authentication tokens and strict CORS."""

    def test_control_auth_token_generation(self):
        token = get_control_auth_token()
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 16)

    def test_control_handler_unauthorized_post(self):
        # Simulate request handler with dummy socket/rfile/wfile
        handler = ExecutionControlHandler.__new__(ExecutionControlHandler)
        handler.headers = {"X-Control-Token": "invalid_token_123"}
        handler.path = "/pause"
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()

        handler.send_response.assert_called_with(401)
        response_body = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("Unauthorized control signal", response_body)

    def test_control_handler_authorized_post(self):
        valid_token = get_control_auth_token()
        handler = ExecutionControlHandler.__new__(ExecutionControlHandler)
        handler.headers = {"X-Control-Token": valid_token}
        handler.path = "/pause"
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()

        handler.send_response.assert_called_with(200)
        response_body = handler.wfile.getvalue().decode("utf-8")
        parsed = json.loads(response_body)
        self.assertTrue(parsed.get("ok"))
        self.assertEqual(parsed.get("signal"), "PAUSE")


if __name__ == "__main__":
    unittest.main()
