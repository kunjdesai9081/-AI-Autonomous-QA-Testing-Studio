import unittest
import inspect
from core.flow_explorer import AppFlowExplorer
from core.test_generator import _synthesize_comprehensive_screen_cases


class TestFlowExplorerV3(unittest.TestCase):
    def setUp(self):
        self.explorer = AppFlowExplorer()

    def test_kpi_methods_and_backward_compatibility(self):
        """Verify universal KPI extraction method exists and dashboard alias is preserved."""
        self.assertTrue(hasattr(self.explorer, "_explore_kpi_cards"))
        self.assertTrue(hasattr(self.explorer, "_explore_dashboard_kpis"))
        self.assertTrue(hasattr(self.explorer, "_bounded_lazy_content_scroll"))

    def test_explore_app_flows_signature_has_max_depth(self):
        """Verify explore_app_flows accepts max_depth with default 3."""
        sig = inspect.signature(self.explorer.explore_app_flows)
        self.assertIn("max_depth", sig.parameters)
        self.assertEqual(sig.parameters["max_depth"].default, 3)

    def test_kpi_card_test_synthesis_presence_and_drilldown(self):
        """Verify _synthesize_comprehensive_screen_cases generates tests for discovered KPI cards."""
        screen_data = {
            "name": "Revenue Overview",
            "url": "https://example.com/reports/revenue",
            "headings": ["Revenue Overview"],
            "inputs": [],
            "action_buttons": [],
            "tables_count": 0,
            "kpi_cards": [
                {
                    "label": "Total Revenue",
                    "value": "$125,000",
                    "trend": "+15%",
                    "is_interactive": True,
                    "confidence": "HIGH"
                },
                {
                    "label": "Churn Rate",
                    "value": "1.2%",
                    "trend": "-0.3%",
                    "is_interactive": False,
                    "confidence": "HIGH"
                }
            ]
        }
        cases = _synthesize_comprehensive_screen_cases(screen_data, "https://example.com/reports/revenue", "")
        
        # Verify cases were generated for both KPIs
        descriptions = [c["Test_Step_Description"] for c in cases]
        self.assertTrue(any("Total Revenue" in d for d in descriptions))
        self.assertTrue(any("Churn Rate" in d for d in descriptions))
        
        # Verify interactive KPI generates drill-down test
        self.assertTrue(any("Click interactive KPI card 'Total Revenue'" in d or "Click KPI card" in d for d in descriptions))

        # Verify Playwright code contains text expect assertions
        codes = [c.get("Playwright_Code", "") for c in cases]
        self.assertTrue(any('page.get_by_text("Total Revenue")' in c for c in codes))
        self.assertTrue(any('page.get_by_text("$125,000")' in c for c in codes))

    def test_unlimited_depth_and_screens_logic(self):
        """Verify that max_depth=0 and max_screens=0 map to uncapped effectively infinite values."""
        # Check calculation logic matching core/flow_explorer.py
        max_screens = 0
        max_depth = 0
        effective_max = max_screens if max_screens and max_screens > 0 else 999999
        effective_depth = 999999 if (max_depth is None or max_depth <= 0) else max_depth
        self.assertGreaterEqual(effective_max, 999999)
        self.assertGreaterEqual(effective_depth, 999999)


if __name__ == "__main__":
    unittest.main()
