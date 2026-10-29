import json
import os
import tempfile
import unittest
from core.session_capturer import (
    get_auth_session_summary,
    save_auth_target_url,
    clear_auth_session,
)


class TestSessionTargetPersistence(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.auth_file = os.path.join(self.test_dir.name, "auth.json")
        self.meta_file = os.path.join(self.test_dir.name, "auth_meta.json")

    def tearDown(self):
        self.test_dir.cleanup()

    def test_empty_or_missing_session(self):
        summary = get_auth_session_summary(self.auth_file)
        self.assertFalse(summary.get("active"))
        self.assertEqual(summary.get("target_url"), "")

    def test_save_target_url_creates_meta_and_updates_auth(self):
        # Create a valid auth.json with cookies
        initial_auth = {
            "cookies": [{"name": "sid", "value": "123", "domain": "example.com"}],
            "origins": [],
        }
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump(initial_auth, f)

        # Save target URL
        url = "https://example.com/login"
        saved = save_auth_target_url(url, auth_path=self.auth_file)
        self.assertTrue(saved)

        # Verify target URL is inside auth.json
        with open(self.auth_file, "r", encoding="utf-8") as f:
            updated_auth = json.load(f)
        self.assertEqual(updated_auth.get("target_url"), url)

        # Verify target URL is inside companion meta file
        self.assertTrue(os.path.exists(self.meta_file))
        with open(self.meta_file, "r", encoding="utf-8") as mf:
            meta_data = json.load(mf)
        self.assertEqual(meta_data.get("target_url"), url)

        # Verify summary extracts target_url correctly
        summary = get_auth_session_summary(self.auth_file)
        self.assertTrue(summary.get("active"))
        self.assertEqual(summary.get("target_url"), url)

    def test_clear_auth_session_removes_both_auth_and_target_meta(self):
        # Create auth file and companion meta
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "token", "value": "abc"}]}, f)
        save_auth_target_url("https://dashboard.example.com", auth_path=self.auth_file)

        self.assertTrue(os.path.exists(self.auth_file))
        self.assertTrue(os.path.exists(self.meta_file))

        # Clear session
        cleared = clear_auth_session(auth_path=self.auth_file)
        self.assertTrue(cleared)

        # Ensure both files are removed
        self.assertFalse(os.path.exists(self.auth_file))
        self.assertFalse(os.path.exists(self.meta_file))

        # Ensure summary reflects inactive with empty target
        summary = get_auth_session_summary(self.auth_file)
        self.assertFalse(summary.get("active"))
        self.assertEqual(summary.get("target_url"), "")

    def test_fallback_target_url_from_origins_or_cookies(self):
        # Case 1: Origin present
        auth_with_origin = {
            "cookies": [{"name": "sess", "value": "xyz"}],
            "origins": [{"origin": "https://portal.test.io", "localStorage": []}],
        }
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump(auth_with_origin, f)

        summary = get_auth_session_summary(self.auth_file)
        self.assertTrue(summary.get("active"))
        self.assertEqual(summary.get("target_url"), "https://portal.test.io")

        # Case 2: Origin empty, domain in cookie
        auth_with_cookie_only = {
            "cookies": [{"name": "sess", "value": "xyz", "domain": "app.mysite.org"}],
            "origins": [],
        }
        with open(self.auth_file, "w", encoding="utf-8") as f:
            json.dump(auth_with_cookie_only, f)

        summary2 = get_auth_session_summary(self.auth_file)
        self.assertTrue(summary2.get("active"))
        self.assertEqual(summary2.get("target_url"), "https://app.mysite.org")

    def test_capture_interactive_session_invalid_url_rejection(self):
        from core.session_capturer import capture_interactive_session
        res = capture_interactive_session("invalid-url-string", output_auth_path=self.auth_file)
        self.assertEqual(res.get("status"), "error")
        self.assertIn("valid HTTP/HTTPS", res.get("message", ""))

    def test_import_session_from_browser_profile_structure(self):
        from unittest.mock import patch, MagicMock
        from core.session_capturer import import_session_from_browser

        # Validates profile detection and candidate profile sorting without opening real browsers
        with patch("os.path.exists", return_value=True), \
             patch("os.listdir", return_value=["Profile 1", "Default"]), \
             patch("os.path.isdir", return_value=True), \
             patch("os.path.getmtime", return_value=123456789):
            res = import_session_from_browser("https://example.com/login", output_auth_path=self.auth_file)
            self.assertIn("status", res)


if __name__ == "__main__":
    unittest.main()
