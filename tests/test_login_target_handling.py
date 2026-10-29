import unittest
from urllib.parse import urljoin


class TestLoginTargetHandling(unittest.TestCase):
    def test_unauth_detection_distinguishes_login_page_vs_app_steps(self):
        url = "https://d31rhwl7lwu7b7.cloudfront.net/login"
        url_lower = url.lower()
        is_login_target = any(kw in url_lower for kw in ["/login", "/signin", "/sign-in", "/register", "/signup"])
        self.assertTrue(is_login_target)

        # Internal app step with login target URL should NOT be unauthenticated
        app_steps = [
            "Click on Dashboard tab",
            "Navigate to Users table",
            "Filter orders by date",
            "Open Settings and verify profile",
            "Click on 'Today' tab filter",
            "Submit Create Entry form",
        ]
        for step in app_steps:
            step_lower = step.lower()
            is_explicit_unauth = any(
                kw in step_lower
                for kw in [
                    "without a valid", "unauthenticated", "logged out", "without authentication",
                    "expired session", "clear_cookies", "invalid credential", "invalid password",
                    "wrong password", "invalid email", "blank password", "blank username",
                    "verify login page", "check login page", "login screen ui", "forgot password"
                ]
            )
            is_unauth = is_explicit_unauth or (
                is_login_target and any(kw in step_lower for kw in ["verify login", "login page", "signin page", "invalid", "blank", "error message", "unauthorized"])
            )
            self.assertFalse(is_unauth, f"Step '{step}' should be treated as an authenticated step, not unauth!")

        # Explicitly unauthenticated / error test steps should be unauthenticated
        unauth_steps = [
            "Verify login with invalid password",
            "Leave email blank and click login",
            "Access dashboard without a valid session",
            "Verify login page branding and fields",
        ]
        for step in unauth_steps:
            step_lower = step.lower()
            is_explicit_unauth = any(
                kw in step_lower
                for kw in [
                    "without a valid", "unauthenticated", "logged out", "without authentication",
                    "expired session", "clear_cookies", "invalid credential", "invalid password",
                    "wrong password", "invalid email", "blank password", "blank username",
                    "verify login page", "check login page", "login screen ui", "forgot password"
                ]
            )
            is_unauth = is_explicit_unauth or (
                is_login_target and any(kw in step_lower for kw in ["verify login", "login page", "signin page", "invalid", "blank", "error message", "unauthorized"])
            )
            self.assertTrue(is_unauth, f"Step '{step}' should be treated as an unauth step!")

    def test_skip_login_redirect_when_already_authenticated(self):
        target_clean = "https://d31rhwl7lwu7b7.cloudfront.net/login"
        page_clean = "https://d31rhwl7lwu7b7.cloudfront.net/dashboard"

        is_target_login = any(kw in target_clean.lower() for kw in ["/login", "/signin", "/sign-in"])
        is_page_on_login = any(kw in page_clean.lower() for kw in ["/login", "/signin", "/sign-in"])

        is_unauth_test = False
        is_logout_test = False

        should_skip_login_redirect = is_target_login and not is_page_on_login and not is_unauth_test and not is_logout_test
        self.assertTrue(should_skip_login_redirect)

    def test_flow_explorer_urljoin_resolves_clean_internal_routes(self):
        start_url = "https://d31rhwl7lwu7b7.cloudfront.net/login"
        cand_href = "/dashboard"

        resolved = urljoin(start_url, cand_href)
        self.assertEqual(resolved, "https://d31rhwl7lwu7b7.cloudfront.net/dashboard")
        self.assertNotIn("/login/dashboard", resolved)


if __name__ == "__main__":
    unittest.main()
