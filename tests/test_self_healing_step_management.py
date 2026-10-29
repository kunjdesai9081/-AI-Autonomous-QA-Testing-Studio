import unittest
import pandas as pd
from unittest.mock import MagicMock
from core.self_healing import SelfHealingEngine, StepSkippedException


class TestSelfHealingStepManagement(unittest.TestCase):
    """
    Tests that Self-Healing actively manages the steps of test cases
    and gracefully marks test cases as SKIPPED rather than FAILED
    when execution or healing is not possible on the active screen.
    """

    def setUp(self):
        self.engine = SelfHealingEngine()

    def test_decompose_steps_numbered_and_newlines(self):
        step_text = (
            "1. Navigate to Settings\n"
            "2. Click on Profile\n"
            "3. Update display name\n"
            "4. Click Save Changes"
        )
        steps = self.engine.decompose_steps(step_text)
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0], "Navigate to Settings")
        self.assertEqual(steps[1], "Click on Profile")
        self.assertEqual(steps[2], "Update display name")
        self.assertEqual(steps[3], "Click Save Changes")

    def test_decompose_steps_arrow_flow(self):
        step_text = "Dashboard -> User Management -> Add New User"
        steps = self.engine.decompose_steps(step_text)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], "Dashboard")
        self.assertEqual(steps[1], "User Management")
        self.assertEqual(steps[2], "Add New User")

    def test_decompose_steps_conjunctions(self):
        step_text = "Enter email and password, then click Login button"
        steps = self.engine.decompose_steps(step_text)
        self.assertGreaterEqual(len(steps), 2)
        self.assertIn("Enter email and password", steps[0])
        self.assertIn("Login button", steps[1])

    def test_decompose_steps_strips_tester_banner(self):
        step_text = "QA / authorized tester: Open School ERP dev environment, navigate to Login Page"
        steps = self.engine.decompose_steps(step_text)
        self.assertNotIn("QA / authorized tester:", steps[0])
        self.assertIn("Open School ERP dev environment", steps[0])

    def test_semantic_healing_generates_managed_steps(self):
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        step_text = "Enter a valid Email and Password, then click 'Submit'"
        code = self.engine._apply_live_dom_semantic_healing(
            mock_page, step_text, "Dashboard loads", test_data={"email": "tester@school.edu", "password": "SecretPassword123"}
        )
        self.assertIn("Sub-Step #1: Fill email & password", code)
        self.assertIn("tester@school.edu", code)
        self.assertIn("SecretPassword123", code)
        self.assertIn("Try interactive locator for 'Submit'", code)

    def test_self_healing_exhaustion_signals_not_possible(self):
        mock_page = MagicMock()
        mock_page.is_closed.return_value = False
        mock_page.url = "https://school.erp/login"

        # Simulate execution that cannot be resolved
        success, code, log_msg = self.engine.heal_and_execute(
            page=mock_page,
            failed_code="page.locator('button#nonexistent-button').click()",
            error=Exception("Timeout 30000ms exceeded waiting for locator('button#nonexistent-button')"),
            step_desc="Click on 'Nonexistent Functionality'",
            expected_result="Feature opens",
            test_data={},
            exec_scope={}
        )
        self.assertFalse(success)
        self.assertIn("[NOT POSSIBLE]", log_msg)

    def test_step_skipped_exception_type(self):
        err = StepSkippedException("[NOT APPLICABLE] Target element not found on active screen")
        self.assertIsInstance(err, Exception)
        self.assertIn("[NOT APPLICABLE]", str(err))


if __name__ == "__main__":
    unittest.main()
