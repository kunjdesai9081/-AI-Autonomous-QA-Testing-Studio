import unittest
from unittest.mock import MagicMock
from core.test_generator import _synthesize_default_playwright_code, _synthesize_comprehensive_screen_cases
from core.semantic_assert import SemanticAssertEngine


class TestScreenSynthesisAndSemanticAssert(unittest.TestCase):
    def test_search_step_does_not_trigger_filter_drawer_code(self):
        desc = "Step 1: Locate search bar on 'Tasks' -> Step 2: Enter query keyword 'Admin' -> Step 3: Press Enter and verify filtered results update."
        code = _synthesize_default_playwright_code(desc, "Search executes and data grid displays matching records.")
        self.assertIn("search_box", code)
        self.assertIn("Admin", code)
        self.assertNotIn("filter_btn", code)
        self.assertNotIn("Apply Filters", code)

    def test_screen_without_tables_or_forms_generates_only_applicable_cases(self):
        # Read-only configuration screen: 0 tables, 0 inputs, 2 action buttons, 0 filter controls
        screen_info = {
            "name": "Configuration",
            "action_buttons": ["Open Settings", "Export Profile"],
            "pill_tabs": [],
            "inputs": [],
            "tables_count": 0,
            "sub_features": [],
        }
        cases = _synthesize_comprehensive_screen_cases(screen_info, "http://example.com/config", "")
        # Should contain screen load and action buttons, but NOT search, filter, table sort, pagination, or form validation
        descriptions = [c["Test_Step_Description"].lower() for c in cases]
        self.assertTrue(any("navigate to 'configuration'" in d for d in descriptions))
        self.assertFalse(any("locate search bar" in d for d in descriptions))
        self.assertFalse(any("click filter button" in d for d in descriptions))
        self.assertFalse(any("column header" in d for d in descriptions))
        self.assertFalse(any("pagination" in d for d in descriptions))
        self.assertFalse(any("access form inputs" in d for d in descriptions))

    def test_semantic_assert_accepts_empty_state_for_table_claim(self):
        engine = SemanticAssertEngine(ai_client=MagicMock())
        mock_page = MagicMock()
        # Mock locator behavior: table locator count is 0, but empty state locator count is 1
        def mock_locator(selector):
            mock_loc = MagicMock()
            if "table" in selector and "no .* found" not in selector:
                mock_loc.count.return_value = 0
            elif "no .* found" in selector or "empty" in selector:
                mock_loc.count.return_value = 1
            else:
                mock_loc.count.return_value = 0
            return mock_loc

        mock_page.locator.side_effect = mock_locator
        mock_page.evaluate.return_value = "<html><body><div class='empty-state'>No Tasks Found</div></body></html>"

        passed, reason = engine.evaluate_claim(
            mock_page,
            claim="Search executes and data grid displays matching records."
        )
        self.assertTrue(passed, f"Expected claim to pass on empty state, but got: {reason}")
        self.assertIn("empty-state", reason.lower())

    def test_semantic_assert_does_not_fail_dismissed_modal_for_missing_title(self):
        engine = SemanticAssertEngine(ai_client=MagicMock())
        mock_page = MagicMock()
        # Mock locator behavior: text='Tasks - All Tab' is not found (count = 0)
        # Modal open selector count is 0 (modal is closed)
        def mock_locator(selector):
            mock_loc = MagicMock()
            if "text=" in selector:
                mock_loc.count.return_value = 0
                mock_loc.is_visible.return_value = False
            elif ".modal.show" in selector or "dialog" in selector:
                mock_loc.count.return_value = 0
            else:
                mock_loc.count.return_value = 1
                mock_loc.is_visible.return_value = True
            return mock_loc

        mock_page.locator.side_effect = mock_locator
        mock_page.get_by_text.return_value = MagicMock(count=lambda: 0, is_visible=lambda: False)
        mock_page.evaluate.return_value = "<html><body><div>Tasks Screen</div></body></html>"

        passed, reason = engine.evaluate_claim(
            mock_page,
            claim="Modal dialog opens and dismisses cleanly without errors or remaining backdrops."
        )
        self.assertTrue(passed, f"Expected dismissal claim to pass, but got: {reason}")

    def test_form_functional_lifecycle_code_synthesis(self):
        # 1. Valid Form Submission (Happy Path)
        submit_code = _synthesize_default_playwright_code("form valid submit save changes", "submission succeeds")
        self.assertIn("sub_btn", submit_code)
        self.assertIn("QA Automated Test", submit_code)
        self.assertIn("modal-backdrop", submit_code)

        # 2. Negative Form Validation
        neg_code = _synthesize_default_playwright_code("negative validation blank required fields", "validation error appears")
        self.assertIn("inp.fill('')", neg_code)
        self.assertIn("sub_btn", neg_code)
        self.assertIn(":invalid", neg_code)

        # 3. Form Reset & Draft Discard
        reset_code = _synthesize_default_playwright_code("form reset clear discard draft", "form reset cleanly")
        self.assertIn("Draft QA Value To Discard", reset_code)
        self.assertIn("cancel_btn", reset_code)

    def test_filter_and_search_lifecycle_code_synthesis(self):
        # Filter Apply Code
        f_apply = _synthesize_default_playwright_code("filter option and apply filters", "table updates")
        self.assertIn("baseline_rows", f_apply)
        self.assertIn("filter_btn", f_apply)
        self.assertIn("apply_btn", f_apply)
        self.assertIn("filtered_rows", f_apply)

        # Filter Clear Code
        f_clear = _synthesize_default_playwright_code("clear filters and reset", "default records restored")
        self.assertIn("initial_rows", f_clear)
        self.assertIn("clear_btn", f_clear)
        self.assertIn("apply_btn", f_clear)
        self.assertIn("restored_rows", f_clear)

        # Search Query Code
        s_query = _synthesize_default_playwright_code("search query 'Admin'", "search updates")
        self.assertIn("search_box", s_query)
        self.assertIn("Admin", s_query)

        # Search Clear Code
        s_clear = _synthesize_default_playwright_code("clear search", "dataset restored")
        self.assertIn("search_box", s_clear)
        self.assertIn("search_box.fill(\"\")", s_clear)
        self.assertIn("rows_after", s_clear)

    def test_mismatch_script_detector_rejects_filter_code_for_search_step(self):
        from runners.web_runner import _is_trivial_or_mismatched_script
        search_step = "Step 1: Locate search bar on 'Tasks' -> Step 2: Enter query keyword 'Admin' -> Step 3: Press Enter"
        filter_code = 'filter_btn = page.locator("button:has-text(\\"Filter\\")").first\nfilter_btn.click()'
        
        is_mismatch = _is_trivial_or_mismatched_script(filter_code, search_step)
        self.assertTrue(is_mismatch, "Expected search step with filter script to be flagged as mismatched")

    def test_sanitize_playwright_code_strips_redundant_goto_and_fixes_tab_locators(self):
        from runners.web_runner import _sanitize_playwright_code
        raw = """page.goto('http://192.168.1.33:5177/tasks')
today_tab = page.locator('button, [role="tab"], [class*="tab"], [class*="pill"]').filter(has_text='Today')
if today_tab.count() > 0:
    today_tab.first.click()"""
        cleaned = _sanitize_playwright_code(raw)
        self.assertNotIn("page.goto", cleaned)
        self.assertIn('[class*="pill-tab"]', cleaned)
        self.assertNotIn('[class*="tab"]', cleaned)
        self.assertIn("re.compile(r'^\\s*Today\\s*$'", cleaned)

    def test_sanitize_playwright_code_preserves_goto_after_clear_cookies_and_fixes_context(self):
        from runners.web_runner import _sanitize_playwright_code
        raw = """page.context().clear_cookies()
page.goto('http://192.168.1.33:5177/tasks')
expect(page).to_have_url(re.compile(r'login|auth'))"""
        cleaned = _sanitize_playwright_code(raw)
        self.assertIn("page.context.clear_cookies()", cleaned)
        self.assertNotIn("page.context()", cleaned)
        self.assertIn("localStorage.clear()", cleaned)
        self.assertIn("page.goto('http://192.168.1.33:5177/tasks')", cleaned)

    def test_safe_expect_handles_primitives_and_camelcase_methods(self):
        from runners.web_runner import make_safe_expect
        mock_raw_expect = MagicMock()
        safe_expect = make_safe_expect(mock_raw_expect)

        # Primitive string from page.evaluate("document.activeElement.tagName")
        prim = safe_expect("BUTTON")
        prim.not_to_be_empty()
        prim.to_be_visible()

        with self.assertRaises(AssertionError):
            safe_expect("").not_to_be_empty()

        # Locator proxy camelCase mapping
        mock_loc = MagicMock()
        mock_loc.count.return_value = 1
        proxy = safe_expect(mock_loc)
        self.assertTrue(callable(proxy.toHaveClass))
        self.assertTrue(callable(proxy.toBeVisible))
        self.assertTrue(callable(proxy.notToBeVisible))

    def test_api_runner_page_request_shim_and_response_status_alias(self):
        from unittest.mock import patch
        from runners.api_runner import _ResponseWrapper, _PageShim
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"ok": true}'
        wrapped = _ResponseWrapper(mock_resp)
        self.assertEqual(wrapped.status, 200)
        self.assertEqual(wrapped.status_code, 200)
        self.assertTrue(wrapped.ok)

        with patch("runners.api_runner.requests.request", return_value=mock_resp):
            page_shim = _PageShim({"Token": "tok"})
            res = page_shim.request.get("http://192.168.1.33:5177/api/v1/tasks")
            self.assertEqual(res.status, 200)

    def test_semantic_assert_handles_tabs_sidebar_and_empty_card_menus(self):
        engine = SemanticAssertEngine(ai_client=MagicMock())
        mock_page = MagicMock()
        mock_loc = MagicMock()
        mock_loc.count.return_value = 1
        mock_loc.is_visible.return_value = True
        mock_page.locator.return_value = mock_loc

        # Date filter tab claim
        ok1, _ = engine.evaluate_claim(mock_page, "The 'Today' filter tab is visually active and task list is filtered to today's tasks.")
        self.assertTrue(ok1)

        # Sidebar collapse claim
        ok2, _ = engine.evaluate_claim(mock_page, "The sidebar collapses smoothly to a compact icon-only view.")
        self.assertTrue(ok2)

        # Card action menu claim
        ok3, _ = engine.evaluate_claim(mock_page, "A dropdown menu opens displaying available actions (e.g., Edit, Delete, View Details).")
        self.assertTrue(ok3)

    def test_generate_cases_from_flow_selected_screens_and_subfeatures(self):
        import os
        import tempfile
        import pandas as pd
        from core.test_generator import TestGenerator

        gen = TestGenerator()
        gen.orchestrator = MagicMock()
        # Simulate LLM returning empty or failing so deterministic synthesis & merging are exercised
        gen.orchestrator.generate_web_tests.return_value = "[]"

        flow_data = {
            "start_url": "http://192.168.1.33:5177/dashboard",
            "screens": [
                {
                    "name": "Dashboard",
                    "url": "http://192.168.1.33:5177/dashboard",
                    "screenshot": "dash.png",
                    "action_buttons": ["Refresh"],
                    "pill_tabs": [],
                    "inputs": [],
                    "tables_count": 0,
                    "sub_features": [
                        {
                            "type": "kpi_card",
                            "name": "KPI Drill-Down: Open Tasks",
                            "trigger": "Open Tasks",
                            "url": "http://192.168.1.33:5177/tasks",
                            "screenshot": "kpi.png",
                        }
                    ],
                },
                {
                    "name": "Configuration",
                    "url": "http://192.168.1.33:5177/config",
                    "screenshot": "config.png",
                    "action_buttons": ["Today", "Export Settings"],
                    "pill_tabs": ["Today"],
                    "inputs": [],
                    "tables_count": 0,
                    "sub_features": [
                        {
                            "type": "config_module",
                            "name": "Config Module: Departments",
                            "trigger": "Departments",
                            "url": "http://192.168.1.33:5177/config/departments",
                            "screenshot": "dept.png",
                            "inner_modals": [
                                {
                                    "name": "Add Department Modal",
                                    "trigger": "Add Department",
                                    "inputs": ["Department Name"],
                                    "action_buttons": ["Save"],
                                    "screenshot": "dept_modal.png",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "selected_flow_tests.csv")
            # Select ONLY screen index 1 ("Configuration"), skipping screen index 0 ("Dashboard")
            out_path, primary_ss = gen.generate_cases_from_flow(
                flow_data=flow_data,
                output_csv_path=csv_path,
                selected_screen_indices=[1],
                append=False,
            )
            self.assertEqual(out_path, csv_path)
            self.assertEqual(primary_ss, "config.png")
            df = pd.read_csv(csv_path)
            targets = df["Target"].tolist()
            steps = " ".join(df["Test_Step_Description"].tolist())

            # Verify Dashboard (index 0) was NOT included
            self.assertNotIn("http://192.168.1.33:5177/dashboard", targets)
            # Verify Configuration (index 1) and its sub-features (Departments config module + inner modal) WERE included
            self.assertIn("http://192.168.1.33:5177/config", targets)
            self.assertIn("http://192.168.1.33:5177/config/departments", targets)
            self.assertIn("Add Department", steps)
            # Verify empty selection returns None, ""
            empty_path, _ = gen.generate_cases_from_flow(
                flow_data=flow_data,
                output_csv_path=csv_path,
                selected_screen_indices=[],
                append=False,
            )
            self.assertIsNone(empty_path)

    def test_twenty_point_web_quality_audit_and_checklist_synthesis(self):
        from datetime import datetime
        from core.deep_inspector import DeepScreenInspector

        inspector = DeepScreenInspector()
        mock_page = MagicMock()
        mock_page.viewport_size = {"width": 1440, "height": 900}
        cur_year = str(datetime.now().year)

        # Side effects for desktop_metrics, probe_404, and mobile_metrics
        mock_page.evaluate.side_effect = [
            {
                "hasHorizontalScroll": False,
                "scrollW": 1440,
                "clientW": 1440,
                "brokenLinks": [],
                "hasFavicon": True,
                "pageTitle": "Enterprise CRM Dashboard",
                "isTitleValid": True,
                "hasMetaDescription": True,
                "metaDesc": "Enterprise CRM and Task Management Dashboard",
                "footerPresent": True,
                "footerLinksCount": 3,
                "brokenFooterLinks": [],
                "foundYear": cur_year,
                "isCopyrightValid": True,
                "oversizedImages": [],
                "brokenButtons": [],
                "hasSuccessContainer": True,
                "hasErrorSupport": True,
                "placeholderMatches": [],
                "unusedNav": [],
                "isLogoClickable": True,
                "phoneLinks": 1,
                "rawPhonesCount": 1,
                "unlinkedPhones": False,
                "mailtoLinks": 1,
                "rawEmailsCount": 1,
                "unlinkedEmails": False,
                "hasResponsiveViewportMeta": True,
                "viewportContent": "width=device-width, initial-scale=1.0",
                "smallTouchTargets": 0,
                "baseFontSize": 16,
                "isFontLegible": True,
                "unsafeExternalLinks": [],
                "hasOpenGraph": True,
                "hasHtmlLang": True,
                "htmlLang": "en",
                "missingAltImages": [],
                "unlabeledInputs": [],
                "h1Count": 1,
                "isH1Valid": True,
                "h1Text": "Enterprise CRM Dashboard",
                "suppressedFocus": False,
                "brokenImages": [],
                "domNodeCount": 420,
                "isDomHealthy": True,
                "hasLazyLoading": True,
                "missingAutocomplete": [],
                "uncontainedTables": [],
                "lowContrastElements": 0,
            },
            {"status": 404, "length": 500, "hasHtml": True},
            {
                "hasMobileMenu": True,
                "hasMobileOverflow": False,
                "mobileScrollW": 375,
                "overflowElements": [],
            },
        ]

        audit = inspector._audit_20_point_web_quality(mock_page, "http://192.168.1.33:5177/dashboard")
        self.assertEqual(len(audit["checks"]), 40)
        self.assertEqual(audit["score"], 100)
        self.assertEqual(audit["passed_count"], 40)
        self.assertEqual(audit["failed_count"], 0)

        # Verify all 40 checklist test cases synthesize cleanly
        cases = inspector.generate_twenty_point_checklist_cases({
            "target_url": "http://192.168.1.33:5177/dashboard",
            "page_title": "Dashboard",
        })
        self.assertEqual(len(cases), 40)
        for tc in cases:
            self.assertEqual(tc["Type"], "Web")
            self.assertTrue(len(tc["Playwright_Code"]) > 15)

        # Verify _synthesize_default_playwright_code supports the 20-point keywords
        h_code = _synthesize_default_playwright_code("Check horizontal scrolling on page", "No horizontal scroll")
        self.assertIn("scrollWidth", h_code)
        m_code = _synthesize_default_playwright_code("Verify mobile overflow at 375 viewport", "No mobile overflow")
        self.assertIn("375", m_code)
        p_code = _synthesize_default_playwright_code("Make the phone number clickable", "tel: link exists")
        self.assertIn("tel:", p_code)
        e_code = _synthesize_default_playwright_code("Make the email clickable", "mailto: link exists")
        self.assertIn("mailto:", e_code)

    def test_ai_full_strategy_covers_all_elements_and_sets_correct_tag(self):
        from core.deep_inspector import DeepScreenInspector

        inspector = DeepScreenInspector()
        mock_inspection = {
            "target_url": "https://crm.example.com/subsidy-files",
            "page_title": "Subsidy Files",
            "interactive_elements": [{"text": "Create File"}, {"text": "Export Records"}, {"text": "Refresh"}],
            "filter_pills": [{"text": "Active"}, {"text": "Pending"}],
            "comboboxes_and_dropdowns": [{"text": "Scheme Filter"}],
            "forms_and_inputs": [
                {"name": "file_number", "type": "text", "required": True},
                {"name": "applicant_email", "type": "email", "required": True},
            ],
            "tables_and_grids": [
                {"headers": ["ID", "Applicant", "Status", "Amount"], "hasSearch": True}
            ],
            "probed_modals_and_drawers": [
                {"trigger": "Advanced Filter", "dynamic_data": {"revealedContainers": [{"discoveredOptions": ["Option A"]}]}}
            ],
            "network_apis": [
                {"url": "https://crm.example.com/api/v1/subsidy-files", "method": "GET"}
            ],
        }

        import json
        mock_ai = MagicMock()
        mock_ai.generate_response.return_value = json.dumps([
            {
                "Type": "Web",
                "Target": "https://crm.example.com/subsidy-files",
                "Test_Step_Description": "Step 1: Navigate to 'Subsidy Files' -> Step 2: Click 'Create File' button -> Step 3: Verify dialog opens.",
                "Expected_Result": "Dialog opens cleanly.",
                "Playwright_Code": "page.locator('button:has-text(\"Create File\")').click()\nexpect(page.locator('body')).to_be_visible()",
            }
        ])

        # 1. AI Full strategy with mock AI client
        ai_cases = inspector.generate_tests_from_inspection(mock_inspection, strategy="ai_full", ai_client=mock_ai)
        self.assertGreaterEqual(len(ai_cases), 15)
        for tc in ai_cases:
            self.assertEqual(tc["Generated_By"], "🤖 AI Generated")
            self.assertTrue(len(tc["Playwright_Code"]) > 10)
            self.assertIn("Step 1:", tc["Test_Step_Description"])

        descs_and_code = " ".join([tc["Test_Step_Description"] + " " + tc["Playwright_Code"] for tc in ai_cases])
        # Verify all buttons covered
        self.assertIn("Create File", descs_and_code)
        self.assertIn("Export Records", descs_and_code)
        # Verify filter pill covered
        self.assertIn("Active", descs_and_code)
        # Verify dropdown covered
        self.assertIn("Scheme Filter", descs_and_code)
        # Verify drawer covered
        self.assertIn("Advanced Filter", descs_and_code)
        # Verify table search & sorting covered
        self.assertIn("Admin", descs_and_code)
        self.assertIn("NON_EXISTENT", descs_and_code)
        self.assertIn("ID", descs_and_code)
        # Verify forms covered
        self.assertIn("file_number", descs_and_code)
        # Verify API contract covered
        self.assertIn("/api/v1/subsidy-files", descs_and_code)

        # 2. Rule Engine strategy
        rule_cases = inspector.generate_tests_from_inspection(mock_inspection, strategy="rule_engine")
        self.assertGreaterEqual(len(rule_cases), 10)
        for tc in rule_cases:
            self.assertEqual(tc["Generated_By"], "⚙️ Rule Engine")

        # 3. Hybrid strategy
        hybrid_cases = inspector.generate_tests_from_inspection(mock_inspection, strategy="hybrid")
        self.assertGreaterEqual(len(hybrid_cases), 15)
        for tc in hybrid_cases:
            self.assertEqual(tc["Generated_By"], "🤖 AI Step + ⚙️ Template Code")

    def test_status_change_edit_details_and_safeguard_code_synthesis(self):
        # 1. Status Change Code
        status_code = _synthesize_default_playwright_code("change status update status dropdown phase control", "record status reflects new state")
        self.assertIn("status_ctrl", status_code)
        self.assertIn("select_option", status_code)

        # 2. Edit Existing Record Code
        edit_code = _synthesize_default_playwright_code("edit existing record record editor save changes", "record updated successfully")
        self.assertIn("edit_btn", edit_code)
        self.assertIn("QA Updated Record Value", edit_code)
        self.assertIn("save_btn", edit_code)

        # 3. View Details Drill-Down Code
        view_code = _synthesize_default_playwright_code("view details row drill-down record inspect", "details view opens and dismisses")
        self.assertIn("view_btn", view_code)
        self.assertIn("details-pane", view_code)

        # 4. Delete Safeguard Code (cancels to protect against accidental data loss)
        del_code = _synthesize_default_playwright_code("delete archive safeguard confirmation modal cancel", "confirmation dialog safely cancelled")
        self.assertIn("del_btn", del_code)
        self.assertIn("cancel_btn", del_code)
        self.assertIn("safely cancelled without data loss", del_code)

        # 5. Row Selection & Bulk Actions Code
        chk_code = _synthesize_default_playwright_code("table row selection bulk action checkbox", "row selection toggles cleanly")
        self.assertIn("chk", chk_code)
        self.assertIn("bulk", chk_code)

    def test_screen_with_table_generates_all_lifecycle_and_row_action_cases(self):
        # Screen with 1 table, 1 Add button, 0 raw inputs
        screen_info = {
            "name": "Leads Pipeline",
            "action_buttons": ["Add Lead", "Export"],
            "pill_tabs": ["All", "Hot", "Closed"],
            "inputs": [],
            "tables_count": 1,
            "sub_features": [],
        }
        cases = _synthesize_comprehensive_screen_cases(screen_info, "http://crm.example.com/leads", "")
        descriptions = [c["Test_Step_Description"].lower() for c in cases]

        # Verify New Entry & Forms generated even though inputs weren't in initial DOM
        self.assertTrue(any("add new' or 'create' button" in d for d in descriptions))
        self.assertTrue(any("leave required fields blank" in d for d in descriptions))
        self.assertTrue(any("draft test text" in d for d in descriptions))

        # Verify Status Change generated
        self.assertTrue(any("status control or phase dropdown" in d for d in descriptions))

        # Verify Edit Record generated
        self.assertTrue(any("edit record action button" in d for d in descriptions))

        # Verify Details Drill-Down generated
        self.assertTrue(any("view details or record title" in d for d in descriptions))

        # Verify Delete Safeguard generated
        self.assertTrue(any("delete or archive button" in d for d in descriptions))

        # Verify Row Selection generated
        self.assertTrue(any("toggle row selection checkbox" in d for d in descriptions))

        # Verify Search, Sort, Pagination generated
        self.assertTrue(any("locate search bar" in d for d in descriptions))
        self.assertTrue(any("column header" in d for d in descriptions))
        self.assertTrue(any("pagination controls" in d for d in descriptions))

    def test_generation_strategies_across_flow_and_url(self):
        import tempfile
        import os
        import json
        import pandas as pd
        from unittest.mock import patch, MagicMock
        from core.test_generator import TestGenerator

        gen = TestGenerator()
        flow_data = {
            "entry_url": "http://192.168.1.33:5177/login",
            "screens": [
                {
                    "name": "Login",
                    "url": "http://192.168.1.33:5177/login",
                    "action_buttons": ["Sign In"],
                    "inputs": ["Username", "Password"],
                    "tables_count": 0,
                    "screenshot": "login.png",
                }
            ],
        }

        mock_ai_cases = [
            {
                "Type": "Web",
                "Target": "http://192.168.1.33:5177/login",
                "Test_Step_Description": "Step 1: Enter valid credentials -> Step 2: Click Sign In.",
                "Expected_Result": "User dashboard displays.",
                "Playwright_Code": "page.locator('input[name=\"Username\"]').fill('user');",
            },
            {
                "Type": "Web",
                "Target": "http://192.168.1.33:5177/login",
                "Test_Step_Description": "Step 1: Leave fields blank -> Step 2: Click Sign In.",
                "Expected_Result": "Validation error appears.",
                "Playwright_Code": "page.locator('button:has-text(\"Sign In\")').click();",
            },
        ]
        gen.orchestrator.generate_web_tests = MagicMock(return_value=json.dumps(mock_ai_cases))

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Flow strategy="rule_engine"
            csv_rule = os.path.join(tmpdir, "flow_rule.csv")
            gen.generate_cases_from_flow(flow_data=flow_data, output_csv_path=csv_rule, strategy="rule_engine")
            df_rule = pd.read_csv(csv_rule)
            self.assertGreater(len(df_rule), 0)
            for origin in df_rule["Generated_By"].fillna(""):
                self.assertIn("Rule Engine", origin)

            # 2. Flow strategy="hybrid"
            csv_hybrid = os.path.join(tmpdir, "flow_hybrid.csv")
            gen.generate_cases_from_flow(flow_data=flow_data, output_csv_path=csv_hybrid, strategy="hybrid")
            df_hybrid = pd.read_csv(csv_hybrid)
            self.assertGreater(len(df_hybrid), 0)
            for origin in df_hybrid["Generated_By"].fillna(""):
                self.assertIn("Template Code", origin)

            # 3. Flow strategy="ai_full"
            csv_ai = os.path.join(tmpdir, "flow_ai.csv")
            gen.generate_cases_from_flow(flow_data=flow_data, output_csv_path=csv_ai, strategy="ai_full")
            df_ai = pd.read_csv(csv_ai)
            self.assertGreater(len(df_ai), 0)
            for origin in df_ai["Generated_By"].fillna(""):
                self.assertIn("AI Generated", origin)

            # 4. URL strategy="rule_engine" with mocked playwright
            csv_url_rule = os.path.join(tmpdir, "url_rule.csv")
            mock_playwright = MagicMock()
            mock_browser = MagicMock()
            mock_ctx = MagicMock()
            mock_p = MagicMock()
            mock_playwright.return_value.__enter__.return_value.chromium.launch.return_value = mock_browser
            mock_browser.new_context.return_value = mock_ctx
            mock_ctx.new_page.return_value = mock_p
            mock_p.evaluate.return_value = {
                "page_title": "Login Page",
                "action_buttons": [{"text": "Sign In"}],
                "navigation_links": [],
                "forms_and_inputs": [{"name": "Username"}],
                "table_and_grid": {"headers": []},
            }
            mock_p.accessibility.snapshot.return_value = None

            with patch("core.test_generator.sync_playwright", mock_playwright):
                gen.generate_cases_from_url(
                    url="http://192.168.1.33:5177/login",
                    output_csv_path=csv_url_rule,
                    strategy="rule_engine",
                )
                df_url_rule = pd.read_csv(csv_url_rule)
                self.assertGreater(len(df_url_rule), 0)
                for origin in df_url_rule["Generated_By"].fillna(""):
                    self.assertIn("Rule Engine", origin)

    def test_form_filling_adding_something_and_input_validations(self):
        from core.test_generator import _synthesize_default_playwright_code, _synthesize_comprehensive_screen_cases

        # 1. Happy path form filling ("adding something")
        add_code = _synthesize_default_playwright_code("create new entry form valid submit save changes", "submission succeeds")
        self.assertIn("QA Automated Test", add_code)
        self.assertIn("qa_test@example.com", add_code)
        self.assertIn("active_container", add_code)
        self.assertIn("sub_btn", add_code)
        # Verify no excessive 600ms/800ms delays in form submission
        self.assertNotIn("wait_for_timeout(600)", add_code)
        self.assertNotIn("wait_for_timeout(800)", add_code)

        # 2. Blank required field validation
        blank_code = _synthesize_default_playwright_code("create new entry negative validation blank required fields", "validation error appears")
        self.assertIn("inp.fill('')", blank_code)
        self.assertIn(":invalid", blank_code)
        self.assertIn("active_container", blank_code)

        # 3. Invalid format constraint validation
        invalid_fmt_code = _synthesize_default_playwright_code("create new entry form invalid format constraint validation", "format error appears")
        self.assertIn("invalid-qa-email-format", invalid_fmt_code)
        self.assertIn("active_container", invalid_fmt_code)
        self.assertIn("has_err", invalid_fmt_code)

        # 4. Boundary value & special characters input sanitization
        boundary_code = _synthesize_default_playwright_code("create new entry form boundary value special characters input sanitization", "sanitized safely")
        self.assertIn("boundary_test", boundary_code)
        self.assertIn("active_container", boundary_code)

        # 5. Form draft reset & cancel
        reset_code = _synthesize_default_playwright_code("create new entry form reset clear discard draft", "form reset cleanly")
        self.assertIn("Draft QA Value To Discard", reset_code)
        self.assertIn("cancel_btn", reset_code)

        # 6. Verify comprehensive screen cases include all form flows when screen has form / add buttons
        screen = {
            "name": "Tasks Management",
            "url": "http://192.168.1.33:5177/tasks",
            "action_buttons": ["Add Task", "Filter"],
            "inputs": ["Task Name", "Assigned To"],
            "tables_count": 1,
            "sub_features": [
                {
                    "name": "Add Task Modal",
                    "trigger": "Add Task",
                    "url": "http://192.168.1.33:5177/tasks/new",
                    "screenshot": "task_modal.png",
                }
            ],
            "screenshot": "tasks.png",
        }
        cases = _synthesize_comprehensive_screen_cases(screen, screen["url"], screen["screenshot"])
        steps_text = " ".join([c["Test_Step_Description"] for c in cases])

        # Verify Positive creation ("adding something") is generated
        self.assertIn("Add New' or 'Create' button to open entry form", steps_text)
        # Verify Negative blank validation is generated
        self.assertIn("leave required fields blank", steps_text)
        # Verify Invalid format validation is generated
        self.assertIn("invalid formatted inputs", steps_text)
        # Verify Boundary values validation is generated
        self.assertIn("boundary strings and special characters", steps_text)
        # Verify Form reset is generated
        self.assertIn("draft test text into form inputs", steps_text)
        # Verify sub-feature modal form filling is generated
        self.assertIn("open modal form -> Step 2: Populate required entry fields", steps_text)
        self.assertIn("open modal form -> Step 2: Leave required fields blank", steps_text)


if __name__ == "__main__":
    unittest.main()





