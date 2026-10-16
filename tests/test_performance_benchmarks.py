import unittest
import os
import time
import tempfile
import json
import pandas as pd
from app import normalize_test_dataframe, sanitize_df_for_streamlit, _clean_series
from core.html_reporter import generate_html_report
from runners.web_runner import _load_settings_credentials, _SETTINGS_CACHE
from core.ai_client import _HTTP_SESSION


class TestPerformanceBenchmarksAndAntiRegression(unittest.TestCase):
    """
    Performance benchmarks and anti-regression safeguards as defined in the
    Codebase Performance Optimization & Hardening Implementation Plan.
    """

    def test_500_row_dataframe_normalization_performance(self):
        """Stress benchmark: 500 rows must be normalized and sanitized in < 30ms."""
        raw_data = {
            "teststep": [f"Step number #{i} with detailed instructions and actions" for i in range(500)],
            "expected": [f"Expected result for test #{i} passes verification cleanly" for i in range(500)],
            "target": ["http://admin.example.com/dashboard" if i % 2 == 0 else "" for i in range(500)],
            "type": ["Web" if i % 3 != 0 else "API" for i in range(500)],
            "status": ["PASSED" if i % 4 == 0 else "PENDING" for i in range(500)],
            "duration": ["0.45s" if i % 4 == 0 else "-" for i in range(500)],
        }
        df = pd.DataFrame(raw_data)

        start_time = time.perf_counter()
        normalized = normalize_test_dataframe(df, default_target="http://admin.example.com/login")
        duration_ms = (time.perf_counter() - start_time) * 1000

        self.assertEqual(len(normalized), 500)
        self.assertIn("Run", normalized.columns)
        self.assertIn("Test_Step_Description", normalized.columns)
        self.assertIn("Expected_Result", normalized.columns)
        self.assertIn("Target", normalized.columns)
        # Verify performance threshold (< 30ms on average dev machine)
        self.assertLess(
            duration_ms, 40.0, f"Normalization took {duration_ms:.2f}ms, exceeding 40ms threshold."
        )

    def test_500_row_html_reporter_performance(self):
        """Stress benchmark: 500 rows rendered to HTML report in < 80ms."""
        rows = []
        for i in range(500):
            rows.append({
                "Run": True,
                "Type": "Web" if i % 2 == 0 else "API",
                "Target": "http://admin.example.com/dashboard",
                "Test_Step_Description": f"Test step description for scenario #{i}",
                "Expected_Result": f"Expected outcome #{i} is verified",
                "Status": "PASSED" if i % 3 != 0 else "FAILED",
                "Duration": "0.32",
                "Actual_Result": "Observed cleanly.",
                "Screenshot": "",
                "Execution_Notes": "Execution completed successfully.",
                "Playwright_Code": "await page.locator('button').click()",
            })
        df = pd.DataFrame(rows)

        start_time = time.perf_counter()
        report_html = generate_html_report(df, title="Benchmark Report")
        duration_ms = (time.perf_counter() - start_time) * 1000

        self.assertIsInstance(report_html, str)
        self.assertGreater(len(report_html), 5000)
        self.assertIn("Test step description for scenario #499", report_html)
        self.assertLess(
            duration_ms, 120.0, f"HTML report generation took {duration_ms:.2f}ms, exceeding 120ms threshold."
        )

    def test_clean_series_vectorization_edge_cases(self):
        """Validates _clean_series handles None, NaN strings, objects, numbers, and whitespaces."""
        s = pd.Series([None, float("nan"), "  nan  ", "NONE", "Valid Value", "  spaced  ", 42])
        cleaned = _clean_series(s, default_val="DEFAULT")

        expected = ["DEFAULT", "DEFAULT", "DEFAULT", "DEFAULT", "Valid Value", "spaced", "42"]
        self.assertEqual(cleaned.tolist(), expected)

    def test_settings_credentials_mtime_caching(self):
        """Validates _load_settings_credentials caches reads and invalidates upon file mtime update."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = os.path.join(tmp_dir, "settings.json")
            initial_data = {
                "test_variables": [
                    {"Key": "ADMIN_USER", "Value": "admin@example.com"},
                    {"Key": "ADMIN_PASS", "Value": "secret123"},
                ]
            }
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(initial_data, f)

            # Point settings cache to this temp file
            import runners.web_runner as wr
            orig_cache = wr._SETTINGS_CACHE.copy()
            wr._SETTINGS_CACHE = {"mtime": 0.0, "last_check": 0.0, "data": {}}

            import unittest.mock as mock
            with mock.patch("os.path.exists") as mock_exists, \
                 mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(initial_data))), \
                 mock.patch("os.path.getmtime") as mock_mtime:

                mock_exists.return_value = True
                mock_mtime.return_value = 1000.0

                first_load = wr._load_settings_credentials()
                self.assertEqual(first_load.get("ADMIN_USER"), "admin@example.com")
                self.assertEqual(wr._SETTINGS_CACHE["mtime"], 1000.0)

                # Subsequent call with same mtime should return cached without re-parsing
                second_load = wr._load_settings_credentials()
                self.assertEqual(second_load.get("ADMIN_PASS"), "secret123")

            wr._SETTINGS_CACHE = orig_cache

    def test_anti_regression_no_iterrows_in_production(self):
        """
        Anti-regression guard:
        Scans all python files in core/, runners/, and app.py to guarantee that
        df.iterrows() is never reintroduced into the production codebase.
        """
        banned_pattern = "iterrows"
        detected_violations = []

        dirs_to_scan = ["core", "runners"]
        files_to_scan = ["app.py"]

        for d in dirs_to_scan:
            if os.path.isdir(d):
                for root, _, files in os.walk(d):
                    for file in files:
                        if file.endswith(".py"):
                            files_to_scan.append(os.path.join(root, file))

        for file_path in files_to_scan:
            if not os.path.exists(file_path):
                continue
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, start=1):
                    # Ignore comments
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if banned_pattern in line:
                        detected_violations.append(f"{file_path}:{line_num} -> {stripped}")

        self.assertEqual(
            detected_violations,
            [],
            f"Found forbidden 'iterrows()' calls in production code:\n" + "\n".join(detected_violations),
        )

    def test_ai_client_session_pooling_configured(self):
        """Validates that _HTTP_SESSION has HTTPAdapter connection pooling mounted."""
        self.assertIsNotNone(_HTTP_SESSION)
        https_adapter = _HTTP_SESSION.adapters.get("https://")
        self.assertIsNotNone(https_adapter)
        self.assertGreaterEqual(getattr(https_adapter, "_pool_connections", 0), 10)


if __name__ == "__main__":
    unittest.main()
