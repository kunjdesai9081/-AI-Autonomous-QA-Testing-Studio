import unittest
import pandas as pd
from unittest.mock import MagicMock

from runners.web_runner import (
    _normalize_url as _web_normalize_url,
    _substitute_variables as _web_substitute_variables,
    WebRunner,
)
from runners.api_runner import (
    _normalize_url as _api_normalize_url,
    _substitute_variables as _api_substitute_variables,
    _synthesize_deterministic_api_script,
    APIRunner,
)


class TestInputHandlingAndValidation(unittest.TestCase):
    def test_url_normalization_schemes(self):
        """Verifies missing schemes are correctly assigned based on target host."""
        # Localhost and local loopback must default to http://
        self.assertEqual(_web_normalize_url("localhost:3000"), "http://localhost:3000")
        self.assertEqual(_web_normalize_url("127.0.0.1:8000/api"), "http://127.0.0.1:8000/api")
        self.assertEqual(_api_normalize_url("localhost:5000/v1/auth"), "http://localhost:5000/v1/auth")

        # Remote domain names without scheme must default to https://
        self.assertEqual(_web_normalize_url("example.com/dashboard"), "https://example.com/dashboard")
        self.assertEqual(_api_normalize_url("api.github.com/users"), "https://api.github.com/users")

        # Already valid URLs must be preserved untouched
        self.assertEqual(_web_normalize_url("https://secure.site.com"), "https://secure.site.com")
        self.assertEqual(_api_normalize_url("http://legacy.site.org"), "http://legacy.site.org")

        # Falsy or invalid values must return empty string without errors
        self.assertEqual(_web_normalize_url(None), "")
        self.assertEqual(_web_normalize_url(""), "")
        self.assertEqual(_web_normalize_url("nan"), "")
        self.assertEqual(_web_normalize_url("none"), "")

    def test_variable_substitution_single_and_double_braces(self):
        """Validates {{KEY}} and {KEY} variable replacement in Web and API runners."""
        test_vars = {"Email": "tester@example.com", "User_Id": "42", "AuthToken": "token-xyz-123"}

        text_template = "Login with {{Email}} and user id {user_id} using token {{authtoken}}"
        web_res = _web_substitute_variables(text_template, test_vars)
        self.assertEqual(web_res, "Login with tester@example.com and user id 42 using token token-xyz-123")

        api_res = _api_substitute_variables(text_template, test_vars)
        self.assertEqual(api_res, "Login with tester@example.com and user id 42 using token token-xyz-123")

    def test_variable_substitution_edge_cases(self):
        """Verifies substitution handles None, empty strings, and missing keys cleanly."""
        self.assertEqual(_web_substitute_variables("", {}), "")
        self.assertEqual(_web_substitute_variables(None, None), "")
        # Non-existent variable placeholders remain intact rather than crashing
        self.assertEqual(_web_substitute_variables("Hello {{NonExistent}}", {"Other": "1"}), "Hello {{NonExistent}}")

    def test_api_deterministic_script_payload_resilience(self):
        """Validates that _synthesize_deterministic_api_script handles valid and single-quoted JSON bodies."""
        # 1. Standard JSON body
        script1 = _synthesize_deterministic_api_script(
            url="http://localhost:8000/api/users",
            test_step="POST body: {\"username\": \"alice\", \"role\": \"admin\"}",
            expected_result="HTTP 201 Created",
            test_data={},
        )
        self.assertIn("requests.request(\"POST\"", script1)
        self.assertIn("ast.literal_eval", script1)

        # 2. Python single-quoted dict syntax body
        script2 = _synthesize_deterministic_api_script(
            url="http://localhost:8000/api/items",
            test_step="POST body: {'item_id': 101, 'active': True}",
            expected_result="HTTP 200 OK",
            test_data={},
        )
        self.assertIn("requests.request(\"POST\"", script2)
        # Executing the synthesized script with a mock request ensures the payload parser works at runtime
        mock_req = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"ok": true}'
        mock_req.request.return_value = mock_resp
        exec_scope = {"requests": mock_req, "json": __import__("json"), "time": __import__("time")}
        exec(script2, globals(), exec_scope)
        self.assertEqual(exec_scope["payload"], {"item_id": 101, "active": True})

    def test_runner_variable_substitution_integration(self):
        """Validates WebRunner and APIRunner perform URL and step substitutions upon initialization."""
        test_vars = {"Port": "9090", "Entity": "orders", "Key": "secret"}

        api_runner = APIRunner(
            url="localhost:{{Port}}/api/{Entity}",
            test_data=test_vars,
            test_step="Send GET request with key {{Key}}",
        )
        self.assertEqual(api_runner.url, "http://localhost:9090/api/orders")
        self.assertEqual(api_runner.test_step, "Send GET request with key secret")

        web_runner = WebRunner(
            url="localhost:{{Port}}/dashboard",
            test_data=test_vars,
            test_step="Open {Entity} dashboard",
        )
        self.assertEqual(web_runner.url, "http://localhost:9090/dashboard")
        self.assertEqual(web_runner.test_step, "Open orders dashboard")


if __name__ == "__main__":
    unittest.main()
