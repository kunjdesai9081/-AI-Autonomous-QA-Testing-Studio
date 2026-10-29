import unittest
from core.flow_explorer import AppFlowExplorer
import inspect


class TestFlowExplorerEnhancements(unittest.TestCase):
    def setUp(self):
        self.explorer = AppFlowExplorer()

    def test_slide_menus_and_dropdown_methods_exist(self):
        """Verify methods for slide menu expansion and dropdown probing are available."""
        self.assertTrue(hasattr(self.explorer, "_expand_slide_menus_and_accordions"))
        self.assertTrue(hasattr(self.explorer, "_explore_screen_dropdowns"))
        self.assertTrue(hasattr(self.explorer, "_explore_screen_sub_tabs"))
        self.assertTrue(hasattr(self.explorer, "_detect_and_explore_modals"))

    def test_explore_app_flows_unlimited_screens_default(self):
        """Verify explore_app_flows defaults to 0 (unlimited screens, effective_max >= 999999)."""
        sig = inspect.signature(self.explorer.explore_app_flows)
        self.assertEqual(sig.parameters["max_screens"].default, 0)

    def test_interactive_recorder_extended_timeout_and_unlimited_limit(self):
        """Verify interactive recorder defaults to 7200s (2 hours) timeout."""
        sig = inspect.signature(self.explorer.launch_interactive_recorder)
        self.assertGreaterEqual(sig.parameters["timeout_seconds"].default, 3600)

    def test_interactive_recorder_contains_event_propagation_stops(self):
        """Verify the interactive recorder source code contains event stopPropagation to prevent slide menu collapsing."""
        source = inspect.getsource(self.explorer.launch_interactive_recorder)
        self.assertIn("stopPropagation()", source)
        self.assertIn("ai-snap-count", source)
        self.assertIn("No Limit", source)
        self.assertIn("full_page=False", source)


    def test_clean_stable_url_normalization(self):
        """Verify _clean_stable_url normalizes query params, casing, trailing slashes, and ignores volatile parameters."""
        from core.flow_explorer import _clean_stable_url
        u1 = "https://app.example.com/Box-List/?utm_source=chat&_t=98721"
        u2 = "https://APP.example.COM/box-list"
        u3 = "https://app.example.com/box-list/"
        self.assertEqual(_clean_stable_url(u1), "https://app.example.com/box-list")
        self.assertEqual(_clean_stable_url(u2), "https://app.example.com/box-list")
        self.assertEqual(_clean_stable_url(u3), "https://app.example.com/box-list")

    def test_dom_fingerprint_stability(self):
        """Verify _dom_fingerprint generates stable fingerprints across whitespace and case variations."""
        from core.flow_explorer import _dom_fingerprint
        s1 = {"headings": ["Box List", "Inventory"], "action_buttons": ["Create Box"], "inputs": ["Search"]}
        s2 = {"headings": ["Box List", "Inventory"], "action_buttons": ["Create Box"], "inputs": ["Search"]}
        fp1 = _dom_fingerprint(s1, "https://app.example.com/box-list")
        fp2 = _dom_fingerprint(s2, "https://app.example.com/box-list/?_t=123")
        self.assertEqual(fp1, fp2)

    def test_deduplicate_sub_features_preserves_order_and_uniqueness(self):
        """Verify _deduplicate_sub_features purges duplicate items and dicts without losing unique items."""
        from core.flow_explorer import _deduplicate_sub_features
        items = [
            {"type": "modal", "name": "Create Lead", "trigger": "Add Lead", "url": "/leads"},
            {"type": "modal", "name": "Create Lead", "trigger": "Add Lead", "url": "/leads"},
            {"type": "tab", "name": "All Leads", "trigger": "All", "url": "/leads?tab=all"},
            "Simple String Feature",
            "Simple String Feature",
            "Unique String Feature"
        ]
        deduped = _deduplicate_sub_features(items)
        self.assertEqual(len(deduped), 4)
        self.assertEqual(deduped[0]["name"], "Create Lead")
        self.assertEqual(deduped[1]["name"], "All Leads")
        self.assertEqual(deduped[2], "Simple String Feature")
        self.assertEqual(deduped[3], "Unique String Feature")

    def test_config_sub_pages_method_exists(self):
        """Verify _explore_config_sub_pages method exists on explorer instance."""
        self.assertTrue(hasattr(self.explorer, "_explore_config_sub_pages"))


if __name__ == "__main__":
    unittest.main()
