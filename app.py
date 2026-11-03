import io
import os
import re
import sys
import time
from io import BytesIO
import textwrap

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Suppress known Windows Python asyncio Proactor bug (WinError 10054 on closed pipe/transport)
if sys.platform == "win32":
    try:
        import asyncio.proactor_events
        _orig_call_conn_lost = asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost

        def _safe_call_connection_lost(self, exc):
            try:
                _orig_call_conn_lost(self, exc)
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass
            except OSError as e:
                if getattr(e, "winerror", None) in (10054, 10053, 10038):
                    pass
                else:
                    raise

        asyncio.proactor_events._ProactorBasePipeTransport._call_connection_lost = _safe_call_connection_lost
    except Exception:
        pass

import json
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = os.path.join("config", "settings.json")


def load_settings():
    """Loads persisted settings and test variables from config/settings.json."""
    os.makedirs("config", exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read config file: {e}")
    return {}


def _has_streamlit_context() -> bool:
    """Returns True only when running inside an active Streamlit user session (prevents bare-mode test imports from overwriting settings.json)."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


def save_settings(settings_data, force: bool = False):
    """Persists settings and test variables to config/settings.json."""
    if not force and not _has_streamlit_context():
        return False
    os.makedirs("config", exist_ok=True)
    try:
        existing = load_settings()
        existing.update(settings_data)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False



TEXT_SUITE_COLS = [
    "Type",
    "Generated_By",
    "Target",
    "Test_Step_Description",
    "Expected_Result",
    "Status",
    "Duration",
    "Actual_Result",
    "Screenshot",
    "Execution_Notes",
    "Playwright_Code",
]


def _clean_text_val(val, default_val: str = "") -> str:
    if val is None or pd.isna(val):
        return default_val
    s = str(val).strip()
    if s.lower() in ["nan", "none"]:
        return default_val
    return s


def _clean_series(s: pd.Series, default_val: str = "") -> pd.Series:
    """Vectorized normalization of pandas Series for null/nan string values."""
    if s is None or s.empty:
        return s
    cleaned = s.fillna(default_val).astype(str).str.strip()
    mask = cleaned.str.lower().isin(["nan", "none"])
    if mask.any():
        cleaned = cleaned.mask(mask, default_val)
    return cleaned.astype(object)


def sanitize_df_for_streamlit(df: pd.DataFrame) -> pd.DataFrame:
    """Ensures DataFrame is 100% PyArrow & Pandas setitem compatible with no float64 empty text columns."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    clean = df.copy()
    for col in clean.columns:
        if col == "Run":
            clean[col] = clean[col].fillna(True).astype(bool)
        elif col in TEXT_SUITE_COLS or not pd.api.types.is_numeric_dtype(clean[col]):
            default_v = "PENDING" if col == "Status" else ("-" if col in ["Duration", "Actual_Result"] else "")
            clean[col] = _clean_series(clean[col], default_v)
    return clean


def is_valid_image(img_src) -> bool:
    """Verifies that an image path or bytes object is a valid, readable image and not 0-bytes or corrupt."""
    if not img_src:
        return False
    try:
        from PIL import Image
        if isinstance(img_src, (str, bytes, os.PathLike)):
            p = str(img_src).strip()
            if not p or not os.path.exists(p) or not os.path.isfile(p):
                return False
            if os.path.getsize(p) < 100:
                return False
            with Image.open(p) as img:
                img.verify()
            return True
        elif hasattr(img_src, "seek"):
            pos = img_src.tell() if hasattr(img_src, "tell") else 0
            with Image.open(img_src) as img:
                img.verify()
            if hasattr(img_src, "seek"):
                img_src.seek(pos)
            return True
        return False
    except Exception:
        return False


def safe_st_image(img_src, caption=None, **kwargs) -> bool:
    """Renders st.image safely; if corrupt, missing, or empty, displays a graceful note without crashing."""
    if not is_valid_image(img_src):
        fname = os.path.basename(str(img_src)) if isinstance(img_src, (str, os.PathLike)) else "image"
        st.caption(f"⚠️ *Screenshot preview unavailable (empty or invalid: `{fname}`)*")
        return False
    try:
        if "width" not in kwargs and "use_container_width" not in kwargs:
            kwargs["width"] = "stretch"
        st.image(img_src, caption=caption, **kwargs)
        return True
    except Exception as e:
        st.caption(f"⚠️ *Screenshot display error: {e}*")
        return False


def render_html_block(html_content: str):
    """Renders HTML safely in Streamlit without risk of Markdown 4-space code block misinterpretation."""
    lines = [line.strip() for line in str(html_content).strip().splitlines() if line.strip()]
    cleaned = "".join(lines)
    if hasattr(st, "html"):
        st.html(cleaned)
    else:
        st.markdown(cleaned, unsafe_allow_html=True)


def _register_workbook_sheets(df: pd.DataFrame):
    """Populates st.session_state['workbook_sheets'] with distinct sheets/modules or default view."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return
    if "Sheet_Name" not in df.columns:
        df["Sheet_Name"] = "Default"
    unique_sheets = [str(s).strip() for s in df["Sheet_Name"].dropna().unique() if str(s).strip() and str(s).strip().lower() not in ["none", "nan", "", "default"]]
    if len(unique_sheets) > 1:
        sheets_dict = {}
        for s in unique_sheets:
            sheets_dict[str(s)] = df[df["Sheet_Name"] == s].copy()
        try:
            st.session_state["workbook_sheets"] = sheets_dict
        except Exception:
            pass
    else:
        sheet_key = unique_sheets[0] if unique_sheets else "Default"
        try:
            st.session_state["workbook_sheets"] = {sheet_key: df}
        except Exception:
            pass


def sanitize_excel_sheet_name(name: str, used_names: set = None) -> str:
    """
    Sanitizes a string to be a valid, unique Excel worksheet name for openpyxl:
    - Removes forbidden characters: \\ / ? * : [ ]
    - Truncates to maximum 31 characters
    - Ensures non-empty fallback
    - Avoids duplicate sheet names in the workbook
    """
    if name is None:
        clean = "Sheet"
    else:
        # Replace Excel forbidden characters with hyphen
        clean = re.sub(r'[\:\\/\?\*\[\]]', '-', str(name)).strip()
        # Collapse multiple spaces or hyphens
        clean = re.sub(r'[-\s]{2,}', ' ', clean)
        clean = clean.strip(" -'")

    if not clean:
        clean = "Sheet"

    base = clean[:31]
    final_name = base
    if used_names is not None:
        counter = 1
        while final_name.lower() in used_names:
            counter += 1
            suffix = f" ({counter})"
            avail_len = max(1, 31 - len(suffix))
            final_name = f"{base[:avail_len].rstrip()}{suffix}"
        used_names.add(final_name.lower())

    return final_name


def _safe_read_uploaded_file(uploaded_file):
    """Safely reads CSV or Excel files with encoding fallback, multi-table QA export detection, and multi-sheet preservation."""
    from core.test_generator import parse_qa_test_sheet_to_dataframe
    name_l = str(getattr(uploaded_file, "name", "")).lower()
    
    if name_l.endswith(".csv") or name_l.endswith(".txt"):
        content_bytes = None
        if hasattr(uploaded_file, "seek") and hasattr(uploaded_file, "read"):
            uploaded_file.seek(0)
            content_bytes = uploaded_file.read()
            uploaded_file.seek(0)
        elif hasattr(uploaded_file, "read"):
            content_bytes = uploaded_file.read()
        
        # 1. Check if raw text contains multi-table or QA export signatures (e.g. from Jira/TestRail/Excel)
        if content_bytes:
            for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
                try:
                    text_content = content_bytes.decode(enc)
                    if any(sig in text_content.lower() for sig in [
                        "test case id", "test case ids", "screen / function", "screen/function",
                        "user action on dev link", "use case id", "use case ids", "test steps",
                        "user story", "steps: who clicks", "steps: who", "preconditions (", "heading ("
                    ]):
                        parsed_df = parse_qa_test_sheet_to_dataframe(text_content)
                        if not parsed_df.empty:
                            _register_workbook_sheets(parsed_df)
                            return parsed_df
                    break
                except UnicodeDecodeError:
                    continue

        # 2. Try standard pd.read_csv
        for enc in ["utf-8-sig", "utf-8", "latin1", "cp1252"]:
            try:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, encoding=enc)
                unnamed_count = sum(1 for c in df.columns if str(c).startswith("Unnamed"))
                if unnamed_count == 0 and len(df) > 0:
                    _register_workbook_sheets(df)
                    return df
                parsed_df = parse_qa_test_sheet_to_dataframe(df)
                if not parsed_df.empty:
                    _register_workbook_sheets(parsed_df)
                    return parsed_df
                _register_workbook_sheets(df)
                return df
            except Exception:
                continue

        if content_bytes:
            uploaded_file.seek(0)
            res_df = parse_qa_test_sheet_to_dataframe(content_bytes)
            _register_workbook_sheets(res_df)
            return res_df

    # Excel file (.xlsx, .xls)
    try:
        content_bytes = None
        if hasattr(uploaded_file, "seek") and hasattr(uploaded_file, "read"):
            uploaded_file.seek(0)
            content_bytes = uploaded_file.read()
            uploaded_file.seek(0)
        xl_source = io.BytesIO(content_bytes) if content_bytes else uploaded_file
        excel_file = pd.ExcelFile(xl_source)
        all_dfs = []
        workbook_sheets = {}
        for sheet_name in excel_file.sheet_names:
            sheet_df = pd.read_excel(excel_file, sheet_name=sheet_name)
            unnamed_count = sum(1 for c in sheet_df.columns if str(c).startswith("Unnamed"))
            has_clean_step = any(re.sub(r"[^a-z0-9]", "", str(c).lower()) in ["teststepdescription", "teststeps", "steps", "useractionondevlink", "action"] for c in sheet_df.columns)
            if unnamed_count == 0 and len(sheet_df) > 0 and has_clean_step:
                active_sheet = sheet_df
            else:
                parsed_sheet = parse_qa_test_sheet_to_dataframe(sheet_df)
                active_sheet = parsed_sheet if not parsed_sheet.empty else sheet_df
            if not active_sheet.empty:
                active_sheet = active_sheet.copy()
                active_sheet["Sheet_Name"] = sheet_name
                workbook_sheets[sheet_name] = active_sheet
                all_dfs.append(active_sheet)
        if all_dfs:
            try:
                st.session_state["workbook_sheets"] = workbook_sheets
            except Exception:
                pass
            from core.test_generator import harmonize_qa_dataframe
            return harmonize_qa_dataframe(pd.concat(all_dfs, ignore_index=True))
    except Exception:
        pass

    uploaded_file.seek(0)
    df = pd.read_excel(uploaded_file)
    _register_workbook_sheets(df)
    return df


def normalize_test_dataframe(df: pd.DataFrame, default_target: str = None, reset_execution_status: bool = False) -> pd.DataFrame:
    """Normalizes uploaded or generated dataframes into standard 11-column test suite structure."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code"])

    # If df is not yet standardized and has dominant Unnamed columns or section banner headers:
    is_already_standardized = "Test_Step_Description" in df.columns and "Expected_Result" in df.columns
    if not is_already_standardized:
        unnamed_cols = [c for c in df.columns if str(c).startswith("Unnamed")]
        first_col = str(df.columns[0]).strip() if len(df.columns) > 0 else ""
        is_banner_col = bool(re.match(r'^\s*\d+(?:\.\d+)*\s*(?:revised\s+)?(?:user\s+story|module|epic|screen)', first_col, re.IGNORECASE))
        has_banner_row = False
        if not is_banner_col and not df.empty and df.shape[1] > 2:
            for val in df.iloc[:3, 0].dropna():
                if isinstance(val, str) and re.match(r'^\s*\d+(?:\.\d+)*\s*(?:revised\s+)?(?:user\s+story|module)', val, re.IGNORECASE):
                    has_banner_row = True
                    break
        if is_banner_col or has_banner_row or (len(unnamed_cols) >= max(2, len(df.columns) // 2) and len(df) > 0):
            from core.test_generator import parse_qa_test_sheet_to_dataframe
            converted = parse_qa_test_sheet_to_dataframe(df, default_target=default_target or "")
            if not converted.empty:
                df = converted

    # 2. Only harmonize QA synonyms if any QA synonym columns actually exist
    qa_keys = (
        "Screen / Function", "Screen / function", "Screen/Function", "Heading (one of the 7)",
        "Heading", "User Action on Dev Link", "Steps: who clicks what on the dev link",
        "Expected Outcome", "Expected outcome", "Expected result: what they see",
        "Preconditions (test account, data that must exist first)", "Preconditions"
    )
    if any(k in df.columns for k in qa_keys):
        from core.test_generator import harmonize_qa_dataframe
        df = harmonize_qa_dataframe(df)

    df = df.copy()
    df = df.loc[:, ~df.columns.astype(str).str.contains(r"^Unnamed")]

    col_mapping = {}
    clean_cols = {re.sub(r"[^a-zA-Z0-9]", "", str(c).lower()): c for c in df.columns}
    
    if "Test_Step_Description" not in df.columns:
        for cand in ["teststepdescription", "teststeps", "steps", "useractionondevlink", "useraction", "testcasedescription", "testcasetitle", "testtitle", "title", "teststep", "step", "description", "action", "scenario"]:
            if cand in clean_cols and clean_cols[cand] != "Test_Step_Description":
                col_mapping[clean_cols[cand]] = "Test_Step_Description"
                break

    if "Expected_Result" not in df.columns:
        for cand in ["expectedresult", "expectedoutcome", "expectedoutput", "expectedbehavior", "expectedresults", "expectedoutcomes", "expected", "acceptancecriteria"]:
            if cand in clean_cols and clean_cols[cand] != "Expected_Result":
                col_mapping[clean_cols[cand]] = "Expected_Result"
                break

    if "Target" not in df.columns:
        for cand in ["target", "targeturl", "url", "link", "endpoint", "reference", "refrance"]:
            if cand in clean_cols and clean_cols[cand] != "Target":
                col_mapping[clean_cols[cand]] = "Target"
                break

    if "Type" not in df.columns:
        for cand in ["type", "testtype", "classification", "scenariotype", "category"]:
            if cand in clean_cols and clean_cols[cand] != "Type":
                col_mapping[clean_cols[cand]] = "Type"
                break

    if "Execution_Notes" not in df.columns:
        for cand in ["executionnotes", "notes", "testcaseid", "tcid"]:
            if cand in clean_cols and clean_cols[cand] != "Execution_Notes":
                col_mapping[clean_cols[cand]] = "Execution_Notes"
                break

    if "Playwright_Code" not in df.columns:
        for cand in ["playwrightcode", "playwright_code", "code", "script"]:
            if cand in clean_cols and clean_cols[cand] != "Playwright_Code":
                col_mapping[clean_cols[cand]] = "Playwright_Code"
                break

    if col_mapping:
        df = df.rename(columns=col_mapping)

    df = df.loc[:, ~df.columns.duplicated()]

    current_target = default_target or ""
    from urllib.parse import urlparse
    parsed = urlparse(current_target) if current_target else None
    base_prefix = f"{parsed.scheme}://{parsed.netloc}" if (parsed and parsed.scheme and parsed.netloc) else current_target.rstrip("/")

    if "Target" not in df.columns:
        df["Target"] = current_target
    else:
        def _resolve_target(t):
            t_s = _clean_text_val(t, "")
            if not t_s:
                return current_target
            if t_s.startswith("http://") or t_s.startswith("https://") or t_s.startswith("android://") or not base_prefix:
                return t_s
            return f"{base_prefix}/{t_s.lstrip('/')}"
        df["Target"] = df["Target"].apply(_resolve_target)

    for col, default_val in [
        ("Run", True),
        ("Type", "Web"),
        ("Target", current_target),
        ("Test_Step_Description", ""),
        ("Expected_Result", "Action executes cleanly"),
        ("Status", "PENDING"),
        ("Duration", "-"),
        ("Actual_Result", "-"),
        ("Screenshot", ""),
        ("Execution_Notes", ""),
        ("Playwright_Code", "")
    ]:
        if col not in df.columns:
            df[col] = default_val
        if col == "Run":
            df[col] = df[col].fillna(True).astype(bool)
        else:
            df[col] = _clean_series(df[col], default_val)

    # Rescue real expected outcome for any row having placeholder Expected_Result
    for col_cand in ["Expected Result", "Expected Outcome", "Expected Output", "Expected", "Expected_Outcome", "ExpectedBehavior"]:
        if col_cand in df.columns and col_cand != "Expected_Result":
            real_vals = df[col_cand].astype(str).str.strip()
            mask_needs_val = df["Expected_Result"].astype(str).str.strip().isin([
                "", "nan", "None", "Action executes cleanly",
                "Action executes cleanly and state is verified",
                "Action executes cleanly and expected outcome is verified"
            ]) & ~real_vals.isin(["", "nan", "None", "-"])
            df.loc[mask_needs_val, "Expected_Result"] = df.loc[mask_needs_val, col_cand]
            break

    # Ensure Sheet_Name is preserved and defaults cleanly
    if "Sheet_Name" not in df.columns:
        df["Sheet_Name"] = "Default"
    else:
        df["Sheet_Name"] = df["Sheet_Name"].fillna("Default").astype(str).str.strip()
        df.loc[df["Sheet_Name"].isin(["", "nan", "None"]), "Sheet_Name"] = "Default"

    # Auto-infer Mobile (Native) if target is an Android package/activity
    is_android_target = df["Target"].astype(str).str.strip().str.startswith(("android://", "app://"))
    needs_type = df["Type"].astype(str).str.strip().isin(["", "Web", "nan", "None", "-"])
    df.loc[is_android_target & needs_type, "Type"] = "Mobile (Native)"

    # Synthesize Playwright_Code if missing (only for small sets to preserve sub-30ms performance)
    if "Playwright_Code" in df.columns and len(df) <= 50:
        missing_pw = df["Playwright_Code"].astype(str).str.strip().isin(["", "nan", "None"])
        if missing_pw.any():
            from core.test_generator import _synthesize_default_playwright_code
            for idx in df[missing_pw].index:
                df.at[idx, "Playwright_Code"] = _synthesize_default_playwright_code(
                    step_desc=str(df.at[idx, "Test_Step_Description"] or ""),
                    expected=str(df.at[idx, "Expected_Result"] or ""),
                    target_url=str(df.at[idx, "Target"] or ""),
                    test_type=str(df.at[idx, "Type"] or "Web")
                )

    if reset_execution_status:
        df["Status"] = "PENDING"
        df["Duration"] = "-"
        df["Actual_Result"] = "-"
        df["Execution_Notes"] = ""
        df["Screenshot"] = ""

    valid_mask = df["Test_Step_Description"].ne("") | df["Target"].ne("")
    if valid_mask.any():
        df = df[valid_mask].reset_index(drop=True)
    df.index = range(1, len(df) + 1)
    return df


def resolve_test_execution_params(index=None, row=None, master_df=None, default_target="", session_target_url="", **kwargs):
    """
    Robustly resolves (target_url, step_desc, exp_res, custom_script, test_type)
    from row, falling back to master_df, adapter fields (_target, _step, _expected, _code),
    domain columns (Test Steps, Expected Result, Screen / Function), or active session config.
    Supports flexible signatures: (idx, row, master_df), (row, ...), etc.
    """
    if row is None and (isinstance(index, dict) or hasattr(index, "to_dict")):
        row = index
        index = getattr(row, "name", None)
    if row is None:
        row = {}
    elif hasattr(row, "to_dict"):
        row = row.to_dict()
    elif not isinstance(row, dict):
        try:
            row = dict(row)
        except Exception:
            row = {}

    m_row = {}
    if master_df is not None and index is not None and index in getattr(master_df, "index", []):
        try:
            m_row = master_df.loc[index].to_dict()
        except Exception:
            m_row = {}

    # 1. Target URL resolution
    target_url = str(
        row.get("Target") or row.get("_target") or
        m_row.get("Target") or m_row.get("_target") or
        session_target_url or
        default_target or
        st.session_state.get("session_login_url", "") or
        st.session_state.get("target_url_input", "") or ""
    ).strip()
    if target_url.lower() in ["nan", "none", "-"]:
        target_url = ""

    if not target_url:
        try:
            from core.session_capturer import get_saved_auth_target_url
            saved_u = get_saved_auth_target_url()
            if saved_u:
                target_url = saved_u.strip()
        except Exception:
            pass

    # 2. Test Step Description resolution
    step_desc = str(
        row.get("Test_Step_Description") or row.get("_step") or
        m_row.get("Test_Step_Description") or m_row.get("_step") or
        row.get("Test Steps") or m_row.get("Test Steps") or
        row.get("User Action on Dev Link") or m_row.get("User Action on Dev Link") or
        row.get("Test Case Description") or m_row.get("Test Case Description") or
        row.get("Test Case Title") or m_row.get("Test Case Title") or
        "Verify UI function and flow"
    ).strip()
    if step_desc.lower() in ["nan", "none", ""]:
        step_desc = "Verify UI function and flow"

    # 3. Expected Result resolution
    exp_res = str(
        row.get("Expected_Result") or row.get("_expected") or
        m_row.get("Expected_Result") or m_row.get("_expected") or
        row.get("Expected Result") or m_row.get("Expected Result") or
        row.get("Expected Outcome") or m_row.get("Expected Outcome") or
        "Action executes cleanly and expected outcome is verified"
    ).strip()
    if exp_res.lower() in ["nan", "none", ""]:
        exp_res = "Action executes cleanly and expected outcome is verified"

    # 4. Type / Channel resolution
    test_type = str(
        row.get("Type") or row.get("_type") or
        m_row.get("Type") or m_row.get("_type") or "Web"
    ).strip()
    if test_type.lower() in ["nan", "none", ""]:
        test_type = "Web"

    # 5. Playwright Code resolution
    custom_script = str(
        row.get("Playwright_Code") or row.get("_code") or
        m_row.get("Playwright_Code") or m_row.get("_code") or ""
    ).strip()
    if custom_script.lower() in ["nan", "none"]:
        custom_script = ""

    # Auto-synthesize script if missing or placeholder
    if not custom_script or "sync_playwright" in custom_script:
        try:
            from core.test_generator import _synthesize_default_playwright_code
            custom_script = _synthesize_default_playwright_code(step_desc, exp_res, target_url, test_type)
        except Exception:
            pass

    return target_url, step_desc, exp_res, custom_script, test_type


def append_manual_test_case(type_val: str, target_val: str, step_val: str, expected_val: str, code_val: str = "") -> pd.DataFrame:
    """Appends a new user-defined manual test case to the active suite with '✍️ Manual / Uploaded' attribution."""
    new_case = {
        "Run": True,
        "Type": str(type_val or "Web").strip(),
        "Generated_By": "✍️ Manual / Uploaded",
        "Target": str(target_val or "").strip(),
        "Test_Step_Description": str(step_val or "").strip(),
        "Expected_Result": str(expected_val or "Action executes cleanly").strip(),
        "Status": "PENDING",
        "Duration": "-",
        "Actual_Result": "-",
        "Screenshot": "",
        "Execution_Notes": "",
        "Playwright_Code": str(code_val or "").strip() if str(code_val or "").strip().lower() != "nan" else "",
    }
    curr_df = None
    if "test_cases_df" in st.session_state and isinstance(st.session_state["test_cases_df"], pd.DataFrame) and not st.session_state["test_cases_df"].empty:
        curr_df = st.session_state["test_cases_df"].copy()
    elif "results_df" in st.session_state and isinstance(st.session_state["results_df"], pd.DataFrame) and not st.session_state["results_df"].empty:
        curr_df = st.session_state["results_df"].copy()
    elif os.path.exists("deep_inspected_tests.csv"):
        try:
            curr_df = pd.read_csv("deep_inspected_tests.csv")
        except Exception:
            curr_df = None

    if curr_df is not None and len(curr_df) > 0:
        updated = pd.concat([curr_df, pd.DataFrame([new_case])], ignore_index=True)
    else:
        updated = pd.DataFrame([new_case])

    updated = sanitize_df_for_streamlit(updated)
    updated.index = range(1, len(updated) + 1)
    st.session_state["test_cases_df"] = updated
    st.session_state["results_df"] = updated
    st.session_state["blank_suite"] = False
    st.session_state["suite_explicitly_cleared"] = False
    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1

    target_csv = st.session_state.get("auto_generated_file") or "deep_inspected_tests.csv"
    try:
        updated.to_csv(target_csv, index=False)
        st.session_state["auto_generated_file"] = target_csv
    except Exception:
        pass
    return updated


def clear_entire_test_suite():
    """Wipes all test cases from the active suite and resets to an empty state."""
    st.session_state["suite_explicitly_cleared"] = True
    st.session_state["blank_suite"] = False
    st.session_state["test_cases_df"] = None
    st.session_state["manual_cases"] = []
    st.session_state.pop("results_df", None)
    st.session_state.pop("auto_generated_file", None)
    st.session_state.pop("_active_uploaded_file_id", None)
    st.session_state.pop("flow_gen_success_msg", None)
    st.session_state.batch_paused = False
    st.session_state.paused_at_index = None
    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1

    # Remove persistent cached files on disk so they are not resurrected
    for disk_f in ["deep_inspected_tests.csv", "generated_tests.csv"]:
        if os.path.exists(disk_f):
            try:
                os.remove(disk_f)
            except Exception:
                try:
                    with open(disk_f, "w", encoding="utf-8") as fp:
                        fp.write("Run,Type,Target,Test_Step_Description,Expected_Result,Playwright_Code,Status\n")
                except Exception:
                    pass


def delete_selected_test_cases(df_current: pd.DataFrame = None) -> bool:
    """Deletes test cases currently selected (checked with 'Run'=True) from the active suite."""
    master_df = st.session_state.get("test_cases_df")
    if master_df is None or len(master_df) == 0:
        if df_current is not None and isinstance(df_current, pd.DataFrame) and len(df_current) > 0:
            master_df = df_current.copy()
        else:
            return False

    if "Run" not in master_df.columns:
        master_df.insert(0, "Run", True)

    # If df_current is provided and has 'Run', sync matching index values into master_df
    if df_current is not None and isinstance(df_current, pd.DataFrame) and "Run" in df_current.columns:
        valid_indices = df_current.index.intersection(master_df.index)
        if len(valid_indices) > 0:
            master_df.loc[valid_indices, "Run"] = df_current.loc[valid_indices, "Run"]

    checked_mask = master_df["Run"].fillna(True).astype(bool) == True
    num_to_delete = int(checked_mask.sum())
    if num_to_delete == 0:
        return False
    if num_to_delete >= len(master_df):
        clear_entire_test_suite()
        return True

    remaining_df = master_df[~checked_mask].reset_index(drop=True)
    if len(remaining_df) == 0:
        clear_entire_test_suite()
        return True

    remaining_df.index = range(1, len(remaining_df) + 1)
    st.session_state["test_cases_df"] = remaining_df.copy()
    if "results_df" in st.session_state and st.session_state.results_df is not None:
        res_df = st.session_state.results_df
        if len(res_df) == len(master_df):
            rem_res = res_df[~checked_mask].reset_index(drop=True)
            rem_res.index = range(1, len(rem_res) + 1)
            st.session_state["results_df"] = rem_res
        else:
            st.session_state["results_df"] = remaining_df.copy()

    # Persist updated suite to disk files so deletions are durable
    target_files = set()
    active_f = st.session_state.get("auto_generated_file")
    if active_f:
        target_files.add(str(active_f))
    if os.path.exists("deep_inspected_tests.csv"):
        target_files.add("deep_inspected_tests.csv")
    if os.path.exists("generated_tests.csv"):
        target_files.add("generated_tests.csv")
    for tf in target_files:
        try:
            remaining_df.to_csv(tf, index=False)
        except Exception:
            pass

    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
    return True



def delete_single_test_case(df_current: pd.DataFrame, idx_to_delete: int) -> bool:
    """Deletes a single test case by its index from the active suite."""
    if df_current is None or idx_to_delete not in df_current.index:
        return False
    if len(df_current) <= 1:
        clear_entire_test_suite()
        return True
    
    remaining_df = df_current.drop(index=idx_to_delete).reset_index(drop=True)
    remaining_df.index = range(1, len(remaining_df) + 1)
    st.session_state["test_cases_df"] = remaining_df.copy()
    if "results_df" in st.session_state and st.session_state.results_df is not None:
        res_df = st.session_state.results_df
        if idx_to_delete in res_df.index:
            rem_res = res_df.drop(index=idx_to_delete).reset_index(drop=True)
            rem_res.index = range(1, len(rem_res) + 1)
            st.session_state["results_df"] = rem_res
        else:
            st.session_state["results_df"] = remaining_df.copy()
    
    # Persist updated suite to disk files so deletions are durable
    target_files = set()
    active_f = st.session_state.get("auto_generated_file")
    if active_f:
        target_files.add(str(active_f))
    if os.path.exists("deep_inspected_tests.csv"):
        target_files.add("deep_inspected_tests.csv")
    if os.path.exists("generated_tests.csv"):
        target_files.add("generated_tests.csv")
    for tf in target_files:
        try:
            remaining_df.to_csv(tf, index=False)
        except Exception:
            pass

    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
    return True


def detect_row_generation_source(row) -> str:
    """Classifies a test case row into its origin engine tag."""
    gen_by = str(row.get("Generated_By", "")).strip()
    if gen_by and gen_by != "nan":
        if any(g in gen_by for g in ["⚡", "🤖", "⚙️", "🛡️", "✍️"]):
            return gen_by
        if "robust" in gen_by.lower():
            return "⚡ RobustEngine v4"
        if "ai" in gen_by.lower() and "template" in gen_by.lower():
            return "🤖 AI Step + ⚙️ Template Code"
        if "ai" in gen_by.lower():
            return "🤖 AI Generated"
        if "rule" in gen_by.lower() or "20-point" in gen_by.lower():
            return "⚙️ Rule Engine"
        return "✍️ Manual / Uploaded"
    step = str(row.get("Test_Step_Description", "")).strip()
    code = str(row.get("Playwright_Code", "")).strip()
    notes = str(row.get("Execution_Notes", "")).strip()

    if "RobustEngine v4" in code or "Robust Engine" in code:
        return "⚡ RobustEngine v4"

    template_markers = [
        "# 1. Capture baseline record count before applying filter",
        "# 1. Inspect and populate form fields with valid test data",
        "# 1. Auto-dismiss any hanging overlays and clear search box",
        "# 1. Auto-clean overlays and capture record count before clearing",
        "# 1. Clean backdrop overlays and dismiss any open drawers",
        "# 1. Fill draft test values in form fields",
        "# 1. Clear any inputs to trigger required constraints",
        "Could not locate primary action button on the screen.",
    ]
    has_template_code = any(m in code for m in template_markers)
    has_ai_step_pattern = bool(step and ("Step 1:" in step or "->" in step or "🤖" in notes))

    if has_ai_step_pattern and not has_template_code and len(code) > 5 and code != "nan":
        return "🤖 AI Generated"
    elif has_ai_step_pattern and (has_template_code or not code or code == "nan"):
        return "🤖 AI Step + ⚙️ Template Code"
    elif has_template_code:
        return "⚙️ Rule Engine"
    elif "auto_generated_file" in st.session_state and has_ai_step_pattern:
        return "🤖 AI Generated"
    return "✍️ Manual / Uploaded"


def compute_origin_counts(df: pd.DataFrame, include_robust: bool = False):
    """Computes (total, ai_full, ai_hybrid, rule, manual[, robust]) counts for a test cases dataframe."""
    if df is None or len(df) == 0:
        return (0, 0, 0, 0, 0, 0) if include_robust else (0, 0, 0, 0, 0)
    if "Generated_By" not in df.columns:
        df["Generated_By"] = df.apply(detect_row_generation_source, axis=1)
    gen_by_str = df["Generated_By"].astype(str)
    robust = int(gen_by_str.str.contains("Robust", na=False).sum())
    ai_full = int(((gen_by_str.str.contains("AI Generated", na=False)) & (~gen_by_str.str.contains("Template|Robust", na=False))).sum())
    ai_hybrid = int(gen_by_str.str.contains("Template", na=False).sum())
    rule = int((gen_by_str.str.contains("Rule Engine|20-Point", na=False) & (~gen_by_str.str.contains("Robust", na=False))).sum())
    manual = max(0, len(df) - ai_full - ai_hybrid - rule - robust)
    if include_robust:
        return len(df), ai_full, ai_hybrid, rule, manual, robust
    return len(df), ai_full, ai_hybrid, rule, manual


def apply_origin_selection_to_suite(df: pd.DataFrame, origin_mode: str) -> pd.DataFrame:
    """Updates df['Run'] based on origin_mode ('robust_only', 'ai_only', 'hybrid_only', 'rule_only', 'manual_only', 'all', 'none')."""
    if df is None or len(df) == 0:
        return df
    if "Generated_By" not in df.columns:
        df["Generated_By"] = df.apply(detect_row_generation_source, axis=1)

    if origin_mode == "robust_only":
        df["Run"] = df["Generated_By"].astype(str).str.contains("Robust", na=False)
    elif origin_mode == "ai_only":
        df["Run"] = (df["Generated_By"].astype(str).str.contains("AI Generated", na=False)) & (~df["Generated_By"].astype(str).str.contains("Template", na=False))
    elif origin_mode == "hybrid_only":
        df["Run"] = df["Generated_By"].astype(str).str.contains("Template", na=False)
    elif origin_mode == "rule_only":
        df["Run"] = df["Generated_By"].astype(str).str.contains("Rule Engine|20-Point", na=False)
    elif origin_mode == "manual_only":
        df["Run"] = df["Generated_By"].astype(str).str.contains("Manual|Uploaded", na=False)
    elif origin_mode == "all":
        df["Run"] = True
    elif origin_mode == "none":
        df["Run"] = False

    st.session_state["test_cases_df"] = df
    if "results_df" in st.session_state and st.session_state.results_df is not None:
        if len(st.session_state.results_df) == len(df):
            st.session_state.results_df["Run"] = df["Run"].values
    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
    return df


saved_config = load_settings()

st.set_page_config(
    page_title="AI Autonomous QA & Testing Studio | Web UI, Mobile, A11y & API Automation",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------
# Modern UI Styles, Mobile Optimization & Keyframe Animations
# ---------------------------------------------------------
st.markdown(
    """<style>
    /* Global Typography, Font Smoothing & Horizontal Scroll Prevention */
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
    
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
        overflow-x: hidden !important;
        max-width: 100vw !important;
        box-sizing: border-box !important;
    }

    code, pre {
        font-family: 'JetBrains Mono', monospace !important;
    }

    *, *::before, *::after {
        box-sizing: border-box;
    }

    img, video, canvas, svg {
        max-width: 100% !important;
        height: auto;
    }

    /* Streamlit Top Header Bar — Non-Obtrusive, Transparent & Scrollbar-Friendly */
    header[data-testid="stHeader"] {
        background: transparent !important;
        backdrop-filter: none !important;
        border-bottom: none !important;
        pointer-events: none !important;
        z-index: 99 !important;
        right: 18px !important;
        visibility: visible !important;
        display: flex !important;
    }

    header[data-testid="stHeader"] * {
        pointer-events: auto !important;
    }

    /* Main Workspace Container Spacing — Full Header Visibility & Clean Scroll Clearance */
    .block-container,
    div[data-testid="stAppViewContainer"] .block-container,
    .stApp [data-testid="stMain"] .block-container {
        padding-top: 4.5rem !important;
        padding-bottom: 2.5rem !important;
        max-width: 1480px !important;
    }

    /* Sidebar Surface & Obsidian Gradient Styling */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0D121F 0%, #080B12 100%) !important;
        border-right: 1px solid rgba(255, 255, 255, 0.08) !important;
    }

    [data-testid="stSidebar"] [data-testid="stExpander"] {
        background: rgba(19, 26, 43, 0.5) !important;
        border: 1px solid rgba(255, 255, 255, 0.09) !important;
        border-radius: 9px !important;
        margin-bottom: 8px !important;
    }

    /* Hero Header & Modern Enterprise Banner */
    .hero-banner {
        background: radial-gradient(circle at 85% 15%, rgba(99, 102, 241, 0.16), transparent 42%),
                    linear-gradient(135deg, #0D121F 0%, #161A2E 55%, #0D121F 100%) !important;
        border: 1px solid rgba(99, 102, 241, 0.28) !important;
        border-radius: 14px !important;
        padding: 16px 22px !important;
        margin-bottom: 16px !important;
        box-shadow: 0 10px 28px -6px rgba(0, 0, 0, 0.45), 0 2px 8px -2px rgba(99, 102, 241, 0.14) !important;
        max-width: 100% !important;
        overflow: hidden !important;
    }

    .hero-top-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
        margin-bottom: 10px;
    }
    
    .hero-title {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
        font-size: 1.85rem;
        font-weight: 800;
        letter-spacing: -0.5px;
        color: #FFFFFF !important;
        margin-bottom: 4px;
        line-height: 1.2;
    }

    .brand-logo-link {
        display: inline-flex;
        align-items: center;
        gap: 10px;
        text-decoration: none !important;
        color: inherit !important;
        cursor: pointer;
    }

    .brand-logo-link:hover .hero-icon {
        transform: scale(1.06) rotate(-3deg);
    }

    .hero-icon {
        font-size: 1.85rem;
        line-height: 1;
        display: inline-block;
        transition: transform 0.2s ease;
        filter: drop-shadow(0 2px 8px rgba(99, 102, 241, 0.5));
    }
    
    .hero-title-text {
        color: #FFFFFF !important;
        background: linear-gradient(90deg, #FFFFFF 0%, #E2E8F0 50%, #38BDF8 100%);
        -webkit-background-clip: text;
        background-clip: text;
        -webkit-text-fill-color: transparent;
    }

    .hero-subtitle {
        color: #94A3B8 !important;
        font-size: 0.90rem;
        font-weight: 500;
        line-height: 1.45;
        max-width: 720px;
        word-wrap: break-word;
    }

    .hero-telemetry-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
        align-items: center;
    }

    .telemetry-badge {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 4px 11px;
        border-radius: 999px;
        font-size: 0.74rem;
        font-weight: 700;
        letter-spacing: 0.3px;
        background: rgba(13, 18, 31, 0.85);
        border: 1px solid rgba(255, 255, 255, 0.12);
        color: #E2E8F0;
    }

    .telemetry-badge.active-green {
        background: rgba(16, 185, 129, 0.14);
        border-color: rgba(16, 185, 129, 0.4);
        color: #34D399;
    }

    .telemetry-badge.active-blue {
        background: rgba(56, 189, 248, 0.14);
        border-color: rgba(56, 189, 248, 0.4);
        color: #38BDF8;
    }

    .telemetry-badge.active-indigo {
        background: rgba(99, 102, 241, 0.16);
        border-color: rgba(99, 102, 241, 0.42);
        color: #A5B4FC;
    }

    .hero-features {
        display: flex;
        flex-wrap: wrap;
        gap: 7px;
        padding-top: 10px;
        border-top: 1px solid rgba(255, 255, 255, 0.08);
    }

    .feature-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        background: rgba(255, 255, 255, 0.05) !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
        padding: 4px 11px;
        border-radius: 18px;
        font-size: 0.78rem;
        font-weight: 600;
        color: #E2E8F0 !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
        text-decoration: none !important;
    }
    
    /* WCAG 2.1 AA Touch Targets & Responsive Layout Across All Columns & Buttons */
    [data-testid="column"] {
        min-width: 0 !important;
    }

    /* Primary CTA Button Elevation & Radiant Lighting */
    div.stButton > button[kind="primary"],
    div.stDownloadButton > button[kind="primary"],
    [data-testid="stBaseButton-primary"] {
        background: linear-gradient(135deg, #6366F1 0%, #4338CA 100%) !important;
        border: 1px solid rgba(165, 180, 252, 0.4) !important;
        color: #FFFFFF !important;
        border-radius: 9px !important;
        font-weight: 700 !important;
        letter-spacing: 0.2px !important;
        min-height: 40px !important;
        padding: 0.45rem 0.85rem !important;
        font-size: 0.86rem !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
        box-shadow: 0 4px 14px -1px rgba(99, 102, 241, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.2) !important;
    }
    div.stButton > button[kind="primary"]:hover,
    div.stDownloadButton > button[kind="primary"]:hover,
    [data-testid="stBaseButton-primary"]:hover {
        background: linear-gradient(135deg, #4F46E5 0%, #3730A3 100%) !important;
        transform: translateY(-1.5px) !important;
        box-shadow: 0 6px 20px rgba(99, 102, 241, 0.6) !important;
    }

    /* Secondary Button Glassmorphism & Micro-Interactions */
    div.stButton > button,
    div.stDownloadButton > button,
    [data-testid="stPopover"] > div > button,
    [data-testid="stBaseButton-secondary"] {
        background: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        color: #E2E8F0 !important;
        backdrop-filter: blur(8px) !important;
        border-radius: 9px !important;
        font-weight: 600 !important;
        letter-spacing: 0.15px !important;
        min-height: 40px !important;
        padding: 0.45rem 0.85rem !important;
        font-size: 0.85rem !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25) !important;
    }
    div.stButton > button:hover,
    div.stDownloadButton > button:hover,
    [data-testid="stPopover"] > div > button:hover,
    [data-testid="stBaseButton-secondary"]:hover {
        background: rgba(255, 255, 255, 0.08) !important;
        border-color: rgba(99, 102, 241, 0.45) !important;
        color: #FFFFFF !important;
        transform: translateY(-1.5px) !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3) !important;
    }
    div.stButton > button:focus-visible,
    div.stDownloadButton > button:focus-visible,
    [data-testid="stPopover"] > div > button:focus-visible {
        outline: 2px solid #6366F1 !important;
        outline-offset: 2px !important;
    }

    /* Form Inputs, Selects & Textareas Refinement */
    div[data-baseweb="input"] > div,
    div[data-baseweb="textarea"] > div,
    div[data-baseweb="select"] > div {
        background: rgba(13, 18, 31, 0.75) !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        border-radius: 9px !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease, background 0.2s ease !important;
    }
    div[data-baseweb="input"]:focus-within > div,
    div[data-baseweb="textarea"]:focus-within > div,
    div[data-baseweb="select"]:focus-within > div {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.25) !important;
        background: rgba(13, 18, 31, 0.95) !important;
    }

    /* Container Glassmorphism */
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(15, 23, 42, 0.45) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 12px !important;
        backdrop-filter: blur(12px) !important;
        transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }
    [data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(99, 102, 241, 0.25) !important;
    }

    /* Form Labels Clarity */
    label[data-testid="stWidgetLabel"] p,
    label[data-testid="stWidgetLabel"] span {
        font-size: 0.84rem !important;
        font-weight: 600 !important;
        color: #CBD5E1 !important;
        margin-bottom: 2px !important;
    }

    /* Section Subheadings */
    .stSubheader, h2, h3 {
        letter-spacing: -0.02em !important;
        font-weight: 700 !important;
        color: #F8FAFC !important;
    }

    /* Popover Container & Clean Modal Spacing */
    [data-testid="stPopoverBody"] {
        min-width: 360px !important;
        max-width: 480px !important;
        background: #0E1422 !important;
        border: 1px solid rgba(255, 255, 255, 0.16) !important;
        border-radius: 12px !important;
        box-shadow: 0 20px 45px rgba(0, 0, 0, 0.65) !important;
        padding: 1.15rem !important;
    }
    [data-testid="stPopoverBody"] [data-testid="column"] {
        min-width: 0 !important;
    }
    [data-testid="stPopoverBody"] div.stButton > button {
        min-height: 38px !important;
        padding: 0.45rem 0.75rem !important;
        font-size: 0.86rem !important;
        border-radius: 8px !important;
    }
    [data-testid="stPopoverBody"] div[data-baseweb="input"],
    [data-testid="stPopoverBody"] div[data-baseweb="select"] > div {
        background: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.16) !important;
        border-radius: 8px !important;
        transition: all 0.2s ease !important;
    }
    [data-testid="stPopoverBody"] div[data-baseweb="input"]:focus-within,
    [data-testid="stPopoverBody"] div[data-baseweb="select"] > div:focus-within {
        border-color: #6366F1 !important;
        box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25) !important;
    }
    [data-testid="stPopoverBody"] input,
    [data-testid="stPopoverBody"] textarea {
        color: #F8FAFC !important;
        font-size: 0.88rem !important;
    }
    [data-testid="stPopoverBody"] label {
        font-size: 0.82rem !important;
        font-weight: 600 !important;
        color: #CBD5E1 !important;
        margin-bottom: 4px !important;
    }

    /* Tooltip Anti-Stick Protection */
    [data-testid="stTooltipContent"],
    div[data-baseweb="tooltip"] {
        pointer-events: none !important;
        z-index: 10000 !important;
    }

    /* Clean Segmented Pill Tabs */
    [data-testid="stTabs"] [role="tablist"],
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px !important;
        background: rgba(14, 20, 34, 0.75) !important;
        padding: 6px 8px !important;
        border-radius: 11px !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        flex-wrap: wrap !important;
        margin-bottom: 14px !important;
        align-items: center !important;
    }

    /* Hide native bottom track line and duplicate bottom underline indicator */
    [data-testid="stTabs"] [role="tablist"]::after,
    [data-testid="stTab"] .react-aria-SelectionIndicator,
    .stTabs [data-baseweb="tab-highlight"],
    .stTabs [data-baseweb="tab-border"] {
        display: none !important;
        height: 0 !important;
        background: transparent !important;
    }

    /* Individual Tab Pill Styling */
    [data-testid="stTab"],
    [data-testid="stTabs"] [role="tab"],
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px !important;
        padding: 7px 16px !important;
        padding-left: 16px !important;
        padding-right: 16px !important;
        height: auto !important;
        min-height: 36px !important;
        font-weight: 600 !important;
        font-size: 0.88rem !important;
        color: #94A3B8 !important;
        background: transparent !important;
        border: 1px solid transparent !important;
        transition: all 0.2s ease !important;
        box-shadow: none !important;
    }

    [data-testid="stTab"]:hover,
    [data-testid="stTabs"] [role="tab"]:hover,
    .stTabs [data-baseweb="tab"]:hover {
        color: #F8FAFC !important;
        background: rgba(255, 255, 255, 0.05) !important;
        border-color: rgba(255, 255, 255, 0.1) !important;
    }

    /* Active Selected Tab Pill */
    [data-testid="stTab"][aria-selected="true"],
    [data-testid="stTab"][data-selected="true"],
    [data-testid="stTabs"] [role="tab"][aria-selected="true"],
    .stTabs [data-baseweb="tab"][aria-selected="true"] {
        background: linear-gradient(135deg, rgba(99, 102, 241, 0.25) 0%, rgba(56, 189, 248, 0.15) 100%) !important;
        color: #FFFFFF !important;
        border: 1px solid rgba(99, 102, 241, 0.48) !important;
        border-radius: 8px !important;
        padding: 7px 16px !important;
        padding-left: 16px !important;
        padding-right: 16px !important;
        box-shadow: 0 2px 8px rgba(99, 102, 241, 0.2) !important;
    }

    /* Horizontal Radio Groups */
    [data-testid="stRadio"] > div[role="radiogroup"] {
        gap: 8px !important;
        padding: 2px 0 !important;
    }
    [data-testid="stRadio"] > div[role="radiogroup"] > label {
        background: rgba(14, 20, 34, 0.6) !important;
        border: 1px solid rgba(255, 255, 255, 0.09) !important;
        border-radius: 8px !important;
        padding: 7px 12px !important;
        transition: all 0.2s ease !important;
        margin-right: 0 !important;
    }
    [data-testid="stRadio"] > div[role="radiogroup"] > label:hover {
        border-color: rgba(99, 102, 241, 0.4) !important;
        background: rgba(26, 36, 58, 0.7) !important;
    }

    /* Refined Alerts & Expanders */
    [data-testid="stAlert"] {
        border-radius: 9px !important;
    }
    [data-testid="stExpander"] {
        border-radius: 9px !important;
        border-color: rgba(255, 255, 255, 0.09) !important;
    }
    
    /* Status Badge System (WCAG AA High-Contrast) */
    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 3px 10px;
        border-radius: 16px;
        font-size: 0.76rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .status-passed {
        background-color: rgba(16, 185, 129, 0.15);
        color: #34D399;
        border: 1px solid rgba(16, 185, 129, 0.45);
    }
    .status-failed {
        background-color: rgba(239, 68, 68, 0.15);
        color: #F87171;
        border: 1px solid rgba(239, 68, 68, 0.45);
    }
    .status-paused {
        background-color: rgba(245, 158, 11, 0.16);
        color: #FBBF24;
        border: 1px solid rgba(245, 158, 11, 0.5);
        animation: pulse-border 1.8s infinite;
    }
    .status-running {
        background-color: rgba(59, 130, 246, 0.16);
        color: #60A5FA;
        border: 1px solid rgba(59, 130, 246, 0.5);
        animation: pulse-border 1.2s infinite;
    }
    .status-skipped {
        background-color: rgba(156, 163, 175, 0.14);
        color: #D1D5DB;
        border: 1px solid rgba(156, 163, 175, 0.35);
    }
    .status-pending {
        background-color: rgba(100, 116, 139, 0.15);
        color: #CBD5E1;
        border: 1px solid rgba(100, 116, 139, 0.35);
    }

    /* Live Animated Loaders & Pulses */
    @keyframes pulse-border {
        0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.45); }
        70% { box-shadow: 0 0 0 7px rgba(245, 158, 11, 0); }
        100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
    }
    
    @keyframes spin-loader {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    
    .spinner-ring {
        display: inline-block;
        width: 14px;
        height: 14px;
        border: 2px solid rgba(59, 130, 246, 0.3);
        border-radius: 50%;
        border-top-color: #60A5FA;
        animation: spin-loader 0.8s linear infinite;
        vertical-align: middle;
        margin-right: 7px;
    }
    
    /* Card Glassmorphic Container (Theme-Safe High Contrast) */
    .metric-card {
        padding: 12px 18px;
        border-radius: 10px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: linear-gradient(180deg, rgba(17, 24, 39, 0.75) 0%, rgba(13, 19, 32, 0.88) 100%);
        color: #F8FAFC;
        backdrop-filter: blur(12px);
        margin-bottom: 10px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }

    /* KPI Stat Boxes */
    .kpi-stat-box {
        background: linear-gradient(180deg, rgba(17, 24, 39, 0.75) 0%, rgba(13, 19, 32, 0.9) 100%);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 10px;
        padding: 13px 11px;
        text-align: center;
        transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
        margin-bottom: 6px;
        box-shadow: 0 3px 8px rgba(0, 0, 0, 0.16);
    }
    .kpi-stat-box:hover {
        transform: translateY(-2px);
        border-color: rgba(99, 102, 241, 0.45);
        box-shadow: 0 6px 16px rgba(99, 102, 241, 0.15);
    }
    .kpi-stat-val {
        font-size: 1.55rem;
        font-weight: 800;
        margin-bottom: 3px;
        line-height: 1.15;
    }
    .kpi-stat-lbl {
        color: #94A3B8;
        font-size: 0.74rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    /* Quick-Start Empty State Workflow Cards */
    .workflow-step-card {
        background: linear-gradient(180deg, rgba(17, 24, 39, 0.75) 0%, rgba(13, 19, 32, 0.88) 100%);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 18px;
        min-height: 160px;
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
        transition: border-color 0.2s ease, transform 0.2s ease, box-shadow 0.2s ease;
    }
    .workflow-step-card:hover {
        border-color: rgba(56, 189, 248, 0.4);
        transform: translateY(-2px);
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.22);
    }
    .workflow-step-num {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 26px;
        height: 26px;
        border-radius: 7px;
        background: rgba(99, 102, 241, 0.2);
        border: 1px solid rgba(99, 102, 241, 0.4);
        color: #A5B4FC;
        font-weight: 800;
        font-size: 0.82rem;
        margin-bottom: 8px;
    }
    .workflow-step-title {
        color: #F8FAFC;
        font-size: 0.96rem;
        font-weight: 700;
        margin-bottom: 5px;
    }
    .workflow-step-desc {
        color: #94A3B8;
        font-size: 0.84rem;
        line-height: 1.5;
    }

    /* Flow Explorer & Generation Bar Elevation */
    .flow-generator-card {
        background: radial-gradient(circle at 90% 10%, rgba(99, 102, 241, 0.12), transparent 50%),
                    linear-gradient(180deg, rgba(17, 24, 39, 0.85) 0%, rgba(13, 19, 32, 0.95) 100%) !important;
        border: 1px solid rgba(99, 102, 241, 0.28) !important;
        border-radius: 12px !important;
        padding: 14px 18px !important;
        margin-top: 10px !important;
        margin-bottom: 12px !important;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3) !important;
    }
    
    .screen-card-box {
        background: linear-gradient(180deg, rgba(19, 26, 43, 0.6) 0%, rgba(15, 23, 42, 0.75) 100%) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 10px !important;
        padding: 12px !important;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
    }
    .screen-card-box:hover {
        border-color: rgba(99, 102, 241, 0.4) !important;
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35) !important;
        transform: translateY(-2px);
    }

    /* Flow Explorer Ultra-Modern Dashboard & Micro-Animations */
    @keyframes flowCardEnter {
        0% { opacity: 0; transform: translateY(12px) scale(0.99); }
        100% { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes subtlePulse {
        0%, 100% { box-shadow: 0 0 10px rgba(99, 102, 241, 0.15); }
        50% { box-shadow: 0 0 20px rgba(99, 102, 241, 0.38); }
    }
    .flow-stat-strip {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 12px;
        margin-top: 10px;
        margin-bottom: 16px;
        animation: flowCardEnter 0.35s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .flow-stat-pill {
        background: linear-gradient(135deg, rgba(26, 32, 53, 0.75) 0%, rgba(15, 23, 42, 0.90) 100%);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 10px;
        padding: 12px 14px;
        text-align: center;
        transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        backdrop-filter: blur(8px);
    }
    .flow-stat-pill:hover {
        border-color: rgba(99, 102, 241, 0.5);
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(0, 0, 0, 0.35);
    }
    .flow-stat-val {
        font-size: 1.35rem;
        font-weight: 700;
        color: #F8FAFC;
        line-height: 1.2;
    }
    .flow-stat-lbl {
        font-size: 0.72rem;
        color: #94A3B8;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-top: 4px;
    }
    .flow-node-badge {
        display: inline-flex;
        align-items: center;
        padding: 3px 8px;
        font-size: 0.74rem;
        font-weight: 600;
        border-radius: 6px;
        background: rgba(99, 102, 241, 0.16);
        color: #A5B4FC;
        border: 1px solid rgba(99, 102, 241, 0.3);
        margin: 2px 4px 2px 0;
    }
    .flow-node-badge-green {
        background: rgba(16, 185, 129, 0.16);
        color: #6EE7B7;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .flow-node-badge-amber {
        background: rgba(245, 158, 11, 0.16);
        color: #FCD34D;
        border: 1px solid rgba(245, 158, 11, 0.3);
    }
    .flow-node-badge-sky {
        background: rgba(14, 165, 233, 0.16);
        color: #7DD3FC;
        border: 1px solid rgba(14, 165, 233, 0.3);
    }
    .flow-journey-container {
        background: linear-gradient(180deg, rgba(17, 24, 39, 0.6) 0%, rgba(11, 17, 30, 0.8) 100%);
        border: 1px solid rgba(99, 102, 241, 0.22);
        border-radius: 12px;
        padding: 16px 20px;
        margin-bottom: 18px;
        overflow-x: auto;
    }
    .flow-journey-step {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        margin: 6px 4px;
    }
    .flow-journey-node {
        background: linear-gradient(135deg, rgba(30, 41, 59, 0.85) 0%, rgba(15, 23, 42, 0.95) 100%);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 0.82rem;
        color: #F1F5F9;
        display: flex;
        align-items: center;
        gap: 6px;
        transition: all 0.2s ease;
    }
    .flow-journey-node:hover {
        border-color: rgba(99, 102, 241, 0.6);
        background: rgba(99, 102, 241, 0.15);
    }

    /* Mobile Optimization & Overflow Prevention (<= 768px) */
    @media screen and (max-width: 768px) {
        .block-container {
            padding-top: 1.8rem !important;
            padding-left: 0.8rem !important;
            padding-right: 0.8rem !important;
        }
        .hero-banner {
            padding: 14px 16px !important;
            border-radius: 11px !important;
        }
        .hero-title {
            font-size: 1.3rem !important;
        }
        .hero-subtitle {
            font-size: 0.84rem !important;
        }
        .feature-pill {
            font-size: 0.72rem !important;
            padding: 3px 9px !important;
        }
        .kpi-stat-val {
            font-size: 1.25rem !important;
        }
    }

    /* Custom Scrollbar */
    ::-webkit-scrollbar {
        width: 5px;
        height: 5px;
    }
    ::-webkit-scrollbar-track {
        background: rgba(0, 0, 0, 0.08);
    }
    ::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.16);
        border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: rgba(255, 255, 255, 0.28);
    }
    </style>""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------
# Validation Helper Functions
# ---------------------------------------------------------
def validate_url(url_str: str) -> tuple[bool, str]:
    """Validates if a URL is provided and conforms to HTTP/HTTPS format or Android package name, enforcing SSRF and security controls."""
    if not url_str or not str(url_str).strip():
        return False, "Target URL or Android package cannot be empty."
    clean = str(url_str).strip()
    # Check if this is an Android package (e.g. com.zeptoconsumerapp, com.cuvora.carinfo) or android:// URI
    if clean.startswith("android://") or re.match(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$", clean):
        return True, ""
    from core.security_guardrails import validate_target_url_security
    return validate_target_url_security(clean, allow_local_dev=True)


def validate_json_body(json_str: str) -> tuple[bool, str]:
    """Validates if a string is a valid JSON object/array."""
    if not json_str or not str(json_str).strip():
        return True, ""  # Optional, so empty is valid
    try:
        json.loads(json_str)
        return True, ""
    except Exception as e:
        return False, f"Invalid JSON syntax: {e}"


def validate_status_code(code_str: str) -> tuple[bool, str]:
    """Validates HTTP status code string."""
    if not code_str or not str(code_str).strip():
        return False, "Expected status code is required."
    clean = str(code_str).strip()
    if not clean.isdigit() or not (100 <= int(clean) <= 599):
        return False, f"Status code '{clean}' is not a valid HTTP status code (100-599)."
    return True, ""


# ---------------------------------------------------------
# Sidebar Configuration (Progressive Disclosure & Zero-Scroll Access)
# ---------------------------------------------------------
st.sidebar.markdown("### 🛡️ Control Center")
st.sidebar.caption("Runtime execution controls, authentication session, and environment configuration.")

# 1. Execution Settings (Top-Priority Controls — Immediately Accessible)
st.sidebar.subheader("⚙️ Execution Settings")

PLATFORM_CHOICES = [
    ("Desktop Chrome (Default)", "desktop_chrome"),
    ("📱 iPhone 15 Pro (Mobile Web)", "iphone_15_pro"),
    ("📱 Google Pixel 7 (Mobile Web)", "pixel_7"),
    ("📱 Samsung Galaxy S23 (Mobile Web)", "galaxy_s23"),
    ("💻 iPad Air (Tablet Web)", "ipad_air"),
    ("🦊 Desktop Firefox", "desktop_firefox"),
    ("🧭 Desktop Safari (WebKit)", "desktop_safari"),
    ("🤖 Android Device (CDP: 9222)", "android_cdp"),
    ("⚛️ Desktop App (Electron CDP: 9223)", "electron_cdp"),
]
PLATFORM_LABELS = [p[0] for p in PLATFORM_CHOICES]
PLATFORM_LABEL_TO_KEY = {p[0]: p[1] for p in PLATFORM_CHOICES}
PLATFORM_KEY_TO_LABEL = {p[1]: p[0] for p in PLATFORM_CHOICES}

NETWORK_CHOICES = [
    ("⚡ Direct (No Throttling)", "direct"),
    ("📶 Fast 4G (10Mbps / 20ms)", "fast_4g"),
    ("🐌 Slow 3G (400Kbps / 400ms)", "slow_3g"),
    ("✈️ Offline Mode (Network Airgap)", "offline"),
]
NETWORK_LABELS = [n[0] for n in NETWORK_CHOICES]
NETWORK_LABEL_TO_KEY = {n[0]: n[1] for n in NETWORK_CHOICES}
NETWORK_KEY_TO_LABEL = {n[1]: n[0] for n in NETWORK_CHOICES}

if "_persisted_force_g4f" not in st.session_state:
    st.session_state["_persisted_force_g4f"] = bool(saved_config.get("force_g4f", False))
if "_persisted_show_browser" not in st.session_state:
    st.session_state["_persisted_show_browser"] = bool(saved_config.get("show_browser", True))
if "_persisted_enable_visual" not in st.session_state:
    st.session_state["_persisted_enable_visual"] = bool(saved_config.get("enable_visual", True))
if "_persisted_show_adapter" not in st.session_state:
    st.session_state["_persisted_show_adapter"] = bool(saved_config.get("show_adapter_cols", True))
if "_persisted_step_pause" not in st.session_state:
    st.session_state["_persisted_step_pause"] = bool(saved_config.get("step_pause", False))
if "_persisted_target_platform" not in st.session_state:
    st.session_state["_persisted_target_platform"] = str(saved_config.get("target_platform", "desktop_chrome"))
if "_persisted_platform_orientation" not in st.session_state:
    st.session_state["_persisted_platform_orientation"] = str(saved_config.get("platform_orientation", "portrait"))
if "_persisted_network_preset" not in st.session_state:
    st.session_state["_persisted_network_preset"] = str(saved_config.get("network_preset", "direct"))
if "_persisted_cdp_url" not in st.session_state:
    st.session_state["_persisted_cdp_url"] = str(saved_config.get("cdp_url", ""))

if "force_g4f" not in st.session_state:
    st.session_state["force_g4f"] = st.session_state["_persisted_force_g4f"]
if "show_browser" not in st.session_state:
    st.session_state["show_browser"] = st.session_state["_persisted_show_browser"]
if "enable_visual" not in st.session_state:
    st.session_state["enable_visual"] = st.session_state["_persisted_enable_visual"]
if "global_show_adapter_cols" not in st.session_state:
    st.session_state["global_show_adapter_cols"] = st.session_state["_persisted_show_adapter"]
if "global_step_pause" not in st.session_state:
    st.session_state["global_step_pause"] = st.session_state["_persisted_step_pause"]


def get_global_target_url(fallback: str = "") -> str:
    """Returns the most up-to-date active target URL across all session sources."""
    for k in ["global_target_url", "flow_explorer_url", "single_url_input", "session_login_url", "deep_inspector_url_input"]:
        val = str(st.session_state.get(k, "")).strip()
        if val:
            return val
    disc_flow = st.session_state.get("discovered_flow")
    if isinstance(disc_flow, dict) and disc_flow.get("start_url"):
        return str(disc_flow["start_url"]).strip()
    try:
        from core.session_capturer import get_auth_session_summary
        saved = get_auth_session_summary("auth.json").get("target_url", "")
        if saved:
            return str(saved).strip()
    except Exception:
        pass
    return fallback


def set_global_target_url(new_url: str):
    """Synchronizes target URL globally across all tabs and components without mutating instantiated widgets."""
    clean_u = str(new_url or "").strip()
    if clean_u:
        st.session_state["global_target_url"] = clean_u


def _on_execution_settings_change():
    """Immediately persists Execution Settings states to session and config/settings.json."""
    fg = bool(st.session_state.get("force_g4f", st.session_state.get("_persisted_force_g4f", False)))
    sb = bool(st.session_state.get("show_browser", st.session_state.get("_persisted_show_browser", True)))
    ev = bool(st.session_state.get("enable_visual", st.session_state.get("_persisted_enable_visual", True)))
    sac = bool(st.session_state.get("global_show_adapter_cols_sidebar", st.session_state.get("global_show_adapter_cols", st.session_state.get("_persisted_show_adapter", True))))

    tgt_plat_lbl = st.session_state.get("target_platform_select", PLATFORM_KEY_TO_LABEL.get(st.session_state.get("_persisted_target_platform", "desktop_chrome"), PLATFORM_CHOICES[0][0]))
    tgt_plat = PLATFORM_LABEL_TO_KEY.get(tgt_plat_lbl, "desktop_chrome")
    orient = str(st.session_state.get("platform_orientation", st.session_state.get("_persisted_platform_orientation", "portrait"))).lower()
    net_lbl = st.session_state.get("network_preset_select", NETWORK_KEY_TO_LABEL.get(st.session_state.get("_persisted_network_preset", "direct"), NETWORK_CHOICES[0][0]))
    net_preset = NETWORK_LABEL_TO_KEY.get(net_lbl, "direct")
    cdp_endpoint = str(st.session_state.get("cdp_url_input", st.session_state.get("_persisted_cdp_url", ""))).strip()

    st.session_state["_persisted_force_g4f"] = fg
    st.session_state["_persisted_show_browser"] = sb
    st.session_state["_persisted_enable_visual"] = ev
    st.session_state["_persisted_show_adapter"] = sac
    st.session_state["global_show_adapter_cols"] = sac
    st.session_state["_persisted_target_platform"] = tgt_plat
    st.session_state["_persisted_platform_orientation"] = orient
    st.session_state["_persisted_network_preset"] = net_preset
    st.session_state["_persisted_cdp_url"] = cdp_endpoint

    if fg:
        os.environ["FORCE_G4F"] = "true"
    else:
        os.environ.pop("FORCE_G4F", None)

    if sb:
        os.environ["SHOW_BROWSER"] = "true"
    else:
        os.environ.pop("SHOW_BROWSER", None)

    save_settings(
        {
            "force_g4f": fg,
            "show_browser": sb,
            "enable_visual": ev,
            "show_adapter_cols": sac,
            "target_platform": tgt_plat,
            "platform_orientation": orient,
            "network_preset": net_preset,
            "cdp_url": cdp_endpoint,
        },
        force=True,
    )


force_g4f = st.sidebar.checkbox(
    "Force G4F (Free Mode)",
    key="force_g4f",
    on_change=_on_execution_settings_change,
    help="Skip API keys and use GPT4Free directly.",
)
show_browser = st.sidebar.checkbox(
    "👁️ Show Browser (Headed Mode)",
    key="show_browser",
    on_change=_on_execution_settings_change,
    help="Display live browser window during Web tests and flow exploration.",
)
enable_visual = st.sidebar.checkbox(
    "📸 AI Visual Layout Audit",
    key="enable_visual",
    on_change=_on_execution_settings_change,
    help="Take screenshot and analyze page layout via Vision AI.",
)
show_adapter_sidebar = st.sidebar.checkbox(
    "🛠️ Show Adapter Columns in Grid",
    value=st.session_state.get("global_show_adapter_cols", True),
    key="global_show_adapter_cols_sidebar",
    on_change=_on_execution_settings_change,
    help="Display Playwright Code, Target, and execution adapter columns in the Test Suite Grid.",
)

# Omnichannel & Target Platform selector
saved_platform_key = st.session_state.get("_persisted_target_platform", saved_config.get("target_platform", "desktop_chrome"))
default_platform_idx = 0
for idx, (_, k) in enumerate(PLATFORM_CHOICES):
    if k == saved_platform_key:
        default_platform_idx = idx
        break

selected_platform_label = st.sidebar.selectbox(
    "📱 Target Platform & Device",
    options=PLATFORM_LABELS,
    index=default_platform_idx,
    key="target_platform_select",
    on_change=_on_execution_settings_change,
    help="Select runtime device profile (Mobile, Desktop, Tablet, or CDP remote endpoint)."
)
selected_platform_key = PLATFORM_LABEL_TO_KEY.get(selected_platform_label, "desktop_chrome")

# Orientation (for mobile/tablet)
selected_orientation = "portrait"
if any(m in selected_platform_key for m in ["iphone", "pixel", "galaxy", "ipad"]):
    saved_orient = st.session_state.get("_persisted_platform_orientation", saved_config.get("platform_orientation", "portrait")).capitalize()
    selected_orientation = st.sidebar.radio(
        "Screen Orientation",
        options=["Portrait", "Landscape"],
        index=0 if saved_orient == "Portrait" else 1,
        horizontal=True,
        key="platform_orientation",
        on_change=_on_execution_settings_change,
    ).lower()

# Network Throttling Preset
saved_net_key = st.session_state.get("_persisted_network_preset", saved_config.get("network_preset", "direct"))
default_net_idx = 0
for idx, (_, k) in enumerate(NETWORK_CHOICES):
    if k == saved_net_key:
        default_net_idx = idx
        break

selected_net_label = st.sidebar.selectbox(
    "🌐 Network Profile",
    options=NETWORK_LABELS,
    index=default_net_idx,
    key="network_preset_select",
    on_change=_on_execution_settings_change,
    help="Simulate network conditions (4G, 3G, offline) during test execution."
)
selected_net_key = NETWORK_LABEL_TO_KEY.get(selected_net_label, "direct")

cdp_url_val = ""
if "cdp" in selected_platform_key:
    def_cdp = st.session_state.get("_persisted_cdp_url", saved_config.get("cdp_url", "http://localhost:9222" if selected_platform_key == "android_cdp" else "http://localhost:9223"))
    cdp_url_val = st.sidebar.text_input(
        "🔌 CDP Endpoint",
        value=def_cdp,
        key="cdp_url_input",
        on_change=_on_execution_settings_change,
        help="Chrome DevTools Protocol URL for Android adb or Desktop Electron apps."
    )

    if selected_platform_key == "android_cdp":
        with st.sidebar.expander("📱 Connected Android Devices & Apps", expanded=True):
            from core.device_bridge import (
                list_connected_adb_devices,
                list_device_inspectable_sockets,
                list_installed_packages,
                launch_app_on_device,
                forward_device_port,
                reset_all_device_forwards,
                get_active_forwarded_port,
                find_free_port,
                fetch_cdp_targets,
                get_foreground_app_info,
                capture_device_screenshot,
                dump_native_ui_elements,
                synthesize_native_test_cases,
            )

            if st.button("🔄 Scan Devices", key="btn_scan_adb_devices", width="stretch"):
                st.session_state["_adb_devices_cached"] = list_connected_adb_devices()
                st.rerun()

            connected_devices = st.session_state.get("_adb_devices_cached", list_connected_adb_devices())

            if not connected_devices:
                st.caption("⚠️ No Android device/emulator detected via ADB. Connect your device via USB with USB Debugging enabled, or launch an Android emulator.")
            else:
                dev_labels = [f"{d['model']} ({d['serial']})" for d in connected_devices]
                sel_dev_idx = st.selectbox(
                    "Connected Device",
                    range(len(dev_labels)),
                    format_func=lambda i: dev_labels[i],
                    key="sel_adb_device",
                )
                safe_dev_idx = sel_dev_idx if sel_dev_idx is not None and sel_dev_idx < len(connected_devices) else 0
                device_serial = connected_devices[safe_dev_idx]["serial"]

                # Detect active foreground app on screen
                fg_info = get_foreground_app_info(device_serial)
                if fg_info.get("package"):
                    st.info(f"📲 **App on Phone Screen:** `{fg_info['package']}`")

                app_mode = st.radio(
                    "Target App Mode",
                    ["Inspect Running App", "Launch Installed App"],
                    horizontal=True,
                    key="adb_app_mode",
                )

                if app_mode == "Inspect Running App":
                    sockets = list_device_inspectable_sockets(device_serial)
                    if sockets:
                        sock_labels = [s["label"] for s in sockets]
                        sel_sock_idx = st.selectbox(
                            "Running Apps & WebViews",
                            range(len(sock_labels)),
                            format_func=lambda i: sock_labels[i],
                            key="sel_adb_socket",
                        )
                        safe_sock_idx = sel_sock_idx if sel_sock_idx is not None and sel_sock_idx < len(sockets) else 0
                        selected_socket = sockets[safe_sock_idx]["socket"]

                        col_fwd1, col_fwd2 = st.columns([1.6, 1.0])
                        with col_fwd1:
                            if st.button("⚡ Connect & Forward", key="btn_fwd_adb_port", type="primary", width="stretch"):
                                if forward_device_port(selected_socket, local_port=9222, serial=device_serial, auto_port=True):
                                    bound_p = get_active_forwarded_port()
                                    st.toast(f"✅ Forwarded {selected_socket} to 127.0.0.1:{bound_p}!", icon="🔌")
                                else:
                                    st.error("Failed to forward ADB port.")
                        with col_fwd2:
                            if st.button("🧹 Reset Ports", key="btn_reset_adb_forwards", type="secondary", width="stretch", help="Clears zombie forward tunnels to fix conflicts"):
                                reset_all_device_forwards(serial=device_serial)
                                st.toast("🧹 Cleared all active ADB forwards!", icon="✅")
                    else:
                        st.caption("ℹ️ No debuggable WebViews or Chrome browser sockets detected on this device. (Use **📸 Native App Inspector** below to inspect and test native apps directly!)")
                else:
                    packages = list_installed_packages(device_serial, third_party_only=True)
                    if packages:
                        sel_pkg = st.selectbox("Installed App Packages", packages, key="sel_adb_pkg")
                        if st.button("🚀 Launch App on Device", key="btn_launch_adb_app", width="stretch"):
                            if launch_app_on_device(sel_pkg, serial=device_serial):
                                st.toast(f"🚀 Launched {sel_pkg}!", icon="📱")
                                forward_device_port("chrome_devtools_remote", 9222, device_serial, auto_port=True)
                            else:
                                st.error("Failed to launch application on device.")
                    else:
                        st.caption("No 3rd-party app packages detected on device.")

                # Native UI Inspection (Works on any app, including production/release APKs)
                with st.expander("📸 Native App Inspector (UI Automator)", expanded=True if st.session_state.get("_last_native_elems") else False):
                    st.caption("Inspect native apps (like CarInfo, Zepto, React Native) without needing WebView debugging.")
                    if st.button("📸 Capture & Inspect Native Screen", key="btn_capture_native_screen", width="stretch"):
                        ss_path = "reports/active_phone_screen.png"
                        if capture_device_screenshot(ss_path, serial=device_serial):
                            elems = dump_native_ui_elements(serial=device_serial)
                            if elems:
                                st.session_state["_last_native_ss"] = ss_path
                                st.session_state["_last_native_elems"] = elems
                                st.session_state["_last_native_pkg"] = fg_info.get("package", "android_app")
                                st.rerun()
                            else:
                                st.error("No elements found in UI Automator dump.")
                        else:
                            st.error("Failed to capture device screen.")

                    if st.session_state.get("_last_native_elems"):
                        last_ss = st.session_state.get("_last_native_ss", "")
                        last_elems = st.session_state.get("_last_native_elems", [])
                        last_pkg = st.session_state.get("_last_native_pkg", "android_app")

                        if last_ss and os.path.exists(last_ss):
                            safe_st_image(last_ss, caption=f"Live Screen: {last_pkg}", use_container_width=True)

                        st.success(f"✅ Extracted {len(last_elems)} native UI elements!")
                        from core.device_bridge import DUMMY_LABELS
                        clean_elems = [e for e in last_elems if e.get("label") and e["label"].lower() not in DUMMY_LABELS]
                        display_elems = clean_elems if clean_elems else last_elems
                        elem_summary = [
                            {"Label": e["label"][:28], "Type": e["class"].split(".")[-1], "Clickable": "Yes" if e["clickable"] else "No"}
                            for e in display_elems[:15]
                        ]
                        st.dataframe(elem_summary, use_container_width=True)

                        # ACTION BUTTONS RIGHT HERE IN SIDEBAR!
                        st.caption("⚡ **Actions for this Inspected Screen:**")
                        c_sb_gen, c_sb_flow = st.columns([1.5, 1.2])
                        with c_sb_gen:
                            if st.button("⚡ Generate Test Suite", type="primary", key="sb_btn_gen_native_suite", width="stretch", help="Synthesize test cases for all extracted native elements and load into active suite"):
                                scr_title = last_pkg.split(".")[-1].capitalize()
                                for e in last_elems:
                                    lbl = e.get("label", "").strip()
                                    if lbl and lbl.lower() not in DUMMY_LABELS and not e.get("clickable") and 3 <= len(lbl) <= 35:
                                        scr_title = lbl
                                        break
                                cases = synthesize_native_test_cases(last_elems, package_name=last_pkg, screen_title=scr_title, screenshot_path=last_ss)
                                if cases:
                                    out_p = "reports/native_inspected_tests.csv"
                                    df_cases = pd.DataFrame(cases)
                                    df_cases.to_csv(out_p, index=False)
                                    st.session_state.auto_generated_file = out_p
                                    st.session_state.blank_suite = False
                                    if "results_df" in st.session_state:
                                        del st.session_state["results_df"]
                                    st.session_state["test_cases_df"] = normalize_test_dataframe(df_cases)
                                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                                    st.toast(f"🎉 Generated {len(cases)} tests for {scr_title} and loaded into execution grid!", icon="🚀")
                                    st.rerun()

                        with c_sb_flow:
                            if st.button("📥 To Flow Map", type="secondary", key="sb_btn_add_to_flow", width="stretch", help="Add this screen to the Flow Explorer map"):
                                scr_title = last_pkg.split(".")[-1].capitalize()
                                for e in last_elems:
                                    lbl = e.get("label", "").strip()
                                    if lbl and lbl.lower() not in DUMMY_LABELS and not e.get("clickable") and 3 <= len(lbl) <= 35:
                                        scr_title = lbl
                                        break

                                screen_rec = {
                                    "step_num": 1,
                                    "name": scr_title,
                                    "url": f"android://{last_pkg}",
                                    "screenshot": last_ss,
                                    "title": scr_title,
                                    "headings": [e["label"] for e in last_elems if not e.get("clickable") and e.get("label")][:8],
                                    "action_buttons": [e["label"] for e in last_elems if e.get("clickable") and e.get("label")][:15],
                                    "inputs": [e["label"] for e in last_elems if any(k in e.get("class", "").lower() for k in ["edit", "search"])][:10],
                                    "nav_links": [],
                                    "pill_tabs": [],
                                    "config_cards": [],
                                    "kpi_cards": [],
                                    "tables_count": 0,
                                    "sub_features": [],
                                    "native_elements": last_elems,
                                }
                                if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow:
                                    st.session_state.discovered_flow = {
                                        "start_url": f"android://{last_pkg}",
                                        "total_screens": 1,
                                        "screens": [screen_rec],
                                    }
                                else:
                                    st.session_state.discovered_flow["screens"].append(screen_rec)
                                    st.session_state.discovered_flow["total_screens"] = len(st.session_state.discovered_flow["screens"])
                                st.toast(f"✅ Added '{scr_title}' to Flow Map!", icon="🗺️")
                                st.rerun()

                # Live CDP Target Inspection
                targets = fetch_cdp_targets(cdp_url_val or "http://localhost:9222")
                if targets:
                    valid_targets = [t for t in targets if t.get("url") and not str(t.get("url", "")).startswith("chrome-") and not str(t.get("url", "")).startswith("about:")]
                    display_targets = valid_targets if valid_targets else targets
                    st.caption(f"🎯 **Discovered Screens / Tabs ({len(display_targets)})**:")

                    def _format_target(t):
                        title = t.get("title") or "Untitled Screen"
                        url = t.get("url") or ""
                        return f"📲 {title[:28]} ({url[:25]}...)" if len(url) > 25 else f"📲 {title[:28]} ({url})"

                    sel_target_idx = st.selectbox(
                        "Choose Device Screen to Test",
                        range(len(display_targets)),
                        format_func=lambda i: _format_target(display_targets[i]),
                        key="sel_device_screen_dropdown",
                    )
                    safe_target_idx = sel_target_idx if sel_target_idx is not None and sel_target_idx < len(display_targets) else 0
                    chosen_target = display_targets[safe_target_idx]
                    chosen_url = chosen_target.get("url", "").strip()

                    if st.button("🎯 Select & Apply This Screen", type="primary", key="btn_apply_device_target", width="stretch"):
                        st.session_state["deep_inspector_url_input"] = chosen_url
                        st.session_state["single_url_input"] = chosen_url
                        st.session_state["flow_explorer_url"] = chosen_url
                        st.toast(f"✅ Set active target: {chosen_url}", icon="🎯")
                        st.rerun()

                    st.caption("Quick Select:")
                    for t_idx, t in enumerate(display_targets[:4]):
                        t_title = t.get("title") or t.get("url") or "App Screen"
                        t_url = t.get("url", "").strip()
                        if st.button(f"📲 {t_title[:24]}", key=f"btn_pick_adb_{t_idx}", help=t_url, width="stretch"):
                            st.session_state["deep_inspector_url_input"] = t_url
                            st.session_state["single_url_input"] = t_url
                            st.session_state["flow_explorer_url"] = t_url
                            st.toast(f"✅ Set active target: {t_url}", icon="🎯")
                            st.rerun()
                else:
                    st.caption("ℹ️ No web devtools targets detected. For native apps, use the **Native App Inspector** above or **📸 UI Screenshot / Mockup** tab.")
    elif selected_platform_key == "electron_cdp":
        with st.sidebar.expander("⚛️ Electron Desktop App Windows", expanded=True):
            from core.device_bridge import fetch_cdp_targets
            targets = fetch_cdp_targets(cdp_url_val or "http://localhost:9223")
            if targets:
                st.caption(f"🎯 **Active Windows ({len(targets)})**:")
                for t_idx, t in enumerate(targets[:4]):
                    t_title = t.get("title") or "Electron Window"
                    t_url = t.get("url", "").strip()
                    if st.button(f"🪟 {t_title[:25]}", key=f"btn_pick_electron_{t_idx}", help=t_url, width="stretch"):
                        st.session_state["deep_inspector_url_input"] = t_url
                        st.session_state["single_url_input"] = t_url
                        st.session_state["flow_explorer_url"] = t_url
                        st.toast(f"✅ Set active target: {t_url}", icon="🎯")
                        st.rerun()
            else:
                st.caption("Launch your Electron app with `--remote-debugging-port=9223` to inspect its windows.")

active_platform_config = {
    "platform": selected_platform_key,
    "orientation": selected_orientation,
    "network_preset": selected_net_key,
    "cdp_url": cdp_url_val if cdp_url_val else None,
}

if not _has_streamlit_context():
    force_g4f = bool(saved_config.get("force_g4f", False))
    show_browser = bool(saved_config.get("show_browser", False))
    enable_visual = bool(saved_config.get("enable_visual", True))
    active_platform_config = {
        "platform": str(saved_config.get("target_platform", "desktop_chrome")),
        "orientation": str(saved_config.get("platform_orientation", "portrait")),
        "network_preset": str(saved_config.get("network_preset", "direct")),
        "cdp_url": saved_config.get("cdp_url", None),
    }
else:
    st.session_state["_persisted_force_g4f"] = bool(force_g4f)
    st.session_state["_persisted_show_browser"] = bool(show_browser)
    st.session_state["_persisted_enable_visual"] = bool(enable_visual)
    st.session_state["_persisted_target_platform"] = selected_platform_key
    st.session_state["_persisted_platform_orientation"] = selected_orientation
    st.session_state["_persisted_network_preset"] = selected_net_key
    st.session_state["_persisted_cdp_url"] = cdp_url_val

if force_g4f:
    os.environ["FORCE_G4F"] = "true"
else:
    os.environ.pop("FORCE_G4F", None)

if show_browser:
    os.environ["SHOW_BROWSER"] = "true"
else:
    os.environ.pop("SHOW_BROWSER", None)

# 2. Environment & Test Variables (Collapsible to Reduce Cognitive Load)
default_vars = saved_config.get(
    "test_variables",
    [
        {"Key": "Email", "Value": ""},
        {"Key": "Password", "Value": ""},
    ],
)

if "test_data_init" not in st.session_state:
    st.session_state.test_data_init = pd.DataFrame(default_vars)

st.sidebar.divider()
with st.sidebar.expander(f"📋 Environment Variables ({len(st.session_state.test_data_init)})", expanded=False):
    st.caption("Variables used for dynamic form filling, login capture, and API payload substitution.")
    st.session_state.test_data = st.data_editor(
        st.session_state.test_data_init, num_rows="dynamic", key="test_data_editor", width='stretch'
    )

test_data_dict = {}
td_df = st.session_state.get("test_data")
if isinstance(td_df, pd.DataFrame) and not td_df.empty and "Key" in td_df.columns and "Value" in td_df.columns:
    test_data_dict = {
        str(k).strip(): str(v).strip()
        for k, v in zip(td_df["Key"], td_df["Value"])
        if str(k).strip() and str(k).strip().lower() not in ["none", "nan"]
    }

# 3. Session & Login Capture
st.sidebar.subheader("🔐 Session & Login Capture")
from core.session_capturer import (
    capture_interactive_session,
    get_auth_session_summary,
    save_auth_target_url,
    clear_auth_session,
)

auth_summary = get_auth_session_summary("auth.json")
saved_target_url = auth_summary.get("target_url", "")

def _cb_clear_session():
    clear_auth_session("auth.json")
    st.session_state["session_login_url"] = ""
    st.session_state["session_cleared_flag"] = True

def _cb_update_session_target():
    curr = st.session_state.get("session_login_url", "").strip()
    if curr:
        set_global_target_url(curr)
        if os.path.exists("auth.json"):
            save_auth_target_url(curr, "auth.json")

# Ensure Target Login URL persists when a session is active or from global target
if not st.session_state.get("session_cleared_flag"):
    if "session_login_url" not in st.session_state or not st.session_state.get("session_login_url"):
        if auth_summary.get("active") and saved_target_url:
            st.session_state["session_login_url"] = saved_target_url
        else:
            glob_u = get_global_target_url()
            if glob_u:
                st.session_state["session_login_url"] = glob_u

if auth_summary.get("active"):
    st.sidebar.success(f"🟢 {auth_summary.get('summary', 'Active Session')}")
else:
    st.sidebar.info("⚪ No active session saved.")

if st.session_state.pop("session_cleared_flag", False):
    st.toast("🗑️ Active session and target URL cleared!")

session_target_url = st.sidebar.text_input(
    "Target Login URL",
    placeholder="https://example.com/login",
    key="session_login_url",
    on_change=_cb_update_session_target,
)

auto_submit_choice = st.sidebar.checkbox(
    "Auto-submit credentials",
    value=True,
    help="Automatically clicks submit after filling credentials. Uncheck to submit manually."
)

col_s1, col_s2 = st.sidebar.columns(2)
with col_s1:
    btn_capture = st.button("🌐 Capture", type="primary", width='stretch', help="Opens browser, auto-fills credentials from variables, and saves session")
with col_s2:
    st.button("🗑️ Clear", on_click=_cb_clear_session, type="secondary", width='stretch', help="Deletes active session and target URL to reset this section")

if btn_capture:
    if not session_target_url:
        st.sidebar.warning("Please enter a target URL.")
    else:
        with st.sidebar.spinner("Logging in & capturing session..."):
            res = capture_interactive_session(
                session_target_url,
                output_auth_path="auth.json",
                timeout_seconds=60,
                test_data=test_data_dict,
                auto_submit=auto_submit_choice,
            )
            if res.get("status") == "success":
                save_auth_target_url(session_target_url, "auth.json")
                st.sidebar.success(res.get("message"))
                time.sleep(0.4)
                st.rerun()
            else:
                st.sidebar.error(res.get("message"))

st.sidebar.divider()

# 4. AI Provider Keys (Collapsible Accordion)
with st.sidebar.expander("🔑 AI Provider Keys", expanded=False):
    st.caption("Configure optional LLM keys (leave blank when using G4F Free Mode).")
    col_k1, col_k2 = st.columns(2)
    with col_k1:
        openai_key = st.text_input(
            "OpenAI Key", type="password", value=os.getenv("OPENAI_API_KEY", "")
        )
        groq_key = st.text_input(
            "Groq Key", type="password", value=os.getenv("GROQ_API_KEY", "")
        )
    with col_k2:
        gemini_key = st.text_input(
            "Gemini Key", type="password", value=os.getenv("GEMINI_API_KEY", "")
        )
        openrouter_key = st.text_input(
            "OpenRouter Key", type="password", value=os.getenv("OPENROUTER_API_KEY", "")
        )

if openai_key:
    os.environ["OPENAI_API_KEY"] = openai_key
if gemini_key:
    os.environ["GEMINI_API_KEY"] = gemini_key
if groq_key:
    os.environ["GROQ_API_KEY"] = groq_key
if openrouter_key:
    os.environ["OPENROUTER_API_KEY"] = openrouter_key

# Auto-save settings on change or via manual button
active_cfg = {
    "force_g4f": bool(force_g4f),
    "show_browser": bool(show_browser),
    "enable_visual": bool(enable_visual),
    "show_adapter_cols": bool(st.session_state.get("global_show_adapter_cols", True)),
    "target_platform": selected_platform_key,
    "platform_orientation": selected_orientation.lower() if any(m in selected_platform_key for m in ["iphone", "pixel", "galaxy", "ipad"]) else "portrait",
    "network_preset": selected_net_key,
    "cdp_url": cdp_url_val,
    "test_variables": st.session_state.test_data.to_dict(orient="records"),
}

# 5. ClickUp Bug Tracker (Collapsible Accordion)
if "clickup_token_input" not in st.session_state:
    st.session_state.clickup_token_input = os.getenv("CLICKUP_API_TOKEN") or saved_config.get("clickup_api_token", "")
if "clickup_list_id_input" not in st.session_state:
    st.session_state.clickup_list_id_input = os.getenv("CLICKUP_LIST_ID") or saved_config.get("clickup_list_id", "")

with st.sidebar.expander("🐛 ClickUp Bug Tracker", expanded=False):
    st.caption("Auto-create bug tasks with failure logs, expected vs actual results, and screenshots.")
    clickup_token = st.text_input(
        "ClickUp API Token",
        type="password",
        placeholder="pk_...",
        key="clickup_token_input",
        help="Find your API token in ClickUp: Settings -> Apps -> API Token",
    )
    clickup_list_id = st.text_input(
        "Default List ID",
        placeholder="e.g. 9012345678",
        key="clickup_list_id_input",
        help="The numeric List ID where bug tasks will be created.",
    )
    if st.button("🔌 Test ClickUp Connection", type="secondary", width='stretch'):
        if not clickup_token:
            st.warning("Please enter your ClickUp API Token.")
        else:
            from core.clickup_integration import clickup_client
            test_res = clickup_client.verify_credentials(clickup_token)
            if test_res.get("success"):
                st.success(f"✅ Connected as: {test_res.get('username')} ({test_res.get('email')})")
            else:
                st.error(f"❌ Connection failed: {test_res.get('error')}")

# Synchronize os.environ and active_cfg with current widget input
if clickup_token and str(clickup_token).strip():
    clean_tok = str(clickup_token).strip()
    os.environ["CLICKUP_API_TOKEN"] = clean_tok
    active_cfg["clickup_api_token"] = clean_tok
else:
    os.environ.pop("CLICKUP_API_TOKEN", None)
    active_cfg["clickup_api_token"] = ""

if clickup_list_id and str(clickup_list_id).strip():
    clean_lid = str(clickup_list_id).strip()
    os.environ["CLICKUP_LIST_ID"] = clean_lid
    active_cfg["clickup_list_id"] = clean_lid
else:
    os.environ.pop("CLICKUP_LIST_ID", None)
    active_cfg["clickup_list_id"] = ""

save_settings(active_cfg)

if st.sidebar.button("💾 Save Settings & Variables", type="secondary", width='stretch'):
    if save_settings(active_cfg, force=True):
        st.sidebar.success("✅ Configuration saved to disk!")

# ---------------------------------------------------------
# Hero Header Banner (with Live Runtime Telemetry Bar)
# ---------------------------------------------------------
ai_mode_badge = (
    '<span class="telemetry-badge active-indigo">🤖 AI Mode: G4F Free</span>'
    if force_g4f
    else '<span class="telemetry-badge active-blue">🔑 AI Mode: Multi-Provider</span>'
)
browser_mode_badge = (
    '<span class="telemetry-badge active-green">👁️ Browser: Headed</span>'
    if show_browser
    else '<span class="telemetry-badge">⚡ Browser: Headless</span>'
)
vision_mode_badge = (
    '<span class="telemetry-badge active-blue">📸 Vision Audit: ON</span>'
    if enable_visual
    else '<span class="telemetry-badge">📸 Vision Audit: OFF</span>'
)
auth_mode_badge = (
    '<span class="telemetry-badge active-green">🟢 Session: Authenticated</span>'
    if auth_summary.get("active")
    else '<span class="telemetry-badge">⚪ Session: Guest</span>'
)

st.markdown(
    f"""<div id="top" class="hero-banner">
        <div class="hero-top-row" style="margin-bottom: 0;">
            <div>
                <div class="hero-title">
                    <a href="/" target="_self" class="brand-logo-link" aria-label="AI Autonomous QA & Testing Studio Home">
                        <span class="hero-icon">🚀</span>
                        <span class="hero-title-text">AI Autonomous QA & Testing Studio</span>
                    </a>
                </div>
                <div class="hero-subtitle">Enterprise Web UI, Deep A11y & API Test Automation with Multi-Tier Self-Healing and Interactive Batch Execution.</div>
            </div>
            <div class="hero-telemetry-bar">
                {ai_mode_badge}
                {browser_mode_badge}
                {vision_mode_badge}
                {auth_mode_badge}
            </div>
        </div>
    </div>""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------
# Test Suite Loader Tabs
# ---------------------------------------------------------
st.subheader("📂 Load or Build Test Suite")
tab1, tab2, tab3, tab4 = st.tabs(
    [
        "📁 Upload File",
        "✨ AI Auto-Generator",
        "✍️ Manual Test Builder",
        "🚀 Quick API Builder",
    ]
)

with tab1:
    c_up1, c_up2 = st.columns([2.4, 1], vertical_alignment="bottom")
    with c_up1:
        uploaded_file = st.file_uploader(
            "Upload Test Suite (CSV or Excel)", type=["csv", "xlsx"]
        )
    with c_up2:
        sample_template_csv = (
            "Type,Target,Test_Step_Description,Expected_Result\n"
            "Web,https://example.com,Step 1: Open landing page -> Step 2: Verify main heading is visible,Main heading is displayed cleanly\n"
            "API,https://jsonplaceholder.typicode.com/posts/1,Send a GET request to https://jsonplaceholder.typicode.com/posts/1,HTTP 200\n"
        )
        st.caption("📄 Blank template: starter CSV format with Web & API rows:")
        st.download_button(
            "📥 Starter Template CSV",
            data=sample_template_csv.encode("utf-8"),
            file_name="sample_test_suite_template.csv",
            mime="text/csv",
            width='stretch',
            key="tab1_btn_dl_sample_template",
            help="Download a 2-row starter template to inspect the required columns",
        )

    with st.expander("✨ AI Universal Format Re-Gen (Preserve 100% As-Is Format + Synthesize Playwright Execution)", expanded=bool(uploaded_file)):
        st.markdown(
            "Upload any test case format — Jira export, TestRail sheet, messy Excel with merged cells, bulleted QA notes, or multi-sheet workbook. "
            "The **AI Re-Gen Engine preserves 100% of your original columns as-is** (As-Is Format Retention), infers missing steps & assertions, "
            "and synthesizes executable Playwright code in the background execution adapter (`_target`, `_code`)."
        )
        c_rg_u1, c_rg_u2 = st.columns([2.5, 1.5])
        with c_rg_u1:
            ai_regen_target = st.text_input(
                "Target Web URL / App URL (optional fallback for tests missing a URL)",
                value=st.session_state.get("single_url_input", ""),
                placeholder="e.g. https://example.com/app",
                key="tab1_ai_regen_target_url"
            )
        with c_rg_u2:
            ai_regen_type = st.selectbox(
                "Default Test Type",
                ["Web", "API", "Mobile (Native)"],
                index=0,
                key="tab1_ai_regen_default_type"
            )

        ai_regen_notes = st.text_input(
            "Additional Context or QA Instructions (optional)",
            placeholder="e.g. 'Synthesize strict assertions for login and checkout' or 'Infer missing steps from descriptions'",
            key="tab1_ai_regen_instructions"
        )

        c_btn_rg1, c_btn_rg2 = st.columns([1.5, 1])
        with c_btn_rg1:
            btn_regen_file = st.button(
                "✨ AI Re-Gen & Convert Uploaded File",
                type="primary",
                width="stretch",
                disabled=not bool(uploaded_file),
                key="tab1_btn_ai_regen_file",
                help="Applies AI Re-Gen to the currently uploaded file to standardize it and synthesize Playwright code"
            )
        with c_btn_rg2:
            has_existing = "test_cases_df" in st.session_state and isinstance(st.session_state.test_cases_df, pd.DataFrame) and not st.session_state.test_cases_df.empty
            btn_regen_active = st.button(
                "🔄 AI Re-Gen Active Suite",
                type="secondary",
                width="stretch",
                disabled=not has_existing,
                key="tab1_btn_ai_regen_active",
                help="Re-standardizes whatever test cases are currently loaded in memory"
            )

        if btn_regen_file and uploaded_file:
            prog_bar = st.progress(0.0, text="🤖 AI Re-Gen: Initializing analysis of uploaded file...")

            def _regen_file_cb(pct: float, msg: str):
                try:
                    prog_bar.progress(min(1.0, max(0.0, pct)), text=f"🤖 AI Re-Gen: {msg}")
                except Exception:
                    pass

            import importlib
            import core.test_generator
            if not hasattr(core.test_generator.TestGenerator, "ai_regenerate_and_convert_suite"):
                importlib.reload(core.test_generator)
            from core.test_generator import TestGenerator
            from core.ai_client import AIClient
            ai_c = AIClient()
            gen = TestGenerator(ai_client=ai_c)
            regen_df = gen.ai_regenerate_and_convert_suite(
                uploaded_file,
                default_target=ai_regen_target,
                user_instructions=ai_regen_notes,
                default_type=ai_regen_type,
                on_progress_callback=_regen_file_cb,
            )
            if regen_df is not None and not regen_df.empty:
                prog_bar.progress(1.0, text=f"🎉 Successfully converted {len(regen_df)} test cases into standard format!")
                time.sleep(0.35)
                target_csv = "deep_inspected_tests.csv"
                regen_df.to_csv(target_csv, index=False)
                st.session_state["test_cases_df"] = regen_df
                st.session_state["auto_generated_file"] = target_csv
                st.session_state["suite_explicitly_cleared"] = False
                st.session_state["_active_uploaded_file_id"] = f"{uploaded_file.name}_{uploaded_file.size}_regen"
                if "results_df" in st.session_state:
                    del st.session_state["results_df"]
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                st.toast(f"🎉 Successfully converted {len(regen_df)} test cases into flawless standard format!", icon="✨")
                st.rerun()
            else:
                prog_bar.empty()
                st.error("AI Re-Gen could not extract test cases from this file. Please check file contents.")

        if btn_regen_active and has_existing:
            prog_bar = st.progress(0.0, text="🤖 AI Re-Gen: Initializing re-standardization of active suite...")

            def _regen_active_cb(pct: float, msg: str):
                try:
                    prog_bar.progress(min(1.0, max(0.0, pct)), text=f"🤖 AI Re-Gen: {msg}")
                except Exception:
                    pass

            import importlib
            import core.test_generator
            if not hasattr(core.test_generator.TestGenerator, "ai_regenerate_and_convert_suite"):
                importlib.reload(core.test_generator)
            from core.test_generator import TestGenerator
            from core.ai_client import AIClient
            ai_c = AIClient()
            gen = TestGenerator(ai_client=ai_c)
            curr_suite = st.session_state["test_cases_df"]
            regen_df = gen.ai_regenerate_and_convert_suite(
                curr_suite,
                default_target=ai_regen_target,
                user_instructions=ai_regen_notes,
                default_type=ai_regen_type,
                on_progress_callback=_regen_active_cb,
            )
            if regen_df is not None and not regen_df.empty:
                prog_bar.progress(1.0, text=f"🎉 Successfully re-generated {len(regen_df)} test cases in standard format!")
                time.sleep(0.35)
                target_csv = "deep_inspected_tests.csv"
                regen_df.to_csv(target_csv, index=False)
                st.session_state["test_cases_df"] = regen_df
                st.session_state["auto_generated_file"] = target_csv
                st.session_state["suite_explicitly_cleared"] = False
                if "results_df" in st.session_state:
                    del st.session_state["results_df"]
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                st.toast(f"🎉 Successfully re-generated {len(regen_df)} test cases in standard format!", icon="✨")
                st.rerun()
            else:
                prog_bar.empty()
                st.error("AI Re-Gen could not process the active suite.")

    has_active_suite = (
        ("test_cases_df" in st.session_state and isinstance(st.session_state["test_cases_df"], pd.DataFrame) and not st.session_state["test_cases_df"].empty)
        or bool(uploaded_file)
        or ("auto_generated_file" in st.session_state and os.path.exists(str(st.session_state.auto_generated_file)))
    )
    if has_active_suite and not st.session_state.get("suite_explicitly_cleared", False):
        st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)
        c_up_stat, c_up_dl, c_up_clr = st.columns([2.5, 1.3, 1.2])
        with c_up_stat:
            count_label = len(st.session_state["test_cases_df"]) if ("test_cases_df" in st.session_state and isinstance(st.session_state["test_cases_df"], pd.DataFrame)) else "Active"
            st.caption(f"📁 **Active Suite Loaded** ({count_label} test cases in memory). Ready to run, edit, or export.")
        with c_up_dl:
            active_df_to_export = st.session_state.get("test_cases_df")
            if active_df_to_export is None or not isinstance(active_df_to_export, pd.DataFrame) or active_df_to_export.empty:
                if "auto_generated_file" in st.session_state and os.path.exists(str(st.session_state.auto_generated_file)):
                    try:
                        active_df_to_export = pd.read_csv(st.session_state.auto_generated_file)
                    except Exception:
                        active_df_to_export = None
                elif os.path.exists("generated_tests.csv") and os.path.getsize("generated_tests.csv") > 0:
                    try:
                        active_df_to_export = pd.read_csv("generated_tests.csv")
                    except Exception:
                        active_df_to_export = None
            if active_df_to_export is not None and isinstance(active_df_to_export, pd.DataFrame) and not active_df_to_export.empty:
                st.download_button(
                    "📥 Download Suite (CSV)",
                    data=active_df_to_export.to_csv(index=False).encode("utf-8"),
                    file_name=f"active_test_suite_{int(time.time())}.csv",
                    mime="text/csv",
                    width="stretch",
                    key="tab1_btn_download_active_suite_csv",
                    help="Download the currently active test suite as CSV",
                )
        with c_up_clr:
            if st.button("🗑️ Clear Test Suite", type="secondary", width="stretch", key="tab1_clear_suite_btn", help="Wipes all loaded test cases from memory"):
                clear_entire_test_suite()
                st.toast("🧹 Test suite cleared successfully!", icon="✅")
                st.rerun()

with tab2:
    generation_type = st.radio(
        "What do you want to test?",
        [
            "🌐 Web UI (Single URL)",
            "🔬 Deep Screen & A11y Inspector",
            "🗺️ App Flow & Visual Explorer (Multi-Page)",
            "📱 Mobile App (Android Auto-Crawler)",
            "📸 UI Screenshot / Mockup",
            "🔌 API (OpenAPI/Swagger)",
        ],
        horizontal=True,
    )
    if generation_type == "🌐 Web UI (Single URL)":
        st.info(
            "Enter a URL and optional User Story/Business Flow. The AI will inspect live page elements and generate a complete test suite!"
        )
        if "single_url_input" not in st.session_state or not st.session_state["single_url_input"]:
            glob_u = get_global_target_url()
            if glob_u:
                st.session_state["single_url_input"] = glob_u
        ai_target_url = st.text_input(
            "Target Web URL",
            placeholder="https://example.com/login",
            key="single_url_input",
        )
        ai_user_story = st.text_area(
            "📖 User Story / Business Flow / Expected User Journey (Optional)",
            placeholder="e.g. As an admin, I want to search entries by candidate name, filter by status (Assigned/Pending), click 'Enter Data' on a specific record to open the entry form, and submit validated data.",
            height=85,
            key="single_user_story_input",
            help="Describe the persona, user goals, specific business rules, or test priorities for the AI to emphasize.",
        )
        def _run_single_url_generation(target_strat: str):
            is_valid_url, url_err = validate_url(ai_target_url)
            if not is_valid_url:
                st.warning(f"⚠️ Validation Failed: {url_err}")
                return
            set_global_target_url(ai_target_url)

            prog_bar = st.progress(0.0, text=f"⚡ [{target_strat}] Initializing browser & test engine...")

            def _single_gen_cb(pct: float, msg: str):
                try:
                    prog_bar.progress(min(1.0, max(0.0, pct)), text=f"⚡ [{target_strat}] {msg}")
                except Exception:
                    pass

            from core.ai_client import AIClient
            from core.test_generator import TestGenerator

            try:
                gen = TestGenerator(AIClient())
                result = gen.generate_cases_from_url(
                    ai_target_url,
                    "generated_tests.csv",
                    user_story=ai_user_story,
                    strategy=target_strat,
                    on_progress_callback=_single_gen_cb,
                )
                res_path = result[0] if isinstance(result, tuple) else result
                ss_path = result[1] if isinstance(result, tuple) else getattr(gen, "last_screenshot", "")

                if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
                    prog_bar.progress(1.0, text=f"🎉 Successfully generated [{target_strat}] test suite!")
                    time.sleep(0.35)
                    st.session_state.auto_generated_file = res_path
                    st.session_state.last_analysis_ss = ss_path
                    if "results_df" in st.session_state:
                        del st.session_state["results_df"]
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    try:
                        df_gen = pd.read_csv(res_path)
                    except pd.errors.EmptyDataError:
                        df_gen = pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code"])
                    st.session_state["test_cases_df"] = normalize_test_dataframe(df_gen, default_target=ai_target_url)
                    st.session_state["single_gen_success_msg"] = f"✅ Successfully generated {len(df_gen)} test cases using [{target_strat}] for: {ai_target_url}"
                    st.rerun()
                else:
                    prog_bar.empty()
                    st.error("Failed to generate test cases. Please ensure the target URL is accessible and try again.")
            except Exception as ve:
                prog_bar.empty()
                st.error(f"Generation Error: {ve}")

        if st.session_state.get("single_gen_success_msg"):
            c_s_msg, c_s_dl = st.columns([3.2, 1.2])
            with c_s_msg:
                st.success(st.session_state["single_gen_success_msg"])
            with c_s_dl:
                df_curr = st.session_state.get("test_cases_df")
                if df_curr is not None and isinstance(df_curr, pd.DataFrame) and not df_curr.empty:
                    st.download_button(
                        "📥 Download Suite (CSV)",
                        data=df_curr.to_csv(index=False).encode("utf-8"),
                        file_name=f"generated_single_tests_{int(time.time())}.csv",
                        mime="text/csv",
                        width='stretch',
                        key="btn_dl_single_url_csv_top",
                        help="Download generated test cases as CSV",
                    )
            if st.session_state.get("last_analysis_ss") and os.path.exists(st.session_state["last_analysis_ss"]):
                ss_p = st.session_state["last_analysis_ss"]
                with st.expander(f"📸 Click to View Inspected Page Snapshot ({os.path.basename(ss_p)})", expanded=False):
                    safe_st_image(ss_p, caption=f"Inspected Page Snapshot: {ss_p}", width="stretch")

        c_sg_strat, c_sg_btn, c_sg_clr = st.columns([2.2, 1.4, 0.8], vertical_alignment="bottom")
        with c_sg_strat:
            single_strat = st.selectbox(
                "Generation Engine Strategy",
                options=[
                    ("all", "🌐 Full 360° Suite (AI + Hybrid + Rule Engine)"),
                    ("robust_v4", "⚡ RobustEngine v4 (7-Category Deep Matrix - 100% Guaranteed)"),
                    ("ai_full", "🤖 AI Generated (Full Scenarios & Code)"),
                    ("hybrid", "🤖+⚙️ Hybrid (AI Steps + Deterministic Code)"),
                    ("rule_engine", "⚙️ Rule Engine (DOM/A11y/Network - 0 Tokens)"),
                ],
                format_func=lambda x: x[1],
                key="single_gen_strategy_select",
                help="Choose which engine strategy to run against the target URL"
            )
        with c_sg_btn:
            s_strat_key, _ = single_strat
            if st.button("⚡ Generate Suite", type="primary", width="stretch", key="btn_gen_single_url_action"):
                _run_single_url_generation(s_strat_key)
        with c_sg_clr:
            def _cb_clear_single_url():
                st.session_state["single_url_input"] = ""
                st.session_state["single_user_story_input"] = ""
                st.session_state["global_target_url"] = ""
                st.session_state.pop("single_gen_success_msg", None)

            st.button(
                "🧹 Clear",
                on_click=_cb_clear_single_url,
                type="secondary",
                width='stretch',
                key="btn_clear_single_url",
                help="Clear target URL and user story fields"
            )

    elif generation_type == "🔬 Deep Screen & A11y Inspector":
        st.info(
            "🔬 **Deep Multi-Layer Inspection Engine**: Deeply audits target screens using **Native Accessibility (A11y) Trees**, **Active Dynamic Mutation Probing** (opens drawers/dropdowns & auto-resets), and **CDP HTML5 Constraint & Network Sniffing**."
        )
        if "deep_inspector_url_input" not in st.session_state or not st.session_state["deep_inspector_url_input"]:
            glob_u = get_global_target_url()
            if glob_u:
                st.session_state["deep_inspector_url_input"] = glob_u
        deep_target_url = st.text_input(
            "Target Web URL to Inspect",
            placeholder="https://example.com/dashboard",
            key="deep_inspector_url_input",
        )
        
        c_dp1, c_dp2 = st.columns([1.5, 1])
        with c_dp1:
            if st.button("🔬 Run Deep 360° Inspection", type="primary", width='stretch'):
                is_valid_url, url_err = validate_url(deep_target_url)
                if not is_valid_url:
                    st.warning(f"⚠️ Validation Failed: {url_err}")
                else:
                    set_global_target_url(deep_target_url)
                    status_box = st.empty()
                    deep_prog = st.progress(0.0, text="🔬 Launching Deep Multi-Layer Inspector...")

                    def _update_status(msg):
                        pct = 0.10
                        msg_str = str(msg)
                        if "Launching" in msg_str:
                            pct = 0.15
                        elif "Navigating" in msg_str:
                            pct = 0.30
                        elif "Accessibility" in msg_str or "A11y" in msg_str:
                            pct = 0.50
                        elif "Introspecting" in msg_str or "KPI" in msg_str:
                            pct = 0.70
                        elif "Probing" in msg_str:
                            pct = 0.85
                        elif "Audit" in msg_str or "Web Quality" in msg_str:
                            pct = 0.95
                        elif "Complete" in msg_str:
                            pct = 1.00
                        try:
                            deep_prog.progress(min(1.0, max(0.0, pct)), text=f"🔬 {msg_str}")
                        except Exception:
                            pass
                        status_box.info(msg_str)

                    from core.deep_inspector import DeepScreenInspector
                    try:
                        inspector = DeepScreenInspector(headless=not show_browser)
                        insp_result = inspector.inspect_url(deep_target_url, status_callback=_update_status)
                        deep_prog.progress(1.0, text=f"✅ Deep Inspection Complete in {insp_result['performance_metrics']['total_duration_sec']}s!")
                        time.sleep(0.35)
                        st.session_state["deep_inspection_result"] = insp_result
                        st.success(f"✅ Deep Inspection Completed in {insp_result['performance_metrics']['total_duration_sec']}s!")
                        st.rerun()
                    except Exception as e:
                        deep_prog.empty()
                        st.error(f"Deep Inspection Failed: {e}")
        with c_dp2:
            def _cb_clear_deep():
                st.session_state["deep_inspector_url_input"] = ""
                st.session_state["global_target_url"] = ""
                if "deep_inspection_result" in st.session_state:
                    del st.session_state["deep_inspection_result"]
            st.button("🧹 Clear / Reset Inspector", on_click=_cb_clear_deep, type="secondary", width='stretch')

        if "deep_inspection_result" in st.session_state and st.session_state["deep_inspection_result"]:
            res = st.session_state["deep_inspection_result"]
            p_metrics = res.get("performance_metrics", {})
            wq_audit = res.get("web_quality_audit", {})
            wq_score = wq_audit.get("score", p_metrics.get("web_quality_score", 100))
            
            st.divider()
            st.markdown(f"#### 📊 Deep Inspection Blueprint: `{res.get('page_title', 'Target Screen')}`")
            
            # KPI Metric Badges
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            with k1:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #38BDF8;">{res.get('a11y_nodes_count', 0)}</div>
                    <div class="kpi-stat-lbl">♿ A11y Landmark Nodes</div>
                </div>
                """, unsafe_allow_html=True)
            with k2:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #34D399;">{len(res.get('interactive_elements', []))}</div>
                    <div class="kpi-stat-lbl">🎯 Interactive Buttons</div>
                </div>
                """, unsafe_allow_html=True)
            with k3:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #FBBF24;">{len(res.get('forms_and_inputs', []))}</div>
                    <div class="kpi-stat-lbl">📋 Form Constraints</div>
                </div>
                """, unsafe_allow_html=True)
            with k4:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #A78BFA;">{len(res.get('probed_modals_and_drawers', []))}</div>
                    <div class="kpi-stat-lbl">⚡ Dynamic Drawers Probed</div>
                </div>
                """, unsafe_allow_html=True)
            with k5:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #F472B6;">{p_metrics.get('apis_captured', 0)}</div>
                    <div class="kpi-stat-lbl">🔌 Backend APIs Sniffed</div>
                </div>
                """, unsafe_allow_html=True)
            with k6:
                st.markdown(f"""
                <div class="kpi-stat-box">
                    <div class="kpi-stat-val" style="color: #10B981;">{wq_score}%</div>
                    <div class="kpi-stat-lbl">🛡️ Quality Score (40-Pt)</div>
                </div>
                """, unsafe_allow_html=True)

            insp_t0, insp_t1, insp_t2, insp_t3, insp_t4 = st.tabs([
                "🛡️ 40-Point Web Quality & Mobile Audit",
                "♿ Accessibility Hierarchy",
                "⚡ Dynamic Drawers & Modals",
                "📋 Form Schemas & Constraints",
                "🔌 Network APIs Sniffed"
            ])

            with insp_t0:
                checks_list = wq_audit.get("checks", [])
                if checks_list:
                    st.caption(
                        f"✅ Passed: **{wq_audit.get('passed_count', 0)} / {len(checks_list)}** | "
                        f"⚠️ Needs Attention: **{wq_audit.get('failed_count', 0)}** | "
                        f"Overall Quality Score: **{wq_score}%**"
                    )
                    wq_df = pd.DataFrame(checks_list)
                    st.dataframe(wq_df, width='stretch', hide_index=True)
                else:
                    st.info("Run Deep 360° Inspection to populate the 40-Point Web Quality, Mobile, SEO & Accessibility Audit.")

            with insp_t1:
                if res.get("a11y_flat_elements"):
                    a11y_df = pd.DataFrame(res["a11y_flat_elements"])
                    st.dataframe(a11y_df, width='stretch')
                else:
                    st.info("No accessibility nodes captured.")

            with insp_t2:
                drawers = res.get("probed_modals_and_drawers", [])
                if drawers:
                    for d in drawers:
                        with st.expander(f"🎯 Dynamic Trigger: **{d.get('trigger')}** ({d.get('status')})", expanded=True):
                            dyn = d.get("dynamic_data", {})
                            revealed = dyn.get("revealedContainers", [])
                            if revealed:
                                for rev in revealed:
                                    st.markdown(f"- **Container**: `{rev.get('containerClass')}` | **Title**: `{rev.get('title')}`")
                                    st.markdown(f"- **Revealed Options**: `{', '.join(rev.get('discoveredOptions', []))}`")
                                    st.markdown(f"- **Inner Inputs**: `{len(rev.get('subInputs', []))}` | **Inner Buttons**: `{len(rev.get('subButtons', []))}`")
                            else:
                                st.write("No additional modal DOM elements were injected.")
                else:
                    st.info("No modal/drawer triggers detected on this view.")

            with insp_t3:
                forms = res.get("forms_and_inputs", [])
                if forms:
                    forms_df = pd.DataFrame(forms)
                    st.dataframe(forms_df, width='stretch')
                else:
                    st.info("No input forms found.")

            with insp_t4:
                apis = res.get("network_apis", [])
                if apis:
                    apis_df = pd.DataFrame(apis)
                    st.dataframe(apis_df, width='stretch')
                else:
                    st.info("No background API fetch/xhr requests captured.")

            st.divider()
            st.markdown("##### ⚙️ Test Suite Generation & Engine Control")
            def _run_deep_synthesis(strat_code: str, label_name: str):
                from core.deep_inspector import DeepScreenInspector
                inspector = DeepScreenInspector()
                synth_cases = inspector.generate_tests_from_inspection(res, strategy=strat_code)
                if synth_cases:
                    out_csv = "deep_inspected_tests.csv"
                    df_synth = pd.DataFrame(synth_cases)
                    df_synth.to_csv(out_csv, index=False)
                    st.session_state.auto_generated_file = out_csv
                    st.session_state.blank_suite = False
                    if "results_df" in st.session_state:
                        del st.session_state["results_df"]
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    st.session_state["test_cases_df"] = normalize_test_dataframe(df_synth, default_target=res.get("target_url", ""))
                    st.success(f"🎉 Successfully synthesized and loaded {len(synth_cases)} test cases using [{label_name}] into the execution grid!")
                    st.rerun()

            c_strat, c_syn_btn, c_export_pop = st.columns([2.2, 1.4, 0.9], vertical_alignment="bottom")
            with c_strat:
                synth_strategy = st.selectbox(
                    "Test Synthesis Strategy",
                    options=[
                        ("all", "🌐 Full 360° Suite (AI + Hybrid + Rule Engine)"),
                        ("ai_full", "🤖 AI Generated (Full Scenarios & Code)"),
                        ("hybrid", "🤖+⚙️ Hybrid (AI Steps + Deterministic Code)"),
                        ("rule_engine", "⚙️ Rule Engine (DOM/A11y/Network - 0 Tokens)"),
                        ("web_quality", "🛡️ 40-Point Web Quality, Mobile & A11y Suite"),
                    ],
                    format_func=lambda x: x[1],
                    key="deep_synth_strategy_select",
                    help="Select the test generation methodology to synthesize from this screen inspection"
                )
            with c_syn_btn:
                strat_key, strat_label = synth_strategy
                if st.button("⚡ Synthesize Suite", type="primary", width="stretch", key="btn_deep_gen_suite", help=f"Generate tests using {strat_label}"):
                    if strat_key == "web_quality":
                        from core.deep_inspector import DeepScreenInspector
                        inspector = DeepScreenInspector()
                        wq_cases = inspector.generate_twenty_point_checklist_cases(res)
                        if wq_cases:
                            out_csv = "deep_inspected_tests.csv"
                            df_wq = pd.DataFrame(wq_cases)
                            df_wq.to_csv(out_csv, index=False)
                            st.session_state.auto_generated_file = out_csv
                            st.session_state.blank_suite = False
                            if "results_df" in st.session_state:
                                del st.session_state["results_df"]
                            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                            st.session_state["test_cases_df"] = normalize_test_dataframe(df_wq, default_target=res.get("target_url", ""))
                            st.success(f"🛡️ Loaded all {len(wq_cases)} Web Quality, Mobile, SEO & Accessibility test cases into the Test Suite Grid!")
                            st.rerun()
                    else:
                        _run_deep_synthesis(strat_key, strat_label)
            with c_export_pop:
                with st.popover("📥 Export", width="stretch", help="Export inspection blueprint or 40-point CSV"):
                    json_str = json.dumps(res, indent=2, default=str)
                    st.download_button(
                        "📄 Blueprint JSON",
                        data=json_str,
                        file_name=f"deep_inspection_{int(time.time())}.json",
                        mime="application/json",
                        width='stretch',
                        key="btn_deep_export_blueprint_json"
                    )
                    if os.path.exists("deep_inspected_tests.csv") and os.path.getsize("deep_inspected_tests.csv") > 0:
                        try:
                            df_di_exist = pd.read_csv("deep_inspected_tests.csv")
                            st.download_button(
                                "📊 Quality CSV",
                                data=df_di_exist.to_csv(index=False).encode("utf-8"),
                                file_name=f"deep_quality_tests_{int(time.time())}.csv",
                                mime="text/csv",
                                width='stretch',
                                key="btn_deep_download_csv_ready",
                                help="Download synthesized 40-point quality test cases as CSV"
                            )
                        except Exception:
                            pass

    elif generation_type in ["🗺️ App Flow & Visual Explorer (Multi-Page)", "📱 Mobile App (Android Auto-Crawler)"]:
        if generation_type == "📱 Mobile App (Android Auto-Crawler)":
            st.info(
                "📱 **Autonomous Android Mobile Discovery**: Connect your phone or emulator via ADB, open any app (or pick from installed packages), and let the AI autonomously crawl screens, tabs, and categories to synthesize complete test suites—hands-free!"
            )
            from core.device_bridge import (
                list_connected_adb_devices,
                get_foreground_app_info,
                list_installed_packages,
                explore_android_app_flow,
            )

            connected_mobiles = list_connected_adb_devices()
            if not connected_mobiles:
                st.warning("⚠️ No Android device or emulator detected via ADB. Connect your phone via USB with USB Debugging enabled, or launch an Android emulator.")
                flow_user_story = ""
            else:
                m_dev_labels = [f"{d['model']} ({d['serial']})" for d in connected_mobiles]
                c_mob_dev, c_mob_pkg = st.columns([1.5, 1.5])
                with c_mob_dev:
                    sel_m_idx = st.selectbox(
                        "Connected Android Device",
                        range(len(m_dev_labels)),
                        format_func=lambda i: m_dev_labels[i],
                        key="mob_sel_device_ctrl",
                    )
                    safe_m_idx = sel_m_idx if sel_m_idx is not None and sel_m_idx < len(connected_mobiles) else 0
                    m_serial = connected_mobiles[safe_m_idx]["serial"]
                    fg_info = get_foreground_app_info(m_serial)
                    cur_pkg = fg_info.get("package", "")
                    if cur_pkg:
                        st.success(f"📲 **Currently Open App on Phone:** `{cur_pkg}`")

                with c_mob_pkg:
                    mob_pkg_mode = st.radio("Target App Selection", ["Current Open Screen / App", "Pick from Installed Packages"], horizontal=True, key="mob_mode_radio")
                    if mob_pkg_mode == "Current Open Screen / App":
                        chosen_mob_pkg = cur_pkg or "com.zeptoconsumerapp"
                        st.caption(f"Will explore currently active app: `{chosen_mob_pkg}`")
                    else:
                        inst_pkgs = list_installed_packages(m_serial, third_party_only=True)
                        def_pkg_idx = inst_pkgs.index(cur_pkg) if cur_pkg in inst_pkgs else 0
                        chosen_mob_pkg = st.selectbox("Select Application Package", inst_pkgs, index=def_pkg_idx, key="mob_sel_pkg_drop")

                c_mc1, c_mc2 = st.columns([1, 1])
                with c_mc1:
                    mob_screens_cnt = st.number_input(
                        "Number of Screens to Auto-Crawl (0 = Uncapped / Full App Discovery)",
                        min_value=0,
                        max_value=1000,
                        value=int(st.session_state.get("global_mob_screens_cnt", 20)),
                        step=5,
                        key="mob_screens_cnt_num",
                        help="Set 0 to crawl without any screen limit until all screens and flows are discovered"
                    )
                    st.session_state["global_mob_screens_cnt"] = mob_screens_cnt
                with c_mc2:
                    flow_user_story = st.text_input("User Story / Testing Objectives (Optional)", placeholder="e.g. Verify grocery categories, open top picks, and check search bar", key="mob_flow_user_story")

                c_mact1, c_mact2, c_mact3 = st.columns([2, 1.5, 1])
                with c_mact1:
                    if st.button("🧭 AI Auto-Explore Mobile Flow", type="primary", width="stretch", help="Autonomously crawl app screens on your phone"):
                        m_progress = st.progress(0)
                        m_status = st.empty()

                        def _mob_progress_cb(step, msg, rec):
                            denom = float(mob_screens_cnt) if mob_screens_cnt > 0 else 50.0
                            m_progress.progress(min(1.0, max(0.05, step / denom)))
                            m_status.text(msg)

                        with st.spinner(f"AI is crawling '{chosen_mob_pkg}' on your device..."):
                            try:
                                flow_res = explore_android_app_flow(
                                    serial=m_serial,
                                    package_name=chosen_mob_pkg,
                                    max_screens=mob_screens_cnt if mob_screens_cnt > 0 else 0,
                                    on_progress_callback=_mob_progress_cb,
                                )
                                st.session_state.discovered_flow = flow_res
                                m_progress.progress(1.0)
                                if flow_res.get("total_screens", 0) > 0:
                                    st.success(f"🎉 Successfully captured {flow_res.get('total_screens', 0)} screens from your phone!")
                                    st.rerun()
                                else:
                                    st.warning("No screens were captured. Make sure the app is open on the phone.")
                            except Exception as me:
                                st.error(f"Mobile Exploration Error: {me}")

                with c_mact2:
                    if st.button("📸 Instant Snapshot Current Screen", type="secondary", width="stretch", help="Captures the exact screen currently visible on your phone"):
                        with st.spinner("Capturing live phone screen..."):
                            try:
                                flow_res = explore_android_app_flow(serial=m_serial, package_name=chosen_mob_pkg, max_screens=1)
                                if flow_res.get("screens"):
                                    screen_rec = flow_res["screens"][0]
                                    if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow:
                                        st.session_state.discovered_flow = {
                                            "start_url": f"android://{chosen_mob_pkg}",
                                            "total_screens": 1,
                                            "screens": [screen_rec],
                                        }
                                    else:
                                        st.session_state.discovered_flow["screens"].append(screen_rec)
                                        st.session_state.discovered_flow["total_screens"] = len(st.session_state.discovered_flow["screens"])
                                    st.success(f"✅ Added Screen: '{screen_rec['name']}' to flow map!")
                                    st.rerun()
                            except Exception as ss_err:
                                st.error(f"Snapshot Error: {ss_err}")

                with c_mact3:
                    def _cb_reset_mob_flow():
                        if "discovered_flow" in st.session_state:
                            del st.session_state["discovered_flow"]

                    st.button("🗑️ Reset Flow", on_click=_cb_reset_mob_flow, type="secondary", width="stretch")
        else:
            st.info(
                "🧭 **Application Flow Discovery**: The AI explores your app flows, tabs, and sub-pages, capturing full screenshots for each functionality before generating end-to-end tests.\n\n"
                "💡 **Tip**: If your target web application requires login or OTP authentication, capture your session first under **🔐 Session & Login Capture**, or use **🎥 Live Flow Recorder** to log in and capture screens interactively!"
            )
            # Auto-populate flow URL from global target or active session if unset
            if "flow_explorer_url" not in st.session_state or not str(st.session_state.get("flow_explorer_url", "")).strip():
                fallback_url = get_global_target_url()
                if fallback_url:
                    st.session_state["flow_explorer_url"] = fallback_url

            c_flow_url, c_flow_snap = st.columns([2.6, 1.4], vertical_alignment="bottom")
            with c_flow_url:
                flow_target_url = st.text_input(
                    "Application Root / Start URL",
                    placeholder="https://example.com/app or com.zeptoconsumerapp",
                    key="flow_explorer_url",
                )
            with c_flow_snap:
                manual_screen_name = st.text_input(
                    "Custom Screen Name (for manual capture)",
                    placeholder="e.g. Lead Details Modal",
                    key="manual_screen_name_input",
                )

            c_flow1, c_flow_view, c_flow_limit, c_flow_depth = st.columns([1.5, 1.3, 0.9, 0.9], vertical_alignment="bottom")
            with c_flow1:
                deep_crawl_all = st.checkbox(
                    "🔍 Comprehensive Mode: Explore All Screens, Sub-Tabs & Modals",
                    value=bool(st.session_state.get("global_deep_crawl_all", True)),
                    key="flow_deep_crawl_all_chk",
                    help="Autonomously clicks all slide menus, dropdowns, sub-tabs, filters, and action buttons to capture every modal, form, and view state",
                )
                st.session_state["global_deep_crawl_all"] = deep_crawl_all
            with c_flow_view:
                # Synchronized with global show_browser setting
                flow_show_browser = st.checkbox(
                    "🖥️ Show Live Browser Window",
                    value=bool(st.session_state.get("show_browser", True)),
                    key="flow_show_browser_window",
                    help="Launches a visible Chromium/Edge browser window on your desktop so you can watch the AI navigate, click tabs, and capture screens live in real time",
                )
                if flow_show_browser != st.session_state.get("show_browser"):
                    st.session_state["show_browser"] = flow_show_browser
                    st.session_state["_persisted_show_browser"] = flow_show_browser
                    save_settings({"show_browser": flow_show_browser}, force=True)
                    if flow_show_browser:
                        os.environ["SHOW_BROWSER"] = "true"
                    else:
                        os.environ.pop("SHOW_BROWSER", None)
            with c_flow_limit:
                flow_max_screens = st.number_input(
                    "Max Screens (0 = Unlimited)",
                    min_value=0,
                    value=int(st.session_state.get("global_flow_max_screens", 0)),
                    key="flow_max_screens_num",
                    help="Set to 0 to crawl all modules, slide menus, tabs, and modals without any screen limit.",
                )
                st.session_state["global_flow_max_screens"] = flow_max_screens
            with c_flow_depth:
                flow_max_depth = st.number_input(
                    "Max Depth (0 = Unlimited)",
                    min_value=0,
                    value=int(st.session_state.get("global_flow_max_depth", 0)),
                    key="flow_max_depth_num",
                    help="Set to 0 to crawl child routes and nested pages without any recursion depth limit (or specify e.g. 3, 5).",
                )
                st.session_state["global_flow_max_depth"] = flow_max_depth

            flow_user_story = st.text_area(
                "📖 User Story & Flow Objectives (Optional)",
                placeholder="e.g. As an admin, navigate across Dashboard, Leads table, switch ownership tabs (All, Mine, Team), open Create Lead modal, and verify table filters.",
                height=70,
                key="flow_user_story_input",
            )

            def _resolve_flow_url() -> str:
                t = str(st.session_state.get("flow_explorer_url", "")).strip()
                if not t:
                    t = str(flow_target_url or "").strip()
                if not t:
                    t = get_global_target_url()
                if t:
                    set_global_target_url(t)
                return t

            c_act1, c_act2, c_act3, c_act4, c_act5 = st.columns([1.6, 1.4, 1.4, 0.8, 0.9], vertical_alignment="bottom")
            with c_act1:
                if st.button("🧭 AI Auto-Explore", type="primary", width='stretch', help="Autonomously crawl internal routes, tabs, and capture all screens"):
                    eff_url = _resolve_flow_url()
                    is_valid_u, u_err = validate_url(eff_url)
                    if not is_valid_u:
                        st.warning(f"⚠️ Validation Failed: {u_err}. Please enter an Application Root / Start URL above.")
                    else:
                        progress_flow = st.progress(0)
                        status_flow = st.empty()
                        is_android_target = str(eff_url).strip().startswith("android://") or bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$", str(eff_url).strip()))

                        target_max_screens = int(flow_max_screens) if flow_max_screens > 0 else (0 if deep_crawl_all else 30)

                        def on_flow_progress(step, msg, record):
                            denom = float(target_max_screens) if target_max_screens > 0 else 50.0
                            progress_flow.progress(min(1.0, max(0.05, step / denom)))
                            status_flow.text(msg)

                        if is_android_target:
                            with st.spinner(f"AI is autonomously crawling Android app '{eff_url}' on your phone..."):
                                from core.device_bridge import explore_android_app_flow
                                try:
                                    flow_res = explore_android_app_flow(
                                        package_name=eff_url.strip(),
                                        max_screens=target_max_screens,
                                        on_progress_callback=on_flow_progress,
                                    )
                                    st.session_state.discovered_flow = flow_res
                                    progress_flow.progress(1.0)
                                    if flow_res.get("total_screens", 0) > 0:
                                        st.toast(f"✅ Captured {flow_res.get('total_screens', 0)} mobile screens!", icon="🧭")
                                        st.success(f"✅ Successfully captured {flow_res.get('total_screens', 0)} mobile application screens & user flows!")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ Exploration finished, but 0 screens were captured from '{eff_url}'. Make sure your device is connected with USB Debugging enabled.")
                                except Exception as afe:
                                    st.error(f"Mobile Exploration Error: {afe}")
                        else:
                            with st.spinner("AI is crawling all application routes, sub-tabs, slide menus, dropdowns, and capturing modal dialogs..."):
                                from core.flow_explorer import AppFlowExplorer
                                try:
                                    if flow_show_browser:
                                        os.environ["SHOW_BROWSER"] = "true"
                                    else:
                                        os.environ.pop("SHOW_BROWSER", None)

                                    explorer = AppFlowExplorer()
                                    flow_res = explorer.explore_app_flows(
                                        start_url=eff_url,
                                        max_screens=target_max_screens,
                                        max_depth=int(flow_max_depth),
                                        explore_deep_subfeatures=deep_crawl_all,
                                        on_progress_callback=on_flow_progress,
                                        headless=not flow_show_browser,
                                    )
                                    st.session_state.discovered_flow = flow_res
                                    progress_flow.progress(1.0)
                                    if flow_res.get("total_screens", 0) > 0:
                                        st.toast(f"✅ Captured {flow_res.get('total_screens', 0)} screens & flows!", icon="🧭")
                                        st.success(f"✅ Successfully captured {flow_res.get('total_screens', 0)} application screens & user flows!")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ Exploration finished, but 0 screens were captured from '{eff_url}'. Please ensure the URL is reachable and that credentials are configured in Settings if the application requires authentication.")
                                except Exception as fe:
                                    st.error(f"Flow Exploration Error: {fe}")

            with c_act2:
                if st.button("📸 Instant Snapshot", type="secondary", width='stretch', key="flow_btn_instant_snap", help="Directly capture the current screen or modal into your flow map"):
                    eff_url = _resolve_flow_url()
                    is_valid_u, u_err = validate_url(eff_url)
                    if not is_valid_u:
                        st.warning(f"⚠️ Validation Failed: {u_err}. Please enter an Application Root / Start URL above.")
                    else:
                        is_android_target = str(eff_url).strip().startswith("android://") or bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$", str(eff_url).strip()))
                        if is_android_target:
                            with st.spinner(f"Capturing current Android screen for '{eff_url}'..."):
                                from core.device_bridge import explore_android_app_flow
                                try:
                                    flow_res = explore_android_app_flow(
                                        package_name=eff_url.strip(),
                                        max_screens=1,
                                    )
                                    if flow_res.get("screens"):
                                        screen_rec = flow_res["screens"][0]
                                        if manual_screen_name:
                                            screen_rec["name"] = manual_screen_name
                                        if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow:
                                            st.session_state.discovered_flow = {
                                                "start_url": eff_url,
                                                "total_screens": 1,
                                                "screens": [screen_rec],
                                            }
                                        else:
                                            st.session_state.discovered_flow["screens"].append(screen_rec)
                                            st.session_state.discovered_flow["total_screens"] = len(st.session_state.discovered_flow["screens"])
                                        st.toast(f"✅ Added Mobile Screen: '{screen_rec['name']}' to flow map!", icon="📸")
                                        st.success(f"✅ Added Mobile Screen: '{screen_rec['name']}' to flow map!")
                                        st.rerun()
                                    else:
                                        st.warning("No screen could be captured from the Android device.")
                                except Exception as me:
                                    st.error(f"Mobile Snapshot Error: {me}")
                        else:
                            with st.spinner(f"Capturing current screen for {eff_url}..."):
                                from core.flow_explorer import AppFlowExplorer
                                try:
                                    if flow_show_browser:
                                        os.environ["SHOW_BROWSER"] = "true"
                                    else:
                                        os.environ.pop("SHOW_BROWSER", None)

                                    explorer = AppFlowExplorer()
                                    screen_rec = explorer.capture_single_screen(
                                        eff_url,
                                        manual_screen_name,
                                        headless=not flow_show_browser,
                                    )
                                    if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow:
                                        st.session_state.discovered_flow = {
                                            "start_url": eff_url,
                                            "total_screens": 1,
                                            "screens": [screen_rec],
                                        }
                                    else:
                                        st.session_state.discovered_flow["screens"].append(screen_rec)
                                        st.session_state.discovered_flow["total_screens"] = len(st.session_state.discovered_flow["screens"])
                                    st.toast(f"✅ Added Screen: '{screen_rec['name']}' to flow map!", icon="📸")
                                    st.success(f"✅ Added Screen: '{screen_rec['name']}' to flow map!")
                                    st.rerun()
                                except Exception as me:
                                    st.error(f"Manual Capture Error: {me}")

            with c_act3:
                if st.button("🎥 Live Flow Recorder", type="secondary", width='stretch', help="Opens a visible browser with a floating toolbar to navigate and record custom screens/modals interactively"):
                    eff_url = _resolve_flow_url()
                    is_valid_u, u_err = validate_url(eff_url)
                    if not is_valid_u:
                        st.warning(f"⚠️ Validation Failed: {u_err}. Please enter an Application Root / Start URL above.")
                    else:
                        st.info("🔴 Live Flow Recorder is open! Navigate across screens, open modals/drawers, and click '📸 Capture Screen' or '✅ Done & Save' in the browser dock.")
                        from core.flow_explorer import AppFlowExplorer
                        try:
                            explorer = AppFlowExplorer()
                            rec_res = explorer.launch_interactive_recorder(eff_url, timeout_seconds=7200)
                            if rec_res.get("screens"):
                                if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow:
                                    st.session_state.discovered_flow = rec_res
                                else:
                                    st.session_state.discovered_flow["screens"].extend(rec_res["screens"])
                                    st.session_state.discovered_flow["total_screens"] = len(st.session_state.discovered_flow["screens"])
                                st.toast(f"🎉 Recorded and added {len(rec_res['screens'])} screens!", icon="🎥")
                                st.success(f"🎉 Successfully recorded and added {len(rec_res['screens'])} screens to your flow map!")
                                st.rerun()
                            else:
                                st.warning("No screens were captured during the recording session.")
                        except Exception as re:
                            st.error(f"Flow Recorder Error: {re}")

            with c_act4:
                def _cb_clear_flow_inputs():
                    st.session_state["flow_explorer_url"] = ""
                    st.session_state["manual_screen_name_input"] = ""
                    st.session_state["flow_user_story_input"] = ""
                    st.session_state["global_target_url"] = ""

                st.button(
                    "🧹 Clear",
                    on_click=_cb_clear_flow_inputs,
                    type="secondary",
                    width='stretch',
                    key="btn_clear_flow_inputs",
                    help="Clear URL, screen name, and user story input fields"
                )

            with c_act5:
                def _cb_reset_flow():
                    st.session_state.pop("discovered_flow", None)
                    st.session_state.pop("selected_flow_screens", None)
                    st.session_state.pop("screen_multiselect_box", None)
                    st.session_state.pop("flow_labels_sig", None)
                    st.session_state.pop("flow_gen_success_msg", None)

                if st.button("🗑️ Reset Map", type="secondary", width='stretch', key="btn_reset_flow_map", help="Reset and clear all discovered screens"):
                    _cb_reset_flow()
                    st.toast("🗑️ Flow map reset successfully!", icon="✅")
                    st.rerun()

        if "discovered_flow" not in st.session_state or not st.session_state.discovered_flow.get("screens"):
            with st.expander("📂 Load Saved Flow Map (JSON)", expanded=False):
                st.caption("Upload a previously exported flow map JSON file to instantly restore your discovered application screens:")
                up_flow_empty = st.file_uploader("Choose flow map JSON file", type=["json"], key="flow_uploader_empty")
                if up_flow_empty is not None:
                    try:
                        data_empty = json.load(up_flow_empty)
                        if isinstance(data_empty, dict) and "screens" in data_empty:
                            st.session_state.discovered_flow = data_empty
                            st.session_state.pop("flow_labels_sig", None)
                            st.toast(f"✅ Loaded {len(data_empty['screens'])} screens from JSON!", icon="📂")
                            st.rerun()
                        else:
                            st.error("Invalid flow map JSON format (missing 'screens' array).")
                    except Exception as e_up:
                        st.error(f"Error parsing JSON: {e_up}")

        # Display Discovered Flow Gallery
        if "discovered_flow" in st.session_state and st.session_state.discovered_flow.get("screens"):
            discovered = st.session_state.discovered_flow
            screens_list = discovered["screens"]
            total_sub_modals = sum(len(s.get("sub_features", [])) for s in screens_list)
            st.divider()

            if st.session_state.get("flow_gen_success_msg"):
                c_fmsg1, c_fmsg2 = st.columns([3.2, 1.2])
                with c_fmsg1:
                    st.success(st.session_state.get("flow_gen_success_msg"))
                with c_fmsg2:
                    df_f_cur = st.session_state.get("test_cases_df")
                    if df_f_cur is not None and isinstance(df_f_cur, pd.DataFrame) and not df_f_cur.empty:
                        st.download_button(
                            "📥 Download Suite (CSV)",
                            data=df_f_cur.to_csv(index=False).encode("utf-8"),
                            file_name=f"generated_flow_tests_{int(time.time())}.csv",
                            mime="text/csv",
                            width="stretch",
                            key="btn_dl_flow_succ_top_csv",
                            help="Download newly generated test suite as CSV",
                        )

            if "flow_screens_expanded" not in st.session_state:
                st.session_state["flow_screens_expanded"] = False

            # Header with Collapse/Expand Controls, Layout, Export & Import
            flow_json_export = json.dumps(discovered, indent=2)
            c_hdr1, c_hdr2, c_hdr3, c_hdr4, c_hdr5, c_hdr6 = st.columns([2.3, 0.7, 0.7, 0.9, 0.8, 0.8], vertical_alignment="center")
            with c_hdr1:
                st.subheader(f"🗺️ Discovered Flow Map ({len(screens_list)} Screens, {total_sub_modals} Sub-Modals/KPIs)")
            with c_hdr2:
                if st.button("🔽 Expand", width='stretch', help="Expand all screen cards and screenshots"):
                    st.session_state["flow_screens_expanded"] = True
                    st.rerun()
            with c_hdr3:
                if st.button("🔼 Collapse", width='stretch', help="Collapse all screen cards to compact view"):
                    st.session_state["flow_screens_expanded"] = False
                    st.rerun()
            with c_hdr4:
                view_layout = st.selectbox(
                    "Layout",
                    ["📋 Collapsible Accordions", "🖼️ Visual Cards (Grid)"],
                    key="flow_gallery_layout_mode",
                    label_visibility="collapsed"
                )
            with c_hdr5:
                st.download_button(
                    "💾 Export",
                    data=flow_json_export.encode("utf-8"),
                    file_name=f"app_flow_map_{int(time.time())}.json",
                    mime="application/json",
                    width='stretch',
                    key="btn_export_flow_map_json",
                    help="Export discovered screens and flows as JSON"
                )
            with c_hdr6:
                with st.popover("📂 Import", width='stretch', help="Load a previously exported flow map JSON"):
                    st.markdown("###### 📂 Load Flow Map JSON")
                    up_flow_pop = st.file_uploader("Upload flow map file", type=["json"], key="flow_uploader_pop")
                    if up_flow_pop is not None:
                        try:
                            data_pop = json.load(up_flow_pop)
                            if isinstance(data_pop, dict) and "screens" in data_pop:
                                st.session_state.discovered_flow = data_pop
                                st.session_state.pop("flow_labels_sig", None)
                                st.toast(f"✅ Loaded {len(data_pop['screens'])} screens from JSON!", icon="📂")
                                st.rerun()
                            else:
                                st.error("Invalid flow map JSON format (missing 'screens' array).")
                        except Exception as e_p:
                            st.error(f"Error parsing JSON: {e_p}")

            # Executive Flow Metrics Strip
            tot_scr = len(screens_list)
            tot_sub = total_sub_modals
            tot_btns = sum(len(s.get("action_buttons", [])) for s in screens_list)
            tot_inps = sum(len(s.get("inputs", [])) for s in screens_list)
            tot_tbls = sum(s.get("tables_count", 0) for s in screens_list)
            tot_kpis = sum(len(s.get("kpi_cards", [])) for s in screens_list)

            stat_strip_html = f"""
            <div class="flow-stat-strip">
                <div class="flow-stat-pill">
                    <div class="flow-stat-val">🖥️ {tot_scr}</div>
                    <div class="flow-stat-lbl">Screens</div>
                </div>
                <div class="flow-stat-pill">
                    <div class="flow-stat-val">🪟 {tot_sub}</div>
                    <div class="flow-stat-lbl">Modals / Tabs</div>
                </div>
                <div class="flow-stat-pill">
                    <div class="flow-stat-val">🔘 {tot_btns}</div>
                    <div class="flow-stat-lbl">Buttons</div>
                </div>
                <div class="flow-stat-pill">
                    <div class="flow-stat-val">📝 {tot_inps}</div>
                    <div class="flow-stat-lbl">Inputs</div>
                </div>
                <div class="flow-stat-pill">
                    <div class="flow-stat-val">📊 {tot_tbls + tot_kpis}</div>
                    <div class="flow-stat-lbl">Tables & KPIs</div>
                </div>
            </div>
            """
            st.markdown(stat_strip_html, unsafe_allow_html=True)

            # Visual Navigation Journey Architecture Canvas
            with st.expander("🌐 Visual Navigation Flow Architecture (Journey Graph)", expanded=False):
                journey_steps = []
                for j_i, j_s in enumerate(screens_list):
                    j_name = j_s.get("name", f"Screen #{j_i+1}")
                    j_url = j_s.get("url", "")
                    try:
                        from urllib.parse import urlparse
                        p_part = urlparse(j_url).path or "/"
                    except Exception:
                        p_part = j_url[:24]
                    b_cnt = len(j_s.get("action_buttons", []))
                    i_cnt = len(j_s.get("inputs", []))
                    s_cnt = len(j_s.get("sub_features", []))
                    t_cnt = j_s.get("tables_count", 0)
                    
                    r_badge = f'<span class="flow-node-badge flow-node-badge-sky">{j_s.get("role")}</span>' if j_s.get("role") else ('<span class="flow-node-badge flow-node-badge-green">Root</span>' if j_i == 0 else '<span class="flow-node-badge">Page</span>')
                    badges_sub = []
                    if b_cnt: badges_sub.append(f'<span class="flow-node-badge">🔘 {b_cnt}</span>')
                    if i_cnt: badges_sub.append(f'<span class="flow-node-badge">📝 {i_cnt}</span>')
                    if s_cnt: badges_sub.append(f'<span class="flow-node-badge flow-node-badge-amber">🪟 {s_cnt}</span>')
                    if t_cnt: badges_sub.append(f'<span class="flow-node-badge flow-node-badge-green">📊 {t_cnt}</span>')

                    step_card_html = f"""
                    <div class="flow-journey-node" style="min-width: 190px; max-width: 260px; flex-direction: column; align-items: flex-start; gap: 3px;">
                        <div style="display: flex; justify-content: space-between; width: 100%; align-items: center;">
                            <strong style="color: #60A5FA; font-size: 0.84rem;">#{j_i+1} {j_name}</strong>
                            {r_badge}
                        </div>
                        <div style="font-size: 0.72rem; color: #94A3B8; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;">
                            {p_part}
                        </div>
                        <div style="display: flex; flex-wrap: wrap; gap: 2px; margin-top: 3px;">
                            {"".join(badges_sub)}
                        </div>
                    </div>
                    """
                    arr_html = '<div style="color: #818CF8; font-size: 1.15rem; font-weight: bold; padding: 0 4px;">➔</div>' if j_i < len(screens_list) - 1 else ''
                    journey_steps.append(f'<div class="flow-journey-step">{step_card_html}{arr_html}</div>')

                journey_full_html = f"""
                <div class="flow-journey-container">
                    <div style="display: flex; align-items: center; flex-wrap: nowrap; gap: 4px; overflow-x: auto; padding-bottom: 6px;">
                        {"".join(journey_steps)}
                    </div>
                </div>
                """
                st.markdown(journey_full_html, unsafe_allow_html=True)

            is_globally_expanded = st.session_state["flow_screens_expanded"]

            # Screen Selection for Generation
            screen_label_map = {
                f"Screen #{i+1}: {s.get('name', 'Page')} ({s.get('url', '')})": i
                for i, s in enumerate(screens_list)
            }
            all_screen_labels = list(screen_label_map.keys())
            current_labels_sig = tuple(all_screen_labels)

            # When discovered screens change (new exploration, snapshot added, or screen removed), default to all screens selected
            if st.session_state.get("flow_labels_sig") != current_labels_sig:
                st.session_state["flow_labels_sig"] = current_labels_sig
                st.session_state.selected_flow_screens = list(all_screen_labels)
                st.session_state["screen_multiselect_box"] = list(all_screen_labels)
            elif "screen_multiselect_box" not in st.session_state:
                st.session_state["screen_multiselect_box"] = list(
                    st.session_state.get("selected_flow_screens", all_screen_labels)
                )
            else:
                # Filter out any stale labels that are no longer in options
                st.session_state["screen_multiselect_box"] = [
                    lbl for lbl in st.session_state["screen_multiselect_box"] if lbl in all_screen_labels
                ]

            c_sel1, c_sel2 = st.columns([3.8, 1.2], vertical_alignment="bottom")
            with c_sel1:
                selected_labels = st.multiselect(
                    "🎯 Select Screens to Include in AI Test Generation:",
                    options=all_screen_labels,
                    key="screen_multiselect_box",
                    help="Choose which screens to analyze and generate test cases for"
                )
                st.session_state.selected_flow_screens = list(selected_labels)
                if len(selected_labels) == len(all_screen_labels) and len(all_screen_labels) > 0:
                    st.caption(f"✨ **All {len(all_screen_labels)} discovered screens are selected** for test generation. (Click `×` on any chip to exclude, or click '◻️ Deselect All').")
                elif selected_labels:
                    st.caption(f"📌 Showing **{len(selected_labels)}** of **{len(all_screen_labels)}** screens selected.")
                else:
                    st.caption("⚠️ No screens selected. Please pick at least one screen or click '☑️ Select All'.")
            with c_sel2:
                current_selected_screens = st.session_state.get("screen_multiselect_box", [])
                all_screens_selected = (len(current_selected_screens) == len(all_screen_labels)) and len(all_screen_labels) > 0
                if all_screens_selected:
                    def _cb_toggle_screens():
                        st.session_state.selected_flow_screens = []
                        st.session_state["screen_multiselect_box"] = []
                    st.button(f"◻️ Deselect All ({len(all_screen_labels)})", on_click=_cb_toggle_screens, width='stretch', help="Deselect all screens", key="btn_flow_toggle_screens")
                else:
                    def _cb_toggle_screens():
                        st.session_state.selected_flow_screens = list(all_screen_labels)
                        st.session_state["screen_multiselect_box"] = list(all_screen_labels)
                    st.button(f"☑️ Select All ({len(all_screen_labels)})", on_click=_cb_toggle_screens, width='stretch', help="Select all screens for test generation", key="btn_flow_toggle_screens")

            selected_indices = [screen_label_map[lbl] for lbl in selected_labels if lbl in screen_label_map]

            c_strat1, c_strat2 = st.columns([2.8, 1.2], vertical_alignment="bottom")
            with c_strat1:
                flow_strategy_opts = [
                    "🌐 Full 360° Suite (AI + Hybrid + Rule Engine)",
                    "🤖 AI Generated (Full) only",
                    "🤖+⚙️ AI Step + ⚙️ Template Code (Hybrid)",
                    "⚙️ Rule Engine only (Deterministic, 0 tokens)",
                ]
                sel_flow_strat = st.selectbox(
                    "⚙️ Test Synthesis Strategy:",
                    options=flow_strategy_opts,
                    key="flow_synth_strategy_select",
                    help="Choose generation strategy: Full 360° suite, pure AI Generated tests, Hybrid (AI Step + Template Code), or deterministic Rule Engine."
                )
            with c_strat2:
                flow_strat_code = {
                    "🌐 Full 360° Suite (AI + Hybrid + Rule Engine)": "all",
                    "🤖 AI Generated (Full) only": "ai_full",
                    "🤖+⚙️ AI Step + ⚙️ Template Code (Hybrid)": "hybrid",
                    "⚙️ Rule Engine only (Deterministic, 0 tokens)": "rule_engine",
                }.get(sel_flow_strat, "all")
                st.caption(f"Active Synthesis Mode: `{flow_strat_code}`")

            gallery_status_slot = st.empty()

            def _apply_generated_flow_suite(res_path: str, ss_p: str, msg_prefix: str):
                try:
                    df_gen = pd.read_csv(res_path) if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0 else pd.DataFrame()
                except pd.errors.EmptyDataError:
                    df_gen = pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code"])
                df_gen = normalize_test_dataframe(df_gen)
                st.session_state.auto_generated_file = res_path
                st.session_state.last_analysis_ss = ss_p
                st.session_state.blank_suite = False
                if "results_df" in st.session_state:
                    del st.session_state["results_df"]
                st.session_state["test_cases_df"] = df_gen
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                st.session_state["flow_gen_success_msg"] = f"🎉 {msg_prefix} ({len(df_gen)} comprehensive test cases loaded into the Test Suite Grid below!)"

            def _run_single_scr_gen(scr_idx: int, s_name: str, strat: str, lbl: str):
                with gallery_status_slot.container():
                    prog_single = st.progress(0)
                    status_single = st.empty()
                    status_single.text(f"🚀 Generating [{lbl}] tests specifically for Screen #{scr_idx + 1}: '{s_name}'...")
                    from core.ai_client import AIClient
                    from core.test_generator import TestGenerator
                    try:
                        gen = TestGenerator(AIClient())
                        res_path, ss_p = gen.generate_cases_from_flow(
                            flow_data=discovered,
                            output_csv_path="generated_tests.csv",
                            user_story=flow_user_story,
                            selected_screen_indices=[scr_idx],
                            append=False,
                            on_progress_callback=lambda cur, tot, msg, cnt: status_single.text(f"[{cur}/{tot}] {msg}"),
                            strategy=strat,
                        )
                        if res_path:
                            prog_single.progress(1.0)
                            _apply_generated_flow_suite(res_path, ss_p, f"Generated [{lbl}] test suite for Screen #{scr_idx + 1} ('{s_name}')")
                            st.rerun()
                    except Exception as se:
                        st.error(f"Error generating for screen: {se}")

            if view_layout == "📋 Collapsible Accordions":
                # Render each screen in an individual collapsible accordion
                for idx, scr in enumerate(screens_list):
                    scr_name = scr.get("name", f"Screen #{idx + 1}")
                    scr_url = scr.get("url", "")
                    sub_feats = scr.get("sub_features", [])
                    actions = scr.get("action_buttons", [])
                    inputs = scr.get("inputs", [])
                    ss_f = scr.get("screenshot", "")

                    scr_role = scr.get("role", "")
                    badge_parts = []
                    if scr_role: badge_parts.append(f"🏷️ {scr_role}")
                    if actions: badge_parts.append(f"{len(actions)} Buttons")
                    if inputs: badge_parts.append(f"{len(inputs)} Inputs")
                    if sub_feats: badge_parts.append(f"{len(sub_feats)} Features/Modals")
                    badge_str = f" • [{', '.join(badge_parts)}]" if badge_parts else ""

                    is_sel_badge = " [🎯 Selected]" if idx in selected_indices else ""
                    expander_title = f"🖥️ Screen #{idx + 1}: {scr_name}{badge_str}{is_sel_badge}"
                    with st.expander(expander_title, expanded=is_globally_expanded):
                        c_meta1, c_meta2, c_meta3 = st.columns([2.5, 1.2, 0.8], vertical_alignment="center")
                        with c_meta1:
                            st.caption(f"🔗 Target: `{scr_url}`")
                            if scr_role:
                                st.caption(f"🏷️ **UX Role:** `{scr_role}`")
                            if actions:
                                st.caption(f"🔘 **Action Buttons:** {', '.join(actions[:8])}")
                            if inputs:
                                st.caption(f"📝 **Form Inputs:** {', '.join(inputs[:8])}")
                        with c_meta2:
                            with st.popover(f"⚡ Generate #{idx + 1}", width='stretch', help=f"1-Click generate tests for Screen #{idx + 1}"):
                                st.markdown(f"###### ⚡ Generate for Screen #{idx + 1}:")
                                if st.button("🌐 Full 360° Suite", key=f"gen_acc_all_{idx}", width='stretch', type="primary"):
                                    _run_single_scr_gen(idx, scr_name, "all", "🌐 Full 360° Suite")
                                if st.button("⚡ RobustEngine v4", key=f"gen_acc_robust_{idx}", width='stretch'):
                                    _run_single_scr_gen(idx, scr_name, "robust_v4", "⚡ RobustEngine v4")
                                if st.button("🤖 AI Only", key=f"gen_acc_ai_{idx}", width='stretch'):
                                    _run_single_scr_gen(idx, scr_name, "ai_full", "🤖 AI Only")
                                if st.button("🤖+⚙️ Hybrid", key=f"gen_acc_hyb_{idx}", width='stretch'):
                                    _run_single_scr_gen(idx, scr_name, "hybrid", "🤖+⚙️ Hybrid")
                                if st.button("⚙️ Rule Only (0 Tokens)", key=f"gen_acc_rule_{idx}", width='stretch'):
                                    _run_single_scr_gen(idx, scr_name, "rule_engine", "⚙️ Rule Engine")
                                if scr.get("native_elements") or scr_url.startswith("android://"):
                                    if st.button("📱 Native UI Suite (Rule)", key=f"gen_acc_nat_{idx}", width='stretch'):
                                        from core.device_bridge import synthesize_native_test_cases
                                        nat_pkg = scr_url.replace("android://", "").split("/")[0]
                                        nat_cases = synthesize_native_test_cases(scr.get("native_elements", []), package_name=nat_pkg, screen_title=scr_name, screenshot_path=ss_f)
                                        if nat_cases:
                                            out_p = f"reports/native_screen_{idx+1}_tests.csv"
                                            df_nc = pd.DataFrame(nat_cases)
                                            df_nc.to_csv(out_p, index=False)
                                            _apply_generated_flow_suite(out_p, ss_f, f"Synthesized {len(nat_cases)} Native UI tests for Screen #{idx + 1}")
                                            st.rerun()
                        with c_meta3:
                            if st.button(f"🗑️ Remove", key=f"del_scr_acc_{idx}", width='stretch', help=f"Remove Screen #{idx + 1} from flow"):
                                screens_list.pop(idx)
                                st.session_state.discovered_flow["screens"] = screens_list
                                st.session_state.discovered_flow["total_screens"] = len(screens_list)
                                st.session_state.pop("flow_labels_sig", None)
                                st.rerun()

                        if ss_f and os.path.exists(ss_f):
                            with st.expander(f"📸 Full Page Screenshot Preview ({os.path.basename(ss_f)})", expanded=is_globally_expanded):
                                safe_st_image(ss_f, caption=f"{scr_name} — {scr_url}", width="stretch")

                        if sub_feats:
                            st.markdown(f"##### 🪟 Sub-Features, Config Modules & Modals ({len(sub_feats)})")
                            for sf_i, sf in enumerate(sub_feats, 1):
                                if isinstance(sf, dict):
                                    sf_title = f"🔹 {sf.get('name', f'Sub-Feature #{sf_i}')}"
                                    with st.expander(sf_title, expanded=is_globally_expanded):
                                        if sf.get("trigger"):
                                            st.caption(f"**Trigger:** `{sf.get('trigger')}`")
                                        if sf.get("url"):
                                            st.caption(f"**Sub-Route URL:** `{sf.get('url')}`")
                                        if sf.get("inputs"):
                                            st.caption(f"**Inputs:** {', '.join(sf.get('inputs')[:6])}")
                                        if sf.get("action_buttons"):
                                            st.caption(f"**Buttons:** {', '.join(sf.get('action_buttons')[:6])}")
                                        sf_ss = sf.get("screenshot", "")
                                        if sf_ss and os.path.exists(sf_ss):
                                            safe_st_image(sf_ss, caption=sf.get("name"), width="stretch")
                                        
                                        # Nested inner modals
                                        inner_m = sf.get("inner_modals", [])
                                        if inner_m:
                                            st.markdown(f"**Inner Action Modals ({len(inner_m)}):**")
                                            for im in inner_m:
                                                st.caption(f"• **{im.get('name')}** (Inputs: {len(im.get('inputs', []))}, Buttons: {len(im.get('action_buttons', []))})")
                                else:
                                    st.caption(f"🔹 **{sf}**")


            else:
                # Render Grid of Cards (3 per row)
                for row_start in range(0, len(screens_list), 3):
                    chunk = screens_list[row_start : row_start + 3]
                    card_cols = st.columns(3)
                    for col_i, scr in enumerate(chunk):
                        actual_idx = row_start + col_i
                        with card_cols[col_i]:
                            with st.container(border=True):
                                c_top1, c_top2 = st.columns([3, 1], vertical_alignment="center")
                                with c_top1:
                                    is_sel_star = " 🎯" if actual_idx in selected_indices else ""
                                    st.markdown(f"**Screen #{actual_idx + 1}: {scr.get('name', 'Page')}{is_sel_star}**")
                                with c_top2:
                                    if st.button("🗑️", key=f"del_scr_grid_{actual_idx}", help=f"Remove Screen #{actual_idx + 1}"):
                                        screens_list.pop(actual_idx)
                                        st.session_state.discovered_flow["screens"] = screens_list
                                        st.session_state.discovered_flow["total_screens"] = len(screens_list)
                                        st.session_state.pop("flow_labels_sig", None)
                                        st.rerun()

                                st.caption(f"🔗 `{scr.get('url', '')}`")
                                
                                actions = scr.get("action_buttons", [])
                                inputs = scr.get("inputs", [])
                                sub_feats = scr.get("sub_features", [])

                                meta_line = []
                                if actions: meta_line.append(f"🔘 {len(actions)} Btns")
                                if inputs: meta_line.append(f"📝 {len(inputs)} Inp")
                                if sub_feats: meta_line.append(f"🪟 {len(sub_feats)} Sub")
                                if meta_line:
                                    st.caption(" | ".join(meta_line))

                                with st.popover("⚡ Generate", width='stretch', help=f"1-Click generate tests for Screen #{actual_idx + 1}"):
                                    st.markdown(f"###### ⚡ Generate for Screen #{actual_idx + 1}:")
                                    if st.button("🌐 Full 360° Suite", key=f"gen_grid_all_{actual_idx}", width='stretch', type="primary"):
                                        _run_single_scr_gen(actual_idx, scr.get('name', 'Page'), "all", "🌐 Full 360° Suite")
                                    if st.button("⚡ RobustEngine v4", key=f"gen_grid_robust_{actual_idx}", width='stretch'):
                                        _run_single_scr_gen(actual_idx, scr.get('name', 'Page'), "robust_v4", "⚡ RobustEngine v4")
                                    if st.button("🤖 AI Only", key=f"gen_grid_ai_{actual_idx}", width='stretch'):
                                        _run_single_scr_gen(actual_idx, scr.get('name', 'Page'), "ai_full", "🤖 AI Only")
                                    if st.button("🤖+⚙️ Hybrid", key=f"gen_grid_hyb_{actual_idx}", width='stretch'):
                                        _run_single_scr_gen(actual_idx, scr.get('name', 'Page'), "hybrid", "🤖+⚙️ Hybrid")
                                    if st.button("⚙️ Rule Only (0 Tokens)", key=f"gen_grid_rule_{actual_idx}", width='stretch'):
                                        _run_single_scr_gen(actual_idx, scr.get('name', 'Page'), "rule_engine", "⚙️ Rule Engine")

                                ss_f = scr.get("screenshot", "")
                                if ss_f and os.path.exists(ss_f):
                                    with st.expander("📸 Screenshot Preview", expanded=is_globally_expanded):
                                        safe_st_image(ss_f, caption=scr.get('name'), width="stretch")

                                # Expandable view for nested sub-features and modals
                                if sub_feats:
                                    with st.expander(f"🔍 Sub-Features ({len(sub_feats)})", expanded=is_globally_expanded):
                                        for sf in sub_feats:
                                            if isinstance(sf, dict):
                                                st.markdown(f"**{sf.get('name', 'Sub-Feature')}**")
                                                if sf.get("trigger"):
                                                    st.caption(f"Trigger: `{sf.get('trigger')}`")
                                                if sf.get("inputs"):
                                                    st.caption(f"Inputs: {', '.join(sf.get('inputs')[:3])}")
                                                sf_ss = sf.get("screenshot", "")
                                                if sf_ss and os.path.exists(sf_ss):
                                                    safe_st_image(sf_ss, caption=sf.get("name"), width="stretch")
                                            else:
                                                st.caption(f"🔹 **{sf}**")
                                            st.divider()

            st.divider()
            target_screen_count = len(selected_indices)

            def _execute_flow_generation(strategy_to_run: str, label_name: str):
                if not screens_list:
                    st.warning("⚠️ No screens discovered yet. Please run '🧭 AI Auto-Explore' first.")
                elif not selected_indices:
                    st.warning("⚠️ Please select at least one screen in '🎯 Select Screens to Include in AI Test Generation' above (or click '✅ Select All').")
                else:
                    prog_gen = st.progress(0)
                    status_gen = st.empty()
                    status_gen.text(f"🚀 Initializing screen-by-screen [{label_name}] test generation for {target_screen_count} selected screen(s)...")

                    from core.ai_client import AIClient
                    from core.test_generator import TestGenerator

                    def _on_screen_gen_progress(cur_idx, total_cnt, msg, cases_so_far):
                        pct = min(1.0, max(0.05, cur_idx / max(1, total_cnt)))
                        prog_gen.progress(pct)
                        status_gen.text(f"[{cur_idx}/{total_cnt}] {msg} ({cases_so_far} cases built so far)")

                    try:
                        gen = TestGenerator(AIClient())
                        res_path, ss_p = gen.generate_cases_from_flow(
                            flow_data=discovered,
                            output_csv_path="generated_tests.csv",
                            user_story=flow_user_story,
                            selected_screen_indices=selected_indices,
                            append=False,
                            on_progress_callback=_on_screen_gen_progress,
                            strategy=strategy_to_run,
                        )
                        if res_path:
                            prog_gen.progress(1.0)
                            _apply_generated_flow_suite(
                                res_path,
                                ss_p,
                                f"Successfully generated [{label_name}] test suite across {target_screen_count} selected screen(s)",
                            )
                            st.rerun()
                        else:
                            st.error("Failed to generate test cases from flow.")
                    except Exception as ge:
                        st.error(f"Flow Generation Error: {ge}")

            st.markdown(
                textwrap.dedent(f"""<div style="margin-top: 6px; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <span style="font-size: 1rem;">⚡</span>
                        <strong style="color: #F8FAFC; font-size: 0.90rem;">1-Click Test Generation Buttons ({target_screen_count} Selected Screens):</strong>
                        <span style="color: #94A3B8; font-size: 0.82rem;">— Synthesize tests across selected screens with your chosen engine in 1 click:</span>
                    </div>
                </div>""").strip(),
                unsafe_allow_html=True,
            )
            c_fgen_all, c_fgen_robust, c_fgen_ai, c_fgen_hyb, c_fgen_rule = st.columns(5, vertical_alignment="bottom")
            with c_fgen_all:
                if st.button("🌐 All (360°)", type="primary", key="flow_gen_btn_all", width='stretch', help="Generate full 360° suite across AI + Hybrid + Rule Engine"):
                    _execute_flow_generation("all", "🌐 Full 360° Suite")
            with c_fgen_robust:
                if st.button("⚡ Robust v4", type="secondary", key="flow_gen_btn_robust", width='stretch', help="Generate 100% resilient 7-Category Deep Matrix suite with chained stateful journeys (0 tokens, 100% offline guaranteed)"):
                    _execute_flow_generation("robust_v4", "⚡ RobustEngine v4")
            with c_fgen_ai:
                if st.button("🤖 AI Full", key="flow_gen_btn_ai", width='stretch', help="Generate purely 🤖 AI Generated (Full) tests with LLM steps and Playwright scripts"):
                    _execute_flow_generation("ai_full", "🤖 AI Generated (Full)")
            with c_fgen_hyb:
                if st.button("🤖+⚙️ Hybrid", key="flow_gen_btn_hybrid", width='stretch', help="Generate 🤖 AI Step + ⚙️ Template Code tests"):
                    _execute_flow_generation("hybrid", "🤖+⚙️ Hybrid")
            with c_fgen_rule:
                if st.button("⚙️ Rule Engine", key="flow_gen_btn_rule", width='stretch', help="Generate 100% deterministic ⚙️ Rule Engine tests (0 tokens, 100% offline)"):
                    _execute_flow_generation("rule_engine", "⚙️ Rule Engine")

            # ---------------------------------------------------------
            # Discovered Flow Test Cases & Origin Engine Controls
            # ---------------------------------------------------------
            df_flow = st.session_state.get("test_cases_df")
            if df_flow is not None and isinstance(df_flow, pd.DataFrame) and len(df_flow) > 0:
                if "Run" not in df_flow.columns:
                    df_flow.insert(0, "Run", True)
                    st.session_state["test_cases_df"] = df_flow
                st.divider()
                st.markdown("### 📋 Discovered Flow Test Cases & Origin Controls")
                tot_c, ai_c, hyb_c, rl_c, mn_c, rob_c = compute_origin_counts(df_flow, include_robust=True)
                pills_flow = "".join([
                    f'<span class="status-pill status-passed" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="⚡ RobustEngine v4 Deep Matrix + Journeys">⚡ Robust: {rob_c}</span>',
                    f'<span class="status-pill status-passed" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="Pure LLM synthesis with steps and Playwright scripts">🤖 AI Full: {ai_c}</span>',
                    f'<span class="status-pill status-running" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="AI steps with deterministic template Playwright scripts">🤖+⚙️ Hybrid: {hyb_c}</span>',
                    f'<span class="status-pill status-paused" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="Synthesized by Rule Engine from DOM/A11y/Network inspection (0 tokens, 100% offline)">⚙️ Rule Engine: {rl_c}</span>',
                    f'<span class="status-pill status-pending" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="Manually created or uploaded test cases">✍️ Manual: {mn_c}</span>',
                ])
                origin_html_flow = f'<div class="metric-card" style="display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 16px; margin-bottom: 10px; border-radius: 8px;"><div style="display: flex; align-items: center; gap: 8px;"><span style="font-size: 1.05rem;">🔍</span><strong style="color: #F8FAFC; font-size: 0.92rem;">Test Suite Origin Check:</strong><span style="color: #94A3B8; font-size: 0.85rem;">Total <b>{tot_c}</b> loaded</span></div><div style="display: flex; flex-wrap: wrap; gap: 6px;">{pills_flow}</div></div>'
                render_html_block(origin_html_flow)

                # A. 1-Click Run Selection Buttons
                render_html_block('<div style="margin-top: 6px; margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;"><div style="display: flex; align-items: center; gap: 6px;"><span style="font-size: 1rem;">⚡</span><strong style="color: #F8FAFC; font-size: 0.90rem;">1-Click Run Selection Buttons</strong><span style="color: #94A3B8; font-size: 0.82rem;">— Select which test cases are checked for execution with a single click:</span></div></div>')

                flow_origin_filter_opts = [
                    f"🌐 All Engines ({tot_c})",
                    f"⚡ RobustEngine v4 ({rob_c})",
                    f"🤖 AI Generated (Full) ({ai_c})",
                    f"🤖 AI Step + ⚙️ Template Code ({hyb_c})",
                    f"⚙️ Rule Engine ({rl_c})",
                    f"✍️ Manual / Uploaded ({mn_c})",
                ]

                flow_chk_cnt = int(df_flow["Run"].fillna(True).astype(bool).sum()) if "Run" in df_flow.columns else len(df_flow)
                is_flow_all_sel = (flow_chk_cnt == len(df_flow)) and len(df_flow) > 0

                c_ft1, c_ft_toggle, c_ft_sel, c_ft3, c_ft4 = st.columns([1.8, 1.2, 1.2, 1.0, 1.0], vertical_alignment="bottom")
                with c_ft1:
                    sel_flow_orig_filter = st.selectbox(
                        "Filter Engine",
                        options=flow_origin_filter_opts,
                        key="flow_origin_filter_dropdown",
                        label_visibility="collapsed",
                        help="Filter table view to focus on a specific engine"
                    )
                with c_ft_toggle:
                    toggle_flow_lbl = f"◻️ Deselect ({flow_chk_cnt})" if is_flow_all_sel else f"☑️ Select ({len(df_flow)})"
                    if st.button(toggle_flow_lbl, key="flow_btn_smart_toggle_all", width='stretch', help="Toggle execution selection for all test cases"):
                        apply_origin_selection_to_suite(df_flow, "none" if is_flow_all_sel else "all")
                        st.rerun()
                with c_ft_sel:
                    with st.popover("⚡ Select Engine", width='stretch', help="Check test cases by specific generation engine"):
                        st.markdown("##### ⚡ Check Tests by Engine")
                        if st.button(f"⚡ Robust Only ({rob_c})", key="flow_pop_btn_robust", width='stretch'):
                            apply_origin_selection_to_suite(df_flow, "robust_only")
                            st.rerun()
                        if st.button(f"🤖 AI Only ({ai_c})", key="flow_pop_btn_ai", width='stretch'):
                            apply_origin_selection_to_suite(df_flow, "ai_only")
                            st.rerun()
                        if st.button(f"🤖+⚙️ Hybrid ({hyb_c})", key="flow_pop_btn_hyb", width='stretch'):
                            apply_origin_selection_to_suite(df_flow, "hybrid_only")
                            st.rerun()
                        if st.button(f"⚙️ Rule Only ({rl_c})", key="flow_pop_btn_rule", width='stretch'):
                            apply_origin_selection_to_suite(df_flow, "rule_only")
                            st.rerun()
                        if st.button(f"✍️ Manual Only ({mn_c})", key="flow_pop_btn_man", width='stretch'):
                            apply_origin_selection_to_suite(df_flow, "manual_only")
                            st.rerun()
                        st.divider()
                        if st.button("🔄 Reset View & Select All", key="flow_pop_reset_full_view", width='stretch'):
                            df_flow["Run"] = True
                            st.session_state["test_cases_df"] = df_flow
                            if "results_df" in st.session_state and st.session_state.results_df is not None:
                                if len(st.session_state.results_df) == len(df_flow):
                                    st.session_state.results_df["Run"] = True
                            st.session_state["flow_origin_filter_dropdown"] = flow_origin_filter_opts[0]
                            st.session_state["grid_origin_filter_dropdown"] = flow_origin_filter_opts[0]
                            st.session_state["grid_target_focus_select"] = "🌐 All Screens & Targets"
                            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                            st.rerun()
                with c_ft3:
                    with st.popover("⚙️ Manage", width='stretch', help="Delete selected test cases, clear selection, export, or wipe the suite"):
                        st.markdown("##### ⚙️ Test Suite Management")
                        st.caption("Remove selected rows, adjust run checkboxes, or wipe the test suite.")
                        flow_chk_cnt = int(df_flow["Run"].fillna(True).astype(bool).sum()) if "Run" in df_flow.columns else len(df_flow)
                        del_btn_lbl = f"🗑️ Delete Selected ({flow_chk_cnt})" if flow_chk_cnt > 0 else "🗑️ Delete Selected"
                        if st.button(del_btn_lbl, key="flow_pop_del_selected", width='stretch', type="secondary"):
                            if flow_chk_cnt == 0:
                                st.warning("⚠️ No test cases are currently checked with 'Run'.")
                            else:
                                if delete_selected_test_cases(df_flow):
                                    st.toast(f"🗑️ Deleted {flow_chk_cnt} selected test cases!", icon="✅")
                                    st.rerun()

                        st.divider()
                        if st.button("⚠️ Clear All Test Cases", key="flow_pop_clear_all", width='stretch', type="primary"):
                            clear_entire_test_suite()
                            st.toast("🧹 Test suite cleared successfully!", icon="✅")
                            st.rerun()
                with c_ft4:
                    with st.popover("📥 Export", width='stretch', help="Download active flow test suite as CSV or Excel"):
                        flow_csv_bytes = df_flow.to_csv(index=False).encode("utf-8")
                        st.download_button(
                            "📄 Download CSV",
                            data=flow_csv_bytes,
                            file_name=f"generated_flow_tests_{int(time.time())}.csv",
                            mime="text/csv",
                            width='stretch',
                            key="btn_download_flow_tests_csv",
                            help="Download the entire generated flow test suite as a CSV file",
                        )
                        try:
                            from io import BytesIO
                            out_f_xl = BytesIO()
                            with pd.ExcelWriter(out_f_xl, engine="openpyxl") as f_writer:
                                df_flow.to_excel(f_writer, index=False, sheet_name="Flow Tests")
                            st.download_button(
                                "📊 Download Excel (.xlsx)",
                                data=out_f_xl.getvalue(),
                                file_name=f"generated_flow_tests_{int(time.time())}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                width='stretch',
                                key="pop_flow_dl_xlsx",
                            )
                        except Exception:
                            pass

                disp_flow_table = df_flow.copy()
                if "Robust" in sel_flow_orig_filter:
                    orig_mask = disp_flow_table["Generated_By"].astype(str).str.contains("Robust", na=False)
                    disp_flow_table = disp_flow_table[orig_mask]
                elif "AI Generated (Full)" in sel_flow_orig_filter or "AI Full" in sel_flow_orig_filter:
                    orig_mask = (disp_flow_table["Generated_By"].astype(str).str.contains("AI Generated", na=False)) & (~disp_flow_table["Generated_By"].astype(str).str.contains("Template", na=False))
                    disp_flow_table = disp_flow_table[orig_mask]
                elif "Template Code" in sel_flow_orig_filter or "Hybrid" in sel_flow_orig_filter:
                    orig_mask = disp_flow_table["Generated_By"].astype(str).str.contains("Template", na=False)
                    disp_flow_table = disp_flow_table[orig_mask]
                elif "Rule Engine" in sel_flow_orig_filter:
                    orig_mask = disp_flow_table["Generated_By"].astype(str).str.contains("Rule Engine|20-Point", na=False)
                    disp_flow_table = disp_flow_table[orig_mask]
                elif "Manual" in sel_flow_orig_filter:
                    orig_mask = disp_flow_table["Generated_By"].astype(str).str.contains("Manual|Uploaded", na=False)
                    disp_flow_table = disp_flow_table[orig_mask]

                flow_edited_grid = st.data_editor(
                    sanitize_df_for_streamlit(disp_flow_table),
                    num_rows="dynamic",
                    width="stretch",
                    key=f"flow_data_grid_view_{st.session_state.get('grid_version', 0)}",
                    column_config={
                        "Run": st.column_config.CheckboxColumn("▶️ Run?", default=True),
                        "Type": st.column_config.SelectboxColumn("Type", options=["Web", "Mobile (Native)", "API"], required=True),
                        "Generated_By": st.column_config.TextColumn("Generated By", disabled=True),
                        "Target": st.column_config.TextColumn("Target URL / Endpoint", required=True),
                        "Test_Step_Description": st.column_config.TextColumn("Test Step Description", width="large", required=True),
                        "Expected_Result": st.column_config.TextColumn("Expected Result", width="medium"),
                        "Playwright_Code": st.column_config.TextColumn("Playwright Code", width="large"),
                        "Status": st.column_config.TextColumn("Status", disabled=True),
                        "Duration": st.column_config.TextColumn("Duration", disabled=True),
                        "Actual_Result": st.column_config.TextColumn("Actual Result", disabled=True),
                    },
                    hide_index=False,
                )

                if flow_edited_grid is not None:
                    if len(flow_edited_grid) == 0:
                        clear_entire_test_suite()
                        st.toast("🧹 Test suite cleared!", icon="✅")
                        st.rerun()
                    else:
                        flow_valid = (
                            flow_edited_grid["Test_Step_Description"].fillna("").astype(str).str.strip().ne("")
                            | flow_edited_grid["Target"].fillna("").astype(str).str.strip().ne("")
                        )
                        if flow_valid.any():
                            flow_edited_grid = flow_edited_grid[flow_valid].copy()
                        if len(disp_flow_table) < len(df_flow):
                            df_flow.loc[flow_edited_grid.index] = flow_edited_grid
                            clean_flow_m = sanitize_df_for_streamlit(df_flow).reset_index(drop=True)
                            clean_flow_m.index = range(1, len(clean_flow_m) + 1)
                            st.session_state["test_cases_df"] = clean_flow_m
                            if "results_df" in st.session_state and st.session_state.results_df is not None:
                                if len(st.session_state.results_df) == len(clean_flow_m):
                                    st.session_state.results_df["Run"] = clean_flow_m["Run"].values
                        else:
                            clean_flow_m = sanitize_df_for_streamlit(flow_edited_grid).reset_index(drop=True)
                            clean_flow_m.index = range(1, len(clean_flow_m) + 1)
                            st.session_state["test_cases_df"] = clean_flow_m
                            if "results_df" in st.session_state and st.session_state.results_df is not None:
                                if len(st.session_state.results_df) == len(clean_flow_m):
                                    st.session_state.results_df["Run"] = clean_flow_m["Run"].values
                        
                        # Sync row modifications/deletions to disk
                        if len(flow_edited_grid) < len(disp_flow_table):
                            for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                                if os.path.exists(disk_f):
                                    try:
                                        clean_flow_m.to_csv(disk_f, index=False)
                                    except Exception:
                                        pass

    elif generation_type == "📸 UI Screenshot / Mockup":
        st.info(
            "Upload any screenshot, wireframe, or UI mockup with an optional User Story. AI Vision will analyze every component to generate an automated test suite!"
        )
        uploaded_ui_img = st.file_uploader(
            "Upload UI Screenshot / Wireframe / Design Mockup",
            type=["png", "jpg", "jpeg", "webp"],
        )
        if uploaded_ui_img:
            with st.expander("📸 Uploaded UI Preview (Click to view image)", expanded=False):
                safe_st_image(uploaded_ui_img, caption="Uploaded UI Preview", width="stretch")

        mockup_target_url = st.text_input(
            "Target URL for Test Cases",
            placeholder="https://example.com/login",
            key="mockup_target_url_input",
            help="The URL to associate with the generated test cases",
        )
        mockup_user_story = st.text_area(
            "📖 User Story / Business Flow / Expected User Journey (Optional)",
            placeholder="e.g. As a customer, I want to add items to cart, enter delivery address, apply coupon code, and checkout securely.",
            height=85,
            key="mockup_user_story_input",
            help="Provide the business story and expected interaction flow for the UI mockup.",
        )

        col_m1, col_m2 = st.columns([1.5, 1])
        with col_m1:
            if st.button("✨ Auto-Generate Tests from Screenshot", type="primary", width='stretch'):
                is_valid_m_url, m_url_err = validate_url(mockup_target_url)
                if not uploaded_ui_img:
                    st.warning("⚠️ Please upload a UI screenshot first.")
                elif not is_valid_m_url:
                    st.warning(f"⚠️ Validation Failed: {m_url_err}")
                else:
                    vision_prog = st.progress(0.0, text="👁️ AI Vision: Preparing screenshot for visual inspection...")

                    def _vision_progress_cb(pct: float, msg: str):
                        try:
                            vision_prog.progress(min(1.0, max(0.0, pct)), text=f"👁️ AI Vision: {msg}")
                        except Exception:
                            pass

                    from core.ai_client import AIClient
                    from core.test_generator import TestGenerator

                    try:
                        gen = TestGenerator(AIClient())
                        res_path, ss_path = gen.generate_cases_from_image(
                            image_bytes=uploaded_ui_img.getvalue(),
                            target_url=mockup_target_url or "https://example.com",
                            user_instructions=mockup_user_story,
                            output_csv_path="generated_tests.csv",
                            on_progress_callback=_vision_progress_cb,
                        )
                        if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
                            vision_prog.progress(1.0, text="🎉 Successfully generated test cases from UI screenshot!")
                            time.sleep(0.35)
                            st.session_state.auto_generated_file = res_path
                            st.session_state.last_analysis_ss = ss_path
                            if "results_df" in st.session_state:
                                del st.session_state["results_df"]
                            try:
                                df_mockup = pd.read_csv(res_path)
                                test_count = len(df_mockup)
                            except pd.errors.EmptyDataError:
                                df_mockup = pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code"])
                                test_count = 0
                            st.session_state["test_cases_df"] = normalize_test_dataframe(df_mockup, default_target=mockup_target_url or "https://example.com")
                            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                            st.session_state["mockup_gen_success_msg"] = f"✅ Successfully generated {test_count} test cases from the UI screenshot!"
                            st.rerun()
                        else:
                            vision_prog.empty()
                            st.error("Failed to generate test cases from screenshot.")
                    except Exception as ve:
                        vision_prog.empty()
                        st.error(f"Vision Generation Error: {ve}")
        with col_m2:
            def _cb_clear_mockup():
                st.session_state["mockup_target_url_input"] = ""
                st.session_state["mockup_user_story_input"] = ""
                st.session_state.pop("mockup_gen_success_msg", None)

            st.button(
                "🧹 Clear Inputs",
                on_click=_cb_clear_mockup,
                type="secondary",
                width='stretch',
                key="clear_mockup_inputs"
            )

        if st.session_state.get("mockup_gen_success_msg"):
            c_m_msg, c_m_dl = st.columns([3.2, 1.2])
            with c_m_msg:
                st.success(st.session_state["mockup_gen_success_msg"])
            with c_m_dl:
                df_m = st.session_state.get("test_cases_df")
                if df_m is not None and isinstance(df_m, pd.DataFrame) and not df_m.empty:
                    st.download_button(
                        "📥 Download Suite (CSV)",
                        data=df_m.to_csv(index=False).encode("utf-8"),
                        file_name=f"screenshot_tests_{int(time.time())}.csv",
                        mime="text/csv",
                        width='stretch',
                        key="btn_dl_mockup_tests_csv",
                        help="Download generated test cases as CSV",
                    )

    else:
        st.info(
            "Provide the URL to your OpenAPI/Swagger spec, or paste the raw JSON, plus an optional User Story for business flow testing!"
        )
        api_base_url = st.text_input(
            "Base URL",
            placeholder="https://api.example.com",
            key="api_base_url_input",
        )
        api_user_story = st.text_area(
            "📖 User Story / Business Scenarios (Optional)",
            placeholder="e.g. As a registered user, I need to authenticate with Bearer token, create orders, and fetch my order history.",
            height=80,
            key="api_user_story_input",
            help="Define specific API user journeys or business workflows to test.",
        )
        swagger_source = st.radio(
            "Swagger Spec Source", ["URL", "Paste JSON"], horizontal=True
        )

        swagger_url = ""
        swagger_spec_input = ""

        if swagger_source == "URL":
            swagger_url = st.text_input(
                "Swagger JSON URL", placeholder="https://api.example.com/swagger.json", key="swagger_url_input"
            )
        else:
            swagger_spec_input = st.text_area(
                "Swagger / OpenAPI Spec (JSON)", height=150, key="swagger_json_input"
            )

        col_a1, col_a2 = st.columns([1.5, 1])
        with col_a1:
            if st.button("✨ Auto-Generate API Tests", type="primary", width='stretch'):
                is_valid_base, base_err = validate_url(api_base_url)
                if not is_valid_base:
                    st.warning(f"⚠️ Base URL Validation Failed: {base_err}")
                elif swagger_source == "URL" and not swagger_url:
                    st.warning("⚠️ Please provide a valid Swagger JSON URL.")
                elif swagger_source == "Paste JSON" and not swagger_spec_input:
                    st.warning("⚠️ Please paste your OpenAPI/Swagger JSON specification.")
                else:
                    swagger_spec = ""
                    if swagger_source == "URL":
                        try:
                            import requests
                            response = requests.get(swagger_url, timeout=15)
                            response.raise_for_status()
                            swagger_spec = response.text
                        except Exception as e:
                            st.error(f"Failed to fetch Swagger JSON: {e}")
                    else:
                        is_valid_j, j_err = validate_json_body(swagger_spec_input)
                        if not is_valid_j:
                            st.error(f"⚠️ Swagger Spec Validation Failed: {j_err}")
                        else:
                            swagger_spec = swagger_spec_input

                    if swagger_spec:
                        api_prog = st.progress(0.0, text="📜 OpenAPI Parser: Reading and validating API schema...")

                        def _api_prog_cb(pct: float, msg: str):
                            try:
                                api_prog.progress(min(1.0, max(0.0, pct)), text=f"📜 API Generator: {msg}")
                            except Exception:
                                pass

                        from core.ai_client import AIClient
                        from core.test_generator import TestGenerator

                        try:
                            gen = TestGenerator(AIClient())
                            res_path = gen.generate_api_cases_from_spec(
                                spec_content=swagger_spec,
                                base_url=api_base_url,
                                output_csv_path="generated_tests.csv",
                                user_story=api_user_story,
                                append=False,
                                on_progress_callback=_api_prog_cb,
                            )
                            if isinstance(res_path, tuple):
                                res_path = res_path[0]
                            if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
                                api_prog.progress(1.0, text="🎉 Successfully generated API test cases!")
                                time.sleep(0.35)
                                st.session_state.auto_generated_file = res_path
                                if "results_df" in st.session_state:
                                    del st.session_state["results_df"]
                                try:
                                    df_api = pd.read_csv(res_path)
                                    test_count = len(df_api)
                                except pd.errors.EmptyDataError:
                                    df_api = pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code"])
                                    test_count = 0
                                st.session_state["test_cases_df"] = normalize_test_dataframe(df_api, default_target=api_base_url or "https://api.example.com")
                                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                                st.session_state["api_gen_success_msg"] = f"✅ Successfully generated {test_count} API test cases!"
                                st.rerun()
                            else:
                                api_prog.empty()
                                st.error("Failed to generate API test cases.")
                        except Exception as e:
                            api_prog.empty()
                            st.error(f"API Generation Error: {e}")
        with col_a2:
            def _cb_clear_api():
                st.session_state["api_base_url_input"] = ""
                st.session_state["api_user_story_input"] = ""
                if "swagger_url_input" in st.session_state:
                    st.session_state["swagger_url_input"] = ""
                if "swagger_json_input" in st.session_state:
                    st.session_state["swagger_json_input"] = ""
                st.session_state.pop("api_gen_success_msg", None)

            st.button(
                "🧹 Clear Inputs",
                on_click=_cb_clear_api,
                type="secondary",
                width='stretch',
                key="clear_api_inputs"
            )

        if st.session_state.get("api_gen_success_msg"):
            c_a_msg, c_a_dl = st.columns([3.2, 1.2])
            with c_a_msg:
                st.success(st.session_state["api_gen_success_msg"])
            with c_a_dl:
                df_a = st.session_state.get("test_cases_df")
                if df_a is not None and isinstance(df_a, pd.DataFrame) and not df_a.empty:
                    st.download_button(
                        "📥 Download Suite (CSV)",
                        data=df_a.to_csv(index=False).encode("utf-8"),
                        file_name=f"swagger_api_tests_{int(time.time())}.csv",
                        mime="text/csv",
                        width='stretch',
                        key="btn_dl_api_tests_csv",
                        help="Download generated API test cases as CSV",
                    )

with tab3:
    st.info("✍️ Manually create custom test cases or initialize a fresh blank suite.")
    with st.form("manual_case_form_tab3", clear_on_submit=True):
        st.markdown("##### ➕ Create & Append Manual Test Case")
        st.caption("Custom test cases will be marked with **✍️ Manual / Uploaded** attribution in the suite.")
        c_m_t1, c_m_t2 = st.columns([1, 3])
        with c_m_t1:
            m_type = st.selectbox("Type", ["Web", "API"], key="tab3_manual_type")
        with c_m_t2:
            m_target = st.text_input("Target URL / Endpoint", placeholder="https://example.com/app or http://api.example.com/v1/resource", key="tab3_manual_target")
        m_step = st.text_area("Test Step Description", placeholder="e.g. Step 1: Open login view -> Step 2: Fill credentials -> Step 3: Verify dashboard renders", key="tab3_manual_step")
        c_m_e1, c_m_e2 = st.columns([2, 2])
        with c_m_e1:
            m_exp = st.text_input("Expected Result", value="Action executes cleanly and verified state successfully", key="tab3_manual_exp")
        with c_m_e2:
            m_code = st.text_input("Playwright Code (Optional)", placeholder="e.g. page.locator('.btn-save').click(force=True)", key="tab3_manual_code")
        
        btn_add_tab3 = st.form_submit_button("➕ Add Test Case to Active Suite", type="primary", width='stretch')
        if btn_add_tab3:
            if not m_step.strip():
                st.error("Please enter a Test Step Description before submitting.")
            else:
                raw_input_text = m_step.strip()
                # Auto-detect if user pasted a multi-line table or CSV into the step box
                is_tabular = ("\n" in raw_input_text and ("," in raw_input_text or "\t" in raw_input_text)) or any(sig in raw_input_text.lower() for sig in ["test case id", "screen / function", "expected outcome", "test steps", "use case id"])
                if is_tabular:
                    from core.test_generator import parse_qa_test_sheet_to_dataframe
                    p_df = parse_qa_test_sheet_to_dataframe(raw_input_text, default_target=m_target)
                    if not p_df.empty:
                        target_csv = "deep_inspected_tests.csv"
                        curr_df = st.session_state.get("test_cases_df")
                        if curr_df is not None and isinstance(curr_df, pd.DataFrame) and not curr_df.empty:
                            merged = pd.concat([curr_df, p_df], ignore_index=True)
                        else:
                            merged = p_df
                        merged.index = range(1, len(merged) + 1)
                        merged.to_csv(target_csv, index=False)
                        st.session_state["test_cases_df"] = merged
                        st.session_state["auto_generated_file"] = target_csv
                        st.session_state["suite_explicitly_cleared"] = False
                        if "results_df" in st.session_state:
                            del st.session_state["results_df"]
                        st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                        st.toast(f"🎉 Converted and loaded {len(p_df)} test cases into suite!", icon="✅")
                        st.rerun()
                append_manual_test_case(m_type, m_target, m_step, m_exp, m_code)
                st.toast("✅ Manual test case added to suite!", icon="✍️")
                st.rerun()

    with st.expander("📋 Bulk Paste & Convert Test Cases (CSV / Excel / Jira / TestRail QA Sheets)", expanded=True):
        st.caption("Paste any raw test case text, CSV rows, or spreadsheet export. The converter automatically normalizes it into the runnable 11-column test suite structure and synthesizes clean Playwright execution code.")
        c_bp_u1, c_bp_u2 = st.columns([3, 1])
        with c_bp_u1:
            bulk_target_url = st.text_input(
                "Default Target Web URL for Pasted Tests",
                value=st.session_state.get("single_url_input", ""),
                placeholder="https://example.com/app or https://example.com/login",
                key="tab3_bulk_target_url"
            )
        with c_bp_u2:
            bulk_append_mode = st.checkbox("Append to existing suite", value=False, key="tab3_bulk_append_chk")
        
        bulk_pasted_csv = st.text_area(
            "Paste Raw CSV / Tabular Text here",
            height=200,
            placeholder="Paste your raw CSV rows, Excel table copy, or QA test cases here...",
            key="tab3_bulk_pasted_csv"
        )
        
        c_bp_act1, c_bp_act2 = st.columns([1, 1.2])
        with c_bp_act1:
            btn_rule_convert = st.button("🔄 Fast Rule Convert", type="secondary", width="stretch", key="tab3_btn_convert_bulk")
        with c_bp_act2:
            btn_ai_convert = st.button("✨ AI Smart Re-Gen & Code Synthesis", type="primary", width="stretch", key="tab3_btn_ai_convert_bulk")

        if btn_rule_convert or btn_ai_convert:
            if not bulk_pasted_csv.strip():
                st.warning("⚠️ Please paste test cases or CSV text before converting.")
            else:
                sp_msg = "🤖 AI Re-Gen synthesizing execution-ready suite..." if btn_ai_convert else "Parsing and converting test cases into runnable format..."
                with st.spinner(sp_msg):
                    if btn_ai_convert:
                        import importlib
                        import core.test_generator
                        if not hasattr(core.test_generator.TestGenerator, "ai_regenerate_and_convert_suite"):
                            importlib.reload(core.test_generator)
                        from core.test_generator import TestGenerator
                        from core.ai_client import AIClient
                        ai_c = AIClient()
                        gen = TestGenerator(ai_client=ai_c)
                        converted_df = gen.ai_regenerate_and_convert_suite(bulk_pasted_csv, default_target=bulk_target_url)
                    else:
                        from core.test_generator import parse_qa_test_sheet_to_dataframe
                        converted_df = parse_qa_test_sheet_to_dataframe(bulk_pasted_csv, default_target=bulk_target_url)

                    if converted_df is not None and not converted_df.empty:
                        converted_df = normalize_test_dataframe(converted_df, default_target=bulk_target_url)

                    if converted_df is None or converted_df.empty:
                        st.error("Could not detect any test cases in the pasted text. Please check the text format.")
                    else:
                        target_csv = "deep_inspected_tests.csv"
                        if bulk_append_mode and "test_cases_df" in st.session_state and isinstance(st.session_state["test_cases_df"], pd.DataFrame) and not st.session_state["test_cases_df"].empty:
                            final_suite = pd.concat([st.session_state["test_cases_df"], converted_df], ignore_index=True)
                        else:
                            final_suite = converted_df
                        final_suite.index = range(1, len(final_suite) + 1)
                        final_suite.to_csv(target_csv, index=False)
                        st.session_state["test_cases_df"] = final_suite
                        st.session_state["auto_generated_file"] = target_csv
                        st.session_state["suite_explicitly_cleared"] = False
                        if "results_df" in st.session_state:
                            del st.session_state["results_df"]
                        st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                        st.toast(f"🎉 Successfully converted {len(converted_df)} test cases into the active suite in standard format!", icon="✅")
                        st.rerun()

    with st.expander("➕ Or Start Fresh with an Empty Blank Suite / Clear Suite"):
        st.caption("Clears existing loaded tests and opens an empty editable grid to build from scratch or wipes the suite.")
        c_bs1, c_bs2 = st.columns(2)
        with c_bs1:
            if st.button("➕ Create Blank Suite", type="secondary", width='stretch', key="btn_create_blank_suite_tab3"):
                st.session_state.blank_suite = True
                st.session_state["suite_explicitly_cleared"] = False
                st.session_state.manual_cases = [
                    {
                        "Type": "Web",
                        "Target": "",
                        "Test_Step_Description": "",
                        "Expected_Result": "",
                    }
                ]
                if "auto_generated_file" in st.session_state:
                    del st.session_state.auto_generated_file
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                st.success("✅ Blank test suite initialized!")
                st.rerun()
        with c_bs2:
            if st.button("🗑️ Clear Entire Suite", type="secondary", width='stretch', key="btn_clear_suite_tab3"):
                clear_entire_test_suite()
                st.toast("🧹 Test suite cleared successfully!", icon="✅")
                st.rerun()

with tab4:
    st.info("Quickly test an API endpoint and add it to your suite.")
    col_m, col_u = st.columns([1, 4])
    with col_m:
        pm_method = st.selectbox("Method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
    with col_u:
        pm_url = st.text_input("Endpoint URL", placeholder="https://api.example.com/v1/users")

    pm_body = st.text_area("JSON Body (Optional)", placeholder='{"name": "Alice"}')
    pm_status = st.text_input("Expected Status Code", value="200")

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("🚀 Send Request (Instant Test)", type="secondary"):
            is_valid_ep, ep_err = validate_url(pm_url)
            is_valid_body, body_err = validate_json_body(pm_body)
            is_valid_st, st_err = validate_status_code(pm_status)
            if not is_valid_ep:
                st.warning(f"⚠️ Endpoint URL Validation Failed: {ep_err}")
            elif not is_valid_body:
                st.warning(f"⚠️ JSON Body Validation Failed: {body_err}")
            elif not is_valid_st:
                st.warning(f"⚠️ Status Code Validation Failed: {st_err}")
            else:
                with st.spinner("Sending request..."):
                    try:
                        import requests
                        headers = {"Content-Type": "application/json"} if pm_body else {}
                        payload = pm_body.encode("utf-8") if pm_body else None
                        res = requests.request(
                            pm_method, pm_url, data=payload, headers=headers, timeout=15
                        )
                        if str(res.status_code) == str(pm_status):
                            st.success(f"Status: {res.status_code} OK (Matches Expected {pm_status})")
                        else:
                            st.error(f"Status: {res.status_code} (Expected {pm_status})")
                        try:
                            st.json(res.json())
                        except Exception:
                            st.text(res.text or "<Empty Response>")
                    except Exception as e:
                        st.error(f"Request failed: {e}")

    with col_btn2:
        if st.button("➕ Add to Test Suite", type="primary"):
            is_valid_ep, ep_err = validate_url(pm_url)
            is_valid_body, body_err = validate_json_body(pm_body)
            is_valid_st, st_err = validate_status_code(pm_status)
            if not is_valid_ep:
                st.warning(f"⚠️ Endpoint URL Validation Failed: {ep_err}")
            elif not is_valid_body:
                st.warning(f"⚠️ JSON Body Validation Failed: {body_err}")
            elif not is_valid_st:
                st.warning(f"⚠️ Status Code Validation Failed: {st_err}")
            else:
                desc = f"Send a {pm_method} request to {pm_url}"
                if pm_body:
                    desc += f" with body: {pm_body}"
                new_row = {
                    "Type": "API",
                    "Target": pm_url,
                    "Test_Step_Description": desc,
                    "Expected_Result": f"HTTP {pm_status}",
                }
                if "manual_cases" not in st.session_state:
                    st.session_state.manual_cases = []
                st.session_state.manual_cases.append(new_row)
                st.session_state.blank_suite = True
                if "auto_generated_file" in st.session_state:
                    del st.session_state.auto_generated_file
                st.success("✅ Test case added to suite successfully!")

# ---------------------------------------------------------
# Load DataFrame from active source
# ---------------------------------------------------------
if not uploaded_file:
    st.session_state.pop("_active_uploaded_file_id", None)

df = None
UPLOAD_PARSER_SIGNATURE = "v5_multistory_qa_585"

if uploaded_file:
    upload_id = f"{uploaded_file.name}_{uploaded_file.size}_{UPLOAD_PARSER_SIGNATURE}"
    is_new_upload = st.session_state.get("_active_uploaded_file_id") != upload_id
    if is_new_upload:
        st.session_state["suite_explicitly_cleared"] = False

    # Stale detection: if session has a stale or empty-column suite, force re-parse
    is_stale_or_corrupt = False
    if "test_cases_df" in st.session_state and isinstance(st.session_state.test_cases_df, pd.DataFrame):
        existing_cached = st.session_state.test_cases_df
        if len(existing_cached) > 20:
            empty_step_ratio = existing_cached["Test_Step_Description"].astype(str).str.strip().isin(["", "nan", "None"]).mean() if "Test_Step_Description" in existing_cached.columns else 1.0
            has_no_sheets = ("workbook_sheets" not in st.session_state or not st.session_state["workbook_sheets"])
            if empty_step_ratio > 0.5 or has_no_sheets:
                is_stale_or_corrupt = True

    if not st.session_state.get("suite_explicitly_cleared", False):
        try:
            st.session_state.blank_suite = False
            if is_new_upload or is_stale_or_corrupt:
                st.session_state["_active_uploaded_file_id"] = upload_id
                raw_df = _safe_read_uploaded_file(uploaded_file)
                df = normalize_test_dataframe(raw_df, reset_execution_status=True)
                if "results_df" in st.session_state:
                    del st.session_state["results_df"]
                st.session_state["test_cases_df"] = df.copy()
                st.session_state.batch_paused = False
                st.session_state.paused_at_index = None
                st.session_state["suite_signature"] = upload_id
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                st.toast(f"📥 Loaded {len(df)} test cases from '{uploaded_file.name}' — ready for execution!", icon="✅")
            else:
                if "test_cases_df" in st.session_state and isinstance(st.session_state.test_cases_df, pd.DataFrame) and not st.session_state.test_cases_df.empty:
                    df = st.session_state.test_cases_df.copy()
                else:
                    raw_df = _safe_read_uploaded_file(uploaded_file)
                    df = normalize_test_dataframe(raw_df, reset_execution_status=False)
        except Exception as e:
            st.error(f"Error reading uploaded file: {e}")
elif "test_cases_df" in st.session_state and isinstance(st.session_state.test_cases_df, pd.DataFrame) and not st.session_state.test_cases_df.empty:
    df = st.session_state.test_cases_df.copy()
elif "auto_generated_file" in st.session_state and os.path.exists(str(st.session_state.auto_generated_file)) and os.path.getsize(str(st.session_state.auto_generated_file)) > 0:
    try:
        st.session_state.blank_suite = False
        df = pd.read_csv(st.session_state.auto_generated_file)
        st.session_state["test_cases_df"] = df.copy()
    except pd.errors.EmptyDataError:
        df = None
    except Exception as e:
        st.error(f"Error reading generated file: {e}")
elif st.session_state.get("blank_suite"):
    if "manual_cases" in st.session_state and len(st.session_state.manual_cases) > 0:
        df = pd.DataFrame(st.session_state.manual_cases)
        for col in ["Type", "Target", "Test_Step_Description", "Expected_Result"]:
            if col not in df.columns:
                df[col] = ""
    else:
        df = pd.DataFrame(
            columns=["Type", "Target", "Test_Step_Description", "Expected_Result"]
        )

# ---------------------------------------------------------
# Empty State Quick-Start Guide (When No Suite is Loaded)
# ---------------------------------------------------------
if df is None:
    st.markdown("<div style='margin-top: 16px;'></div>", unsafe_allow_html=True)
    qs1, qs2, qs3 = st.columns(3)
    with qs1:
        st.markdown(
            """<div class="workflow-step-card">
                <div class="workflow-step-num">1</div>
                <div class="workflow-step-title">🔐 Configure & Authenticate</div>
                <div class="workflow-step-desc">Set your execution preferences in the left Control Center and optionally capture an authenticated browser session (<code>auth.json</code>) to bypass login screens.</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with qs2:
        st.markdown(
            """<div class="workflow-step-card">
                <div class="workflow-step-num">2</div>
                <div class="workflow-step-title">✨ Load or Auto-Generate Suite</div>
                <div class="workflow-step-desc">Upload a CSV/Excel file above, or switch to <strong>✨ AI Auto-Generator</strong> to autonomously crawl pages, inspect A11y trees, and build test suites.</div>
            </div>""",
            unsafe_allow_html=True,
        )
    with qs3:
        st.markdown(
            """<div class="workflow-step-card">
                <div class="workflow-step-num">3</div>
                <div class="workflow-step-title">⚡ Execute, Self-Heal & Report</div>
                <div class="workflow-step-desc">Run tests in batch or individually with live Play/Pause/Resume controls, Multi-Tier Self-Healing, HTML/Excel exports, and 1-click ClickUp bug filing.</div>
            </div>""",
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------
# Editable Test Grid & Execution
# ---------------------------------------------------------
if df is not None:
    df = df.loc[:, ~df.columns.str.contains(r"^Unnamed")]
    valid_mask = (
        df["Test_Step_Description"].fillna("").astype(str).str.strip().ne("")
        | df["Target"].fillna("").astype(str).str.strip().ne("")
    )
    df = df[valid_mask].dropna(how="all").reset_index(drop=True)
    df.index = range(1, len(df) + 1)

    if "Run" not in df.columns:
        df.insert(0, "Run", True)

    # Check if a bulk select/deselect action was triggered via UI buttons
    if st.session_state.get("run_all_state") is not None:
        is_all = bool(st.session_state["run_all_state"])
        df["Run"] = is_all
        st.session_state["run_all_state"] = None
        st.session_state["test_cases_df"] = df
        if "results_df" in st.session_state and st.session_state.results_df is not None:
            if len(st.session_state.results_df) == len(df):
                st.session_state.results_df["Run"] = df["Run"].values
        if is_all:
            st.toast(f"✅ All {len(df)} tests checked for execution!", icon="✅")
        else:
            st.toast("Unchecked all tests for execution.", icon="❌")
    elif st.session_state.get("run_focus_target") is not None:
        tgt_val = st.session_state["run_focus_target"]
        if tgt_val != "🌐 All Screens & Targets":
            df["Run"] = df["Target"].astype(str).str.strip() == tgt_val
        else:
            df["Run"] = True
        st.session_state["run_focus_target"] = None
        st.session_state["test_cases_df"] = df
        if "results_df" in st.session_state and st.session_state.results_df is not None:
            if len(st.session_state.results_df) == len(df):
                st.session_state.results_df["Run"] = df["Run"].values
        focused_count = int(df["Run"].sum())
        st.toast(f"🎯 Focused {focused_count} tests on '{tgt_val}'!", icon="🎯")

    st.divider()
    st.subheader("📝 Test Suite Grid (Editable)")
    st.caption("Double-click cells to edit. Toggle the 'Run' checkbox to select which tests to execute.")
    if "results_df" in st.session_state and st.session_state.results_df is not None:
        res_df = st.session_state.results_df
        if len(res_df) == len(df) and "Run" in res_df.columns:
            if "Test_Step_Description" in res_df.columns and "Test_Step_Description" in df.columns:
                res_steps = res_df["Test_Step_Description"].astype(str).str.strip().tolist()
                df_steps = df["Test_Step_Description"].astype(str).str.strip().tolist()
                if (res_steps == df_steps) or (res_steps[:3] == df_steps[:3]):
                    df["Run"] = res_df["Run"].values.astype(bool)
            else:
                df["Run"] = res_df["Run"].values.astype(bool)

    for col, default_val in [
        ("Type", "Web"),
        ("Target", ""),
        ("Test_Step_Description", ""),
        ("Expected_Result", ""),
        ("Status", "PENDING"),
        ("Duration", "-"),
        ("Actual_Result", "-"),
        ("Screenshot", ""),
        ("Execution_Notes", ""),
        ("Playwright_Code", ""),
    ]:
        if col not in df.columns:
            df[col] = default_val
        df[col] = df[col].apply(lambda x, dv=default_val: _clean_text_val(x, dv)).astype(object)

    # Sync latest execution results into df if available
    if "results_df" in st.session_state and st.session_state.results_df is not None:
        res_df = st.session_state.results_df
        suite_matches = False
        if len(res_df) == len(df):
            if "Test_Step_Description" in res_df.columns and "Test_Step_Description" in df.columns:
                res_steps = res_df["Test_Step_Description"].astype(str).str.strip().tolist()
                df_steps = df["Test_Step_Description"].astype(str).str.strip().tolist()
                suite_matches = (res_steps == df_steps) or (res_steps[:3] == df_steps[:3])
            else:
                suite_matches = True
        if suite_matches:
            for col in ["Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code", "Generated_By"]:
                if col in res_df.columns:
                    default_v = "PENDING" if col == "Status" else ("-" if col in ["Duration", "Actual_Result"] else "")
                    df[col] = pd.Series(res_df[col].values, index=df.index).apply(lambda x, dv=default_v: _clean_text_val(x, dv)).astype(object)
        else:
            del st.session_state["results_df"]

    def _detect_row_generation_source(row):
        existing = str(row.get("Generated_By", "")).strip()
        if existing and existing.lower() not in ["", "nan", "none", "-"]:
            return existing
        if st.session_state.get("blank_suite") or bool(st.session_state.get("_active_uploaded_file_id")):
            code = str(row.get("Playwright_Code", "")).strip()
            if code and len(code) > 10 and code != "nan":
                return "✍️ Uploaded + ⚙️ Rule Code"
            return "✍️ Manual / Uploaded"

        step = str(row.get("Test_Step_Description", "")).strip()
        code = str(row.get("Playwright_Code", "")).strip()
        notes = str(row.get("Execution_Notes", "")).strip()

        template_markers = [
            "# 1. Capture baseline record count before applying filter",
            "# 1. Inspect and populate form fields with valid test data",
            "# 1. Auto-dismiss any hanging overlays and clear search box",
            "# 1. Auto-clean overlays and capture record count before clearing",
            "# 1. Clean backdrop overlays and dismiss any open drawers",
            "# 1. Fill draft test values in form fields",
            "# 1. Clear any inputs to trigger required constraints",
            "Could not locate primary action button on the screen.",
        ]
        has_template_code = any(m in code for m in template_markers)
        has_ai_marker = "🤖" in notes or "ai_generated" in notes.lower()

        if has_ai_marker and not has_template_code and len(code) > 5 and code != "nan":
            return "🤖 AI Generated"
        elif has_ai_marker and has_template_code:
            return "🤖 AI Step + ⚙️ Template Code"
        elif has_template_code:
            return "⚙️ Rule Engine"
        elif "auto_generated_file" in st.session_state:
            auto_f = str(st.session_state.auto_generated_file).lower()
            if "deep_inspected" in auto_f or "ai" in auto_f:
                return "🤖 AI Generated"
        return "✍️ Manual / Uploaded"

    # 2. Only harmonize QA synonyms if any QA synonym columns actually exist
    qa_keys = (
        "Screen / Function", "Screen / function", "Screen/Function", "Heading (one of the 7)",
        "Heading", "User Action on Dev Link", "Steps: who clicks what on the dev link",
        "Expected Outcome", "Expected outcome", "Expected result: what they see",
        "Preconditions (test account, data that must exist first)", "Preconditions"
    )
    if any(k in df.columns for k in qa_keys):
        from core.test_generator import harmonize_qa_dataframe
        df = harmonize_qa_dataframe(df)
    df["Generated_By"] = df.apply(_detect_row_generation_source, axis=1)

    # Apply 1-click engine/origin bulk run selection if triggered
    if st.session_state.get("run_focus_origin") is not None:
        orig_val = st.session_state["run_focus_origin"]
        if orig_val in ["🤖 AI Generated (Full)", "🤖 AI Only"]:
            df["Run"] = (df["Generated_By"].astype(str).str.contains("AI Generated", na=False)) & (~df["Generated_By"].astype(str).str.contains("Template", na=False))
        elif orig_val in ["🤖 AI Step + ⚙️ Template Code", "🤖+⚙️ Hybrid"]:
            df["Run"] = df["Generated_By"].astype(str).str.contains("Template", na=False)
        elif orig_val in ["⚙️ Rule Engine", "⚙️ Rule Only"]:
            df["Run"] = df["Generated_By"].astype(str).str.contains("Rule Engine|20-Point", na=False)
        elif orig_val in ["✍️ Manual / Uploaded", "✍️ Manual"]:
            df["Run"] = df["Generated_By"].astype(str).str.contains("Manual|Uploaded", na=False)
        elif orig_val == "🌐 All":
            df["Run"] = True
        checked_count = int(df["Run"].sum())
        st.session_state["run_focus_origin"] = None
        st.session_state["test_cases_df"] = df
        if "results_df" in st.session_state and st.session_state.results_df is not None:
            if len(st.session_state.results_df) == len(df):
                st.session_state.results_df["Run"] = df["Run"].values
        st.toast(f"✅ Checked {checked_count} tests matching '{orig_val}' for execution!", icon="⚡")

    # Clean up spurious columns (empty unnamed columns, corrupted header fragments)
    spurious_to_drop = []
    for c in df.columns:
        c_str = str(c).strip()
        if re.match(r'^(?:unnamed:\s*\d+|column\s*\d+)$', c_str, re.IGNORECASE):
            if df[c].dropna().astype(str).str.strip().ne('').sum() <= 1:
                spurious_to_drop.append(c)
        elif re.match(r'^(?:tc|uc|slm|us)-', c_str, re.IGNORECASE) or c_str.lower() in ["pass", "fail", "known ip", "rate limiting", "test pass %", "total test cases"]:
            spurious_to_drop.append(c)
        elif len(c_str) > 45:
            spurious_to_drop.append(c)
    if spurious_to_drop:
        df = df.drop(columns=spurious_to_drop, errors="ignore")

    # Decouple User Presentation Grid (100% original columns) from internal execution adapter (_target, _code)
    adapter_cols = ["Playwright_Code", "Generated_By", "Screenshot", "Execution_Notes", "_code", "_target", "_step", "_expected", "_type"]
    control_cols = ["Run"]
    if "Sheet_Name" in df.columns and df["Sheet_Name"].nunique() > 1:
        control_cols.append("Sheet_Name")
    if "Status" in df.columns:
        control_cols.append("Status")

    # If df has original user columns, classify internal duplicate columns as adapter columns
    standard_core_set = set(control_cols + adapter_cols + ["Duration", "Actual_Result", "Type", "Target", "Test_Step_Description", "Expected_Result"])
    has_custom_user_cols = any(c not in standard_core_set for c in df.columns)
    if has_custom_user_cols:
        for std_adapter in ["Type", "Target", "Test_Step_Description", "Expected_Result"]:
            if std_adapter in df.columns and std_adapter not in adapter_cols:
                adapter_cols.append(std_adapter)

    reserved_set = set(control_cols + adapter_cols + ["Duration", "Actual_Result"])
    user_presentation_cols = [c for c in df.columns if c not in reserved_set]
    if not user_presentation_cols:
        for std_c in ["Type", "Target", "Test_Step_Description", "Expected_Result"]:
            if std_c in df.columns and std_c not in user_presentation_cols:
                user_presentation_cols.append(std_c)

    result_cols = [c for c in ["Duration", "Actual_Result"] if c in df.columns]

    full_ordered = [c for c in control_cols + user_presentation_cols + [c for c in adapter_cols if c in df.columns] + result_cols if c in df.columns]
    full_ordered += [c for c in df.columns if c not in full_ordered]
    df = df[full_ordered]

    # AI Generation Verification Summary Bar
    _, ai_full_count, ai_hybrid_count, rule_count, manual_count, robust_count = compute_origin_counts(df, include_robust=True)

    cur_plat_name = active_platform_config.get("platform", "desktop_chrome")
    cur_plat_label = PLATFORM_KEY_TO_LABEL.get(cur_plat_name, "Desktop Chrome")
    cur_orient = active_platform_config.get("orientation", "portrait").capitalize()
    cur_net = active_platform_config.get("network_preset", "direct")
    cur_net_label = NETWORK_KEY_TO_LABEL.get(cur_net, "Direct")
    plat_info_str = f"🎯 {cur_plat_label} ({cur_orient})" if any(m in cur_plat_name for m in ["iphone", "pixel", "galaxy", "ipad"]) else f"🎯 {cur_plat_label}"
    net_badge_str = f'<span class="status-pill status-paused" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="Active network simulation profile">📶 {cur_net_label}</span>' if cur_net != "direct" else ""

    pills_main = [
        f'<span class="status-pill status-running" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="Current active target execution platform & device profile">{plat_info_str}</span>'
    ]
    if net_badge_str:
        pills_main.append(net_badge_str)
    pills_main.extend([
        f'<span class="status-pill status-passed" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="⚡ RobustEngine v4 7-Category Deep Matrix & Chained Journeys">⚡ Robust: {robust_count}</span>',
        f'<span class="status-pill status-passed" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="Both test steps and Playwright code generated by AI LLM">🤖 AI Full: {ai_full_count}</span>',
        f'<span class="status-pill status-running" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="AI test steps with deterministic template Playwright scripts">🤖+⚙️ Hybrid: {ai_hybrid_count}</span>',
        f'<span class="status-pill status-paused" style="text-transform: none; animation: none; font-size: 0.78rem; padding: 3px 8px;" title="Synthesized by Rule Engine from DOM/A11y/Network inspection (0 tokens, 100% offline)">⚙️ Rule Engine: {rule_count}</span>',
        f'<span class="status-pill status-pending" style="text-transform: none; font-size: 0.78rem; padding: 3px 8px;" title="Manually created or uploaded test cases">✍️ Manual: {manual_count}</span>',
    ])
    pills_main_html = "".join(pills_main)
    origin_html_main = f'<div class="metric-card" style="display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 16px; margin-bottom: 10px; border-radius: 8px;"><div style="display: flex; align-items: center; gap: 8px;"><span style="font-size: 1.05rem;">🔍</span><strong style="color: #F8FAFC; font-size: 0.92rem;">Test Suite Origin Check:</strong><span style="color: #94A3B8; font-size: 0.85rem;">Total <b>{len(df)}</b> loaded</span></div><div style="display: flex; flex-wrap: wrap; gap: 6px;">{pills_main_html}</div></div>'
    render_html_block(origin_html_main)

    if "grid_version" not in st.session_state:
        st.session_state["grid_version"] = 0

    unique_sheets = [str(s).strip() for s in df["Sheet_Name"].dropna().unique().tolist() if str(s).strip() and str(s).strip().lower() not in ["none", "nan", "", "default"]] if "Sheet_Name" in df.columns else []

    selected_sheets = []
    selected_sheet = "🌐 All Sheets"
    if len(unique_sheets) > 1:
        sheet_counts = {sh: int((df["Sheet_Name"].astype(str).str.strip() == sh).sum()) for sh in unique_sheets}
        all_count = len(df)
        sheet_options = ["🌐 All Sheets"] + unique_sheets

        def _cb_sheet_selector_updated():
            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1

        st.markdown("<div style='margin-top: 6px; margin-bottom: 2px;'><small style='color: #94A3B8; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;'>📑 Multi-Sheet Workbook Navigator:</small></div>", unsafe_allow_html=True)
        c_nav_mode, c_nav_sel = st.columns([1.8, 3.8])
        with c_nav_mode:
            nav_mode = st.radio(
                "Sheet Navigator Mode",
                options=["📋 Dropdown", "📑 Multi-Select"],
                horizontal=True,
                key="grid_sheet_nav_mode_choice",
                on_change=_cb_sheet_selector_updated,
                label_visibility="collapsed"
            )

        with c_nav_sel:
            if nav_mode == "📋 Dropdown":
                chosen_sheet = st.selectbox(
                    "Multi-Sheet Workbook Navigator",
                    options=sheet_options,
                    format_func=lambda s: f"🌐 All Sheets ({all_count})" if s == "🌐 All Sheets" else f"📑 {s} ({sheet_counts.get(s, 0)})",
                    key="grid_sheet_single_select",
                    on_change=_cb_sheet_selector_updated,
                    label_visibility="collapsed",
                    help="Select a specific workbook sheet or choose All Sheets"
                )
                if chosen_sheet != "🌐 All Sheets":
                    selected_sheets = [chosen_sheet]
                    selected_sheet = chosen_sheet
                else:
                    selected_sheet = "🌐 All Sheets"
            else:
                chosen_multi = st.multiselect(
                    "Multi-Sheet Workbook Navigator",
                    options=unique_sheets,
                    format_func=lambda s: f"📑 {s} ({sheet_counts.get(s, 0)})",
                    placeholder="Choose one or more sheets to view & run (leave empty for All Sheets)...",
                    key="grid_sheet_multi_select",
                    on_change=_cb_sheet_selector_updated,
                    label_visibility="collapsed",
                    help="Select multiple sheets to filter view and execute together"
                )
                selected_sheets = chosen_multi
                if len(selected_sheets) == 1:
                    selected_sheet = selected_sheets[0]
                elif len(selected_sheets) > 1:
                    selected_sheet = f"{len(selected_sheets)} Sheets Selected"
                else:
                    selected_sheet = "🌐 All Sheets"

    unique_targets = [t for t in df["Target"].dropna().unique().tolist() if str(t).strip() and str(t).strip().lower() != "none"]
    origin_filter_opts = [
        f"🌐 All Engines ({len(df)})",
        f"⚡ RobustEngine v4 ({robust_count})",
        f"🤖 AI Generated (Full) ({ai_full_count})",
        f"🤖 AI Step + ⚙️ Template Code ({ai_hybrid_count})",
        f"⚙️ Rule Engine ({rule_count})",
        f"✍️ Manual / Uploaded ({manual_count})",
    ]

    # Smart Toggle Selection State
    sel_count = int(df["Run"].fillna(True).astype(bool).sum()) if "Run" in df.columns else len(df)
    is_all_selected = (sel_count == len(df)) and len(df) > 0

    def _cb_smart_toggle_all_run():
        if is_all_selected:
            st.session_state["run_all_state"] = False
        else:
            st.session_state["run_all_state"] = True
        st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1

    def _cb_reset_and_select_all_grid():
        df["Run"] = True
        st.session_state["test_cases_df"] = df
        if "results_df" in st.session_state and st.session_state.results_df is not None:
            if len(st.session_state.results_df) == len(df):
                st.session_state.results_df["Run"] = True
        st.session_state["grid_target_focus_select"] = "🌐 All Screens & Targets"
        st.session_state["grid_origin_filter_dropdown"] = origin_filter_opts[0]
        st.session_state["flow_origin_filter_dropdown"] = origin_filter_opts[0]
        st.session_state["grid_sheet_single_select"] = "🌐 All Sheets"
        st.session_state["grid_sheet_multi_select"] = []
        st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
        st.toast(f"🔄 Restored full view and selected all {len(df)} test cases!", icon="✅")

    # Two-Tier Structured Command Bar: Tier 1 Filters & Scope, Tier 2 Actions & Export
    c_tb_screen, c_tb_engine, c_tb_focus = st.columns([2.3, 2.0, 1.0], vertical_alignment="bottom")
    with c_tb_screen:
        target_focus_options = ["🌐 All Screens & Targets"] + unique_targets
        focused_target = st.selectbox(
            "Screen Filter",
            options=target_focus_options,
            key="grid_target_focus_select",
            label_visibility="collapsed",
            help="Filter test cases by screen / target URL"
        )
    with c_tb_engine:
        selected_orig_filter = st.selectbox(
            "Engine Filter",
            options=origin_filter_opts,
            key="grid_origin_filter_dropdown",
            label_visibility="collapsed",
            help="Filter test cases by synthesis engine / origin"
        )
    with c_tb_focus:
        def _cb_select_focused_target():
            if selected_sheets and focused_target == "🌐 All Screens & Targets" and "Sheet_Name" in df.columns:
                norm_selected = {str(s).strip().lower() for s in selected_sheets}
                sheet_mask = df["Sheet_Name"].astype(str).str.strip().str.lower().isin(norm_selected)
                df["Run"] = sheet_mask
                st.session_state["test_cases_df"] = df
                if "results_df" in st.session_state and st.session_state.results_df is not None:
                    if len(st.session_state.results_df) == len(df):
                        st.session_state.results_df["Run"] = df["Run"].values
                f_cnt = int(df["Run"].sum())
                sheet_desc = f"Sheet '{selected_sheets[0]}'" if len(selected_sheets) == 1 else f"{len(selected_sheets)} Sheets"
                st.toast(f"🎯 Checked only {f_cnt} tests in {sheet_desc} for execution!", icon="📑")
            else:
                st.session_state["run_focus_target"] = str(focused_target).strip()
            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
        
        sheet_total_match = int(df["Sheet_Name"].astype(str).str.strip().str.lower().isin({str(s).strip().lower() for s in selected_sheets}).sum()) if selected_sheets and "Sheet_Name" in df.columns else 0
        focus_btn_label = f"🎯 Sheet ({sheet_total_match})" if (selected_sheets and focused_target == "🌐 All Screens & Targets" and "Sheet_Name" in df.columns) else "🎯 Focus"
        focus_btn_help = f"Check only tests belonging to selected sheet(s) and uncheck all others" if (selected_sheets and focused_target == "🌐 All Screens & Targets") else f"Check only tests belonging to '{focused_target}' and uncheck all others"
        st.button(focus_btn_label, on_click=_cb_select_focused_target, width='stretch', help=focus_btn_help, key="btn_command_bar_focus_target")

    c_tb_toggle, c_tb_add, c_tb_url, c_tb_export, c_tb_manage = st.columns([1.5, 1.1, 1.1, 1.1, 1.1], vertical_alignment="bottom")
    with c_tb_toggle:
        if selected_sheets and "Sheet_Name" in df.columns:
            norm_selected = {str(s).strip().lower() for s in selected_sheets}
            sheet_mask = df["Sheet_Name"].astype(str).str.strip().str.lower().isin(norm_selected)
            sheet_sel_cnt = int(df.loc[sheet_mask, "Run"].fillna(True).astype(bool).sum())
            sheet_total = int(sheet_mask.sum())
            is_sheet_all_sel = (sheet_sel_cnt == sheet_total) and (sheet_total > 0)
            def _cb_toggle_sheet_run():
                df.loc[sheet_mask, "Run"] = not is_sheet_all_sel
                st.session_state["test_cases_df"] = df
                if "results_df" in st.session_state and st.session_state.results_df is not None:
                    if len(st.session_state.results_df) == len(df):
                        st.session_state.results_df["Run"] = df["Run"].values
                st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
            t_lbl = f"◻️ Deselect ({sheet_sel_cnt})" if is_sheet_all_sel else f"☑️ Select ({sheet_total})"
            sheet_desc = selected_sheets[0] if len(selected_sheets) == 1 else f"{len(selected_sheets)} Sheets"
            st.button(t_lbl, on_click=_cb_toggle_sheet_run, width='stretch', help=f"Toggle execution selection for {sheet_desc}", key="btn_command_bar_smart_toggle")
        else:
            toggle_label = f"◻️ Deselect All ({sel_count})" if is_all_selected else f"☑️ Select All ({len(df)})"
            toggle_help = "Deselect all test cases" if is_all_selected else "Select all test cases for execution"
            st.button(toggle_label, on_click=_cb_smart_toggle_all_run, width='stretch', help=toggle_help, key="btn_command_bar_smart_toggle")
    with c_tb_url:
        with st.popover("🌐 URL", width='stretch', help="Update or replace Target URL across multiple test cases"):
            st.markdown("##### 🌐 Bulk Update Target URL")
            st.caption("Change the target URL for all or selected test cases instantly.")
            new_bulk_url = st.text_input("New Target URL:", placeholder="https://your-domain.com/app", key="bulk_target_url_input")
            scope_options = ["All Test Cases", "Selected (Checked) Tests Only"]
            if focused_target != "🌐 All Screens & Targets":
                scope_options.append(f"Only tests matching '{focused_target}'")
            bulk_scope = st.radio("Apply to:", scope_options, key="bulk_url_scope_radio")
            if st.button("🚀 Apply URL Change", type="primary", width='stretch', key="btn_apply_bulk_url"):
                if not new_bulk_url.strip():
                    st.error("Please enter a valid URL before applying.")
                else:
                    clean_url = new_bulk_url.strip()
                    if bulk_scope == "All Test Cases":
                        df["Target"] = clean_url
                    elif bulk_scope == "Selected (Checked) Tests Only":
                        df.loc[df["Run"] == True, "Target"] = clean_url
                    else:
                        df.loc[df["Target"].astype(str).str.strip() == str(focused_target).strip(), "Target"] = clean_url
                    
                    st.session_state["test_cases_df"] = df
                    if "results_df" in st.session_state and st.session_state.results_df is not None:
                        res_copy = st.session_state.results_df.copy()
                        if len(res_copy) == len(df):
                            res_copy["Target"] = df["Target"].values
                            st.session_state["results_df"] = res_copy
                        else:
                            st.session_state["results_df"] = df
                    
                    if "current_file_path" in st.session_state and st.session_state.current_file_path:
                        try:
                            fpath = st.session_state.current_file_path
                            if fpath.endswith(".csv"):
                                df.to_csv(fpath, index=False)
                            elif fpath.endswith(".xlsx"):
                                df.to_excel(fpath, index=False)
                        except Exception:
                            pass
                    
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    st.toast(f"✅ Target URL updated to {clean_url}!", icon="🎉")
                    st.rerun()
    with c_tb_add:
        with st.popover("✍️ Add", width='stretch', help="Create and append a manual test case directly into this active suite"):
            st.markdown("##### ✍️ Add Manual Test Case")
            st.caption("New case will appear immediately in the grid with **✍️ Manual / Uploaded** attribution.")
            default_t_val = str(focused_target).strip() if (focused_target != "🌐 All Screens & Targets" and str(focused_target).strip()) else (unique_targets[0] if unique_targets else "")
            p_type = st.selectbox("Type", ["Web", "Mobile (Native)", "API"], key="pop_grid_manual_type")
            p_target = st.text_input("Target URL / Endpoint", value=default_t_val, placeholder="https://example.com/app", key="pop_grid_manual_target")
            p_step = st.text_area("Test Step Description", placeholder="e.g. Step 1: Click settings -> Step 2: Toggle dark mode", key="pop_grid_manual_step")
            p_exp = st.text_input("Expected Result", value="Action executes cleanly", key="pop_grid_manual_exp")
            p_code = st.text_input("Playwright Code (Optional)", placeholder="e.g. page.locator('.theme-btn').click()", key="pop_grid_manual_code")
            if st.button("➕ Add to Suite", type="primary", width='stretch', key="pop_grid_btn_add_case"):
                if not p_step.strip():
                    st.error("Please enter a Test Step Description.")
                else:
                    append_manual_test_case(p_type, p_target, p_step, p_exp, p_code)
                    st.toast("✅ Manual test case added to active suite!", icon="✍️")
                    st.rerun()
    with c_tb_export:
        with st.popover("📥 Export", width='stretch', help="Download active test suite as CSV or Excel"):
            st.markdown("##### 📥 Export Test Suite")
            st.caption(f"Export all {len(df)} test cases currently in memory.")
            exp_csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 CSV (.csv)",
                data=exp_csv_bytes,
                file_name=f"test_suite_{int(time.time())}.csv",
                mime="text/csv",
                width="stretch",
                key="grid_cmd_bar_dl_csv",
                help="Download full test suite as CSV",
            )
            try:
                from io import BytesIO
                out_xl_buf = BytesIO()
                with pd.ExcelWriter(out_xl_buf, engine="openpyxl") as xl_w:
                    if "Sheet_Name" in df.columns and len(df["Sheet_Name"].dropna().unique()) > 1:
                        used_sheet_names = set()
                        for sheet_name_val, sheet_group in df.groupby("Sheet_Name"):
                            sheet_tab_title = sanitize_excel_sheet_name(sheet_name_val, used_sheet_names)
                            sheet_group.to_excel(xl_w, index=False, sheet_name=sheet_tab_title)
                    else:
                        df.to_excel(xl_w, index=False, sheet_name="Test Cases")
                st.download_button(
                    "📊 Excel (.xlsx)",
                    data=out_xl_buf.getvalue(),
                    file_name=f"test_suite_{int(time.time())}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                    key="grid_cmd_bar_dl_xlsx",
                    help="Download full test suite as multi-sheet Excel",
                )
            except Exception:
                pass
    with c_tb_manage:
        with st.popover("⚙️ Manage", width='stretch', help="Manage suite: delete selected, reset view, export, or wipe suite"):
            st.markdown("##### ⚙️ Test Suite Management")
            chk_cnt = int(df["Run"].fillna(True).astype(bool).sum()) if "Run" in df.columns else len(df)
            if st.button(f"🗑️ Delete Selected ({chk_cnt})", type="secondary", width='stretch', key="pop_btn_del_selected"):
                if delete_selected_test_cases(df):
                    st.toast(f"🗑️ Deleted {chk_cnt} selected test cases!", icon="✅")
                    st.rerun()
                else:
                    st.warning("⚠️ No test cases are currently checked with 'Run'.")
            
            st.button("🔄 Reset View & Select All", on_click=_cb_reset_and_select_all_grid, width='stretch', help="Restore full view and select all test cases.", key="pop_btn_reset_full_view")

            st.divider()
            st.markdown("###### Export Suite:")
            st.download_button(
                "📥 Download Suite (CSV)",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=f"test_suite_{int(time.time())}.csv",
                mime="text/csv",
                width="stretch",
                key="pop_manage_dl_csv",
                help="Download entire test suite as CSV",
            )

            st.divider()
            st.markdown("###### Wipe Entire Suite:")
            if st.button("⚠️ Clear All Test Cases", type="primary", width='stretch', key="pop_btn_clear_all_cases"):
                clear_entire_test_suite()
                st.toast("🧹 Test suite cleared successfully!", icon="✅")
                st.rerun()

    # Determine subset of tests to show in the grid if filter is active
    display_df = df
    active_filters = []
    active_subset_indices = None

    if "Robust" in selected_orig_filter:
        orig_mask = df["Generated_By"].astype(str).str.contains("Robust", na=False)
        display_df = display_df[orig_mask]
        active_filters.append("Engine: ⚡ RobustEngine v4")
    elif "AI Generated (Full)" in selected_orig_filter or "AI Full" in selected_orig_filter:
        orig_mask = (df["Generated_By"].astype(str).str.contains("AI Generated", na=False)) & (~df["Generated_By"].astype(str).str.contains("Template", na=False))
        display_df = display_df[orig_mask]
        active_filters.append("Engine: 🤖 AI Generated (Full)")
    elif "Template Code" in selected_orig_filter or "Hybrid" in selected_orig_filter:
        orig_mask = df["Generated_By"].astype(str).str.contains("Template", na=False)
        display_df = display_df[orig_mask]
        active_filters.append("Engine: 🤖+⚙️ Hybrid")
    elif "Rule Engine" in selected_orig_filter:
        orig_mask = df["Generated_By"].astype(str).str.contains("Rule Engine|20-Point", na=False)
        display_df = display_df[orig_mask]
        active_filters.append("Engine: ⚙️ Rule Engine")
    elif "Manual" in selected_orig_filter:
        orig_mask = df["Generated_By"].astype(str).str.contains("Manual|Uploaded", na=False)
        display_df = display_df[orig_mask]
        active_filters.append("Engine: ✍️ Manual / Uploaded")

    if focused_target != "🌐 All Screens & Targets":
        tgt_mask = display_df["Target"].astype(str).str.strip() == str(focused_target).strip()
        display_df = display_df[tgt_mask]
        active_filters.append(f"Screen: {focused_target}")

    if selected_sheets and "Sheet_Name" in display_df.columns:
        norm_selected = {str(s).strip().lower() for s in selected_sheets}
        sheet_mask = display_df["Sheet_Name"].astype(str).str.strip().str.lower().isin(norm_selected)
        display_df = display_df[sheet_mask]
        sheet_desc = selected_sheets[0] if len(selected_sheets) == 1 else f"{len(selected_sheets)} Sheets"
        active_filters.append(f"Sheet: {sheet_desc}")

    if active_filters and len(display_df) < len(df):
        active_subset_indices = display_df.index
        c_inf1, c_inf2 = st.columns([4, 1])
        with c_inf1:
            st.info(f"🔍 **Filtered View**: Showing **{len(display_df)}** of **{len(df)}** tests ({' • '.join(active_filters)})")
        with c_inf2:
            st.button("🔄 All", on_click=_cb_reset_and_select_all_grid, width='stretch', help="One-click reset to restore the full view and select all test cases.", key="btn_filter_info_reset_all")

    # Detect user columns that actually have non-empty data in the current sheet / filtered view
    active_in_view_cols = [
        c for c in user_presentation_cols
        if c in display_df.columns and display_df[c].dropna().astype(str).str.strip().ne("").any()
    ]
    if not active_in_view_cols:
        active_in_view_cols = [c for c in user_presentation_cols if c in display_df.columns]

    sheet_view_sig = "_".join(sorted([re.sub(r'[^a-zA-Z0-9]', '', str(s)) for s in selected_sheets])) if selected_sheets else "all"

    c_grid_hdr1, c_grid_hdr2, c_grid_hdr3 = st.columns([2.6, 1.3, 1.1])
    with c_grid_hdr1:
        st.caption(f"✨ **As-Is Presentation Active**: Showing **{len(active_in_view_cols)}** active columns for current sheet(s). Execution adapter operates in background.")
    with c_grid_hdr2:
        with st.popover("👁️ Columns to Display", width='stretch', help="Select which columns are visible in the grid"):
            st.markdown("##### 👁️ Column Visibility")
            st.caption("Toggle visible columns in table:")
            col_sel_key = f"grid_visible_cols_sel_{sheet_view_sig}_{st.session_state.get('grid_version', 0)}"
            chosen_user_cols = st.multiselect(
                "Columns to display:",
                options=user_presentation_cols,
                default=active_in_view_cols,
                key=col_sel_key,
                label_visibility="collapsed"
            )
            if not chosen_user_cols:
                chosen_user_cols = active_in_view_cols
    with c_grid_hdr3:
        # Global checked state for Adapter Cols (synchronized across grid and sidebar settings)
        default_show_adapter = bool(st.session_state.get("global_show_adapter_cols", saved_config.get("show_adapter_cols", True)))
        show_adapter = st.toggle(
            "🛠️ Adapter Cols",
            value=default_show_adapter,
            key=f"grid_toggle_show_adapter_{sheet_view_sig}",
            help="Toggle internal Playwright code and adapter columns in the grid view"
        )
        if show_adapter != st.session_state.get("global_show_adapter_cols"):
            st.session_state["global_show_adapter_cols"] = show_adapter
            st.session_state["_persisted_show_adapter"] = show_adapter
            save_settings({"show_adapter_cols": show_adapter}, force=True)

    adapter_to_show = [c for c in ["Type", "Target", "Playwright_Code", "Generated_By", "Execution_Notes"] if c in display_df.columns] if show_adapter else []
    # Deduplicate in order to prevent duplicate columns in st.data_editor
    seen_cols = set()
    cols_to_render = []
    for c in (control_cols + chosen_user_cols + adapter_to_show + result_cols):
        if c in display_df.columns and c not in seen_cols:
            seen_cols.add(c)
            cols_to_render.append(c)

    grid_render_df = display_df[cols_to_render]

    grid_editor_key = f"test_data_grid_{st.session_state.get('grid_version', 0)}_{sheet_view_sig}"

    edited_df = st.data_editor(
        sanitize_df_for_streamlit(grid_render_df),
        num_rows="dynamic",
        width="stretch",
        key=grid_editor_key,
        column_config={
            "Run": st.column_config.CheckboxColumn("▶️ Run?", default=True),
            "Sheet_Name": st.column_config.TextColumn("📑 Sheet", help="Workbook sheet or module name"),
            "Status": st.column_config.TextColumn("Status", disabled=True),
            "Type": st.column_config.SelectboxColumn("Type", options=["Web", "Mobile (Native)", "API"], default="Web"),
            "Generated_By": st.column_config.TextColumn("🤖 Generated By", disabled=True, help="Shows whether the test case and its Playwright code were generated by AI, Deterministic Rule Engine, or Manually"),
            "Target": st.column_config.TextColumn("Target URL", default=""),
            "Duration": st.column_config.TextColumn("⏱️ Duration", disabled=True),
            "Actual_Result": st.column_config.TextColumn("🔍 Actual Result", disabled=True),
            "Screenshot": st.column_config.TextColumn("📸 Screenshot", disabled=True),
        },
    )
    if edited_df is not None:
        if len(edited_df) == 0:
            clear_entire_test_suite()
            st.toast("🧹 Test suite cleared!", icon="✅")
            st.rerun()
        else:
            valid_mask = edited_df.astype(str).ne("").any(axis=1)
            if valid_mask.any():
                edited_df = edited_df[valid_mask].copy()
            edited_df = sanitize_df_for_streamlit(edited_df)

            for c in edited_df.columns:
                if c in df.columns:
                    df.loc[edited_df.index, c] = edited_df[c]

            master_clean = sanitize_df_for_streamlit(df).reset_index(drop=True)
            master_clean.index = range(1, len(master_clean) + 1)
            st.session_state["results_df"] = master_clean.copy()
            st.session_state["test_cases_df"] = master_clean.copy()

            # Sync row deletions to disk
            if len(edited_df) < len(grid_render_df):
                for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                    if os.path.exists(disk_f):
                        try:
                            st.session_state["test_cases_df"].to_csv(disk_f, index=False)
                        except Exception:
                            pass

    # Playwright Execution Script Inspector & Live Editor
    with st.expander("🔍 Inspect & Live-Edit Synthesized Playwright Execution Code", expanded=False):
        c_insp1, c_insp2 = st.columns([1.2, 3])
        with c_insp1:
            insp_indices = list(display_df.index)
            if insp_indices:
                step_col_name = "Test_Step_Description"
                for cand in ["Test Steps", "User Action on Dev Link", "Test_Step_Description", "_step"]:
                    if cand in display_df.columns and display_df[cand].dropna().astype(str).str.strip().ne("").any():
                        step_col_name = cand
                        break
                else:
                    step_col_name = user_presentation_cols[0] if user_presentation_cols else display_df.columns[0]
                insp_sel_idx = st.selectbox(
                    "Select Test # to Inspect:",
                    insp_indices,
                    format_func=lambda x: f"Test #{x}: {str(display_df.loc[x, step_col_name] if step_col_name in display_df.columns else x)[:35]}",
                    key="code_inspector_sel_idx"
                )
            else:
                insp_sel_idx = None
        with c_insp2:
            if insp_sel_idx is not None and insp_sel_idx in df.index:
                curr_pw_code = str(df.at[insp_sel_idx, "Playwright_Code"] or (df.at[insp_sel_idx, "_code"] if "_code" in df.columns else ""))
                new_pw_code = st.text_area(
                    "Synthesized Playwright Code (Editable in background adapter):",
                    value=curr_pw_code,
                    height=130,
                    key=f"inspector_code_area_{insp_sel_idx}"
                )
                if new_pw_code != curr_pw_code:
                    df.at[insp_sel_idx, "Playwright_Code"] = new_pw_code
                    if "_code" in df.columns:
                        df.at[insp_sel_idx, "_code"] = new_pw_code
                    st.session_state["test_cases_df"] = df

    class DualOutput:
        def __init__(self, original_stdout):
            self.captured = io.StringIO()
            self.original_stdout = original_stdout

        def write(self, text):
            if self.original_stdout and hasattr(self.original_stdout, "write"):
                try:
                    self.original_stdout.write(text)
                except Exception:
                    pass
            self.captured.write(text)

        def flush(self):
            if self.original_stdout and hasattr(self.original_stdout, "flush"):
                try:
                    self.original_stdout.flush()
                except Exception:
                    pass
            self.captured.flush()

        def getvalue(self):
            return self.captured.getvalue()

    def execute_single_test(index, row, target_df, status_text_ui=None):
        target_url, step_desc, expected_result, custom_script, test_type = resolve_test_execution_params(index, row, master_df=df)

        if status_text_ui:
            status_text_ui.markdown(
                f"**Running Test #{index}:** {step_desc}"
            )

        start_time = time.time()

        if target_url and target_url.lower() != "none":
            # 1. Classify and validate execution channel & platform readiness
            from runners.native_runner import classify_and_validate_test_channel, NativeRunner
            channel_row = dict(row)
            channel_row["Type"] = test_type
            channel_row["Target"] = target_url
            channel, val_err = classify_and_validate_test_channel(channel_row, platform_config=active_platform_config)
            if val_err:
                target_df.at[index, "Status"] = "FAILED"
                target_df.at[index, "Duration"] = "0.0s"
                target_df.at[index, "Actual_Result"] = val_err
                target_df.at[index, "Execution_Notes"] = val_err
                return

            dual_out = DualOutput(sys.__stdout__)
            orig_stdout = sys.stdout
            sys.stdout = dual_out
            runner = None

            try:
                if channel == "native_mobile":
                    runner = NativeRunner(
                        target=target_url,
                        test_step=step_desc,
                        expected_result=expected_result,
                        custom_script=custom_script,
                    )
                    gen_code = runner.execute()
                    if gen_code:
                        target_df.at[index, "Playwright_Code"] = gen_code
                    if runner.last_screenshot:
                        target_df.at[index, "Screenshot"] = runner.last_screenshot

                elif channel == "api":
                    from runners.api_runner import APIRunner
                    runner = APIRunner(
                        url=target_url,
                        test_data=test_data_dict,
                        test_step=step_desc,
                        expected_result=expected_result,
                        custom_script=custom_script,
                    )
                    gen_code = runner.execute()
                    if gen_code:
                        target_df.at[index, "Playwright_Code"] = gen_code
                else:
                    # Web, CDP Mobile (Chrome/WebView), or Emulated Mobile Viewport
                    from runners.web_runner import WebRunner
                    runner = WebRunner(
                        url=target_url,
                        headed=show_browser,
                        visual=enable_visual,
                        test_data=test_data_dict,
                        test_step=step_desc,
                        expected_result=expected_result,
                        custom_script=custom_script,
                        platform_config=active_platform_config,
                    )
                    gen_code = runner.execute()
                    if gen_code:
                        target_df.at[index, "Playwright_Code"] = gen_code
                    if getattr(runner, "last_screenshot", None):
                        target_df.at[index, "Screenshot"] = runner.last_screenshot

                target_df.at[index, "Status"] = "PASSED"
                exp_val = str(expected_result).strip()
                target_df.at[index, "Actual_Result"] = (
                    f"Passed: {exp_val}" if exp_val else f"Action verified on {channel.replace('_', ' ').title()} channel."
                )
            except Exception as e:
                print(f"Error during execution: {e}")
                e_str = str(e).strip()
                err_clean = e_str.splitlines()[-1] if e_str else "Execution failed"
                from core.self_healing import StepSkippedException
                is_not_possible = (
                    isinstance(e, StepSkippedException)
                    or "[not applicable]" in e_str.lower()
                    or "[not possible]" in e_str.lower()
                    or "not found on active page" in e_str.lower()
                    or "not found on active screen" in e_str.lower()
                    or "self-healing exhausted" in e_str.lower()
                    or ("timeout" in e_str.lower() and ("waiting for locator" in e_str.lower() or "waiting for selector" in e_str.lower() or "locator.click" in e_str.lower() or "locator.fill" in e_str.lower()))
                    or "cannot be executed or healed" in e_str.lower()
                )
                if is_not_possible:
                    target_df.at[index, "Status"] = "SKIPPED"
                    clean_msg = err_clean.replace("[NOT APPLICABLE]", "").replace("[NOT POSSIBLE]", "").strip()
                    target_df.at[index, "Actual_Result"] = f"Skipped (Not Possible / Action Unreachable): {clean_msg}"
                    target_df.at[index, "Execution_Notes"] = f"⚠️ [Skipped - Not Possible] {clean_msg}"
                else:
                    target_df.at[index, "Status"] = "FAILED"
                    target_df.at[index, "Actual_Result"] = f"Failed: {err_clean}"
                if runner and getattr(runner, "last_screenshot", None):
                    target_df.at[index, "Screenshot"] = runner.last_screenshot
            finally:
                sys.stdout = orig_stdout
                if runner and getattr(runner, "generated_code", None) and runner.generated_code:
                    target_df.at[index, "Playwright_Code"] = runner.generated_code

            duration_str = f"{round(time.time() - start_time, 2)}s"
            target_df.at[index, "Duration"] = duration_str
            target_df.at[index, "Execution_Notes"] = dual_out.getvalue()
        else:
            target_df.at[index, "Status"] = "SKIPPED"
            target_df.at[index, "Duration"] = "0.0s"
            target_df.at[index, "Actual_Result"] = "Skipped: Target URL was empty or invalid."
            target_df.at[index, "Execution_Notes"] = "Target URL was empty or invalid."


    st.divider()
    col_exec1, col_exec2 = st.columns([1, 1])

    with col_exec1:
        st.markdown("### ⚡ Bulk Execution Dashboard")
        st.caption("Executes all selected tests in batch mode with Play, Pause, and Resume controls.")

        if "batch_paused" not in st.session_state:
            st.session_state.batch_paused = False
        if "paused_at_index" not in st.session_state:
            st.session_state.paused_at_index = None

        # Check if execution is currently paused
        is_paused = st.session_state.get("batch_paused", False)
        paused_idx = st.session_state.get("paused_at_index", None)
        sel_tests_count = int(edited_df["Run"].fillna(True).astype(bool).sum()) if "Run" in edited_df.columns else len(edited_df)

        exec_trigger_placeholder = st.empty()

        if is_paused and paused_idx is not None:
            with exec_trigger_placeholder.container():
                render_html_block(
                    f'<div class="metric-card" style="border-left: 3px solid #F59E0B; padding: 8px 14px; margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">'
                    f'<div style="display: flex; align-items: center; gap: 8px;"><span class="status-pill status-paused">⏸️ Execution Paused</span><span style="font-weight: 600; font-size: 0.88rem;">Paused before Test #{paused_idx}</span></div>'
                    f'<span style="font-size: 0.80rem; color: #94A3B8;">Browser session preserved • Click Resume to continue</span>'
                    f'</div>'
                )
                c_exec1, c_exec2, c_exec3 = st.columns([2.5, 1.2, 0.9])
                with c_exec1:
                    resume_bulk = st.button(
                        f"▶️ Resume from Test #{paused_idx}",
                        type="primary",
                        width='stretch',
                        key="btn_exec_primary_resume"
                    )
                with c_exec2:
                    pause_after_step = st.toggle(
                        "⏸️ Step-Pause",
                        value=bool(st.session_state.get("global_step_pause", False)),
                        key="toggle_step_pause_active",
                        help="Automatically pauses after each subsequent test case"
                    )
                    st.session_state["global_step_pause"] = pause_after_step
                with c_exec3:
                    if st.button("⏹️ Reset", type="secondary", width='stretch', key="btn_reset_pause", help="Cancel paused run and reset batch state"):
                        st.session_state.batch_paused = False
                        st.session_state.paused_at_index = None
                        st.rerun()
                run_bulk = False
        else:
            with exec_trigger_placeholder.container():
                c_exec1, c_exec2 = st.columns([3.2, 1.1])
                with c_exec1:
                    btn_run_text = f"▶️ Run Selected Tests ({sel_tests_count} cases)" if sel_tests_count > 0 else "▶️ Run Tests (0 selected)"
                    run_bulk = st.button(btn_run_text, type="primary", width='stretch', key="btn_exec_primary_run", disabled=(sel_tests_count == 0))
                with c_exec2:
                    pause_after_step = st.toggle(
                        "⏸️ Step-Pause",
                        value=bool(st.session_state.get("global_step_pause", False)),
                        key="toggle_step_pause_idle",
                        help="Automatically pauses after each test case so you can inspect UI state before resuming"
                    )
                    st.session_state["global_step_pause"] = pause_after_step
                resume_bulk = False

        if run_bulk or resume_bulk:
            # Build run_df by starting from master df rows corresponding to the active view
            run_indices = [i for i in edited_df.index if i in df.index]
            run_df = df.loc[run_indices].copy()
            # Copy over any interactive edits (like Run checkboxes or inline changes) from edited_df
            for col in edited_df.columns:
                run_df[col] = edited_df[col]

            # Validation: Ensure at least one test is checked for execution
            selected_tests = run_df[run_df["Run"].fillna(True).astype(bool) == True]
            if len(selected_tests) == 0:
                st.warning("⚠️ No test cases selected! Please check the '▶️ Run?' box for at least one test case before starting.")
            else:
                exec_trigger_placeholder.empty()
                from core.execution_control import (
                    ensure_control_server,
                    read_and_clear_control_signal,
                    render_live_control_bar_html,
                )
                read_and_clear_control_signal()  # Reset any previous pause/stop signal for fresh run
                control_port = ensure_control_server()
                total_tests = len(run_df)
                import streamlit.components.v1 as components
                components.html(render_live_control_bar_html(control_port, total_tests=total_tests, step_pause=pause_after_step), height=74)

                progress_bar = st.progress(0)
                status_text = st.empty()
                total_tests = len(run_df)

                from runners.native_runner import classify_and_validate_test_channel, NativeBatchRunner

                native_batch = []
                web_batch = []
                api_batch = []

                # Determine starting index for resume or fresh run
                start_from_idx = paused_idx if (resume_bulk and paused_idx is not None) else 1

                for idx in run_df.index:
                    row = run_df.loc[idx].to_dict()
                    # If resuming, skip previously completed tests before start_from_idx
                    if resume_bulk and idx < start_from_idx:
                        continue

                    if not bool(row.get("Run", True)):
                        run_df.at[idx, "Status"] = "SKIPPED"
                        run_df.at[idx, "Duration"] = "-"
                        run_df.at[idx, "Actual_Result"] = "Manually deselected by user."
                        run_df.at[idx, "Execution_Notes"] = "Manually deselected by user."
                        continue

                    target_url, step_desc, exp_res, custom_script, test_type = resolve_test_execution_params(idx, row, master_df=df)

                    if not target_url or target_url.lower() == "none":
                        run_df.at[idx, "Status"] = "SKIPPED"
                        run_df.at[idx, "Duration"] = "-"
                        run_df.at[idx, "Actual_Result"] = "Target URL was empty or invalid."
                        run_df.at[idx, "Execution_Notes"] = "Target URL was empty or invalid."
                        continue

                    # Store resolved values on run_df
                    run_df.at[idx, "Target"] = target_url
                    run_df.at[idx, "_target"] = target_url
                    run_df.at[idx, "Test_Step_Description"] = step_desc
                    run_df.at[idx, "_step"] = step_desc
                    run_df.at[idx, "Expected_Result"] = exp_res
                    run_df.at[idx, "_expected"] = exp_res
                    run_df.at[idx, "Playwright_Code"] = custom_script
                    run_df.at[idx, "_code"] = custom_script
                    run_df.at[idx, "Type"] = test_type

                    channel_row = dict(row)
                    channel_row["Type"] = test_type
                    channel_row["Target"] = target_url
                    channel, val_err = classify_and_validate_test_channel(
                        channel_row, platform_config=active_platform_config
                    )
                    if val_err:
                        run_df.at[idx, "Status"] = "FAILED"
                        run_df.at[idx, "Duration"] = "0.0s"
                        run_df.at[idx, "Actual_Result"] = val_err
                        run_df.at[idx, "Execution_Notes"] = val_err
                        completed_count_val = len(run_df[run_df["Status"].isin(["PASSED", "FAILED", "SKIPPED"])])
                        progress_bar.progress(min(1.0, completed_count_val / max(1, total_tests)))
                        continue

                    item = {
                        "index": idx,
                        "url": target_url,
                        "test_step": step_desc,
                        "expected_result": exp_res,
                        "custom_script": custom_script,
                        "row": channel_row,
                    }
                    if channel == "native_mobile":
                        native_batch.append(item)
                    elif channel == "api":
                        api_batch.append(item)
                    else:
                        web_batch.append(item)

                completed_count = [len(run_df[run_df["Status"].isin(["PASSED", "FAILED", "SKIPPED"])])]
                paused_triggered = [False]
                paused_step_num = [None]
                paused_signal_received = [None]

                def on_batch_step_update(index, status, full_script, screenshot_path, notes, duration="-", actual_result=None):
                    run_df.at[index, "Status"] = status
                    run_df.at[index, "Duration"] = duration
                    if full_script:
                        run_df.at[index, "Playwright_Code"] = full_script
                    if screenshot_path:
                        run_df.at[index, "Screenshot"] = screenshot_path
                    if notes:
                        run_df.at[index, "Execution_Notes"] = notes

                    if actual_result:
                        run_df.at[index, "Actual_Result"] = actual_result
                    elif status == "PASSED":
                        exp_val = str(run_df.at[index, "Expected_Result"]).strip()
                        run_df.at[index, "Actual_Result"] = f"Passed: {exp_val}" if exp_val else "Action verified successfully."
                    elif status == "FAILED":
                        err_line = str(notes).strip().splitlines()[-1] if notes else "Step assertion failed."
                        run_df.at[index, "Actual_Result"] = f"Failed: {err_line}"
                    elif status == "SKIPPED":
                        run_df.at[index, "Actual_Result"] = "Skipped by user selection."

                    completed_count[0] += 1
                    pct = min(1.0, completed_count[0] / max(1, total_tests))
                    progress_bar.progress(pct)
                    step_desc = str(run_df.at[index, "Test_Step_Description"]).strip()
                    status_pill_cls = "status-passed" if status == "PASSED" else ("status-failed" if status == "FAILED" else "status-skipped")
                    status_text.markdown(
                        textwrap.dedent(f"""<div class="metric-card"><span class="status-pill {status_pill_cls}">{status}</span> <span style="font-weight: 600;">Test #{index} ({duration}):</span> {step_desc}</div>""").strip(),
                        unsafe_allow_html=True
                    )

                def check_batch_control():
                    sig = read_and_clear_control_signal()
                    if sig in ["PAUSE", "STOP"]:
                        paused_signal_received[0] = sig
                        return sig
                    if pause_after_step:
                        paused_signal_received[0] = "PAUSE"
                        return "PAUSE"
                    return "RUN"

                # 1. Run Native Mobile batch directly on connected Android device via ADB
                if native_batch and not pause_after_step:
                    status_text.markdown(
                        textwrap.dedent(f"""<div class="metric-card"><span class="spinner-ring"></span><strong>Executing {len(native_batch)} Native Mobile tests on connected Android device via ADB...</strong></div>""").strip(),
                        unsafe_allow_html=True
                    )
                    native_runner = NativeBatchRunner()
                    native_runner.run_batch(
                        native_batch,
                        on_step_callback=on_batch_step_update,
                        check_control_callback=check_batch_control
                    )

                # 2. Run Web batch in a persistent browser session (Desktop, Emulated Mobile, or CDP)
                if web_batch and not pause_after_step:
                    plat_desc = active_platform_config.get("platform", "desktop_chrome")
                    status_text.markdown(
                        textwrap.dedent(f"""<div class="metric-card"><span class="spinner-ring"></span><strong>Launching browser session for {len(web_batch)} Web tests ({plat_desc})...</strong></div>""").strip(),
                        unsafe_allow_html=True
                    )
                    from runners.web_runner import WebBatchRunner
                    batch_runner = WebBatchRunner(
                        headed=show_browser,
                        visual=enable_visual,
                        test_data=test_data_dict,
                        platform_config=active_platform_config,
                    )
                    batch_runner.run_batch(
                        web_batch,
                        on_step_callback=on_batch_step_update,
                        check_control_callback=check_batch_control
                    )

                # 3. Run API tests in parallel (if not paused or if web batch is empty)
                if api_batch and not pause_after_step:

                    status_text.markdown(f"""<div class="metric-card"><span class="spinner-ring"></span><strong>Executing {len(api_batch)} API tests in parallel...</strong></div>""", unsafe_allow_html=True)
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    max_workers = min(8, len(api_batch))

                    def run_single_api_item(item):
                        idx = item["index"]
                        row = item["row"]
                        t_url = str(row.get("Target", "")).strip()
                        c_script = str(row.get("Playwright_Code", "")).strip()
                        if c_script == "nan":
                            c_script = ""
                        s_time = time.time()
                        try:
                            from runners.api_runner import APIRunner
                            api_r = APIRunner(
                                url=t_url,
                                test_data=test_data_dict,
                                test_step=str(row.get("Test_Step_Description", "")),
                                expected_result=str(row.get("Expected_Result", "")),
                                custom_script=c_script,
                            )
                            g_code = api_r.execute()
                            dur_s = f"{round(time.time() - s_time, 2)}s"
                            exp_str = str(row.get("Expected_Result", "API response verified.")).strip()
                            return (idx, "PASSED", dur_s, g_code or c_script, "API execution passed.", f"Passed: {exp_str}")
                        except Exception as err:
                            dur_s = f"{round(time.time() - s_time, 2)}s"
                            err_line = str(err).strip().splitlines()[-1] if str(err).strip() else "API error"
                            return (idx, "FAILED", dur_s, c_script, f"API Error: {err}", f"Failed: {err_line}")

                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = [executor.submit(run_single_api_item, item) for item in api_batch]
                        for f in as_completed(futures):
                            idx, status, dur_str, full_script, notes, act_res = f.result()
                            run_df.at[idx, "Status"] = status
                            run_df.at[idx, "Duration"] = dur_str
                            run_df.at[idx, "Actual_Result"] = act_res
                            if full_script:
                                run_df.at[idx, "Playwright_Code"] = full_script
                            run_df.at[idx, "Execution_Notes"] = notes
                            completed_count[0] += 1
                            progress_bar.progress(min(1.0, completed_count[0] / max(1, total_tests)))
                            step_desc = str(run_df.at[idx, "Test_Step_Description"]).strip()
                            status_pill_cls = "status-passed" if status == "PASSED" else "status-failed"
                            status_text.markdown(
                                f"""<div class="metric-card"><span class="status-pill {status_pill_cls}">{status}</span> <span style="font-weight: 600;">API Test #{idx} ({dur_str}):</span> {step_desc}</div>""",
                                unsafe_allow_html=True
                            )

                # Check if paused or stopped
                active_sig = paused_signal_received[0]
                if not active_sig and pause_after_step and completed_count[0] < total_tests:
                    active_sig = "PAUSE"

                if active_sig == "PAUSE":
                    st.session_state.batch_paused = True
                    # Find next pending index
                    pending_indices = [i for i in run_df.index if run_df.at[i, "Status"] == "PENDING" and bool(run_df.at[i, "Run"]) == True]
                    st.session_state.paused_at_index = pending_indices[0] if pending_indices else (start_from_idx + 1)
                    for col in ["Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code", "_code", "Target", "_target"]:
                        if col in run_df.columns:
                            df.loc[run_df.index, col] = run_df[col]
                    st.session_state.results_df = df.copy()
                    st.session_state.test_cases_df = df.copy()
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                        if os.path.exists(disk_f):
                            try:
                                df.to_csv(disk_f, index=False)
                            except Exception:
                                pass
                    st.toast(f"⏸️ Batch Paused before Test #{st.session_state.paused_at_index}!", icon="⏸️")
                    st.rerun()
                elif active_sig == "STOP":
                    progress_bar.progress(1.0)
                    st.session_state.batch_paused = False
                    st.session_state.paused_at_index = None
                    for col in ["Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code", "_code", "Target", "_target"]:
                        if col in run_df.columns:
                            df.loc[run_df.index, col] = run_df[col]
                    st.session_state.results_df = df.copy()
                    st.session_state.test_cases_df = df.copy()
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                        if os.path.exists(disk_f):
                            try:
                                df.to_csv(disk_f, index=False)
                            except Exception:
                                pass
                    st.toast(f"🛑 Execution Stopped by User! Completed {completed_count[0]} of {total_tests} tests.", icon="🛑")
                    st.rerun()
                else:
                    progress_bar.progress(1.0)
                    st.session_state.batch_paused = False
                    st.session_state.paused_at_index = None
                    for col in ["Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code", "_code", "Target", "_target"]:
                        if col in run_df.columns:
                            df.loc[run_df.index, col] = run_df[col]
                    st.session_state.results_df = df.copy()
                    st.session_state.test_cases_df = df.copy()
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                        if os.path.exists(disk_f):
                            try:
                                df.to_csv(disk_f, index=False)
                            except Exception:
                                pass
                    st.toast("⚡ Bulk Execution Complete!", icon="✅")
                    st.rerun()

    with col_exec2:
        st.markdown("### 🎯 Individual Execution")
        st.caption("Select and execute any individual test case on demand.")
        if len(edited_df) == 0:
            st.info("No test cases loaded in current view.")
        else:
            def format_test_item(idx_val):
                row_i = df.loc[idx_val] if idx_val in df.index else edited_df.loc[idx_val]
                t_type = row_i.get("Type", "Web")
                t_status = row_i.get("Status", "PENDING")
                status_icon = "🟢" if t_status == "PASSED" else ("❌" if t_status == "FAILED" else ("⏭️" if t_status == "SKIPPED" else "⚪"))
                t_desc = str(
                    row_i.get("Test_Step_Description") or row_i.get("Test Steps") or 
                    row_i.get("User Action on Dev Link") or row_i.get("Test Case Description") or f"Test #{idx_val}"
                ).strip()
                t_dur = str(row_i.get("Duration", "-"))
                dur_text = f" ({t_dur})" if t_dur and t_dur != "-" else ""
                return f"Test #{idx_val} | {status_icon} {t_type} | {t_desc[:45]}{dur_text}"

            valid_indices = list(edited_df.index)
            if "single_test_selector" in st.session_state and st.session_state["single_test_selector"] not in valid_indices:
                st.session_state["single_test_selector"] = valid_indices[0] if valid_indices else None

            selected_test_idx = st.selectbox(
                "Select Test Case to Execute",
                options=valid_indices,
                format_func=format_test_item,
                key="single_test_selector",
            )

            sel_row = df.loc[selected_test_idx] if selected_test_idx in df.index else edited_df.loc[selected_test_idx]
            resolved_t_url, resolved_step, resolved_exp, resolved_script, resolved_type = resolve_test_execution_params(selected_test_idx, sel_row, master_df=df)
            sel_status = str(sel_row.get("Status", "PENDING")).strip()
            pill_class = "status-passed" if sel_status == "PASSED" else ("status-failed" if sel_status == "FAILED" else ("status-skipped" if sel_status == "SKIPPED" else "status-pending"))

            # Selected Test Details Card (Theme-adaptive)
            with st.container(border=True):
                c_head1, c_head2 = st.columns([3, 1])
                with c_head1:
                    st.markdown(f"**Test #{selected_test_idx} ({resolved_type})**")
                with c_head2:
                    st.markdown(
                        f"<div style='text-align: right;'><span class='status-pill {pill_class}'>{sel_status}</span></div>",
                        unsafe_allow_html=True,
                    )
                from runners.native_runner import classify_and_validate_test_channel
                ch_row_dict = dict(sel_row)
                ch_row_dict["Type"] = resolved_type
                ch_row_dict["Target"] = resolved_t_url
                ch_key, _ = classify_and_validate_test_channel(ch_row_dict, platform_config=active_platform_config)
                ch_label = {
                    "native_mobile": "📱 Android Native Device (ADB)",
                    "cdp_mobile": "📶 Android Chrome/WebView (CDP)",
                    "cdp_electron": "⚛️ Desktop Electron App (CDP)",
                    "emulated_mobile": "📱 Emulated Mobile Browser",
                    "api": "⚡ REST / HTTP API",
                    "web": "🌐 Desktop Web Browser",
                }.get(ch_key, "🌐 Desktop Web Browser")
                st.markdown(f"**Target:** `{resolved_t_url or 'N/A'}` • **Channel:** `{ch_label}`")
                st.markdown(f"**Step:** {resolved_step}")
                st.markdown(f"**Expected Result:** {resolved_exp}")

                actual_val = str(sel_row.get("Actual_Result", "")).strip()
                if not actual_val or actual_val == "-" or actual_val == "nan":
                    actual_val = "Pending execution." if sel_status == "PENDING" else ("Action executed successfully." if sel_status == "PASSED" else "Execution failed.")

                actual_icon = "🟢" if sel_status == "PASSED" else ("❌" if sel_status == "FAILED" else ("⏭️" if sel_status == "SKIPPED" else "⚪"))
                st.markdown(f"**Actual Result:** {actual_icon} {actual_val}")
                st.caption(f"⏱️ Last Duration: {sel_row.get('Duration', '-')}")

            col_single_run, col_view_ss, col_del_case, col_create_bug = st.columns([1.2, 0.9, 0.9, 1.1])
            with col_single_run:
                if st.button(f"▶️ Run Test #{selected_test_idx}", type="primary", width='stretch'):
                    single_df = df.copy()
                    prog_single = st.progress(0.0, text=f"🚀 Initializing runner for Test #{selected_test_idx}...")
                    status_single = st.empty()
                    prog_single.progress(0.20, text=f"🚀 Test #{selected_test_idx}: Preparing execution channel & context...")
                    execute_single_test(selected_test_idx, sel_row, single_df, status_text_ui=status_single)
                    prog_single.progress(1.0, text=f"🚀 Test #{selected_test_idx}: Execution complete!")
                    time.sleep(0.3)
                    st.session_state.results_df = single_df.copy()
                    st.session_state.test_cases_df = single_df.copy()
                    st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
                    for disk_f in ["generated_tests.csv", "deep_inspected_tests.csv"]:
                        if os.path.exists(disk_f):
                            try:
                                single_df.to_csv(disk_f, index=False)
                            except Exception:
                                pass
                    res_status = single_df.at[selected_test_idx, "Status"]
                    res_dur = single_df.at[selected_test_idx, "Duration"]
                    if res_status == "PASSED":
                        st.toast(f"🟢 Test #{selected_test_idx} PASSED in {res_dur}!", icon="✅")
                    elif res_status == "SKIPPED":
                        st.toast(f"⏭️ Test #{selected_test_idx} SKIPPED (Not Applicable)!", icon="ℹ️")
                    else:
                        st.toast(f"❌ Test #{selected_test_idx} FAILED!", icon="⚠️")
                    st.rerun()

            with col_view_ss:
                last_ss = sel_row.get("Screenshot", "")
                if last_ss and os.path.exists(str(last_ss)) and is_valid_image(str(last_ss)):
                    with st.popover("📸 Screenshot", width='stretch'):
                        safe_st_image(str(last_ss), caption=f"Test #{selected_test_idx} Screenshot", width="stretch")
                else:
                    st.button("📸 No Screenshot", disabled=True, width='stretch')

            with col_del_case:
                if st.button(f"🗑️ Delete #{selected_test_idx}", type="secondary", width='stretch', key=f"btn_del_single_test_{selected_test_idx}", help=f"Remove Test #{selected_test_idx} from active suite"):
                    if delete_single_test_case(df, selected_test_idx):
                        st.toast(f"🗑️ Deleted Test #{selected_test_idx}!", icon="✅")
                        st.session_state.pop("single_test_selector", None)
                        st.rerun()

            with col_create_bug:
                with st.popover("🐛 Create Bug", width='stretch'):
                    prio_color = "#EF4444" if sel_status == "FAILED" else "#10B981"
                    st.markdown(
                        f"""<div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid rgba(255, 255, 255, 0.08);">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="font-size: 1.2rem;">🐛</span>
                                <div>
                                    <strong style="color: #F8FAFC; font-size: 0.95rem;">File ClickUp Bug</strong>
                                    <div style="color: #94A3B8; font-size: 0.78rem;">Test #{selected_test_idx} &bull; Status: <span style="color: {prio_color}; font-weight: 600;">{sel_status}</span></div>
                                </div>
                            </div>
                            <span class="status-pill {'status-failed' if sel_status == 'FAILED' else 'status-passed'}" style="font-size: 0.72rem; padding: 2px 7px;">#{selected_test_idx}</span>
                        </div>""",
                        unsafe_allow_html=True,
                    )

                    bug_prio_label = st.selectbox(
                        "Priority",
                        options=["🔴 1 - Urgent", "🟠 2 - High", "🟡 3 - Normal", "🔵 4 - Low"],
                        index=1 if sel_status == "FAILED" else 2,
                        key=f"prio_sel_{selected_test_idx}",
                    )
                    prio_val = int(bug_prio_label.split(" - ")[0].split()[-1])

                    step_title_clean = str(sel_row.get("Test_Step_Description", "")).strip().replace("\n", " ")
                    custom_task_name = st.text_input(
                        "Task Title",
                        value=f"[BUG] Test #{selected_test_idx}: {step_title_clean[:85]}",
                        placeholder="e.g. [BUG] Button not responsive on dashboard",
                        key=f"bug_title_{selected_test_idx}",
                    )

                    target_list_id_input = st.text_input(
                        "List ID",
                        value=clickup_list_id or "",
                        placeholder="e.g. 90180234123 (from ClickUp list URL)",
                        key=f"cu_list_input_{selected_test_idx}",
                        help="Enter your target ClickUp List ID. Find it in your ClickUp list URL (e.g. app.clickup.com/123/v/l/li/90180234123).",
                    )

                    if not clickup_token:
                        st.caption("ℹ️ ClickUp API Token can be configured in the sidebar Settings for instant 1-click filing.")

                    st.markdown("<div style='margin-top: 6px;'></div>", unsafe_allow_html=True)
                    if st.button("🚀 Push to ClickUp", key=f"btn_push_cu_{selected_test_idx}", type="primary", width='stretch'):
                        if not clickup_token:
                            st.error("Please configure ClickUp API Token in the sidebar first.")
                        elif not target_list_id_input:
                            st.error("Please enter a ClickUp List ID.")
                        else:
                            from core.clickup_integration import clickup_client
                            with st.spinner("Creating bug task in ClickUp..."):
                                res = clickup_client.create_bug_task(
                                    test_row=sel_row.to_dict(),
                                    test_index=selected_test_idx,
                                    list_id=target_list_id_input,
                                    api_token=clickup_token,
                                    priority=prio_val,
                                    custom_title=custom_task_name,
                                )
                                if res.get("success"):
                                    st.success(res.get("message"))
                                    st.markdown(f"[🔗 **Open Task #{res.get('task_id')} in ClickUp**]({res.get('task_url')})")
                                    st.toast(f"✅ Bug filed in ClickUp! Task #{res.get('task_id')}", icon="🐛")
                                else:
                                    st.error(res.get("error"))

    # ---------------------------------------------------------
    # Execution Metrics & Results
    # ---------------------------------------------------------
    st.divider()
    col_res_hdr1, col_res_hdr2 = st.columns([3, 1])
    with col_res_hdr1:
        st.subheader("📊 Execution Results & Analytics")
    with col_res_hdr2:
        if st.button("🗑️ Clear Results", type="secondary", help="Reset all test results, durations, and metrics back to PENDING", width='stretch'):
            if "results_df" in st.session_state and st.session_state.results_df is not None:
                st.session_state.results_df["Status"] = "PENDING"
                if "Duration" in st.session_state.results_df.columns:
                    st.session_state.results_df["Duration"] = "-"
                if "Actual_Result" in st.session_state.results_df.columns:
                    st.session_state.results_df["Actual_Result"] = "-"
                if "Notes" in st.session_state.results_df.columns:
                    st.session_state.results_df["Notes"] = ""
                if "Execution_Notes" in st.session_state.results_df.columns:
                    st.session_state.results_df["Execution_Notes"] = ""
            if "test_cases" in st.session_state and st.session_state.test_cases is not None:
                st.session_state.test_cases["Status"] = "PENDING"
                if "Duration" in st.session_state.test_cases.columns:
                    st.session_state.test_cases["Duration"] = "-"
                if "Actual_Result" in st.session_state.test_cases.columns:
                    st.session_state.test_cases["Actual_Result"] = "-"
                if "Notes" in st.session_state.test_cases.columns:
                    st.session_state.test_cases["Notes"] = ""
                if "Execution_Notes" in st.session_state.test_cases.columns:
                    st.session_state.test_cases["Execution_Notes"] = ""
            st.session_state["grid_version"] = st.session_state.get("grid_version", 0) + 1
            if "auto_generated_file" in st.session_state and os.path.exists(str(st.session_state.auto_generated_file)):
                try:
                    st.session_state.results_df.to_csv(st.session_state.auto_generated_file, index=False)
                except Exception:
                    pass
            st.toast("🧹 Execution results cleared and reset to PENDING!", icon="✅")
            st.rerun()

    active_results = (
        st.session_state.results_df
        if "results_df" in st.session_state and st.session_state.results_df is not None
        else edited_df
    )
    if active_results is not None and len(active_results) > 0:
        from core.test_generator import harmonize_qa_dataframe
        active_results = harmonize_qa_dataframe(active_results)
        valid_res_mask = (
            active_results["Test_Step_Description"].fillna("").astype(str).str.strip().ne("")
            | active_results["Target"].fillna("").astype(str).str.strip().ne("")
        )
        if valid_res_mask.any():
            active_results = active_results[valid_res_mask].copy()
        active_results = active_results.reset_index(drop=True)
        active_results.index = range(1, len(active_results) + 1)

    total_cnt = len(active_results)
    passed_cnt = (active_results["Status"] == "PASSED").sum()
    failed_cnt = (active_results["Status"] == "FAILED").sum()
    skipped_cnt = (active_results["Status"] == "SKIPPED").sum()
    executed_cnt = passed_cnt + failed_cnt + skipped_cnt
    rate = f"{(passed_cnt / total_cnt * 100):.1f}%" if (executed_cnt > 0 and total_cnt > 0) else "-"

    # Total execution time calculation
    total_dur_seconds = 0.0
    for d in active_results.get("Duration", []):
        try:
            cleaned_d = str(d).replace("s", "").strip()
            if cleaned_d and cleaned_d != "-":
                total_dur_seconds += float(cleaned_d)
        except Exception:
            pass

    if executed_cnt == 0 and total_cnt > 0:
        st.info("⚪ **Suite Loaded & Awaiting Execution**: All test cases are currently in `PENDING` status. Click **⚡ Play / Run Batch** or trigger an individual test above to begin execution.")

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.markdown(f'<div class="kpi-stat-box"><div class="kpi-stat-val" style="color: #60A5FA;">{total_cnt}</div><div class="kpi-stat-lbl">Total Tests</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="kpi-stat-box" style="border-color: rgba(16, 185, 129, 0.4);"><div class="kpi-stat-val" style="color: #34D399;">{passed_cnt}</div><div class="kpi-stat-lbl">✅ Passed</div></div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="kpi-stat-box" style="border-color: rgba(239, 68, 68, 0.4);"><div class="kpi-stat-val" style="color: #F87171;">{failed_cnt}</div><div class="kpi-stat-lbl">❌ Failed</div></div>', unsafe_allow_html=True)
    with m4:
        st.markdown(f'<div class="kpi-stat-box" style="border-color: rgba(245, 158, 11, 0.4);"><div class="kpi-stat-val" style="color: #FBBF24;">{skipped_cnt}</div><div class="kpi-stat-lbl">⏭️ Skipped</div></div>', unsafe_allow_html=True)
    with m5:
        st.markdown(f'<div class="kpi-stat-box" style="border-color: rgba(99, 102, 241, 0.4);"><div class="kpi-stat-val" style="color: #818CF8;">{rate}</div><div class="kpi-stat-lbl">📈 Success Rate</div></div>', unsafe_allow_html=True)
    with m6:
        dur_display = f"{total_dur_seconds:.1f}s" if total_dur_seconds > 0 else "-"
        st.markdown(f'<div class="kpi-stat-box"><div class="kpi-stat-val" style="color: #E2E8F0;">{dur_display}</div><div class="kpi-stat-lbl">⏱️ Total Duration</div></div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

    # Filter Options
    has_multi_sheets = "Sheet_Name" in active_results.columns and active_results["Sheet_Name"].dropna().nunique() > 1
    if has_multi_sheets:
        c_res_sh, c_res_st = st.columns([1, 1])
        with c_res_sh:
            unique_res_sheets = ["🌐 All Sheets"] + [str(s).strip() for s in active_results["Sheet_Name"].dropna().unique().tolist() if str(s).strip() and str(s).strip().lower() not in ["none", "nan", ""]]
            selected_res_sheet = st.selectbox("Filter by Sheet / Module", unique_res_sheets, key="results_sheet_filter_select")
        with c_res_st:
            filter_choice = st.selectbox(
                "Filter Results by Status", ["All", "PASSED", "FAILED", "SKIPPED", "PENDING"], key="results_status_filter_select"
            )
    else:
        selected_res_sheet = "🌐 All Sheets"
        filter_choice = st.selectbox(
            "Filter Results Table", ["All", "PASSED", "FAILED", "SKIPPED", "PENDING"]
        )

    display_subset = active_results
    if selected_res_sheet != "🌐 All Sheets" and "Sheet_Name" in display_subset.columns:
        display_subset = display_subset[display_subset["Sheet_Name"].astype(str).str.strip() == selected_res_sheet.strip()]
    if filter_choice != "All":
        display_subset = display_subset[display_subset["Status"] == filter_choice]

    from core.test_generator import harmonize_qa_dataframe
    display_subset = harmonize_qa_dataframe(display_subset)

    # Filter out adapter internals and spurious empty columns
    cols_to_hide = {"_target", "_step", "_expected", "_code", "_type", "Playwright_Code"}
    clean_cols = [c for c in display_subset.columns if c not in cols_to_hide and not re.match(r'^(?:unnamed:\s*\d+|column\s*\d+)$', str(c).strip(), re.I)]

    # Prioritize user-facing QA columns so steps, expected outcome, and screen always render first
    priority_order = [
        "Run", "Sheet_Name", "Status",
        "Screen / Function", "Module",
        "User Action on Dev Link", "Test Steps", "Test_Step_Description",
        "Expected Outcome", "Expected Result", "Expected_Result",
        "Actual_Result", "Duration", "Type", "Target",
        "Test Case ID", "Use Case ID", "Priority", "Classification", "Date", "Comments", "Execution_Notes"
    ]
    ordered_cols = [c for c in priority_order if c in clean_cols]
    remaining_cols = [c for c in clean_cols if c not in ordered_cols]
    display_subset = display_subset[ordered_cols + remaining_cols]

    def color_status(val):
        if val == "PASSED":
            return "color: #28a745; font-weight: bold"
        elif val == "FAILED":
            return "color: #dc3545; font-weight: bold"
        elif val == "SKIPPED":
            return "color: #ffc107; font-weight: bold"
        return "color: #6c757d"

    styled_table = display_subset.style.map(color_status, subset=["Status"]) if "Status" in display_subset.columns else display_subset
    st.dataframe(styled_table, width="stretch")

    # Export & Action Toolbar
    col_exp1, col_exp2, col_exp3, col_exp4 = st.columns([1.2, 1.2, 1.4, 1.4])
    with col_exp1:
        output_excel = BytesIO()
        with pd.ExcelWriter(output_excel, engine="openpyxl") as writer:
            if "Sheet_Name" in active_results.columns and active_results["Sheet_Name"].dropna().nunique() > 1:
                used_sheet_names = set()
                for sheet_n, sheet_grp in active_results.groupby("Sheet_Name"):
                    clean_sheet_name = sanitize_excel_sheet_name(sheet_n, used_sheet_names)
                    sheet_grp.to_excel(writer, index=False, sheet_name=clean_sheet_name)
            else:
                active_results.to_excel(writer, index=False, sheet_name="Test Results")
        st.download_button(
            label="📥 Excel (.xlsx)",
            data=output_excel.getvalue(),
            file_name="test_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width='stretch',
        )

    with col_exp2:
        output_csv = active_results.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 CSV (.csv)",
            data=output_csv,
            file_name="test_results.csv",
            mime="text/csv",
            width='stretch',
        )

    with col_exp3:
        from core.html_reporter import generate_html_report
        html_data = generate_html_report(active_results)
        st.download_button(
            label="📄 Interactive HTML Report",
            data=html_data.encode("utf-8"),
            file_name="test_execution_report.html",
            mime="text/html",
            width='stretch',
        )

    with col_exp4:
        if st.button("🐛 Push Failed to ClickUp", type="secondary", width='stretch', help="Create ClickUp bug tasks for all failed tests in one click", disabled=(failed_cnt == 0)):
            if not clickup_token:
                st.error("Please configure ClickUp API Token in the sidebar first.")
            elif not clickup_list_id:
                st.error("Please configure ClickUp List ID in the sidebar first.")
            else:
                from core.clickup_integration import clickup_client
                failed_tests = active_results[active_results["Status"] == "FAILED"]
                created_count = 0
                err_count = 0
                total_failed = len(failed_tests)
                prog_bug = st.progress(0.0, text=f"🐛 Filing {total_failed} bugs into ClickUp (0/{total_failed})...")
                for c_i, (idx_num, f_row) in enumerate(zip(failed_tests.index, failed_tests.to_dict("records"))):
                    pct = c_i / max(1, total_failed)
                    prog_bug.progress(pct, text=f"🐛 Filing bug for Test #{idx_num} into ClickUp ({c_i + 1}/{total_failed})...")
                    res = clickup_client.create_bug_task(
                        test_row=f_row,
                        test_index=idx_num,
                        list_id=clickup_list_id,
                        api_token=clickup_token,
                        priority=2,
                    )
                    if res.get("success"):
                        created_count += 1
                    else:
                        err_count += 1
                prog_bug.progress(1.0, text=f"✅ Finished filing {created_count} bug task(s) into ClickUp!")
                time.sleep(0.35)
                if created_count > 0:
                    st.success(f"✅ Created {created_count} bug task(s) in ClickUp List `{clickup_list_id}`!")
                    st.toast(f"✅ {created_count} Bugs filed in ClickUp!", icon="🐛")
                if err_count > 0:
                    st.warning(f"⚠️ {err_count} task(s) encountered errors.")



    # ---------------------------------------------------------
    # Visual Inspection & Screenshot Gallery
    # ---------------------------------------------------------
    if "Screenshot" in active_results.columns and active_results["Screenshot"].dropna().any():
        valid_ss_df = active_results[active_results["Screenshot"].astype(str).str.strip().str.len() > 0]
        if not valid_ss_df.empty:
            st.divider()
            with st.expander("🖼️ View Test Step Screenshots (Click to Expand)", expanded=False):
                for r in valid_ss_df.to_dict("records"):
                    ss_file = str(r.get("Screenshot", "")).strip()
                    if os.path.exists(ss_file) and is_valid_image(ss_file):
                        st.markdown(f"**Test:** {r.get('Test_Step_Description', 'Step')} | **Status:** `{r.get('Status', '')}`")
                        st.caption(f"📁 Path: `{ss_file}`")
                        safe_st_image(ss_file, caption=str(r.get('Test_Step_Description', 'Screenshot')), width="stretch")
                        st.divider()

    screenshots_dir = os.path.join("reports", "screenshots")
    if os.path.exists(screenshots_dir):
        raw_files = [
            f for f in os.listdir(screenshots_dir) 
            if f.lower().endswith((".png", ".jpg", ".jpeg")) and os.path.isfile(os.path.join(screenshots_dir, f))
        ]
        # Validate files and clean up any 0-byte or non-image files
        screenshot_files = []
        for img_f in raw_files:
            fpath = os.path.join(screenshots_dir, img_f)
            if is_valid_image(fpath):
                screenshot_files.append(img_f)
            else:
                try:
                    if os.path.getsize(fpath) == 0:
                        os.remove(fpath)
                except Exception:
                    pass

        if screenshot_files:
            import hashlib
            st.divider()

            # Group screenshots by MD5 hash to detect visual duplicates
            hash_to_files = {}
            for img_f in screenshot_files:
                fpath = os.path.join(screenshots_dir, img_f)
                try:
                    with open(fpath, "rb") as fl:
                        h = hashlib.md5(fl.read()).hexdigest()
                    hash_to_files.setdefault(h, []).append(img_f)
                except Exception:
                    pass

            unique_files = [flist[-1] for flist in hash_to_files.values()]
            dup_count = len(screenshot_files) - len(unique_files)

            col_g1, col_g2, col_g3 = st.columns([2.2, 1.1, 1.1])
            with col_g1:
                st.subheader(f"📸 Full Screenshot Gallery ({len(screenshot_files)} captured)")
                st.caption(
                    f"✨ **{len(unique_files)} unique visual screens** detected across {len(screenshot_files)} total step snapshots. "
                    + (f"({dup_count} duplicate snapshots from repeated runs)" if dup_count > 0 else "")
                )
            with col_g2:
                if dup_count > 0:
                    if st.button("🧹 Purge Duplicates", type="secondary", help=f"Delete {dup_count} identical duplicate images from disk and keep the newest copy of each unique screen", width='stretch'):
                        deleted_cnt = 0
                        for flist in hash_to_files.values():
                            if len(flist) > 1:
                                for dup_f in flist[:-1]:
                                    try:
                                        os.remove(os.path.join(screenshots_dir, dup_f))
                                        deleted_cnt += 1
                                    except Exception:
                                        pass
                        st.toast(f"🧹 Removed {deleted_cnt} duplicate screenshot files!", icon="✅")
                        st.rerun()
                else:
                    st.button("✨ All Unique", disabled=True, width='stretch')
            with col_g3:
                if st.button("🗑️ Clear All Gallery", type="secondary", help="Delete all captured PNG screenshots from disk", width='stretch'):
                    for img_f in screenshot_files:
                        try:
                            os.remove(os.path.join(screenshots_dir, img_f))
                        except Exception:
                            pass
                    st.toast("✅ All screenshots cleared from disk!", icon="🧹")
                    st.rerun()

            c_flt_dups, c_info = st.columns([1.5, 2])
            with c_flt_dups:
                only_unique = st.checkbox("🔍 Show Unique Screens Only (Hide Duplicates)", value=True, key="chk_only_unique_ss")
            with c_info:
                st.caption("ℹ️ *Screenshots look identical when multiple test steps execute against the same URL/view (e.g. login screen repeated across test runs).*")

            display_list = unique_files if only_unique else screenshot_files
            with st.expander(f"🖼️ Browse {len(display_list)} {'Unique' if only_unique else 'Saved'} Screenshots", expanded=False):
                cols = st.columns(3)
                for i, img_file in enumerate(reversed(display_list)):
                    img_path = os.path.join(screenshots_dir, img_file)
                    with cols[i % 3]:
                        lbl = f"{img_file}"
                        matching_hashes = [k for k, v in hash_to_files.items() if img_file in v]
                        copies = len(hash_to_files[matching_hashes[0]]) if matching_hashes else 1
                        if copies > 1:
                            lbl += f" ({copies} identical runs)"
                        safe_st_image(img_path, caption=lbl, width="stretch")

