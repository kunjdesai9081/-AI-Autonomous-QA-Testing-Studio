import io
import unittest
import pandas as pd
from unittest.mock import MagicMock

from core.test_generator import parse_qa_test_sheet_to_dataframe, TestGenerator
from core.html_reporter import generate_html_report
from app import normalize_test_dataframe, _safe_read_uploaded_file


class TestMultiSheetAndAsIsColumns(unittest.TestCase):
    """
    Test suite for:
    1. As-Is native column preservation (Jira Key, Epic, Preconditions, Severity, etc.)
    2. Multi-Sheet workbook ingestion, Sheet_Name attribution, and multi-tab Excel export
    3. HTML interactive report sheet tab filtering and native metadata drawer
    """

    def test_as_is_columns_preserved_in_parse_qa_test_sheet(self):
        csv_data = """Test Case ID,Screen / Function,User Action on Dev Link,Expected Outcome,Jira_Key,Epic,Severity,Preconditions
TC-001,Login Page,Enter admin@test.com and click Login,Dashboard renders,PROJ-101,Authentication,High,User exists in DB
TC-002,Settings,Toggle dark mode,Theme changes to dark,PROJ-102,UI Customization,Medium,User is on dashboard
"""
        df = parse_qa_test_sheet_to_dataframe(csv_data, default_target="https://example.com")
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 2)

        # Verify standard columns exist
        for col in ["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Playwright_Code"]:
            self.assertIn(col, df.columns)

        # Verify custom As-Is columns are strictly preserved
        self.assertIn("Jira_Key", df.columns)
        self.assertIn("Epic", df.columns)
        self.assertIn("Severity", df.columns)
        self.assertIn("Preconditions", df.columns)

        self.assertEqual(df.iloc[0]["Jira_Key"], "PROJ-101")
        self.assertEqual(df.iloc[0]["Epic"], "Authentication")
        self.assertEqual(df.iloc[0]["Severity"], "High")
        self.assertEqual(df.iloc[0]["Preconditions"], "User exists in DB")

        self.assertEqual(df.iloc[1]["Jira_Key"], "PROJ-102")
        self.assertEqual(df.iloc[1]["Epic"], "UI Customization")

    def test_normalize_test_dataframe_preserves_custom_columns_and_sheet_name(self):
        raw_df = pd.DataFrame([
            {
                "Target": "https://example.com/checkout",
                "Test_Step_Description": "Click checkout and enter card",
                "Expected_Result": "Order confirmation modal appears",
                "Sheet_Name": "Billing & Checkout",
                "Owner": "QA Lead",
                "Component": "Cart",
                "Bug_ID": "BUG-999"
            }
        ])

        norm_df = normalize_test_dataframe(raw_df)
        self.assertEqual(len(norm_df), 1)

        # Check standard columns
        self.assertTrue(norm_df.iloc[0]["Run"])
        self.assertEqual(norm_df.iloc[0]["Type"], "Web")
        self.assertEqual(norm_df.iloc[0]["Sheet_Name"], "Billing & Checkout")

        # Check custom columns are retained as-is
        self.assertEqual(norm_df.iloc[0]["Owner"], "QA Lead")
        self.assertEqual(norm_df.iloc[0]["Component"], "Cart")
        self.assertEqual(norm_df.iloc[0]["Bug_ID"], "BUG-999")

    def test_multi_sheet_excel_safe_read_and_sheet_name_attribution(self):
        # Create an in-memory multi-sheet Excel workbook with 3 distinct sheets
        excel_buffer = io.BytesIO()
        sheet1_df = pd.DataFrame([
            {"Test_Step_Description": "Login with valid credentials", "Expected_Result": "Dashboard displayed", "Target": "https://example.com/login", "Jira_Key": "AUTH-1"},
            {"Test_Step_Description": "Login with locked account", "Expected_Result": "Error message shown", "Target": "https://example.com/login", "Jira_Key": "AUTH-2"}
        ])
        sheet2_df = pd.DataFrame([
            {"Test_Step_Description": "Search for products", "Expected_Result": "Search results list shown", "Target": "https://example.com/catalog", "Jira_Key": "CAT-1"}
        ])
        sheet3_df = pd.DataFrame([
            {"Test_Step_Description": "Process Stripe payment", "Expected_Result": "Payment successful badge", "Target": "https://example.com/pay", "Jira_Key": "PAY-1"}
        ])

        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            sheet1_df.to_excel(writer, sheet_name="Smoke Tests", index=False)
            sheet2_df.to_excel(writer, sheet_name="Catalog Search", index=False)
            sheet3_df.to_excel(writer, sheet_name="Payment Flows", index=False)

        excel_buffer.seek(0)
        excel_buffer.name = "test_suite_multitab.xlsx"

        unified_df = _safe_read_uploaded_file(excel_buffer)
        self.assertFalse(unified_df.empty)
        self.assertEqual(len(unified_df), 4)

        # Verify Sheet_Name exists on all rows and matches the source sheets
        self.assertIn("Sheet_Name", unified_df.columns)
        unique_sheets = unified_df["Sheet_Name"].unique().tolist()
        self.assertIn("Smoke Tests", unique_sheets)
        self.assertIn("Catalog Search", unique_sheets)
        self.assertIn("Payment Flows", unique_sheets)

        smoke_cases = unified_df[unified_df["Sheet_Name"] == "Smoke Tests"]
        self.assertEqual(len(smoke_cases), 2)
        self.assertEqual(smoke_cases.iloc[0]["Jira_Key"], "AUTH-1")

        cat_cases = unified_df[unified_df["Sheet_Name"] == "Catalog Search"]
        self.assertEqual(len(cat_cases), 1)

        pay_cases = unified_df[unified_df["Sheet_Name"] == "Payment Flows"]
        self.assertEqual(len(pay_cases), 1)

    def test_multi_sheet_excel_round_trip_export(self):
        # Create multi-sheet unified dataframe
        df = pd.DataFrame([
            {"Run": True, "Sheet_Name": "SheetA", "Test_Step_Description": "Step A1", "Target": "https://example.com/a"},
            {"Run": True, "Sheet_Name": "SheetA", "Test_Step_Description": "Step A2", "Target": "https://example.com/a"},
            {"Run": True, "Sheet_Name": "SheetB", "Test_Step_Description": "Step B1", "Target": "https://example.com/b"}
        ])

        out_buffer = io.BytesIO()
        with pd.ExcelWriter(out_buffer, engine="openpyxl") as xl_w:
            for sheet_name_val, sheet_group in df.groupby("Sheet_Name"):
                sheet_group.to_excel(xl_w, index=False, sheet_name=str(sheet_name_val)[:31])

        out_buffer.seek(0)
        xl_in = pd.ExcelFile(out_buffer)
        self.assertIn("SheetA", xl_in.sheet_names)
        self.assertIn("SheetB", xl_in.sheet_names)

        df_a = pd.read_excel(xl_in, sheet_name="SheetA")
        self.assertEqual(len(df_a), 2)
        df_b = pd.read_excel(xl_in, sheet_name="SheetB")
        self.assertEqual(len(df_b), 1)

    def test_html_report_sheet_tabs_and_native_metadata_rendering(self):
        results_df = pd.DataFrame([
            {
                "Run": True,
                "Type": "Web",
                "Sheet_Name": "Authentication",
                "Target": "https://example.com/login",
                "Test_Step_Description": "Fill username and submit",
                "Expected_Result": "Logged in",
                "Status": "PASSED",
                "Duration": "1.2s",
                "Actual_Result": "Dashboard loaded",
                "Screenshot": "",
                "Execution_Notes": "Auth test",
                "Playwright_Code": "page.goto('https://example.com/login')",
                "Jira_Key": "AUTH-404",
                "Severity": "Critical",
                "Preconditions": "Database seeded"
            },
            {
                "Run": True,
                "Type": "Web",
                "Sheet_Name": "Billing",
                "Target": "https://example.com/billing",
                "Test_Step_Description": "Upgrade plan to Pro",
                "Expected_Result": "Pro badge displayed",
                "Status": "FAILED",
                "Duration": "2.4s",
                "Actual_Result": "Timeout waiting for modal",
                "Screenshot": "",
                "Execution_Notes": "Billing check",
                "Playwright_Code": "page.goto('https://example.com/billing')",
                "Jira_Key": "BILL-101",
                "Severity": "High",
                "Preconditions": "Active card attached"
            }
        ])

        html_output = generate_html_report(results_df)

        # 1. Verify sheet tabs container and pills are present
        self.assertIn("sheet-tabs-container", html_output)
        self.assertIn("Authentication", html_output)
        self.assertIn("Billing", html_output)
        self.assertIn("setSheetFilter", html_output)

        # 2. Verify sheet badges on cards
        self.assertIn("sheet-badge-tag", html_output)

        # 3. Verify Native Sheet Metadata drawer with custom As-Is columns
        self.assertIn("Native Sheet Metadata", html_output)
        self.assertIn("AUTH-404", html_output)
        self.assertIn("BILL-101", html_output)
        self.assertIn("Critical", html_output)
        self.assertIn("Database seeded", html_output)
        self.assertIn("Active card attached", html_output)

    def test_ai_regen_preserves_100_percent_as_is_columns_and_execution_adapter(self):
        # Create multi-sheet workbook with rich custom user metadata
        excel_buffer = io.BytesIO()
        sheet1_df = pd.DataFrame([
            {
                "TC_Number": "TC-AUTH-01",
                "Feature_Module": "User Management",
                "User Action": "Submit valid credentials",
                "Expected Outcome": "Redirected to admin dashboard",
                "Preconditions": "Admin user exists",
                "Severity": "Critical",
                "Assignee": "Senior QA",
            }
        ])
        sheet2_df = pd.DataFrame([
            {
                "TC_Number": "TC-SET-01",
                "Feature_Module": "School Profile",
                "User Action": "Update school address and contact",
                "Expected Outcome": "Profile saved toast displayed",
                "Preconditions": "School is active",
                "Severity": "Medium",
                "Assignee": "Automation Eng",
            }
        ])

        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            sheet1_df.to_excel(writer, sheet_name="Admin Security", index=False)
            sheet2_df.to_excel(writer, sheet_name="School Config", index=False)

        excel_buffer.seek(0)
        excel_buffer.name = "School_ERP_Suite.xlsx"

        from core.test_generator import TestGenerator
        gen = TestGenerator(ai_client=None)

        regen_df = gen.ai_regenerate_and_convert_suite(
            excel_buffer,
            default_target="https://example.com/login",
            default_type="Web",
        )

        self.assertFalse(regen_df.empty)
        self.assertEqual(len(regen_df), 2)

        # 1. Verify multi-sheet retention
        self.assertIn("Sheet_Name", regen_df.columns)
        self.assertEqual(set(regen_df["Sheet_Name"].unique()), {"Admin Security", "School Config"})

        # 2. Verify 100% of user original columns are retained as-is
        for expected_col in ["TC_Number", "Feature_Module", "User Action", "Expected Outcome", "Preconditions", "Severity", "Assignee"]:
            self.assertIn(expected_col, regen_df.columns, f"Custom column '{expected_col}' was dropped!")

        self.assertEqual(regen_df.iloc[0]["TC_Number"], "TC-AUTH-01")
        self.assertEqual(regen_df.iloc[0]["Severity"], "Critical")
        self.assertEqual(regen_df.iloc[1]["Feature_Module"], "School Profile")
        self.assertEqual(regen_df.iloc[1]["Assignee"], "Automation Eng")

        # 3. Verify internal execution adapter fields are populated
        for adapter_col in ["Playwright_Code", "Target", "_target", "_code", "_step", "_expected"]:
            self.assertIn(adapter_col, regen_df.columns, f"Internal execution adapter column '{adapter_col}' missing!")

        self.assertEqual(regen_df.iloc[0]["Target"], "https://example.com/login")
        self.assertEqual(regen_df.iloc[0]["_target"], "https://example.com/login")
        self.assertTrue(len(str(regen_df.iloc[0]["Playwright_Code"])) > 0)
        self.assertEqual(regen_df.iloc[0]["_code"], regen_df.iloc[0]["Playwright_Code"])

    def test_expected_result_extraction_without_fallback_override(self):
        """Verify that multi-table QA test sheets accurately extract the actual Expected Result / Expected Outcome
        and never mistakenly overwrite them with 'Action executes cleanly and expected outcome is verified'."""
        raw_csv = """Screen / Function,User Action on Dev Link,Expected Outcome,Use Case ID,Test Case ID,Evidence(failed case)
Super Admin Login Screen,Open the Super Admin login URL without an authenticated session,"Login screen opens with the required branding, Email, Password, Login and Forgot Password controls","UC-SA-LOGIN-01","TC-SA-LOGIN-001",
Existing Authenticated Session,Login successfully and open the Super Admin login URL again,System follows the configured behavior for an already authenticated user,UC-SA-LOGIN-01,TC-SA-LOGIN-013,
,,,,,,,,,,,,,,,,
,Metric,Count,,,,,,,,,,,,,,
,Test Cases Executed,285,,,,,,,,,,,,,,
,Test Pass %,97.2,,,,,,,,,,,,,,
,,,,,,,,,,,,,,,,
Test Case ID,Use Case ID,Date,Module,Test Case Title,Scenario Type,Classification,Test Case Description,Test Steps,Test Data,Expected Result,Actual Result,Priority,Result,Comments,Dev Result,Column 1
TC-SA-LOGIN-001,UC-SA-LOGIN-01,13-09-2026,Super Admin Login,Access Super Admin Login URL,Positive,Functional,Verify dedicated Super Admin login URL loads correctly,1. Open Super Admin URL 2. Wait for page load,Valid Super Admin URL,Login screen loads successfully,Login screen loads successfully,High,Pass,,Pass,
TC-SA-LOGIN-002,UC-SA-LOGIN-01,13-09-2026,Super Admin Login,Verify Login Page Branding,Positive,UI,Verify required School ERP/Super Admin branding is displayed,1. Open login page 2. Observe branding,N/A,Correct branding is displayed,Correct branding is displayed,Medium,Pass,,Pass,
"""
        gen = TestGenerator()
        df = gen.ai_regenerate_and_convert_suite(raw_csv)
        self.assertEqual(len(df), 4)

        # Check Row 1 (from Table 1 Expected Outcome)
        self.assertEqual(df.iloc[0]["Expected_Result"], "Login screen opens with the required branding, Email, Password, Login and Forgot Password controls")
        self.assertEqual(df.iloc[0]["_expected"], "Login screen opens with the required branding, Email, Password, Login and Forgot Password controls")

        # Check Row 2 (from Table 1 Expected Outcome)
        self.assertEqual(df.iloc[1]["Expected_Result"], "System follows the configured behavior for an already authenticated user")

        # Check Row 3 (from Table 2 Expected Result)
        self.assertEqual(df.iloc[2]["Expected_Result"], "Login screen loads successfully")
        self.assertEqual(df.iloc[2]["_expected"], "Login screen loads successfully")

        # Check Row 4 (from Table 2 Expected Result)
        self.assertEqual(df.iloc[3]["Expected_Result"], "Correct branding is displayed")

        # Verify none of the rows received the generic fallback placeholder
        for idx, row in df.iterrows():
            self.assertNotIn("Action executes cleanly and expected outcome is verified", str(row["Expected_Result"]))
            self.assertNotIn("Action executes cleanly and state is verified", str(row["Expected_Result"]))

    def test_spurious_and_empty_columns_pruning_and_multi_sheet_filtering(self):
        # Create multi-sheet data with spurious column (Column 16, empty Unnamed, TC-id headers)
        sheet_a = pd.DataFrame([
            {"Sheet_Name": "Sheet_A", "Test Case ID": "TC-A1", "Screen / Function": "Login", "User Action on Dev Link": "Click", "Expected Outcome": "Success", "Column 16": "", "Unnamed: 4": ""},
            {"Sheet_Name": "Sheet_A", "Test Case ID": "TC-A2", "Screen / Function": "Settings", "User Action on Dev Link": "Toggle", "Expected Outcome": "Saved", "Column 16": None, "Unnamed: 4": ""}
        ])
        sheet_b = pd.DataFrame([
            {"Sheet_Name": "Sheet_B", "Test Case ID": "TC-B1", "Date": "2026-10-27", "Module": "Billing", "Test Steps": "Pay", "Expected Result": "Receipt", "Column 16": ""},
        ])
        combined = pd.concat([sheet_a, sheet_b], ignore_index=True)
        
        # Test spurious column dropping
        spurious_to_drop = []
        import re
        for c in combined.columns:
            c_str = str(c).strip()
            if re.match(r'^(?:unnamed:\s*\d+|column\s*\d+)$', c_str, re.IGNORECASE):
                if combined[c].dropna().astype(str).str.strip().ne('').sum() <= 1:
                    spurious_to_drop.append(c)
        self.assertIn("Column 16", spurious_to_drop)
        self.assertIn("Unnamed: 4", spurious_to_drop)
        cleaned_df = combined.drop(columns=spurious_to_drop)
        self.assertNotIn("Column 16", cleaned_df.columns)
        self.assertNotIn("Unnamed: 4", cleaned_df.columns)

        # Test active columns detection per sheet
        user_cols = [c for c in cleaned_df.columns if c not in ["Sheet_Name", "Run", "Status"]]
        active_a = [c for c in user_cols if cleaned_df[cleaned_df["Sheet_Name"] == "Sheet_A"][c].dropna().astype(str).str.strip().ne("").any()]
        active_b = [c for c in user_cols if cleaned_df[cleaned_df["Sheet_Name"] == "Sheet_B"][c].dropna().astype(str).str.strip().ne("").any()]

        self.assertIn("Screen / Function", active_a)
        self.assertNotIn("Module", active_a)
        self.assertIn("Module", active_b)
        self.assertNotIn("Screen / Function", active_b)

        # Test Multi-Sheet isin filtering
        multi_selected = ["Sheet_A", "Sheet_B"]
        filtered_multi = cleaned_df[cleaned_df["Sheet_Name"].isin(multi_selected)]
        self.assertEqual(len(filtered_multi), 3)

        single_selected = ["Sheet_A"]
        filtered_single = cleaned_df[cleaned_df["Sheet_Name"].isin(single_selected)]
        self.assertEqual(len(filtered_single), 2)

    def test_heterogeneous_multi_sheet_harmonization_fills_all_synonyms(self):
        from core.test_generator import harmonize_qa_dataframe
        sheet_a = pd.DataFrame([
            {"Sheet_Name": "Sheet_A", "Screen / Function": "Login Screen", "User Action on Dev Link": "Enter admin credentials", "Expected Outcome": "Dashboard appears"}
        ])
        sheet_b = pd.DataFrame([
            {"Sheet_Name": "Sheet_B", "Module": "Super Admin Billing", "Test Steps": "Click Invoice and pay", "Expected Result": "Payment Receipt PDF generated"}
        ])
        combined = pd.concat([sheet_a, sheet_b], ignore_index=True)
        harmonized = harmonize_qa_dataframe(combined)
        
        # Sheet_A must have Module, Test Steps, Expected Result populated
        row_a = harmonized[harmonized["Sheet_Name"] == "Sheet_A"].iloc[0]
        self.assertEqual(row_a["Screen / Function"], "Login Screen")
        self.assertEqual(row_a["Module"], "Login Screen")
        self.assertEqual(row_a["User Action on Dev Link"], "Enter admin credentials")
        self.assertEqual(row_a["Test Steps"], "Enter admin credentials")
        self.assertEqual(row_a["Expected Outcome"], "Dashboard appears")
        self.assertEqual(row_a["Expected Result"], "Dashboard appears")

        # Sheet_B must have Screen / Function, User Action on Dev Link, Expected Outcome populated
        row_b = harmonized[harmonized["Sheet_Name"] == "Sheet_B"].iloc[0]
        self.assertEqual(row_b["Screen / Function"], "Super Admin Billing")
        self.assertEqual(row_b["Module"], "Super Admin Billing")
        self.assertEqual(row_b["User Action on Dev Link"], "Click Invoice and pay")
        self.assertEqual(row_b["Test Steps"], "Click Invoice and pay")
        self.assertEqual(row_b["Expected Outcome"], "Payment Receipt PDF generated")
        self.assertEqual(row_b["Expected Result"], "Payment Receipt PDF generated")

    def test_multi_story_qa_spreadsheet_format_parsing(self):
        """Verifies ingestion of combined multi-story QA spreadsheets with descriptive headers and section banners."""
        csv_data = '''
1.1 Revised User Story: Super Admin Web Login & Secure Authentication

#,Heading (one of the 7),Screen / function,"Preconditions (test account, data that must exist first)",Steps: who clicks what on the dev link,Expected result: what they see,Use case IDs,Test case IDs,Build status today (Done / In progress / Not started / Not in M1),Defect ID,Evidence link (screenshot or video),"Environment + build (e.g. dev, build no.)",Executed by,Execution date,"Result (PASS/ Fail,Not in M1 (Partially pass))",Module wise Evidence Link (Screenshot/video)
1,SaaS Super Admin (partial),Login Page,Valid Super Admin URL; N/A,QA / authorized tester: Open School ERP dev environment,Login screen loads successfully; Email field is visible,UC-SA-LOGIN-01,TC-SA-LOGIN-001,Done: 13,,,Dev (build 1),Prakash Tiwari,21/10/2026,Pass,
2,SaaS Super Admin (partial),Authentication,Email=""; Password="",QA / authorized tester: Navigate to Authentication and submit,Email is required displayed,UC-SA-LOGIN-04,TC-SA-LOGIN-014,Done: 30,,,Dev (build 1),Prakash Tiwari,21/10/2026,Pass,

1.2 Revised User Story: Multi-Role Web & Mobile Login and Authentication

#,Heading (one of the 7),Screen / function,"Preconditions (test account, data that must exist first)",Steps: who clicks what on the dev link,Expected result: what they see,Use case IDs,Test case IDs,Build status today (Done / In progress / Not started / Not in M1),Defect ID,Evidence link (screenshot or video),"Environment + build (e.g. dev, build no.)",Executed by,Execution date,"Result (PASS/ Fail,Not in M1 (Partially pass))",Module wise Evidence Link (Screenshot/video)
1,Authentication and JWT session management,Authentication,Authorized test account,Tester: Open application -> Navigate to Login,Login screen is displayed and authentication service is available,UC-LOGIN-001,TC-LOGIN-001,Done: 6,,,Dev (build 1),Prakash Tiwari,21/10/2026,Pass,
'''
        df = parse_qa_test_sheet_to_dataframe(csv_data, default_target="https://dev.schoolerp.com")
        self.assertEqual(len(df), 3)

        # Check Sheet_Name attribution
        sheets = df["Sheet_Name"].unique().tolist()
        self.assertIn("1.1 Revised User Story: Super Admin Web Login & Secure Authentication", sheets)
        self.assertIn("1.2 Revised User Story: Multi-Role Web & Mobile Login and Authentication", sheets)

        # Check column mapping
        row1 = df.iloc[0]
        self.assertIn("QA / authorized tester: Open School ERP dev environment", row1["Test_Step_Description"])
        self.assertEqual(row1["Steps: who clicks what on the dev link"], "QA / authorized tester: Open School ERP dev environment")
        self.assertEqual(row1["Expected_Result"], "Login screen loads successfully; Email field is visible")
        self.assertEqual(row1["Target"], "https://dev.schoolerp.com")
        self.assertIn("TC-SA-LOGIN-001", row1["Execution_Notes"])
        self.assertIn("Prakash Tiwari", row1["Execution_Notes"])
        self.assertEqual(row1["Status"], "PENDING")

        # Test normalization preserves sheets
        norm_df = normalize_test_dataframe(df, default_target="https://dev.schoolerp.com")
        self.assertEqual(len(norm_df), 3)
        self.assertEqual(norm_df["Sheet_Name"].nunique(), 2)

    def test_excel_export_sanitizes_forbidden_characters_and_lengths(self):
        from app import sanitize_excel_sheet_name
        import io

        used = set()
        s1 = sanitize_excel_sheet_name("1.1 Revised User Story: Super Admin Web Login & Secure Authentication", used)
        s2 = sanitize_excel_sheet_name("1.2 Revised User Story: Multi-Role Web / Mobile Login", used)
        s3 = sanitize_excel_sheet_name("What is this? [Testing] *Special* / Case\\", used)

        self.assertNotIn(":", s1)
        self.assertNotIn("/", s2)
        self.assertNotIn("?", s3)
        self.assertNotIn("[", s3)
        self.assertNotIn("]", s3)
        self.assertNotIn("*", s3)
        self.assertNotIn("\\", s3)
        self.assertLessEqual(len(s1), 31)
        self.assertLessEqual(len(s2), 31)
        self.assertLessEqual(len(s3), 31)

        # Test writing a real Excel workbook with openpyxl without ValueError
        df = pd.DataFrame([
            {"Sheet_Name": "1.1 User Story: Super Admin", "Test": "Case 1"},
            {"Sheet_Name": "1.2 User Story: Multi-Role [Admin/User]", "Test": "Case 2"},
        ])
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            used_names = set()
            for name, grp in df.groupby("Sheet_Name"):
                c_name = sanitize_excel_sheet_name(name, used_names)
                grp.to_excel(writer, index=False, sheet_name=c_name)

        buf.seek(0)
        xl = pd.ExcelFile(buf)
        self.assertEqual(len(xl.sheet_names), 2)


if __name__ == "__main__":
    unittest.main()
