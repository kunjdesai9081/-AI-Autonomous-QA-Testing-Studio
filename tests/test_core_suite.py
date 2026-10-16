import unittest
import os
import pandas as pd
import pyarrow as pa
from core.replay_cache import ReplayCache
from core.self_healing import SelfHealingEngine
from core.e2e_exporter import TesterArmyExporter
from app import (
    sanitize_df_for_streamlit,
    normalize_test_dataframe,
    clear_entire_test_suite,
    delete_selected_test_cases,
    delete_single_test_case,
    detect_row_generation_source,
    compute_origin_counts,
    apply_origin_selection_to_suite,
)

class TestCoreModules(unittest.TestCase):
    def test_replay_cache(self):
        test_cache = ".test_cache.json"
        if os.path.exists(test_cache):
            os.remove(test_cache)
        rc = ReplayCache(cache_file=test_cache)
        rc.save_cached_action("http://example.com/test", "Click Save", "page.locator('button').click()", 15.0)
        cached = rc.get_cached_action("http://example.com/test", "Click Save")
        self.assertEqual(cached, "page.locator('button').click()")
        stats = rc.get_stats()
        self.assertGreaterEqual(stats["total_cached_actions"], 1)
        rc.invalidate("http://example.com/test", "Click Save")
        self.assertIsNone(rc.get_cached_action("http://example.com/test", "Click Save"))
        if os.path.exists(test_cache):
            os.remove(test_cache)

    def test_self_healing_heuristics(self):
        she = SelfHealingEngine()
        broken = "page.locator('button').click()"
        err = "Strict mode violation: resolved to 2 elements"
        healed = she._apply_heuristic_healing(broken, err, None)
        self.assertIn(".first", healed)

        broken_overlay = "page.locator('button').click()"
        err_overlay = "element click intercepted by modal-backdrop"
        healed_overlay = she._apply_heuristic_healing(broken_overlay, err_overlay, None)
        self.assertIn("modal-backdrop", healed_overlay)

    def test_e2e_exporter(self):
        tests = [{
            "Type": "Web",
            "Target": "http://admin.leadcrm.uat.s3-website.ap-south-1.amazonaws.com",
            "Test_Step_Description": "Verify dashboard",
            "Expected_Result": "Dashboard is visible",
            "Playwright_Code": "expect(page.locator('body')).to_be_visible()"
        }]
        exporter = TesterArmyExporter()
        ts_code = exporter.generate_typescript_suite(tests)
        self.assertIn("import { test, expect } from './fixtures';", ts_code)
        self.assertIn("test(", ts_code)
        
        all_files = exporter.export_test_files(tests)
        self.assertIn("@playwright/test", all_files["package.json"])
        self.assertIn("@tester-army/e2e", all_files["e2e.config.ts"])

    def test_pyarrow_serialization_robustness(self):
        df = pd.DataFrame({
            "Run": [True, False, None],
            "Target": ["http://test.com", 12345, b"some_bytes"],
            "Test_Step_Description": ["Step 1", None, 456],
            "Status": ["PASSED", "FAILED", 1],
            "Duration": [1.2, "3.4s", "-"]
        })
        clean = sanitize_df_for_streamlit(df)
        table = pa.Table.from_pandas(clean)
        self.assertEqual(table.shape, (3, 5))

    def test_dataframe_normalization_relative_urls(self):
        raw_df = pd.DataFrame({
            "Reference": ["/configuration", "tasks"],
            "Description": ["Open config page", "View task list"],
            "Expected": ["Config loads", "Tasks load"],
        })
        norm_df = normalize_test_dataframe(raw_df, default_target="https://example.com/dashboard")
        self.assertEqual(len(norm_df), 2)
        self.assertEqual(norm_df.iloc[0]["Target"], "https://example.com/configuration")
        self.assertEqual(norm_df.iloc[1]["Target"], "https://example.com/tasks")
        self.assertEqual(norm_df.iloc[0]["Test_Step_Description"], "Open config page")
        self.assertEqual(norm_df.iloc[0]["Expected_Result"], "Config loads")

    def test_functional_form_and_filter_synthesis(self):
        from core.test_generator import _synthesize_default_playwright_code
        filter_code = _synthesize_default_playwright_code("Apply filter by role", "table is filtered")
        self.assertIn("baseline_rows", filter_code)
        self.assertIn("filtered_rows", filter_code)

        clear_code = _synthesize_default_playwright_code("Clear filters and reset view", "original records restored")
        self.assertIn("initial_rows", clear_code)
        self.assertIn("restored_rows", clear_code)

        form_code = _synthesize_default_playwright_code("Fill form and submit", "form saved")
        self.assertIn("sub_btn", form_code)
        self.assertIn("QA Automated Test", form_code)

    def test_execution_settings_persistence(self):
        from app import load_settings, save_settings
        orig_cfg = load_settings()
        try:
            # Verify force=True persists Execution Settings checkboxes
            save_settings({"force_g4f": True, "show_browser": True, "enable_visual": False}, force=True)
            reloaded = load_settings()
            self.assertTrue(reloaded.get("force_g4f"))
            self.assertTrue(reloaded.get("show_browser"))
            self.assertFalse(reloaded.get("enable_visual"))

            # Verify bare-mode save_settings(force=False) does NOT overwrite saved checkbox states
            save_settings({"force_g4f": False, "show_browser": False, "enable_visual": True}, force=False)
            after_bare = load_settings()
            self.assertTrue(after_bare.get("force_g4f"))
            self.assertTrue(after_bare.get("show_browser"))
            self.assertFalse(after_bare.get("enable_visual"))
        finally:
            save_settings(orig_cfg, force=True)

    def test_append_manual_test_case(self):
        from app import append_manual_test_case
        import streamlit as st
        import tempfile
        tmp_csv = tempfile.mktemp(suffix=".csv")
        try:
            st.session_state["auto_generated_file"] = tmp_csv
            st.session_state["test_cases_df"] = pd.DataFrame()
            updated = append_manual_test_case(
                type_val="Web",
                target_val="https://example.com/login",
                step_val="Verify login submit button exists",
                expected_val="Button is clickable",
                code_val="page.locator('button[type=submit]').click()"
            )
            self.assertGreaterEqual(len(updated), 1)
            last_row = updated.iloc[-1]
            self.assertEqual(last_row["Generated_By"], "✍️ Manual / Uploaded")
            self.assertEqual(last_row["Target"], "https://example.com/login")
            self.assertEqual(last_row["Test_Step_Description"], "Verify login submit button exists")
            self.assertTrue(last_row["Run"])
        finally:
            if os.path.exists(tmp_csv):
                os.remove(tmp_csv)
            if "auto_generated_file" in st.session_state and st.session_state["auto_generated_file"] == tmp_csv:
                del st.session_state["auto_generated_file"]

    def test_html_reporter(self):
        from core.html_reporter import generate_html_report
        sample_df = pd.DataFrame([
            {
                "Run": True,
                "Type": "Web",
                "Target": "https://example.com/dashboard",
                "Test_Step_Description": "Step 1: Check <button> exists -> Step 2: Click & verify",
                "Expected_Result": "Button clickable via <a href='tel:'> links",
                "Status": "PASSED",
                "Duration": "1.05s",
                "Actual_Result": "Observed cleanly",
                "Screenshot": "",
                "Execution_Notes": "[AI Autonomous Agent] Step verified",
                "Playwright_Code": "await page.locator('button').click()",
                "Generated_By": "🤖 AI Generated",
            },
            {
                "Run": True,
                "Type": "API",
                "Target": "https://api.example.com/v1/health",
                "Test_Step_Description": "Health check endpoint",
                "Expected_Result": "200 OK",
                "Status": "FAILED",
                "Duration": "0.42s",
                "Actual_Result": "Status 500 received",
                "Screenshot": "",
                "Execution_Notes": "Timeout or server error",
                "Playwright_Code": "response = request.get('/v1/health')",
                "Generated_By": "⚙️ Rule Engine",
            }
        ])
        html_out = generate_html_report(sample_df, title="Regression Test Suite Report")
        self.assertIn("<!DOCTYPE html>", html_out)
        self.assertIn("Regression Test Suite Report", html_out)
        # Verify safe escaping of HTML entities
        self.assertIn("&lt;button&gt;", html_out)
        self.assertNotIn("Step 1: Check <button>", html_out)
        # Verify badges and KPI counters
        self.assertIn("50.0%", html_out) # 1 pass out of 2
        self.assertIn("Playwright Automation Script", html_out)
        self.assertIn("lightboxModal", html_out)
        self.assertIn("copyCode", html_out)

    def test_one_click_run_selection_by_engine(self):
        sample = pd.DataFrame([
            {"Run": True, "Generated_By": "🤖 AI Generated"},
            {"Run": True, "Generated_By": "🤖 AI Step + ⚙️ Template Code"},
            {"Run": True, "Generated_By": "⚙️ Rule Engine"},
            {"Run": True, "Generated_By": "✍️ Manual / Uploaded"},
        ])

        # 1. AI Only
        ai_mask = (sample["Generated_By"].astype(str).str.contains("AI Generated", na=False)) & (~sample["Generated_By"].astype(str).str.contains("Template", na=False))
        self.assertEqual(ai_mask.tolist(), [True, False, False, False])

        # 2. Hybrid
        hyb_mask = sample["Generated_By"].astype(str).str.contains("Template", na=False)
        self.assertEqual(hyb_mask.tolist(), [False, True, False, False])

        # 3. Rule Only
        rule_mask = sample["Generated_By"].astype(str).str.contains("Rule Engine|20-Point", na=False)
        self.assertEqual(rule_mask.tolist(), [False, False, True, False])

        # 4. Manual
        man_mask = sample["Generated_By"].astype(str).str.contains("Manual|Uploaded", na=False)
        self.assertEqual(man_mask.tolist(), [False, False, False, True])

    def test_normalize_test_dataframe_reset_execution_status_for_uploaded_files(self):
        # Simulates an exported test results spreadsheet (test_results.xlsx) being uploaded
        exported_results_df = pd.DataFrame([
            {
                "Type": "Web",
                "Target": "https://example.com/login",
                "Test_Step_Description": "Click submit without entering credentials",
                "Expected_Result": "Validation error displayed",
                "Status": "PASSED",
                "Duration": "1.4s",
                "Actual_Result": "🟢 Action executed successfully.",
                "Execution_Notes": "Execution verified with playwright.",
                "Screenshot": "screen.png"
            },
            {
                "Type": "API",
                "Target": "https://example.com/api/v1/auth",
                "Test_Step_Description": "POST /auth with empty body",
                "Expected_Result": "HTTP 400",
                "Status": "PASSED",
                "Duration": "0.2s",
                "Actual_Result": "🟢 Action executed successfully.",
                "Execution_Notes": "Passed",
                "Screenshot": ""
            }
        ])

        # When reset_execution_status=True (e.g. freshly uploaded test suite)
        clean_df = normalize_test_dataframe(exported_results_df, reset_execution_status=True)
        self.assertEqual(len(clean_df), 2)
        self.assertEqual(clean_df.iloc[0]["Status"], "PENDING")
        self.assertEqual(clean_df.iloc[1]["Status"], "PENDING")
        self.assertEqual(clean_df.iloc[0]["Actual_Result"], "-")
        self.assertEqual(clean_df.iloc[1]["Actual_Result"], "-")
        self.assertEqual(clean_df.iloc[0]["Duration"], "-")
        self.assertEqual(clean_df.iloc[1]["Duration"], "-")
        self.assertEqual(clean_df.iloc[0]["Execution_Notes"], "")
        self.assertEqual(clean_df.iloc[0]["Screenshot"], "")
        self.assertTrue(bool(clean_df.iloc[0]["Run"]))

        # When reset_execution_status=False, existing statuses remain intact
        preserved_df = normalize_test_dataframe(exported_results_df, reset_execution_status=False)
        self.assertEqual(preserved_df.iloc[0]["Status"], "PASSED")
        self.assertEqual(preserved_df.iloc[0]["Actual_Result"], "🟢 Action executed successfully.")
        self.assertEqual(preserved_df.iloc[0]["Duration"], "1.4s")

    def test_delete_selected_and_single_test_cases(self):
        import streamlit as st
        df = pd.DataFrame([
            {"Run": True, "Type": "Web", "Target": "http://example.com/1", "Test_Step_Description": "Step 1", "Expected_Result": "Exp 1"},
            {"Run": False, "Type": "Web", "Target": "http://example.com/2", "Test_Step_Description": "Step 2", "Expected_Result": "Exp 2"},
            {"Run": True, "Type": "Web", "Target": "http://example.com/3", "Test_Step_Description": "Step 3", "Expected_Result": "Exp 3"},
        ])
        df.index = [1, 2, 3]
        st.session_state["test_cases_df"] = df.copy()

        # Delete single test case #2
        deleted = delete_single_test_case(df, 2)
        self.assertTrue(deleted)
        remaining = st.session_state["test_cases_df"]
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining.iloc[0]["Test_Step_Description"], "Step 1")
        self.assertEqual(remaining.iloc[1]["Test_Step_Description"], "Step 3")
        self.assertEqual(list(remaining.index), [1, 2])

        # Delete selected test cases where Run=True
        # remaining has Step 1 (Run=True) and Step 3 (Run=True)
        # uncheck step 3 so only step 1 is deleted
        remaining.at[2, "Run"] = False
        deleted_sel = delete_selected_test_cases(remaining)
        self.assertTrue(deleted_sel)
        final_df = st.session_state["test_cases_df"]
        self.assertEqual(len(final_df), 1)
        self.assertEqual(final_df.iloc[0]["Test_Step_Description"], "Step 3")

    def test_clear_entire_test_suite(self):
        import streamlit as st
        st.session_state["test_cases_df"] = pd.DataFrame([{"Target": "http://example.com"}])
        st.session_state["suite_explicitly_cleared"] = False
        st.session_state["blank_suite"] = True

        clear_entire_test_suite()

        self.assertTrue(st.session_state.get("suite_explicitly_cleared"))
        self.assertFalse(st.session_state.get("blank_suite"))
        self.assertIsNone(st.session_state.get("test_cases_df"))
        self.assertEqual(st.session_state.get("manual_cases"), [])

    def test_origin_engine_detection_counts_and_run_selection(self):
        import streamlit as st
        df = pd.DataFrame([
            {"Run": True, "Type": "Web", "Generated_By": "🤖 AI Generated", "Target": "http://example.com/login", "Test_Step_Description": "Step 1: Fill login form", "Playwright_Code": "page.fill('#email', 'test@test.com')"},
            {"Run": True, "Type": "Web", "Generated_By": "🤖 AI Step + ⚙️ Template Code", "Target": "http://example.com/table", "Test_Step_Description": "Filter table by status", "Playwright_Code": "# 1. Capture baseline record count before applying filter\npass"},
            {"Run": True, "Type": "Web", "Generated_By": "⚙️ Rule Engine", "Target": "http://example.com/audit", "Test_Step_Description": "20-Point Accessibility check", "Playwright_Code": "# deterministic script"},
            {"Run": True, "Type": "Web", "Generated_By": "✍️ Manual / Uploaded", "Target": "http://example.com/manual", "Test_Step_Description": "Manual verification", "Playwright_Code": ""},
        ])
        df.index = [1, 2, 3, 4]
        st.session_state["test_cases_df"] = df.copy()

        # Check compute_origin_counts
        tot, ai_f, hyb, rule, man = compute_origin_counts(df)
        self.assertEqual(tot, 4)
        self.assertEqual(ai_f, 1)
        self.assertEqual(hyb, 1)
        self.assertEqual(rule, 1)
        self.assertEqual(man, 1)

        # 1-Click Run: AI Only
        apply_origin_selection_to_suite(df, "ai_only")
        res = st.session_state["test_cases_df"]
        self.assertTrue(res.loc[1, "Run"])
        self.assertFalse(res.loc[2, "Run"])
        self.assertFalse(res.loc[3, "Run"])
        self.assertFalse(res.loc[4, "Run"])

        # 1-Click Run: Hybrid Only
        apply_origin_selection_to_suite(df, "hybrid_only")
        res = st.session_state["test_cases_df"]
        self.assertFalse(res.loc[1, "Run"])
        self.assertTrue(res.loc[2, "Run"])
        self.assertFalse(res.loc[3, "Run"])
        self.assertFalse(res.loc[4, "Run"])

        # 1-Click Run: Rule Only
        apply_origin_selection_to_suite(df, "rule_only")
        res = st.session_state["test_cases_df"]
        self.assertFalse(res.loc[1, "Run"])
        self.assertFalse(res.loc[2, "Run"])
        self.assertTrue(res.loc[3, "Run"])
        self.assertFalse(res.loc[4, "Run"])

        # 1-Click Run: Manual Only
        apply_origin_selection_to_suite(df, "manual_only")
        res = st.session_state["test_cases_df"]
        self.assertFalse(res.loc[1, "Run"])
        self.assertFalse(res.loc[2, "Run"])
        self.assertFalse(res.loc[3, "Run"])
        self.assertTrue(res.loc[4, "Run"])

        # 1-Click Run: All (or 🔄 All reset)
        apply_origin_selection_to_suite(df, "all")
        res = st.session_state["test_cases_df"]
        self.assertTrue(res["Run"].all())

        # 1-Click Run: None
        apply_origin_selection_to_suite(df, "none")
        res = st.session_state["test_cases_df"]
        self.assertFalse(res["Run"].any())

    def test_delete_selected_test_cases_and_clear_selection(self):
        import streamlit as st
        df = pd.DataFrame([
            {"Type": "Web", "Target": "http://example.com/1", "Test_Step_Description": "Step 1", "Run": True},
            {"Type": "Web", "Target": "http://example.com/2", "Test_Step_Description": "Step 2", "Run": False},
            {"Type": "Web", "Target": "http://example.com/3", "Test_Step_Description": "Step 3", "Run": True},
        ], index=[1, 2, 3])
        st.session_state["test_cases_df"] = df.copy()

        # Delete selected (steps 1 and 3 should be deleted, step 2 remains)
        success = delete_selected_test_cases(df)
        self.assertTrue(success)
        remaining = st.session_state["test_cases_df"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining.iloc[0]["Test_Step_Description"], "Step 2")

        # When Run column is missing, it initializes safely without AttributeError
        df_no_run = pd.DataFrame([
            {"Type": "Web", "Target": "http://example.com/a", "Test_Step_Description": "Step A"},
            {"Type": "Web", "Target": "http://example.com/b", "Test_Step_Description": "Step B"},
        ], index=[1, 2])
        st.session_state["test_cases_df"] = df_no_run.copy()
        success = delete_selected_test_cases(df_no_run)
        self.assertTrue(success)
        self.assertIsNone(st.session_state.get("test_cases_df"))


if __name__ == "__main__":
    unittest.main()



