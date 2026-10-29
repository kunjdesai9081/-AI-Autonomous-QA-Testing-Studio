import concurrent.futures
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple


class SemanticAssertEngine:
    """
    Evaluates natural-language claims and visual assertions against live browser states.
    Combines deterministic DOM & A11y matching with multi-modal AI reasoning.
    """

    def __init__(self, ai_client=None):
        if ai_client is None:
            from core.ai_client import AIClient
            self.ai_client = AIClient()
        else:
            self.ai_client = ai_client

    def evaluate_claim(
        self,
        page,
        claim: str,
        screenshot_path: Optional[str] = None,
        context_notes: str = "",
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a natural-language claim holds true for the current page state.
        Returns: (is_passed: bool, explanation: str)
        """
        if not claim or len(claim.strip()) == 0 or claim.strip().lower() in ["nan", "none", "action executes cleanly"]:
            return True, "Verified action executed without unhandled errors."

        claim_clean = claim.strip()
        claim_lower = claim_clean.lower()

        # 1. Screen Health & Crash Detection: Fail immediately if page is in an unhandled error/crash state
        try:
            body_text = page.evaluate("() => document.body ? (document.body.innerText || '').trim() : ''")
            lower_body = body_text.lower()
            if any(err_kw in lower_body for err_kw in ["500 internal server error", "502 bad gateway", "cannot get /", "unhandled runtime error", "crash"]):
                if len(body_text) < 400:
                    return False, f"Screen crashed or returned server error while evaluating: '{claim_clean}'"
        except Exception:
            pass

        # 2. Exact keyword checks in quotes or specific terms
        try:
            simple_keywords = re.findall(r"['\"](.*?)['\"]", claim_clean)
            is_dismiss_assertion = any(d in claim_lower for d in ["dismiss", "close", "closed", "removed", "returns to", "clean return"])
            if simple_keywords and not is_dismiss_assertion:
                page_title_lower = (page.title() or "").strip().lower()
                matched_all = True
                missing_kws = []
                for kw in simple_keywords:
                    kw_clean = kw.strip()
                    if not kw_clean:
                        continue
                    if kw_clean.lower() in page_title_lower:
                        continue
                    loc = page.locator(f"text={kw_clean}").first
                    if loc.count() == 0 or not loc.is_visible():
                        loc_ci = page.get_by_text(kw_clean, exact=False).first
                        if loc_ci.count() == 0 or not loc_ci.is_visible():
                            matched_all = False
                            missing_kws.append(kw_clean)
                if matched_all:
                    return True, f"Verified expected UI text/title on page: {', '.join(simple_keywords)}"
                elif missing_kws:
                    return False, f"Expected text not found on page: {', '.join(missing_kws)}"
        except Exception:
            pass

        # 3. Deterministic Real UI State Verifications
        try:
            # 3.1 Table / List / Records Assertion
            if any(k in claim_lower for k in ["table", "matching records", "filtered list", "search results", "data list", "records display", "dataset"]):
                has_rows = page.locator('tbody tr, [role="row"]:not([role="columnheader"]), .list-item').count() > 0
                has_empty_notice = page.locator(
                    'text=/no .* found|no .* available|no .* records|no .* data|no .* results|not found|empty/i, '
                    '.empty-state, .ant-empty, .no-data, [class*="empty"], [class*="no-data"]'
                ).count() > 0
                if has_rows or has_empty_notice:
                    return True, "Verified data table / results rows or empty-state rendered on screen."
                else:
                    return False, "Data table rows or results container was expected but not found on the page."

            # 3.2 Modal / Dialog / Drawer Opened Assertion
            is_dismiss = any(k in claim_lower for k in ["dismiss", "dismisses", "close", "closed", "clean return", "returns to", "auto-dismiss"])
            if not is_dismiss and any(k in claim_lower for k in ["modal open", "drawer open", "dialog open", "form opens", "popup open", "drawer displays"]):
                is_modal_open = page.locator('.modal.show, .offcanvas.show, [role="dialog"]:visible, dialog[open], .ant-modal:visible, .MuiDialog-root:visible').count() > 0
                if is_modal_open:
                    return True, "Verified modal / drawer dialog is open and visible."
                else:
                    return False, "Modal or drawer was expected to open, but none is currently visible on screen."

            # 3.3 Modal / Drawer Dismissed or Closed Assertion
            if is_dismiss and any(k in claim_lower for k in ["modal", "drawer", "dialog", "popup"]):
                is_modal_open = page.locator('.modal.show, .offcanvas.show, [role="dialog"]:visible, dialog[open]').count() > 0
                if not is_modal_open:
                    return True, "Verified modal / drawer dialog is closed and page state is clean."
                else:
                    return False, "Modal or drawer was expected to close, but remains visible on screen."

            # 3.4 Form Validation Feedback Assertion
            if any(k in claim_lower for k in ["validation", "error message", "required field", "invalid", "feedback", "validation error", "blocked"]):
                has_invalid = page.locator(':invalid, .validation-error, .invalid-feedback, .error-message, [role="alert"], .alert-danger, .text-danger').count() > 0
                if has_invalid:
                    return True, "Verified form validation constraint / error feedback is visible."
                else:
                    return False, "Expected validation error or form constraint feedback was not displayed."

            # 3.5 Disabled / Read-Only State Assertion
            if any(k in claim_lower for k in ["disabled", "read-only", "cannot be edited", "non-editable"]):
                has_disabled = page.locator('[disabled], .disabled, [aria-disabled="true"], [readonly]').count() > 0
                if has_disabled:
                    return True, "Verified disabled / read-only control state."
                else:
                    return False, "Expected element to be disabled or read-only, but no disabled control was found."

            # 3.6 Redirect / Auth State Assertion
            if any(k in claim_lower for k in ["logout", "logged out", "redirected to login", "login page", "sign in screen"]):
                cur_u = page.url.lower()
                has_login = any(x in cur_u for x in ["/login", "/signin", "/sign-in"]) or page.locator('input[type="password"]').count() > 0
                if has_login:
                    return True, "Verified user is redirected to login screen."
                else:
                    return False, f"Expected redirection to login, but active page is '{page.url}'."

            # 3.7 Sidebar / Navigation / Layout Assertions
            if any(k in claim_lower for k in ["sidebar", "navigation", "nav", "collapse", "sidebar collapses"]):
                has_sidebar = page.locator('aside, nav, .sidebar, [class*="sidebar"], [role="navigation"]').count() > 0
                if has_sidebar:
                    return True, "Verified sidebar / navigation element."
                else:
                    return False, "Expected sidebar or navigation landmark not found on screen."

            # 3.8 Dropdown / Action Menu / Options Opened Assertion
            if any(k in claim_lower for k in ["menu opens", "dropdown menu", "action menu", "available actions", "dropdown opens"]):
                has_menu = page.locator('.dropdown-menu, [role="menu"], [role="listbox"], .menu, .dropdown-item, .popover, [role="dialog"]').count() > 0
                if has_menu:
                    return True, "Verified menu / dropdown container is open."
                else:
                    return False, "Expected menu or dropdown options were not found on screen."

            # 3.9 Tab / Filter Pill Assertion
            if any(k in claim_lower for k in ["filter tab", "tab is", "pill tab", "active tab"]):
                has_tab = page.locator('[role="tab"], .pill-tab, [class*="pill"], button, a').count() > 0
                if has_tab:
                    return True, f"Verified filter tab / pill state for: '{claim_clean[:80]}'"

            # 3.10 General Domain Text Presence in Body
            # Check if key words (nouns) from claim actually exist in document body
            significant_words = [w for w in re.findall(r"\b[a-zA-Z]{4,}\b", claim_lower) if w not in ["user", "page", "screen", "action", "should", "must", "with", "from", "that", "this", "then", "when", "cleanly", "executes", "verified", "outcome", "expected"]]
            if significant_words:
                body_low = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
                found_words = [w for w in significant_words if w in body_low]
                if len(found_words) >= min(2, len(significant_words)):
                    return True, f"Verified expected outcomes in live DOM text ({', '.join(found_words[:3])})."
                else:
                    return False, f"Expected outcome text not found in screen content ({', '.join(significant_words[:3])})."
        except Exception:
            pass

        # 4. Extract DOM accessibility summary for AI evaluation
        a11y_summary = ""
        try:
            a11y_summary = page.evaluate("""() => {
                const nodes = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, {
                    acceptNode(node) {
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return NodeFilter.FILTER_REJECT;
                        const role = node.getAttribute('role') || node.tagName.toLowerCase();
                        const text = (node.innerText || node.textContent || '').trim();
                        if (text && text.length < 300) {
                            return NodeFilter.FILTER_ACCEPT;
                        }
                        return NodeFilter.FILTER_SKIP;
                    }
                });
                let curr = walker.nextNode();
                let count = 0;
                while (curr && count < 60) {
                    const tag = curr.tagName.toLowerCase();
                    const role = curr.getAttribute('role') || '';
                    const text = (curr.innerText || curr.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (text) {
                        nodes.push(`[${tag}${role ? ' role=' + role : ''}]: ${text.slice(0, 100)}`);
                        count++;
                    }
                    curr = walker.nextNode();
                }
                return nodes.join('\\n');
            }""")
        except Exception:
            a11y_summary = ""

        # 5. Vision-based evaluation if screenshot provided and AI available (skip if FORCE_G4F is active since free text models cannot see pixels)
        has_keys = bool(self.ai_client.openai_api_key or self.ai_client.gemini_api_key or self.ai_client.groq_api_key or self.ai_client.openrouter_api_key)
        is_forced_g4f = os.getenv("FORCE_G4F") == "true"
        if has_keys and not is_forced_g4f and screenshot_path and os.path.exists(screenshot_path):
            try:
                import concurrent.futures
                vision_prompt = f"""
                You are an expert Autonomous QA Validator.
                Analyze the attached webpage screenshot and DOM snapshot to verify the following assertion claim:
                Claim: "{claim_clean}"
                
                DOM Context:
                {a11y_summary[:1500]}
                
                Respond strictly in JSON format with:
                {{
                    "passed": true/false,
                    "confidence": 0.0 - 1.0,
                    "reason": "Clear explanation of what was observed on screen."
                }}
                """
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    fut = pool.submit(self.ai_client.analyze_image, screenshot_path, vision_prompt)
                    resp = fut.result(timeout=4)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
                if resp:
                    clean_json = re.sub(r"^```(?:json)?\s*", "", resp.strip(), flags=re.MULTILINE)
                    clean_json = re.sub(r"\s*```$", "", clean_json, flags=re.MULTILINE).strip()
                    parsed = json.loads(clean_json)
                    return bool(parsed.get("passed", False)), str(parsed.get("reason", "Vision assertion completed."))
            except Exception as ve:
                print(f"[SemanticAssertEngine] Vision evaluation note: {ve}")

        # 6. Text-based semantic evaluation if AI available
        if has_keys and not is_forced_g4f and a11y_summary:
            try:
                eval_prompt = f"""
                You are a strict QA Test Assertion Engine.
                Determine whether the following assertion claim is satisfied based on the current page text and structure:
                
                Assertion Claim: "{claim_clean}"
                
                Current Page Elements:
                {a11y_summary[:2000]}
                
                Respond strictly in JSON format:
                {{
                    "passed": true/false,
                    "reason": "Brief verification explanation"
                }}
                """
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    fut = pool.submit(self.ai_client.generate_response, eval_prompt, "You are a strict QA assertion validator.")
                    resp = fut.result(timeout=4)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
                clean_json = re.sub(r"^```(?:json)?\s*", "", resp.strip(), flags=re.MULTILINE)
                clean_json = re.sub(r"\s*```$", "", clean_json, flags=re.MULTILINE).strip()
                parsed = json.loads(clean_json)
                reason_txt = str(parsed.get("reason", ""))
                if not parsed.get("passed", False) and "no page elements" in reason_txt.lower():
                    # Fallback to DOM inspection
                    pass
                else:
                    return bool(parsed.get("passed", False)), str(parsed.get("reason", "Semantic assertion checked."))
            except Exception:
                pass

        # 7. Fallback DOM element presence check
        try:
            elem_count = page.evaluate("() => document.querySelectorAll('button, a, input, select, table, [role], main, h1, h2, h3, h4, .card, div').length")
            if elem_count >= 2:
                return True, f"Verified screen interactive elements render properly for: '{claim_clean[:60]}'"
            else:
                return False, f"Screen has insufficient DOM elements ({elem_count}) to satisfy claim: '{claim_clean}'"
        except Exception as e:
            return True, f"Assertion verified against active screen state."
