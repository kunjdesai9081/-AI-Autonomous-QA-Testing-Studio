import base64
import html
import os
from datetime import datetime
import pandas as pd


def _escape(val: any) -> str:
    """Safely escapes text for HTML embedding."""
    if val is None or pd.isna(val):
        return ""
    return html.escape(str(val).strip())


def generate_html_report(df: pd.DataFrame, title: str = "Test Execution Quality & Analytics Report") -> str:
    """
    Generates a professional, high-contrast, interactive HTML test execution report
    with embedded KPI charts, multi-criteria filtering, live search, code syntax styling,
    execution logs, screenshot lightbox viewer, and 1-click clipboard copy.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        df = pd.DataFrame(columns=["Run", "Type", "Target", "Test_Step_Description", "Expected_Result", "Status", "Duration", "Actual_Result", "Screenshot", "Execution_Notes", "Playwright_Code", "Generated_By"])

    total_tests = len(df)
    passed_tests = int((df["Status"].astype(str).str.upper() == "PASSED").sum()) if "Status" in df.columns else 0
    failed_tests = int((df["Status"].astype(str).str.upper() == "FAILED").sum()) if "Status" in df.columns else 0
    skipped_tests = int((df["Status"].astype(str).str.upper() == "SKIPPED").sum()) if "Status" in df.columns else 0
    pending_tests = max(0, total_tests - (passed_tests + failed_tests + skipped_tests))
    pass_rate = round((passed_tests / total_tests * 100), 1) if total_tests > 0 else 0.0

    web_tests = int((df["Type"].astype(str).str.upper() == "WEB").sum()) if "Type" in df.columns else 0
    api_tests = int((df["Type"].astype(str).str.upper() == "API").sum()) if "Type" in df.columns else 0

    total_dur_seconds = 0.0
    if "Duration" in df.columns:
        for d in df["Duration"]:
            try:
                c = str(d).replace("s", "").strip()
                if c and c != "-":
                    total_dur_seconds += float(c)
            except Exception:
                pass

    avg_dur = round(total_dur_seconds / total_tests, 2) if total_tests > 0 else 0.0
    timestamp_str = datetime.now().strftime("%b %d, %Y • %I:%M:%S %p")

    # Determine unique targets for metadata badge
    unique_targets = [t for t in df["Target"].dropna().unique().tolist() if str(t).strip() and str(t).strip().lower() not in ["none", "nan", "-"]] if "Target" in df.columns else []
    primary_target = unique_targets[0] if unique_targets else "N/A"

    # Generate rows HTML
    rows_html = []
    for idx, row in enumerate(df.to_dict("records")):
        status = str(row.get("Status", "PENDING")).upper()
        if status == "PASSED":
            badge_cls = "badge-pass"
            badge_icon = "✅"
            status_color = "#34d399"
        elif status == "FAILED":
            badge_cls = "badge-fail"
            badge_icon = "❌"
            status_color = "#f87171"
        elif status == "SKIPPED":
            badge_cls = "badge-skip"
            badge_icon = "⏭️"
            status_color = "#fbbf24"
        else:
            badge_cls = "badge-pending"
            badge_icon = "⏳"
            status_color = "#94a3b8"

        t_type = str(row.get("Type", "Web")).strip().title()
        type_cls = "badge-web" if t_type.lower() == "web" else "badge-api"
        type_icon = "🌐" if t_type.lower() == "web" else "⚡"

        gen_by = str(row.get("Generated_By", "")).strip()
        if not gen_by or gen_by.lower() in ["nan", "none", "-"]:
            gen_by = "🤖 AI Generated" if "🤖" in str(row.get("Execution_Notes", "")) else "⚙️ Rule Engine"
        
        target = str(row.get("Target", "")).strip()
        step = str(row.get("Test_Step_Description", "N/A")).strip()
        expected = str(row.get("Expected_Result", "Action executes cleanly")).strip()
        duration = str(row.get("Duration", "-")).strip()
        if duration and duration != "-" and not duration.endswith("s"):
            duration = f"{duration}s"

        code = str(row.get("Playwright_Code", "")).strip()
        notes = str(row.get("Execution_Notes", "")).strip()
        ss_path = str(row.get("Screenshot", "")).strip()
        actual = str(row.get("Actual_Result", "-")).strip()
        if not actual or actual.lower() in ["nan", "none", "-"]:
            actual = "Observed expected behavior cleanly." if status == "PASSED" else (notes if status == "FAILED" else "Pending execution.")

        # Escape all text fields for HTML injection protection
        esc_step = _escape(step)
        esc_target = _escape(target)
        esc_expected = _escape(expected)
        esc_actual = _escape(actual)
        esc_duration = _escape(duration)
        esc_gen_by = _escape(gen_by)
        esc_code = _escape(code)
        esc_notes = _escape(notes)

        # Handle screenshots with Base64 embedding and Lightbox preview
        ss_html = ""
        has_ss = False
        if ss_path and os.path.exists(ss_path):
            try:
                with open(ss_path, "rb") as img_f:
                    b64_data = base64.b64encode(img_f.read()).decode("utf-8")
                ss_data_uri = f"data:image/png;base64,{b64_data}"
                has_ss = True
                ss_html = f"""
                <div class="screenshot-section">
                    <div class="section-title">
                        <span>📸 Visual State Screenshot</span>
                        <span class="section-badge">Verified Frame</span>
                    </div>
                    <div class="screenshot-wrapper">
                        <img src="{ss_data_uri}" alt="Step #{idx} Screenshot" class="screenshot-thumb" onclick="openLightbox('{ss_data_uri}', 'Step #{idx}: {esc_step}')"/>
                        <div class="screenshot-hover-hint">🔍 Click to zoom full resolution</div>
                    </div>
                </div>
                """
            except Exception:
                ss_html = f'<div class="screenshot-empty"><i>Screenshot captured at: {esc_target}</i></div>'
        else:
            ss_html = '<div class="screenshot-empty"><span>📷 No visual screenshot captured for this test step.</span></div>'

        # Code block
        if code and code.lower() != "nan":
            code_html = f"""
            <div class="code-container">
                <div class="code-header">
                    <span class="code-lang-tag">Playwright Automation Script</span>
                    <button class="btn-copy" onclick="copyCode(this)">📋 Copy Script</button>
                </div>
                <pre class="code-pre"><code>{esc_code}</code></pre>
            </div>
            """
        else:
            code_html = '<div class="code-empty"><i>No custom automation script required for this deterministic validation.</i></div>'

        # Notes / Logs block
        if notes and notes.lower() != "nan":
            notes_html = f"""
            <div class="logs-container">
                <div class="logs-header">
                    <span class="logs-tag">Agent Diagnostics & Console Logs</span>
                    <button class="btn-copy" onclick="copyLogs(this)">📋 Copy Logs</button>
                </div>
                <pre class="logs-pre"><code>{esc_notes}</code></pre>
            </div>
            """
        else:
            notes_html = '<div class="logs-empty"><span>Execution verified cleanly with 0 runtime errors.</span></div>'

        # Target URL display helper
        if target:
            target_display = f'<a href="{esc_target}" target="_blank" rel="noopener noreferrer" class="target-link" onclick="event.stopPropagation()">{esc_target}</a>'
        else:
            target_display = '<span class="text-dim">No target URL specified</span>'

        row_html = f"""
        <tr class="test-row status-{status.lower()} type-{t_type.lower()}" onclick="toggleDetails('details-{idx}', this)">
            <td class="col-id"><span class="test-id-pill">#{idx}</span></td>
            <td class="col-type"><span class="badge {type_cls}">{type_icon} {t_type}</span></td>
            <td class="col-status"><span class="badge {badge_cls}">{badge_icon} {status}</span></td>
            <td class="col-info">
                <div class="step-title">{esc_step}</div>
                <div class="step-target-bar">
                    <span class="target-prefix">Target:</span> {target_display}
                </div>
            </td>
            <td class="col-dur"><span class="duration-pill">⏱️ {esc_duration}</span></td>
            <td class="col-action">
                <button class="btn-expand" onclick="event.stopPropagation(); toggleDetails('details-{idx}', this.closest('tr'))">
                    <span class="expand-label">Details</span>
                    <span class="expand-chevron">▾</span>
                </button>
            </td>
        </tr>
        <tr id="details-{idx}" class="details-row" style="display: none;">
            <td colspan="6" class="details-cell">
                <div class="details-card">
                    <!-- Top Summary Grid -->
                    <div class="details-grid">
                        <div class="spec-card">
                            <div class="card-head">
                                <span class="card-head-title">🎯 Test Specifications</span>
                                <span class="origin-pill">{esc_gen_by}</span>
                            </div>
                            <div class="spec-list">
                                <div class="spec-item">
                                    <div class="spec-label">Step Description</div>
                                    <div class="spec-value desc-val">{esc_step}</div>
                                </div>
                                <div class="spec-item">
                                    <div class="spec-label">Target URL / Screen</div>
                                    <div class="spec-value url-val">
                                        <a href="{esc_target}" target="_blank" rel="noopener noreferrer" class="link-url">{esc_target}</a>
                                        <button class="btn-copy-mini" onclick="copyText('{esc_target}', this)" title="Copy Target URL">📋</button>
                                    </div>
                                </div>
                                <div class="spec-item">
                                    <div class="spec-label">Expected Outcome</div>
                                    <div class="spec-value exp-val">{esc_expected}</div>
                                </div>
                                <div class="spec-item">
                                    <div class="spec-label">Actual Result</div>
                                    <div class="spec-value actual-val status-tint-{status.lower()}">
                                        <span class="actual-status-dot" style="background-color: {status_color};"></span>
                                        <span>{esc_actual}</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="diag-card">
                            <div class="card-head">
                                <span class="card-head-title">📋 Execution Diagnostics</span>
                                <span class="status-duration-badge">⏱️ {esc_duration}</span>
                            </div>
                            {notes_html}
                        </div>
                    </div>

                    <!-- Script & Screenshot Section -->
                    <div class="details-lower-grid">
                        <div class="script-wrapper">
                            <div class="section-title">
                                <span>⚡ Executed Automation Code</span>
                            </div>
                            {code_html}
                        </div>
                        {ss_html}
                    </div>
                </div>
            </td>
        </tr>
        """
        rows_html.append(row_html)

    table_body = "\n".join(rows_html)

    # Calculate overall health badge
    if pass_rate >= 95.0:
        health_badge = '<span class="status-pill-pass">🌟 Excellent Quality (95%+)</span>'
    elif pass_rate >= 80.0:
        health_badge = '<span class="status-pill-warn">⚠️ Acceptable Quality (80%+)</span>'
    else:
        health_badge = '<span class="status-pill-fail">🚨 Action Required (&lt;80%)</span>'

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{_escape(title)}</title>
    <style>
        :root {{
            --bg-page: #080c16;
            --bg-surface: #0f172a;
            --bg-card: #142036;
            --bg-card-alt: #1a2942;
            --bg-hover: #1e3252;
            --bg-code: #090e1a;
            
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-card: rgba(255, 255, 255, 0.12);
            --border-focus: #38bdf8;

            --text-heading: #ffffff;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --text-dim: #64748b;

            --accent-cyan: #38bdf8;
            --accent-blue: #60a5fa;
            --accent-purple: #c084fc;

            --color-pass: #22c55e;
            --color-pass-soft: rgba(34, 197, 94, 0.15);
            --color-pass-border: rgba(34, 197, 94, 0.35);

            --color-fail: #ef4444;
            --color-fail-soft: rgba(239, 68, 68, 0.15);
            --color-fail-border: rgba(239, 68, 68, 0.35);

            --color-skip: #f59e0b;
            --color-skip-soft: rgba(245, 158, 11, 0.15);
            --color-skip-border: rgba(245, 158, 11, 0.35);

            --color-pending: #94a3b8;
            --color-pending-soft: rgba(148, 163, 184, 0.15);
            --color-pending-border: rgba(148, 163, 184, 0.3);

            --shadow-subtle: 0 4px 18px rgba(0, 0, 0, 0.35);
            --shadow-card: 0 8px 30px rgba(0, 0, 0, 0.45);
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", Helvetica, Arial, sans-serif;
        }}

        ::selection {{
            background: #0284c7;
            color: #ffffff;
        }}

        body {{
            background-color: var(--bg-page);
            color: var(--text-primary);
            line-height: 1.55;
            padding: 24px 20px 60px;
            font-size: 14px;
        }}

        .container {{
            max-width: 1360px;
            margin: 0 auto;
        }}

        /* Header Bar */
        .header-bar {{
            background: linear-gradient(135deg, #111d33 0%, #0d1527 100%);
            border: 1px solid var(--border-card);
            border-radius: 14px;
            padding: 22px 28px;
            margin-bottom: 24px;
            box-shadow: var(--shadow-card);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}

        .header-brand {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}

        .brand-icon {{
            font-size: 2rem;
            background: rgba(56, 189, 248, 0.12);
            border: 1px solid rgba(56, 189, 248, 0.25);
            border-radius: 12px;
            padding: 8px 12px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .header-title h1 {{
            font-size: 1.45rem;
            font-weight: 700;
            color: #ffffff;
            letter-spacing: -0.3px;
            margin-bottom: 4px;
        }}

        .header-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            align-items: center;
            font-size: 0.85rem;
            color: var(--text-muted);
        }}

        .meta-tag {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            background: rgba(255, 255, 255, 0.05);
            padding: 3px 8px;
            border-radius: 6px;
            border: 1px solid var(--border-subtle);
        }}

        .header-actions {{
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }}

        .btn-action {{
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            color: var(--text-secondary);
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s ease;
        }}

        .btn-action:hover {{
            background: var(--bg-hover);
            color: #ffffff;
            border-color: var(--accent-cyan);
            transform: translateY(-1px);
        }}

        /* KPI Metric Cards */
        .kpi-row {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: 14px;
            margin-bottom: 24px;
        }}

        .kpi-card {{
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            padding: 16px 20px;
            box-shadow: var(--shadow-subtle);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}

        .kpi-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: rgba(255, 255, 255, 0.15);
        }}

        .kpi-card.pass::before {{ background: var(--color-pass); }}
        .kpi-card.fail::before {{ background: var(--color-fail); }}
        .kpi-card.skip::before {{ background: var(--color-skip); }}
        .kpi-card.rate::before {{ background: var(--accent-cyan); }}

        .kpi-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
        }}

        .kpi-label {{
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--text-muted);
        }}

        .kpi-icon {{
            font-size: 1.1rem;
        }}

        .kpi-value {{
            font-size: 1.95rem;
            font-weight: 800;
            color: #ffffff;
            line-height: 1.2;
            margin-bottom: 4px;
        }}

        .kpi-card.pass .kpi-value {{ color: var(--color-pass); }}
        .kpi-card.fail .kpi-value {{ color: var(--color-fail); }}
        .kpi-card.skip .kpi-value {{ color: var(--color-skip); }}
        .kpi-card.rate .kpi-value {{ color: var(--accent-cyan); }}

        .kpi-sub {{
            font-size: 0.78rem;
            color: var(--text-dim);
        }}

        /* Filter Toolbar */
        .toolbar-card {{
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            padding: 14px 18px;
            margin-bottom: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            box-shadow: var(--shadow-subtle);
        }}

        .search-container {{
            flex: 1;
            min-width: 280px;
            position: relative;
        }}

        .search-input {{
            width: 100%;
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 8px;
            padding: 10px 14px 10px 38px;
            color: #ffffff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.2s ease;
        }}

        .search-input:focus {{
            border-color: var(--accent-cyan);
            box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
        }}

        .search-icon {{
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-dim);
            font-size: 0.95rem;
        }}

        .filter-group {{
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }}

        .filter-btn {{
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 7px 13px;
            border-radius: 7px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.18s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}

        .filter-btn:hover {{
            background: var(--bg-hover);
            color: #ffffff;
            border-color: var(--border-card);
        }}

        .filter-btn.active {{
            background: rgba(56, 189, 248, 0.15);
            color: var(--accent-cyan);
            border-color: var(--accent-cyan);
        }}

        .btn-badge {{
            background: rgba(255, 255, 255, 0.1);
            color: inherit;
            padding: 1px 6px;
            border-radius: 10px;
            font-size: 0.72rem;
            font-weight: 700;
        }}

        /* Test Table */
        .table-wrap {{
            background: var(--bg-surface);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: var(--shadow-card);
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }}

        thead th {{
            background: #111a2e;
            padding: 13px 16px;
            font-size: 0.78rem;
            text-transform: uppercase;
            font-weight: 700;
            letter-spacing: 0.6px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border-card);
        }}

        tbody tr.test-row {{
            cursor: pointer;
            border-bottom: 1px solid var(--border-subtle);
            transition: background-color 0.15s ease;
        }}

        tbody tr.test-row:hover {{
            background-color: var(--bg-hover);
        }}

        tbody td {{
            padding: 14px 16px;
            vertical-align: middle;
            font-size: 0.9rem;
        }}

        .col-id {{ width: 55px; }}
        .col-type {{ width: 100px; }}
        .col-status {{ width: 120px; }}
        .col-info {{ max-width: 550px; }}
        .col-dur {{ width: 100px; text-align: center; }}
        .col-action {{ width: 100px; text-align: right; }}

        .test-id-pill {{
            font-weight: 700;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}

        .step-title {{
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 4px;
            line-height: 1.4;
        }}

        .step-target-bar {{
            font-size: 0.8rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .target-prefix {{
            color: var(--text-dim);
            font-weight: 600;
        }}

        .target-link {{
            color: var(--accent-cyan);
            text-decoration: none;
            font-family: "JetBrains Mono", Consolas, monospace;
            font-size: 0.78rem;
            transition: color 0.15s;
        }}

        .target-link:hover {{
            color: #7dd3fc;
            text-decoration: underline;
        }}

        .duration-pill {{
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-subtle);
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 0.8rem;
            color: var(--text-secondary);
            font-variant-numeric: tabular-nums;
        }}

        /* Status & Type Badges */
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.78rem;
            font-weight: 700;
            letter-spacing: 0.3px;
            white-space: nowrap;
        }}

        .badge-pass {{
            background: var(--color-pass-soft);
            color: var(--color-pass);
            border: 1px solid var(--color-pass-border);
        }}

        .badge-fail {{
            background: var(--color-fail-soft);
            color: var(--color-fail);
            border: 1px solid var(--color-fail-border);
        }}

        .badge-skip {{
            background: var(--color-skip-soft);
            color: var(--color-skip);
            border: 1px solid var(--color-skip-border);
        }}

        .badge-pending {{
            background: var(--color-pending-soft);
            color: var(--color-pending);
            border: 1px solid var(--color-pending-border);
        }}

        .badge-web {{
            background: rgba(56, 189, 248, 0.12);
            color: var(--accent-cyan);
            border: 1px solid rgba(56, 189, 248, 0.28);
        }}

        .badge-api {{
            background: rgba(192, 132, 252, 0.12);
            color: var(--accent-purple);
            border: 1px solid rgba(192, 132, 252, 0.28);
        }}

        .btn-expand {{
            background: transparent;
            border: 1px solid var(--border-subtle);
            color: var(--text-muted);
            padding: 5px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.82rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            transition: all 0.2s ease;
        }}

        .btn-expand:hover {{
            background: var(--bg-hover);
            color: #ffffff;
            border-color: var(--accent-cyan);
        }}

        .expand-chevron {{
            transition: transform 0.2s ease;
            display: inline-block;
        }}

        /* Expanded Details Card */
        .details-cell {{
            background: #0b1120;
            padding: 0 !important;
        }}

        .details-card {{
            padding: 24px;
            border-top: 1px solid rgba(56, 189, 248, 0.2);
            border-bottom: 1px solid rgba(56, 189, 248, 0.2);
            background: linear-gradient(180deg, #0e1628 0%, #090e1c 100%);
        }}

        .details-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }}

        @media (max-width: 900px) {{
            .details-grid {{ grid-template-columns: 1fr; }}
        }}

        .spec-card, .diag-card {{
            background: var(--bg-surface);
            border: 1px solid var(--border-card);
            border-radius: 10px;
            padding: 18px 20px;
            box-shadow: var(--shadow-subtle);
        }}

        .card-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 10px;
        }}

        .card-head-title {{
            font-size: 0.95rem;
            font-weight: 700;
            color: var(--accent-cyan);
            letter-spacing: -0.2px;
        }}

        .origin-pill {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 2px 8px;
            border-radius: 6px;
            font-size: 0.74rem;
            font-weight: 600;
        }}

        .status-duration-badge {{
            color: var(--text-muted);
            font-size: 0.8rem;
            font-weight: 600;
        }}

        .spec-list {{
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .spec-item {{
            display: flex;
            flex-direction: column;
            gap: 3px;
        }}

        .spec-label {{
            font-size: 0.76rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
        }}

        .spec-value {{
            font-size: 0.92rem;
            color: var(--text-primary);
        }}

        .desc-val {{
            font-weight: 600;
            color: #ffffff;
            line-height: 1.45;
        }}

        .url-val {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .link-url {{
            color: var(--accent-cyan);
            text-decoration: none;
            word-break: break-all;
            font-family: "JetBrains Mono", Consolas, monospace;
            font-size: 0.84rem;
        }}

        .link-url:hover {{
            text-decoration: underline;
            color: #7dd3fc;
        }}

        .exp-val {{
            color: var(--text-secondary);
        }}

        .actual-val {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 6px;
            font-weight: 600;
            font-size: 0.88rem;
            margin-top: 2px;
        }}

        .actual-status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
        }}

        .status-tint-passed {{
            background: rgba(34, 197, 94, 0.12);
            color: #4ade80;
            border: 1px solid rgba(34, 197, 94, 0.3);
        }}

        .status-tint-failed {{
            background: rgba(239, 68, 68, 0.12);
            color: #f87171;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }}

        .status-tint-skipped {{
            background: rgba(245, 158, 11, 0.12);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.3);
        }}

        .status-tint-pending {{
            background: rgba(148, 163, 184, 0.1);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.25);
        }}

        /* Code and Logs Containers */
        .details-lower-grid {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 18px;
        }}

        .section-title {{
            font-size: 0.88rem;
            font-weight: 700;
            color: var(--text-secondary);
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .section-badge {{
            font-size: 0.72rem;
            background: rgba(56, 189, 248, 0.1);
            color: var(--accent-cyan);
            padding: 2px 6px;
            border-radius: 4px;
        }}

        .code-container, .logs-container {{
            background: var(--bg-code);
            border: 1px solid var(--border-card);
            border-radius: 8px;
            overflow: hidden;
        }}

        .code-header, .logs-header {{
            background: #0f1626;
            padding: 8px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .code-lang-tag, .logs-tag {{
            font-size: 0.75rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
        }}

        .btn-copy, .btn-copy-mini {{
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--border-subtle);
            color: var(--text-secondary);
            padding: 3px 8px;
            border-radius: 5px;
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .btn-copy:hover, .btn-copy-mini:hover {{
            background: var(--bg-hover);
            color: #ffffff;
            border-color: var(--accent-cyan);
        }}

        .btn-copied {{
            background: rgba(34, 197, 94, 0.2) !important;
            color: var(--color-pass) !important;
            border-color: var(--color-pass) !important;
        }}

        .code-pre, .logs-pre {{
            padding: 14px 16px;
            font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
            font-size: 0.85rem;
            line-height: 1.55;
            color: #bae6fd;
            overflow-x: auto;
            white-space: pre-wrap;
            margin: 0;
        }}

        .logs-pre {{
            color: #e2e8f0;
            max-height: 220px;
            overflow-y: auto;
        }}

        .code-empty, .logs-empty, .screenshot-empty {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px dashed var(--border-subtle);
            border-radius: 8px;
            padding: 14px;
            color: var(--text-dim);
            font-size: 0.85rem;
            text-align: center;
        }}

        /* Screenshot Section */
        .screenshot-section {{
            margin-top: 10px;
        }}

        .screenshot-wrapper {{
            position: relative;
            display: inline-block;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-card);
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
            cursor: pointer;
            transition: all 0.2s ease;
        }}

        .screenshot-wrapper:hover {{
            transform: scale(1.01);
            border-color: var(--accent-cyan);
        }}

        .screenshot-thumb {{
            display: block;
            max-width: 100%;
            max-height: 340px;
            object-fit: cover;
            border-radius: 7px;
        }}

        .screenshot-hover-hint {{
            position: absolute;
            bottom: 8px;
            right: 8px;
            background: rgba(0, 0, 0, 0.75);
            color: #ffffff;
            font-size: 0.75rem;
            padding: 3px 8px;
            border-radius: 5px;
            pointer-events: none;
        }}

        /* Lightbox Modal */
        .lightbox-modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.88);
            backdrop-filter: blur(4px);
            z-index: 99999;
            align-items: center;
            justify-content: center;
            padding: 24px;
            animation: fadeIn 0.2s ease;
        }}

        @keyframes fadeIn {{
            from {{ opacity: 0; }}
            to {{ opacity: 1; }}
        }}

        .lightbox-content {{
            max-width: 92vw;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            background: var(--bg-surface);
            border: 1px solid var(--border-card);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 12px 40px rgba(0, 0, 0, 0.8);
        }}

        .lightbox-head {{
            padding: 12px 18px;
            background: #111a2e;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-subtle);
        }}

        .lightbox-title {{
            font-size: 0.95rem;
            font-weight: 700;
            color: #ffffff;
        }}

        .lightbox-close {{
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            padding: 0 4px;
            line-height: 1;
        }}

        .lightbox-close:hover {{ color: #ffffff; }}

        .lightbox-body {{
            padding: 12px;
            overflow: auto;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .lightbox-img {{
            max-width: 100%;
            max-height: 78vh;
            border-radius: 6px;
        }}

        /* Print media query */
        @media print {{
            body {{ background: #ffffff; color: #000000; padding: 0; }}
            .header-actions, .toolbar-card, .btn-expand, .btn-copy, .btn-copy-mini {{ display: none !important; }}
            .details-row {{ display: table-row !important; }}
            .details-card {{ background: #ffffff; color: #000000; border: 1px solid #ccc; }}
            table {{ border: 1px solid #ddd; }}
            thead th {{ background: #f0f0f0; color: #000000; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header class="header-bar">
            <div class="header-brand">
                <div class="brand-icon">🛡️</div>
                <div class="header-title">
                    <h1>{_escape(title)}</h1>
                    <div class="header-meta">
                        <span class="meta-tag">📅 {timestamp_str}</span>
                        <span class="meta-tag">🌐 Scope: {_escape(primary_target)}</span>
                        {health_badge}
                    </div>
                </div>
            </div>
            <div class="header-actions">
                <button class="btn-action" onclick="toggleAllDetails(true)">📂 Expand All</button>
                <button class="btn-action" onclick="toggleAllDetails(false)">📁 Collapse All</button>
                <button class="btn-action" onclick="window.print()">🖨️ Export PDF / Print</button>
            </div>
        </header>

        <!-- KPI Metrics Grid -->
        <div class="kpi-row">
            <div class="kpi-card">
                <div class="kpi-header">
                    <span class="kpi-label">Total Test Cases</span>
                    <span class="kpi-icon">📋</span>
                </div>
                <div class="kpi-value">{total_tests}</div>
                <div class="kpi-sub">{web_tests} Web • {api_tests} API</div>
            </div>
            <div class="kpi-card pass">
                <div class="kpi-header">
                    <span class="kpi-label">Passed Tests</span>
                    <span class="kpi-icon">✅</span>
                </div>
                <div class="kpi-value">{passed_tests}</div>
                <div class="kpi-sub">Verified clean behavior</div>
            </div>
            <div class="kpi-card fail">
                <div class="kpi-header">
                    <span class="kpi-label">Failed Tests</span>
                    <span class="kpi-icon">❌</span>
                </div>
                <div class="kpi-value">{failed_tests}</div>
                <div class="kpi-sub">Defects / regression detected</div>
            </div>
            <div class="kpi-card skip">
                <div class="kpi-header">
                    <span class="kpi-label">Skipped / Pending</span>
                    <span class="kpi-icon">⏳</span>
                </div>
                <div class="kpi-value">{skipped_tests + pending_tests}</div>
                <div class="kpi-sub">{skipped_tests} Skipped • {pending_tests} Pending</div>
            </div>
            <div class="kpi-card rate">
                <div class="kpi-header">
                    <span class="kpi-label">Success Rate</span>
                    <span class="kpi-icon">📈</span>
                </div>
                <div class="kpi-value">{pass_rate}%</div>
                <div class="kpi-sub">Across full test suite</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-header">
                    <span class="kpi-label">Execution Time</span>
                    <span class="kpi-icon">⏱️</span>
                </div>
                <div class="kpi-value">{total_dur_seconds:.1f}s</div>
                <div class="kpi-sub">Avg {avg_dur}s per test case</div>
            </div>
        </div>

        <!-- Filter & Search Toolbar -->
        <div class="toolbar-card">
            <div class="search-container">
                <span class="search-icon">🔍</span>
                <input type="text" id="searchInput" class="search-input" placeholder="Search test step, target URL, actual result, or automation script..." onkeyup="filterTable()"/>
            </div>
            <div class="filter-group">
                <button class="filter-btn active" data-status="all" onclick="setStatusFilter('all', this)">
                    All <span class="btn-badge">{total_tests}</span>
                </button>
                <button class="filter-btn" data-status="passed" onclick="setStatusFilter('passed', this)">
                    ✅ Passed <span class="btn-badge">{passed_tests}</span>
                </button>
                <button class="filter-btn" data-status="failed" onclick="setStatusFilter('failed', this)">
                    ❌ Failed <span class="btn-badge">{failed_tests}</span>
                </button>
                <button class="filter-btn" data-status="skipped" onclick="setStatusFilter('skipped', this)">
                    ⏭️ Skipped <span class="btn-badge">{skipped_tests}</span>
                </button>
            </div>
            <div class="filter-group">
                <button class="filter-btn active" data-type="all" onclick="setTypeFilter('all', this)">All Types</button>
                <button class="filter-btn" data-type="web" onclick="setTypeFilter('web', this)">🌐 Web ({web_tests})</button>
                <button class="filter-btn" data-type="api" onclick="setTypeFilter('api', this)">⚡ API ({api_tests})</button>
            </div>
        </div>

        <!-- Main Test Table -->
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th class="col-id">#</th>
                        <th class="col-type">Type</th>
                        <th class="col-status">Status</th>
                        <th class="col-info">Test Step Description & Target</th>
                        <th class="col-dur">Duration</th>
                        <th class="col-action">Action</th>
                    </tr>
                </thead>
                <tbody id="testTableBody">
                    {table_body}
                </tbody>
            </table>
        </div>
    </div>

    <!-- Fullscreen Screenshot Lightbox Modal -->
    <div id="lightboxModal" class="lightbox-modal" onclick="closeLightbox(event)">
        <div class="lightbox-content" onclick="event.stopPropagation()">
            <div class="lightbox-head">
                <span id="lightboxTitle" class="lightbox-title">Visual Verification Frame</span>
                <button class="lightbox-close" onclick="closeLightbox()">&times;</button>
            </div>
            <div class="lightbox-body">
                <img id="lightboxImg" src="" alt="Full Resolution Screenshot" class="lightbox-img"/>
            </div>
        </div>
    </div>

    <script>
        let activeStatusFilter = 'all';
        let activeTypeFilter = 'all';

        function toggleDetails(id, rowElem) {{
            const detailsRow = document.getElementById(id);
            if (!detailsRow) return;
            const isHidden = detailsRow.style.display === 'none';
            detailsRow.style.display = isHidden ? 'table-row' : 'none';
            
            // Toggle chevron rotation
            if (rowElem) {{
                const chevron = rowElem.querySelector('.expand-chevron');
                if (chevron) {{
                    chevron.style.transform = isHidden ? 'rotate(180deg)' : 'rotate(0deg)';
                }}
            }}
        }}

        function toggleAllDetails(expand) {{
            const detailsRows = document.querySelectorAll('.details-row');
            const chevrons = document.querySelectorAll('.expand-chevron');
            detailsRows.forEach(r => {{
                // Only toggle if parent test-row is currently visible
                const parentTestRow = r.previousElementSibling;
                if (!parentTestRow || parentTestRow.style.display !== 'none') {{
                    r.style.display = expand ? 'table-row' : 'none';
                }}
            }});
            chevrons.forEach(c => {{
                c.style.transform = expand ? 'rotate(180deg)' : 'rotate(0deg)';
            }});
        }}

        function setStatusFilter(status, btn) {{
            activeStatusFilter = status;
            document.querySelectorAll('[data-status]').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            filterTable();
        }}

        function setTypeFilter(t, btn) {{
            activeTypeFilter = t;
            document.querySelectorAll('[data-type]').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            filterTable();
        }}

        function filterTable() {{
            const query = (document.getElementById('searchInput').value || '').toLowerCase().trim();
            const rows = document.querySelectorAll('#testTableBody tr.test-row');

            rows.forEach(row => {{
                const text = row.innerText.toLowerCase();
                const matchesQuery = !query || text.includes(query);
                
                const hasStatus = activeStatusFilter === 'all' || row.classList.contains('status-' + activeStatusFilter);
                const hasType = activeTypeFilter === 'all' || row.classList.contains('type-' + activeTypeFilter);

                const nextRow = row.nextElementSibling;
                if (matchesQuery && hasStatus && hasType) {{
                    row.style.display = 'table-row';
                }} else {{
                    row.style.display = 'none';
                    if (nextRow && nextRow.classList.contains('details-row')) {{
                        nextRow.style.display = 'none';
                    }}
                }}
            }});
        }}

        function copyText(txt, btn) {{
            if (!txt) return;
            navigator.clipboard.writeText(txt).then(() => {{
                const orig = btn.innerHTML;
                btn.innerHTML = '✓ Copied!';
                btn.classList.add('btn-copied');
                setTimeout(() => {{
                    btn.innerHTML = orig;
                    btn.classList.remove('btn-copied');
                }}, 1800);
            }}).catch(() => {{
                btn.innerHTML = '✓ Copied';
                setTimeout(() => {{ btn.innerHTML = '📋'; }}, 1500);
            }});
        }}

        function copyCode(btn) {{
            const container = btn.closest('.code-container');
            const codeElem = container ? container.querySelector('code') : null;
            if (codeElem) {{
                copyText(codeElem.innerText, btn);
            }}
        }}

        function copyLogs(btn) {{
            const container = btn.closest('.logs-container');
            const logsElem = container ? container.querySelector('code') : null;
            if (logsElem) {{
                copyText(logsElem.innerText, btn);
            }}
        }}

        function openLightbox(src, title) {{
            document.getElementById('lightboxImg').src = src;
            document.getElementById('lightboxTitle').innerText = title || 'Visual Verification Frame';
            document.getElementById('lightboxModal').style.display = 'flex';
        }}

        function closeLightbox(e) {{
            if (!e || e.target.id === 'lightboxModal' || e.target.classList.contains('lightbox-close')) {{
                document.getElementById('lightboxModal').style.display = 'none';
                document.getElementById('lightboxImg').src = '';
            }}
        }}

        document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') {{
                closeLightbox();
            }}
        }});
    </script>
</body>
</html>
"""
    return html_content
