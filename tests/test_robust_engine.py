import unittest
import json
from core.robust_engine import (
    RobustDOMTopologyAnalyzer,
    RobustPlaywrightCodeSynthesizer,
    RobustDeterministicSynthesizer,
    RobustJSONRepairEngine,
)


class TestRobustEngine(unittest.TestCase):
    def setUp(self):
        self.sample_screen = {
            "name": "Box List",
            "url": "https://example.com/box-list",
            "headings": ["Inventory Box Management", "Active Warehouses"],
            "inputs": [
                {"name": "search_box", "placeholder": "Search by Box Code or Name...", "type": "search"},
                {"name": "box_title", "label": "Box Title", "type": "text"},
                {"name": "quantity", "label": "Total Quantity", "type": "number"},
            ],
            "action_buttons": [
                {"text": "All"},
                {"text": "Pending Review"},
                {"text": "Add New Box"},
                {"text": "Save Box Record"},
                {"text": "Delete Record"},
                {"text": "Export CSV"},
            ],
            "kpi_cards": [
                {"label": "Total Boxes In Stock", "value": "1,420", "trend": "+8%"},
                {"label": "Damaged Units", "value": "12", "trend": "-2%"},
            ],
            "tables_count": 1,
        }

    def test_dom_topology_analyzer_classification(self):
        """Verifies accurate semantic classification of inputs, buttons, and views."""
        topo = RobustDOMTopologyAnalyzer.analyze_screen_topology(self.sample_screen, url="https://example.com/box-list")
        self.assertEqual(topo["screen_name"], "Box List")
        self.assertTrue(topo["is_list_view"])
        self.assertFalse(topo["is_auth"])

        # Validate input partitions
        self.assertEqual(len(topo["search_inputs"]), 1)
        self.assertEqual(len(topo["form_inputs"]), 2)

        # Validate button partitions
        filter_texts = [b.get("text") for b in topo["filter_buttons"]]
        self.assertIn("All", filter_texts)

        modal_texts = [b.get("text") for b in topo["modal_triggers"]]
        self.assertIn("Add New Box", modal_texts)

        submit_texts = [b.get("text") for b in topo["submit_buttons"]]
        self.assertIn("Save Box Record", submit_texts)

    def test_deterministic_synthesizer_multi_category_output(self):
        """Verifies zero-AI deterministic generation creates comprehensive, categorized test suites."""
        cases = RobustDeterministicSynthesizer.synthesize_suite_from_topology(
            self.sample_screen,
            url="https://example.com/box-list"
        )
        self.assertGreaterEqual(len(cases), 10)

        categories = {c.get("Category") for c in cases}
        self.assertIn("Rendering & Layout", categories)
        self.assertIn("Search & Filters", categories)
        self.assertIn("Forms & Validation", categories)
        self.assertIn("Security & Resilience", categories)
        self.assertIn("Modals & Dialogs", categories)
        self.assertIn("KPIs & Metrics", categories)
        self.assertIn("Data Tables & Lists", categories)

        for case in cases:
            self.assertEqual(case["Type"], "Web")
            self.assertEqual(case["Target"], "https://example.com/box-list")
            self.assertTrue(str(case["Test_Step_Description"]).startswith("Step 1:"))
            self.assertTrue(len(case["Playwright_Code"]) > 20)
            self.assertEqual(case["Generated_By"], "⚡ Robust Engine v4")
            # Verify execution shield is present in generated code
            self.assertIn("RobustEngine v4 Execution Shield", case["Playwright_Code"])
            self.assertIn("domcontentloaded", case["Playwright_Code"])

    def test_playwright_synthesizer_shield_and_actions(self):
        """Validates that synthesized Playwright blocks include execution shield, domcontentloaded, and safe actions."""
        code = RobustPlaywrightCodeSynthesizer.synthesize_action_block(
            target_url="https://example.com/dashboard",
            steps=[
                {"action": "fill", "target": "Search", "value": "Widget-X", "description": "Type search"},
                {"action": "press", "value": "Enter", "description": "Submit search"},
                {"action": "click", "target": "Apply Filter", "description": "Apply filter"},
            ],
            assertions=["Widget-X found", "1 result"],
        )
        self.assertIn("page.wait_for_load_state('domcontentloaded')", code)
        self.assertIn("RobustEngine v4 Execution Shield", code)
        self.assertIn("fill('Widget-X')", code)
        self.assertIn("press('Enter')", code)
        self.assertIn("Widget-X found", code)

        # Dialog handling should only be present when test explicitly declares a dialog expectation (L-02 fix)
        self.assertNotIn("page.on('dialog'", code)
        code_with_dialog = RobustPlaywrightCodeSynthesizer.synthesize_action_block(
            target_url="https://example.com/dashboard",
            steps=[
                {"action": "click", "target": "Delete Item", "expect_dialog": True, "description": "Trigger dialog"},
            ],
            assertions=["Success"],
        )
        self.assertIn("page.on('dialog'", code_with_dialog)

    def test_json_repair_engine_truncated_and_reasoning(self):
        """Validates that truncated streams, reasoning wrappers, and broken markdown are recovered cleanly."""
        # 1. Truncated stream
        raw_truncated = (
            'Here is the generated suite:\n```json\n'
            '[\n'
            '  {"Type": "Web", "Target": "https://example.com", "Test_Step_Description": "Step 1: Open dashboard", "Expected_Result": "Dashboard loads"},\n'
            '  {"Type": "Web", "Target": "https://example.com", "Test_Step_Description": "Step 2: Click settings", "Expected_Result": "Settings drawer opens"'
            # intentionally missing closing brace and bracket!
        )
        repaired = RobustJSONRepairEngine.clean_and_parse(raw_truncated, default_target="https://example.com")
        self.assertGreaterEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["Test_Step_Description"], "Step 1: Open dashboard")

        # 2. Reasoning / think tag wrapper
        raw_think = (
            '<think>\nI will now analyze the DOM and create tests.\n</think>\n'
            '[{"Type": "Web", "Target": "https://example.com", "Test_Step_Description": "Step 1: Fill form", "Expected_Result": "Success"}]'
        )
        repaired_think = RobustJSONRepairEngine.clean_and_parse(raw_think, default_target="https://example.com")
        self.assertEqual(len(repaired_think), 1)
        self.assertEqual(repaired_think[0]["Test_Step_Description"], "Step 1: Fill form")

        # 3. Empty text with fallback_screen_data
        repaired_fallback = RobustJSONRepairEngine.clean_and_parse(
            "", default_target="https://example.com/box-list", fallback_screen_data=self.sample_screen
        )
        self.assertGreaterEqual(len(repaired_fallback), 10)

    def test_chained_journeys_synthesis(self):
        """Verifies stateful multi-step workflow journeys (Create -> Search -> Row Action -> Modal)."""
        journeys = RobustDeterministicSynthesizer.synthesize_chained_journeys(
            self.sample_screen,
            url="https://example.com/box-list"
        )
        self.assertGreaterEqual(len(journeys), 2)
        categories = {j.get("Category") for j in journeys}
        self.assertIn("Stateful Journeys", categories)

        # Check that journey test steps contain multiple sequential steps
        descriptions = [j.get("Test_Step_Description", "") for j in journeys]
        self.assertTrue(any("Fill inputs" in d or "Fill form" in d for d in descriptions))
        self.assertTrue(any("Filter view using search" in d or "search query" in d.lower() for d in descriptions))

        for j in journeys:
            self.assertEqual(j["Generated_By"], "⚡ Robust Engine v4")
            self.assertIn("RobustEngine v4 Execution Shield", j["Playwright_Code"])

    def test_multi_screen_e2e_journey_synthesis(self):
        """Verifies cross-screen multi-page journey chaining across multiple flow screens."""
        screen2 = {
            "name": "Box Details & Quality Audit",
            "url": "https://example.com/box-list/details/42",
            "headings": ["Box Details #42", "Quality Audit Log"],
            "inputs": [{"name": "audit_notes", "label": "Audit Notes", "type": "text"}],
            "action_buttons": [{"text": "Approve Unit"}, {"text": "Reject Unit"}],
        }
        multi_journey = RobustDeterministicSynthesizer.synthesize_multi_screen_e2e_journey([self.sample_screen, screen2])
        self.assertEqual(len(multi_journey), 1)
        mj = multi_journey[0]
        self.assertEqual(mj["Category"], "Stateful Journeys")
        self.assertIn("Navigate to 'Box List'", mj["Test_Step_Description"])
        self.assertIn("Advance to 'Box Details & Quality Audit'", mj["Test_Step_Description"])
        self.assertIn("RobustEngine v4 Execution Shield", mj["Playwright_Code"])

    def test_flow_generator_robust_v4_integration(self):
        """Verifies TestGenerator.generate_cases_from_flow operates 100% offline with strategy='robust_v4'."""
        import os
        import pandas as pd
        from core.test_generator import TestGenerator
        from core.ai_client import AIClient

        gen = TestGenerator(AIClient())
        flow_data = {
            "start_url": "https://example.com/box-list",
            "screens": [self.sample_screen],
        }
        out_csv = "reports/test_robust_v4_output.csv"
        os.makedirs("reports", exist_ok=True)
        res_path, _ = gen.generate_cases_from_flow(
            flow_data=flow_data,
            output_csv_path=out_csv,
            strategy="robust_v4",
        )
        self.assertTrue(os.path.exists(res_path))
        df = pd.read_csv(res_path)
        self.assertGreaterEqual(len(df), 10)
        self.assertTrue(all(df["Generated_By"] == "⚡ RobustEngine v4"))
        if os.path.exists(out_csv):
            os.remove(out_csv)


if __name__ == "__main__":
    unittest.main()
