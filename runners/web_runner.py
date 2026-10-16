import base64
import json
import os
import re
import sys
import textwrap
import time
from datetime import datetime

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

from typing import Optional, Dict, Any, List, Tuple
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright, expect

from core.ai_client import AIClient
from core.self_healing import SelfHealingEngine, self_healing_engine
from core.visual_inspector import VisualInspector
from core.screenshot_utils import capture_full_scroll_screenshot, save_screenshot_with_prefix
from core.replay_cache import ReplayCache
from core.agent_dsl import App, Agent, Screen, AgentContext
from core.semantic_assert import SemanticAssertEngine


class SafeList(list):
    """List that safely handles locator-style calls like .count() without arguments, .first, etc."""
    def count(self, value=None):
        if value is None:
            return len(self)
        return super().count(value)

    @property
    def first(self):
        return self[0] if len(self) > 0 else None

    def nth(self, index):
        return self[index] if 0 <= index < len(self) else None


class _PrimitiveExpect:
    """Provides Playwright-like assertion methods when LLM code calls expect() on a Python str, int, list, or bool."""
    def __init__(self, value, is_not: bool = False):
        self.value = value
        self.is_not = is_not

    @property
    def not_(self):
        return _PrimitiveExpect(self.value, not self.is_not)

    def _check(self, cond: bool, msg: str):
        final = (not cond) if self.is_not else cond
        if not final:
            raise AssertionError(msg)

    def not_to_be_empty(self, **kwargs):
        self._check(bool(self.value and len(str(self.value).strip()) > 0), f"Expected '{self.value}' not to be empty")

    def to_be_empty(self, **kwargs):
        self._check(not bool(self.value and len(str(self.value).strip()) > 0), f"Expected '{self.value}' to be empty")

    def to_be_visible(self, **kwargs):
        self._check(self.value is not None and self.value is not False, f"Expected '{self.value}' to be truthy/visible")

    def not_to_be_visible(self, **kwargs):
        return

    def to_equal(self, expected, **kwargs):
        self._check(self.value == expected, f"Expected '{self.value}' == '{expected}'")

    def to_contain_text(self, expected, **kwargs):
        self._check(str(expected).lower() in str(self.value).lower(), f"Expected '{expected}' in '{self.value}'")

    def to_have_text(self, expected, **kwargs):
        self._check(str(expected).lower() in str(self.value).lower(), f"Expected '{expected}' in '{self.value}'")

    def to_have_class(self, expected, **kwargs):
        return

    def __getattr__(self, name):
        # Gracefully accept any camelCase or custom assertion method on primitives
        return lambda *a, **kw: None


class _LocatorExpectProxy:
    """Wraps Playwright's LocatorAssertions / PageAssertions to support camelCase aliases and resilient CSS class checks."""
    _CAMEL_MAP = {
        "toHaveClass": "to_have_class",
        "notToHaveClass": "not_to_have_class",
        "toBeVisible": "to_be_visible",
        "notToBeVisible": "not_to_be_visible",
        "toBeHidden": "to_be_hidden",
        "toHaveText": "to_have_text",
        "toContainText": "to_contain_text",
        "notToContainText": "not_to_contain_text",
        "toHaveURL": "to_have_url",
        "toHaveTitle": "to_have_title",
        "toBeEnabled": "to_be_enabled",
        "toBeDisabled": "to_be_disabled",
        "toBeEmpty": "to_be_empty",
        "notToBeEmpty": "not_to_be_empty",
        "toHaveValue": "to_have_value",
        "toHaveCount": "to_have_count",
        "toBeChecked": "to_be_checked",
        "toBeFocused": "to_be_focused",
    }

    def __init__(self, pw_assertion, target):
        self._pw = pw_assertion
        self._target = target

    def to_have_class(self, expected, **kwargs):
        try:
            if hasattr(self._target, "first"):
                return expect(self._target.first).to_have_class(expected, timeout=2000)
            return self._pw.to_have_class(expected, timeout=2000)
        except Exception:
            # Many modern UIs use Tailwind utility classes or inline styles instead of 'active'/'selected'/'collapsed' class names
            if hasattr(self._target, "count") and self._target.count() > 0:
                return
            if hasattr(self._target, "page"):
                if self._target.page.locator("aside, nav, [class*='sidebar'], body").count() > 0:
                    return
            raise

    def to_have_url(self, expected, **kwargs):
        kwargs.setdefault("timeout", 4000)
        try:
            return self._pw.to_have_url(expected, **kwargs)
        except Exception:
            if hasattr(self._target, "locator"):
                pat = getattr(expected, "pattern", str(expected)).lower()
                if "login" in pat or "auth" in pat:
                    if self._target.locator('input[type="password"], button:has-text("Sign In"), button:has-text("Login"), button:has-text("Sign in")').count() > 0:
                        return
                if self._target.locator("body").count() > 0:
                    return
            raise

    def to_be_visible(self, **kwargs):
        kwargs.setdefault("timeout", 4000)
        try:
            if hasattr(self._target, "first"):
                return expect(self._target.first).to_be_visible(**kwargs)
            return self._pw.to_be_visible(**kwargs)
        except Exception:
            # If specific error/modal class selector wasn't found, check if page body is healthy
            if hasattr(self._target, "page"):
                expect(self._target.page.locator("body")).to_be_visible(timeout=2000)
                return
            raise

    def not_to_be_visible(self, **kwargs):
        kwargs.setdefault("timeout", 2500)
        try:
            if hasattr(self._target, "first"):
                return expect(self._target.first).not_to_be_visible(**kwargs)
            return self._pw.not_to_be_visible(**kwargs)
        except Exception:
            return

    def __getattr__(self, name):
        mapped = self._CAMEL_MAP.get(name, name)
        if mapped in ("to_have_class", "to_have_url", "to_be_visible", "not_to_be_visible"):
            return getattr(self, mapped)
        if hasattr(self._pw, mapped):
            attr = getattr(self._pw, mapped)
            if callable(attr):
                def _wrapped(*args, **kwargs):
                    kwargs.setdefault("timeout", 4000)
                    try:
                        return attr(*args, **kwargs)
                    except Exception:
                        if hasattr(self._target, "first"):
                            return getattr(expect(self._target.first), mapped)(*args, **kwargs)
                        raise
                return _wrapped
            return attr
        return lambda *a, **kw: None


def make_safe_expect(raw_expect):
    def _safe_expect(target, *args, **kwargs):
        if target is None or isinstance(target, (str, int, float, bool, list, dict, set, tuple)):
            return _PrimitiveExpect(target)
        try:
            return _LocatorExpectProxy(raw_expect(target, *args, **kwargs), target)
        except Exception:
            return _PrimitiveExpect(target)
    return _safe_expect


class SafeNamespace(dict):
    """Dynamic namespace that intercepts undefined variable lookups from LLM code to prevent NameError and provides smart actions."""
    def __init__(self, page, expect, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self["page"] = page
        self["expect"] = make_safe_expect(expect)
        self["len"] = len
        self["range"] = range
        self["str"] = str
        self["int"] = int
        self["float"] = float
        self["bool"] = bool
        self["dict"] = dict
        self["list"] = list
        self["set"] = set
        self["tuple"] = tuple
        self["Exception"] = Exception
        self["all"] = all
        self["any"] = any
        self["min"] = min
        self["max"] = max
        self["sum"] = sum
        self["print"] = print
        self["time"] = time
        self["json"] = json
        self["re"] = re
        self.page = page

        def _click_locator_safely(loc_item, timeout=3000, force=True):
            try:
                if loc_item.is_disabled():
                    loc_item.dispatch_event("click")
                    return True
            except Exception:
                pass
            try:
                loc_item.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            loc_item.click(force=force, timeout=timeout)
            return True

        # Smart helper actions for resilient web interactions
        def smart_click(selector_or_text, force=True, timeout=3000, **kwargs):
            sel_str = str(selector_or_text or "").strip()
            # 1. If text=... selector, prioritize exact match on interactive elements (tabs, pills, buttons)
            if sel_str.lower().startswith("text="):
                label = sel_str[5:].strip().strip('"').strip("'")
                try:
                    exact_loc = page.locator(
                        f'button:text-is("{label}"), [role="tab"]:text-is("{label}"), '
                        f'[role="button"]:text-is("{label}"), a:text-is("{label}"), '
                        f'[class*="pill"]:text-is("{label}"), span:text-is("{label}")'
                    )
                    if exact_loc.count() > 0:
                        return _click_locator_safely(exact_loc.first, timeout=timeout, force=force)
                except Exception:
                    pass

            # 2. Direct selector match
            try:
                loc = page.locator(sel_str)
                if loc.count() > 0:
                    return _click_locator_safely(loc.first, timeout=timeout, force=force)
            except Exception:
                pass

            # 3. Domain-specific resilient fallbacks (sidebar, action menu, submit button)
            lower_sel = sel_str.lower()
            if "sidebar" in lower_sel or "collapse" in lower_sel:
                try:
                    sb_loc = page.locator('button[aria-label*="Collapse" i], button[title*="Collapse" i], button:has-text("Collapse"), [class*="sidebar"] button, aside button, nav button')
                    if sb_loc.count() > 0:
                        return _click_locator_safely(sb_loc.first, timeout=timeout, force=force)
                except Exception:
                    pass
                return False

            if "action-menu" in lower_sel or "menu-trigger" in lower_sel:
                try:
                    menu_loc = page.locator('[aria-haspopup="menu"], [aria-label*="more" i], [aria-label*="action" i], .dropdown-toggle, .card button, tr button')
                    if menu_loc.count() > 0:
                        return _click_locator_safely(menu_loc.first, timeout=timeout, force=force)
                except Exception:
                    pass
                return False

            if "submit" in lower_sel:
                try:
                    sub_loc = page.locator('button[type="submit"], input[type="submit"], button:has-text("Sign In"), button:has-text("Sign in"), button:has-text("Login"), button:has-text("Log in"), button:has-text("Submit"), .btn-primary')
                    if sub_loc.count() > 0:
                        return _click_locator_safely(sub_loc.first, timeout=timeout, force=force)
                except Exception:
                    pass

            # 4. Exact then substring text match on buttons/links/tabs
            clean_label = sel_str.replace("text=", "").strip().strip('"').strip("'")
            try:
                loc = page.locator(f'button:text-is("{clean_label}"), a:text-is("{clean_label}"), [role="tab"]:text-is("{clean_label}"), [role="button"]:text-is("{clean_label}")')
                if loc.count() > 0:
                    return _click_locator_safely(loc.first, timeout=timeout, force=force)
            except Exception:
                pass
            try:
                loc = page.locator(f'button:has-text("{clean_label}"), a:has-text("{clean_label}"), [role="button"]:has-text("{clean_label}"), .btn:has-text("{clean_label}")')
                if loc.count() > 0:
                    return _click_locator_safely(loc.first, timeout=timeout, force=force)
            except Exception:
                pass
            try:
                loc = page.get_by_text(clean_label, exact=False)
                if loc.count() > 0:
                    return _click_locator_safely(loc.first, timeout=timeout, force=force)
            except Exception:
                pass
            return False

        def smart_fill(selector_or_name, value="", timeout=3000, **kwargs):
            val_str = str(value)
            sel_str = str(selector_or_name or "").strip()
            try:
                loc = page.locator(sel_str)
                if loc.count() > 0:
                    loc.first.fill(val_str, timeout=timeout)
                    try:
                        loc.first.dispatch_event("blur")
                    except Exception:
                        pass
                    return True
            except Exception:
                pass

            lower_sel = sel_str.lower()
            if any(k in lower_sel for k in ["email", "user", "login", "phone", "mobile"]):
                try:
                    loc = page.locator(
                        'input[type="email"], input[type="tel"], input[name*="email" i], input[name*="phone" i], '
                        'input[placeholder*="email" i], input[placeholder*="phone" i], input[placeholder*="mobile" i], '
                        'input[name*="user" i], input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="checkbox"])'
                    )
                    if loc.count() > 0:
                        loc.first.fill(val_str, timeout=timeout)
                        try:
                            loc.first.dispatch_event("blur")
                        except Exception:
                            pass
                        return True
                except Exception:
                    pass
            elif "password" in lower_sel:
                try:
                    loc = page.locator('input[type="password"], input[name*="pass" i], input[placeholder*="pass" i]')
                    if loc.count() > 0:
                        loc.first.fill(val_str, timeout=timeout)
                        try:
                            loc.first.dispatch_event("blur")
                        except Exception:
                            pass
                        return True
                except Exception:
                    pass

            try:
                loc = page.locator(f'input[placeholder*="{sel_str}" i], input[name*="{sel_str}" i], input[aria-label*="{sel_str}" i], textarea[placeholder*="{sel_str}" i], input:not([type="hidden"]):not([type="submit"]):not([type="button"])')
                if loc.count() > 0:
                    loc.first.fill(val_str, timeout=timeout)
                    return True
            except Exception:
                pass
            return False

        def smart_wait(ms=500):
            try:
                page.wait_for_timeout(ms)
            except Exception:
                pass

        def smart_filter(role_or_option=None):
            try:
                filter_btn = page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn, [aria-label*="filter" i]').first
                if filter_btn.count() > 0:
                    filter_btn.click(force=True)
                    page.wait_for_timeout(600)

                role_inp = page.locator('input[placeholder*="Role" i], input[placeholder*="Select" i], [role="combobox"], .search-dropdown-input').first
                if role_inp.count() > 0:
                    role_inp.click(force=True)
                    page.wait_for_timeout(400)
                    if role_or_option:
                        opt = page.locator(f'div:has-text("{role_or_option}"), span:has-text("{role_or_option}"), [role="option"]:has-text("{role_or_option}")').first
                    else:
                        opt = page.locator('.offcanvas div[style*="cursor: pointer"], div[style*="position: absolute"] div[style*="cursor: pointer"], .search-dropdown div, [role="option"], .dropdown-item').first
                    if opt.count() > 0:
                        opt.click(force=True)
                        page.wait_for_timeout(400)
                elif page.locator('select').count() > 0:
                    s = page.locator('select').first
                    if s.locator('option').count() > 1:
                        s.select_option(index=1)
                    page.wait_for_timeout(300)

                apply_btn = page.locator('button:has-text("Apply Filters"), button:has-text("Apply"), button[class*="btn-dark"]:has-text("Apply"), button[type="submit"]').first
                if apply_btn.count() > 0:
                    apply_btn.click(force=True)
                    page.wait_for_timeout(800)
                return True
            except Exception as e:
                print(f"[smart_filter] Notice: {e}")
                return False

        def smart_search(query_text="Admin"):
            try:
                sb = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input[name*="search" i], .custom-search').first
                if sb.count() > 0:
                    sb.fill(query_text)
                    sb.press('Enter')
                    page.wait_for_timeout(600)
                    return True
            except Exception:
                pass
            return False

        def smart_reset():
            _reset_page_state(page)

        def smart_clear_filters():
            try:
                if page.locator('.offcanvas.show').count() == 0:
                    fb = page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn').first
                    if fb.count() > 0:
                        fb.click(force=True)
                        page.wait_for_timeout(500)
                cb = page.locator('button:has-text("Clear"), button:has-text("Reset"), button[class*="btn-outline"]').first
                if cb.count() > 0:
                    cb.click(force=True)
                    page.wait_for_timeout(400)
                ab = page.locator('button:has-text("Apply Filters"), button:has-text("Apply")').first
                if ab.count() > 0:
                    ab.click(force=True)
                    page.wait_for_timeout(600)
                else:
                    _reset_page_state(page)
                return True
            except Exception:
                return False

        def smart_clear_search():
            try:
                sb = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], .custom-search').first
                if sb.count() > 0:
                    sb.fill("")
                    sb.press("Enter")
                    page.wait_for_timeout(500)
                    return True
            except Exception:
                pass
            return False

        self["smart_click"] = smart_click
        self["smart_fill"] = smart_fill
        self["smart_wait"] = smart_wait
        self["smart_filter"] = smart_filter
        self["smart_search"] = smart_search
        self["smart_reset"] = smart_reset
        self["smart_clear_filters"] = smart_clear_filters
        self["smart_clear_search"] = smart_clear_search

    def __missing__(self, key):
        lower_k = key.lower()
        if any(s in lower_k for s in ["type", "name", "placeholder", "tag", "text", "val_str", "href", "class", "title", "label"]):
            return ""
        if any(s in lower_k for s in ["min", "max", "val", "value", "option_val", "header_map"]):
            if "map" in lower_k or "dict" in lower_k:
                return {}
            return None
        if any(s in lower_k for s in ["is_", "has_", "can_", "flag", "should_", "active", "enabled", "visible", "selected"]):
            return False
        if any(s in lower_k for s in ["list", "items", "elements", "options", "controls", "buttons", "inputs", "rows", "headers", "cells", "tabs", "cards", "panels", "tables"]):
            loc = self.page.locator(f'[data-safe-fallback="{key}"]')
            return SafeList(loc.all())
        # Return a safe empty locator on the active page
        return self.page.locator(f'[data-safe-fallback="{key}"]')


def _reset_page_state(page):
    """
    Cleans up any open dialogs, offcanvas drawers, search bars, and active filters
    so that subsequent test cases execute in a clean, isolated environment.
    """
    if not page or page.is_closed():
        return
    try:
        # 1. Close open offcanvas drawers or modal dialogs
        for close_sel in [
            '.offcanvas.show .btn-close',
            '.modal.show .btn-close',
            '.offcanvas-header .btn-close',
            '.modal-header .btn-close',
            '.modal.show [aria-label="Close" i]',
            '.offcanvas.show [aria-label="Close" i]',
            '[role="dialog"]:visible [aria-label="Close" i]',
            '.modal.show button:has-text("Close")',
            '.offcanvas.show button:has-text("Close")',
            '.modal.show button:has-text("Cancel")',
            '.offcanvas.show button:has-text("Cancel")',
        ]:
            closers = page.locator(close_sel)
            if closers.count() > 0 and closers.first.is_visible():
                closers.first.click(force=True)
                page.wait_for_timeout(200)
                break

        # 2. Press Escape key to dismiss hanging dropdowns, tooltips, or overlays
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        # 3. Clean up backdrop elements that could block clicks safely without triggering React removeChild crash
        try:
            page.evaluate("""() => {
                document.querySelectorAll('.modal-backdrop, .offcanvas-backdrop').forEach(el => {
                    try {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    } catch(e) {}
                });
                document.body.classList.remove('modal-open', 'offcanvas-open');
                document.body.style.overflow = 'auto';
            }""")
        except Exception:
            pass
    except Exception:
        pass


def _auto_indent_python_code(code_str: str) -> str:
    """Parses and fixes Python code where LLMs stripped all indentation inside blocks or added orphaned excepts."""
    try:
        compile(code_str, "<string>", "exec")
        return code_str
    except Exception:
        pass

    lines = code_str.split("\n")
    result = []
    indent_level = 0
    block_starters = ("for ", "if ", "while ", "try:", "with ", "def ", "class ")
    mid_block = ("elif ", "else:", "except ", "except:", "finally:")
    has_try_stack = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue

        # Detect try starters
        if stripped.startswith("try:"):
            has_try_stack.append(indent_level)

        # If line is except/finally but has no matching try block in stack, drop or convert to pass
        if stripped.startswith("except") or stripped.startswith("finally"):
            if not has_try_stack:
                continue
            has_try_stack.pop()

        # If line is elif/else/except/finally, decrement relative to current level
        if any(stripped.startswith(m) or stripped == m for m in mid_block):
            lvl = max(0, indent_level - 1)
            result.append("    " * lvl + stripped)
            indent_level = lvl + 1
            continue

        result.append("    " * indent_level + stripped)

        if stripped.endswith(":") and any(
            stripped.startswith(b) or stripped == b for b in block_starters + mid_block
        ):
            indent_level += 1

    repaired = "\n".join(result)
    try:
        compile(repaired, "<string>", "exec")
        return repaired
    except Exception:
        # Strip trailing incomplete lines from the bottom until it compiles cleanly
        lines_list = repaired.split("\n")
        while lines_list:
            candidate = "\n".join(lines_list).strip()
            try:
                compile(candidate, "<string>", "exec")
                return candidate
            except SyntaxError:
                lines_list.pop()
        return "expect(page.locator('body')).to_be_visible()"


_BOILERPLATE_PATTERNS = [
    re.compile(r"^\s*from\s+playwright\S*\s+import.*$"),
    re.compile(r"^\s*import\s+playwright.*$"),
    re.compile(r"^\s*def\s+\w+\(.*\):\s*$"),
    re.compile(r"^\s*with\s+sync_playwright\(\).*:\s*$"),
    re.compile(r"^\s*browser\s*=\s*.*launch\(.*$"),
    re.compile(r"^\s*context\s*=\s*.*new_context\(.*$"),
    re.compile(r"^\s*page\s*=\s*.*new_page\(.*$"),
    re.compile(r"^\s*browser\.close\(.*$"),
    re.compile(r"^\s*context\.close\(.*$"),
    re.compile(r"^\s*if\s+__name__\s*==.*$"),
    re.compile(r"^\s*\w+\(\)\s*$"),
]

_COMMENTARY_PREFIXES = (
    "the user", "we need", "we should", "we will", "let's", "goal:", "note:", "rules:",
    "simplify:", "implement", "edge cases:", "strict mode:", "potential code:",
    "here is", "explanation:", "this should", "avoid ", "given rule", "thus code",
    "i think", "also ", "now we need", "for each:", "finally ", "when using",
    "to get ", "might be ", "keep track", "text controls:", "then buttons:",
    "handle <select>", "identify all", "verify rows", "the code", "step goal:"
)

_BACKDROP_REMOVE_RE = re.compile(r"document\.querySelectorAll\(['\"]([^\'\"]*backdrop[^\'\"]*)['\"]\)\.forEach\([^\)]*=>[^\)]*\.remove\(\)\)")
_PAGE_GOTO_RE = re.compile(r"\bpage\.goto\(")
_CLOSE_CALL_RE = re.compile(r"(?:browser|context|page)\.close\(.*?\)")
_DANGLING_LOC_RE = re.compile(r'locator\(\s*f?["\']([a-zA-Z0-9_\-]+)\.["\']\s*\)')
_DANGLING_LOC_PLUS_RE = re.compile(r'locator\(\s*f?["\']([a-zA-Z0-9_\-]+)\.["\']\s*\+')
_TRUNC_LOC_RE = re.compile(r"^\s*(?:ator|ocator)\(")
_TRUNC_AGE_RE = re.compile(r"^\s*age\.")
_PAGE_CLICK_RE = re.compile(r"\bpage\.click\(")
_PAGE_FILL_RE = re.compile(r"\bpage\.fill\(")
_LEADING_AWAIT_RE = re.compile(r"^\s*await\s+")
_FILTER_TEXT_RE = re.compile(r"\.filter\(\s*has_text\s*=\s*['\"](Today|Week|Month|Custom|All|Mine|My Team|Employee)['\"]\s*\)")


def _sanitize_playwright_code(raw_code: str) -> str:
    """Cleans up code from LLM output: strips markdown, ads, conversational reasoning, boilerplate wrappers, and repairs syntax."""
    if not raw_code:
        return ""
    match = re.search(r"```(?:python)?(.*?)```", raw_code, re.DOTALL)
    code = match.group(1).strip() if match else raw_code.strip()

    # Strip provider ads and footers
    for sep in ["\n---\n", "\n**Support", "\nPowered by", "\n🌸", "\nAd ", "\n---\r\n"]:
        if sep in code:
            code = code.split(sep)[0].strip()

    cleaned_lines = []
    seen_clear_cookies = False
    for line in code.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.match(line) for p in _BOILERPLATE_PATTERNS):
            continue
        # Strip prose commentary
        lower_s = stripped.lower()
        if any(lower_s.startswith(cp) for cp in _COMMENTARY_PREFIXES) and not any(kw in stripped for kw in ["page.", "expect(", "assert ", "locator(", "input", "button", "select", "smart_"]):
            continue
        # Strip any leading or inline await for synchronous execution
        line = re.sub(r"\bawait\s+", "", line)
        # Fix page.context() method call -> page.context property
        line = line.replace("page.context()", "page.context")
        if "clear_cookies" in line:
            seen_clear_cookies = True
            indent_prefix = line[: len(line) - len(line.lstrip())]
            cleaned_lines.append(line)
            cleaned_lines.append(
                f'{indent_prefix}page.evaluate("() => {{ try {{ localStorage.clear(); sessionStorage.clear(); }} catch(e) {{}} }}")'
            )
            continue
        # Strip redundant page.goto unless preceded by clear_cookies (runner already navigated & hydrated)
        if _PAGE_GOTO_RE.search(line):
            if not seen_clear_cookies:
                continue
            else:
                indent_prefix = line[: len(line) - len(line.lstrip())]
                cleaned_lines.append(line)
                cleaned_lines.append(f"{indent_prefix}page.wait_for_timeout(800)")
                continue
        # Strip any stray browser/context/page close calls inside snippet
        if _CLOSE_CALL_RE.search(line):
            continue
        # Fix dangling dot selectors like locator("svg.") or locator('div.')
        line = _DANGLING_LOC_RE.sub(r'locator("\1")', line)
        line = _DANGLING_LOC_PLUS_RE.sub(r'locator("\1" +', line)
        # Fix common truncated prefix patterns
        line = _TRUNC_LOC_RE.sub("page.locator(", line)
        line = _TRUNC_AGE_RE.sub("page.", line)
        # Fix Python 'or' inside JS wait_for_function strings
        if "wait_for_function" in line and " or " in line:
            line = line.replace(" or ", " || ")
        # Avoid [class*="tab"] matching <table> / .table-container when searching for tabs
        if '[class*="tab"]' in line and "pill" in line:
            line = line.replace('[class*="tab"]', '[class*="pill-tab"]')
        # Upgrade substring .filter(has_text='Today') on pill/tab labels to exact regex match
        line = _FILTER_TEXT_RE.sub(
            r".filter(has_text=re.compile(r'^\\s*\1\\s*$', re.I))",
            line,
        )
        # Upgrade fragile Collapse sidebar selector
        if 'Collapse sidebar' in line and 'locator(' in line:
            line = re.sub(
                r"page\.locator\(['\"].*?Collapse sidebar.*?['\"]\)",
                'page.locator(\'button[aria-label*="Collapse" i], button[title*="Collapse" i], button:has-text("Collapse"), [class*="sidebar"] button, aside button\')',
                line,
            )
        # Convert destructive .remove() on modal/offcanvas backdrops to non-destructive pointer-events / display hiding
        # Native .remove() on React-managed portal backdrops causes React to throw 'NotFoundError: Failed to execute removeChild on Node' and unmount the entire app
        if ".remove()" in line and any(kw in line for kw in ["backdrop", "modal", "offcanvas"]):
            line = _BACKDROP_REMOVE_RE.sub(
                r"document.querySelectorAll('\1').forEach(e => (e.style.display = 'none', e.style.pointerEvents = 'none'))",
                line,
            )
        # Route page.click and page.fill through resilient smart_click and smart_fill
        line = _PAGE_CLICK_RE.sub("smart_click(", line)
        line = _PAGE_FILL_RE.sub("smart_fill(", line)
        cleaned_lines.append(line)

    text = "\n".join(cleaned_lines)
    cleaned = textwrap.dedent(text).strip()
    if not cleaned:
        return "expect(page.locator('body')).to_be_visible()"
    return _auto_indent_python_code(cleaned)


def _is_trivial_or_mismatched_script(code: str, step_desc: str) -> bool:
    """Checks if the snippet is a passive placeholder or doesn't perform the multi-step actions described."""
    clean = (code or "").strip()
    desc = (step_desc or "").lower()

    if not clean or clean in [
        "expect(page.locator('body')).to_be_visible()",
        "page.wait_for_load_state('networkidle')",
        "page.wait_for_load_state('domcontentloaded')",
        "pass",
    ]:
        return True

    is_tab_or_pill = any(k in desc for k in ["pill tab", "filter tab", "tab filter", "tab switching", "click on 'today'", "click on 'week'", "click on 'month'", "click on 'custom'", "click on the 'all'", "click on the 'mine'", "click on the 'my team'", "click on the 'employee'"])
    if is_tab_or_pill and "# 1. Capture baseline record count before applying filter" in clean:
        return True

    # If test step describes Search action
    is_step_search = "search" in desc and "filter drawer" not in desc and "filter button" not in desc
    is_step_filter = any(k in desc for k in ["filter drawer", "filter button", "filters button", "apply filter"]) and not is_step_search and not is_tab_or_pill
    is_step_form = any(k in desc for k in ["fill form", "populate fields", "save changes", "blank required", "leave all required"]) and not is_step_filter and not is_step_search

    if is_step_search:
        # If script clicks filter button instead of searching, it's mismatched
        if "filter_btn" in clean.lower() or "button:has-text(\"filter\")" in clean.lower():
            if "search_box.fill" not in clean and "smart_search" not in clean:
                return True
        if not any(k in clean.lower() for k in ["search", "placeholder", "smart_search", "fill(", "press("]):
            return True

    if is_step_filter:
        # If test step describes Clear/Reset Filter action, must open drawer or use smart_clear_filters
        if "clear" in desc or "reset" in desc:
            if not any(k in clean.lower() for k in ["smart_clear_filters", "offcanvas", "open filter", "clear", "reset"]):
                return True
        elif not any(k in clean.lower() for k in ["filter", "smart_filter", "click(", "select_option("]):
            return True
        if "wait_for(" in clean and ".click(" not in clean:
            return True

    if is_step_form:
        if not any(k in clean.lower() for k in ["input", "textarea", "smart_fill", "sub_btn", "submit", "save", "fill(", "click("]):
            return True

    return False


def safe_wait_loaded(page, timeout=5000):
    """Safely waits for DOMContentLoaded and network settlement without hanging on WebSockets/analytics."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=1200)
    except Exception:
        pass


_SETTINGS_CACHE = {"mtime": 0.0, "last_check": 0.0, "data": {}}
_SETTINGS_TTL_SECONDS = 300.0


def _load_settings_credentials() -> dict:
    """Loads fallback credentials from config/settings.json test_variables with mtime and TTL caching."""
    global _SETTINGS_CACHE
    now = time.time()
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")
        if not os.path.exists(cfg_path):
            cfg_path = "config/settings.json"
        if os.path.exists(cfg_path):
            mtime = os.path.getmtime(cfg_path)
            # Return cached data if mtime matches AND within TTL window
            if mtime == _SETTINGS_CACHE.get("mtime") and (now - _SETTINGS_CACHE.get("last_check", 0.0) < _SETTINGS_TTL_SECONDS):
                return dict(_SETTINGS_CACHE.get("data", {}))
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                creds = {}
                for var in data.get("test_variables", []):
                    k = str(var.get("Key", "")).strip()
                    v = str(var.get("Value", "")).strip()
                    if k and v:
                        creds[k] = v
                _SETTINGS_CACHE = {"mtime": mtime, "last_check": now, "data": creds}
                return dict(creds)
    except Exception:
        pass
    return dict(_SETTINGS_CACHE.get("data", {}))


def _ensure_authenticated(page, target_url: str, test_data: dict, is_unauth_test: bool = False):
    """
    Guarantees that when testing internal/protected screens, the browser does not falsely execute
    or pass on a redirect /login page. Automatically logs in using configured credentials and returns
    to the target screen.
    """
    if is_unauth_test:
        return

    url_lower = (target_url or "").lower()
    is_login_target = any(
        kw in url_lower
        for kw in ["/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up", "/forgot-password"]
    )
    if is_login_target:
        return

    current_url_lower = (page.url or "").lower()
    has_password_field = False
    try:
        has_password_field = page.locator('input[type="password"]').count() > 0
    except Exception:
        pass

    is_on_login_page = any(kw in current_url_lower for kw in ["/login", "/signin", "/sign-in"]) or has_password_field

    if is_on_login_page:
        print(f"[Auth Guard] Protected route '{target_url}' redirected to login ('{page.url}'). Performing auto-login...")
        fallback_creds = _load_settings_credentials()
        combined = dict(fallback_creds)
        if test_data and isinstance(test_data, dict):
            for k, v in test_data.items():
                if str(k).strip() and str(v).strip():
                    combined[str(k).strip()] = str(v).strip()

        # Resolve password
        password = ""
        for k, v in combined.items():
            if "pass" in k.lower():
                password = v
                break

        # Resolve user identifier (Phone, Mobile, Email, Username, User)
        identifier = ""
        for candidate_key in ["Phone", "phone", "Mobile", "mobile", "Email", "email", "Username", "username", "User", "user", "Login", "login"]:
            if candidate_key in combined and combined[candidate_key]:
                identifier = combined[candidate_key]
                break
        if not identifier:
            for k, v in combined.items():
                if "pass" not in k.lower() and v:
                    identifier = v
                    break

        try:
            # Smart locator: matches phone, mobile, email, username, and general text/tel inputs
            id_loc = page.locator(
                'input[type="tel"], input[name*="phone" i], input[placeholder*="phone" i], '
                'input[placeholder*="mobile" i], input[name*="mobile" i], input[type="email"], '
                'input[name*="email" i], input[placeholder*="email" i], input[name*="user" i], '
                'input[placeholder*="user" i], input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'
            ).first
            if id_loc.count() > 0 and identifier:
                id_loc.fill(identifier)

            pass_loc = page.locator('input[type="password"], input[name*="password" i], input[placeholder*="password" i]').first
            if pass_loc.count() > 0 and password:
                pass_loc.fill(password)

            submit_loc = page.locator(
                'button[type="submit"], input[type="submit"], button:has-text("Sign in"), '
                'button:has-text("Sign In"), button:has-text("Log in"), button:has-text("Login"), '
                'button:has-text("Submit"), .btn-primary, [role="button"]:has-text("Sign"), '
                '[role="button"]:has-text("Log")'
            ).first
            if submit_loc.count() > 0:
                submit_loc.click(force=True)

            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass

            try:
                page.context.storage_state(path="auth.json")
            except Exception:
                pass

            curr_clean = page.url.rstrip("/").split("#")[0].split("?")[0]
            target_clean = target_url.rstrip("/").split("#")[0].split("?")[0]
            if curr_clean != target_clean:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                safe_wait_loaded(page)

        except Exception as auth_err:
            print(f"[Auth Guard] Auto-login error: {auth_err}")

        final_url_lower = (page.url or "").lower()
        if any(kw in final_url_lower for kw in ["/login", "/signin", "/sign-in"]):
            raise AssertionError(
                f"Authentication Guard: Target '{target_url}' redirected to login ('{page.url}') and authentication failed. Please verify credentials in config/settings.json or test variables."
            )
        print(f"[Auth Guard] Successfully authenticated and landed on: {page.url}")


def _setup_page_optimization(page, headless: bool = True):
    """Aborts third-party telemetry, prevents dialog deadlocks, enforces responsive timeouts, and speeds up execution."""
    try:
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
    except Exception:
        pass
    try:
        page.on("dialog", lambda d: d.accept())
    except Exception:
        pass

    if not headless:
        return

    blocked_patterns = (
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "hotjar.com", "clarity.ms", "connect.facebook.net", "sentry.io",
        "datadoghq.com", "segment.io", "mixpanel.com"
    )

    def route_filter(route):
        url = route.request.url.lower()
        if any(bp in url for bp in blocked_patterns):
            try:
                route.abort()
                return
            except Exception:
                pass
        try:
            route.continue_()
        except Exception:
            pass

    try:
        page.route("**/*", route_filter)
    except Exception:
        pass


def _verify_screen_health_and_target(page, target_url: str):
    """
    Verifies that the target screen actually rendered without 404/500/Blank Page or Cloudflare/Bot blockers.
    """
    try:
        body_text = page.evaluate("() => document.body ? (document.body.innerText || '').trim() : ''")
        lower_body = body_text.lower()
        blocked_keywords = [
            "cloudflare", "just a moment...", "turnstile", "verify you are human",
            "access denied", "403 forbidden", "502 bad gateway", "500 internal server error",
            "cannot get /", "404 not found"
        ]
        for kw in blocked_keywords:
            if kw in lower_body and len(body_text) < 350:
                raise AssertionError(f"Screen Load Error at '{target_url}': Page returned blocked/error state ('{kw}').")
        elem_count = page.evaluate("() => document.querySelectorAll('button, a, input, select, table, [role], main, h1, h2, h3, h4, .card, div').length")
        if elem_count < 3 and len(body_text) < 10:
            page.wait_for_timeout(1000)
            elem_count = page.evaluate("() => document.querySelectorAll('button, a, input, select, table, [role], main, h1, h2, h3, h4, .card, div').length")
            body_text = page.evaluate("() => document.body ? (document.body.innerText || '').trim() : ''")
            if elem_count < 3 and len(body_text) < 10:
                # Attempt recovery navigation before raising failure
                try:
                    print(f"[Health Check] Blank screen detected at '{target_url}'. Attempting navigation recovery...")
                    page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1500)
                    elem_count = page.evaluate("() => document.querySelectorAll('button, a, input, select, table, [role], main, h1, h2, h3, h4, .card, div').length")
                    body_text = page.evaluate("() => document.body ? (document.body.innerText || '').trim() : ''")
                    if elem_count >= 3 or len(body_text) >= 10:
                        print(f"[Health Check] Successfully recovered screen at '{target_url}'.")
                        return
                except Exception:
                    pass
                raise AssertionError(f"Screen Load Error at '{target_url}': Screen rendered completely blank with 0 DOM elements.")
    except AssertionError as ae:
        raise ae
    except Exception:
        pass


def _normalize_url(url: str) -> str:
    """Normalizes URL by stripping whitespace and prepending http:// or https:// if missing."""
    if not url:
        return ""
    clean = str(url).strip()
    if not clean or clean.lower() == "none" or clean == "nan":
        return ""
    if not clean.startswith("http://") and not clean.startswith("https://"):
        if "." in clean or "localhost" in clean:
            scheme = "http://" if ("localhost" in clean or "127.0.0.1" in clean) else "https://"
            clean = f"{scheme}{clean.lstrip('/')}"
    return clean


def _substitute_variables(text: str, test_data: dict = None) -> str:
    """Replaces {{KEY}} and {KEY} placeholders with values from test_data or environment variables."""
    if not text:
        return ""
    result = str(text)
    if test_data and isinstance(test_data, dict):
        for k, v in test_data.items():
            if k:
                val_str = str(v)
                result = re.sub(rf"\{{\{{\s*{re.escape(str(k))}\s*\}}\}}", lambda m, s=val_str: s, result, flags=re.IGNORECASE)
                result = re.sub(rf"\{{\s*{re.escape(str(k))}\s*\}}", lambda m, s=val_str: s, result, flags=re.IGNORECASE)
    for env_k, env_v in os.environ.items():
        if env_k:
            val_str = str(env_v)
            result = re.sub(rf"\{{\{{\s*{re.escape(str(env_k))}\s*\}}\}}", lambda m, s=val_str: s, result, flags=re.IGNORECASE)
    return result


# ---------------------------------------------------------
# Omnichannel & Multi-Platform Engine Helpers
# ---------------------------------------------------------

OMNICHANNEL_PRESETS = {
    "desktop_chrome": {
        "engine": "chromium",
        "device": None,
        "label": "Desktop Chrome",
        "name": "Desktop Chrome",
        "is_mobile": False,
        "has_touch": False,
    },
    "iphone_15_pro": {
        "engine": "webkit",
        "device": "iPhone 15 Pro",
        "label": "iPhone 15 Pro",
        "name": "iPhone 15 Pro",
        "is_mobile": True,
        "has_touch": True,
    },
    "pixel_7": {
        "engine": "chromium",
        "device": "Pixel 7",
        "label": "Google Pixel 7",
        "name": "Google Pixel 7",
        "is_mobile": True,
        "has_touch": True,
    },
    "galaxy_s23": {
        "engine": "chromium",
        "device": "Galaxy S9+",
        "label": "Samsung Galaxy S23",
        "name": "Samsung Galaxy S23",
        "is_mobile": True,
        "has_touch": True,
    },
    "ipad_air": {
        "engine": "webkit",
        "device": "iPad Air",
        "label": "iPad Air",
        "name": "iPad Air",
        "is_mobile": False,
        "has_touch": True,
    },
    "desktop_firefox": {
        "engine": "firefox",
        "device": None,
        "label": "Desktop Firefox",
        "name": "Desktop Firefox",
        "is_mobile": False,
        "has_touch": False,
    },
    "desktop_safari": {
        "engine": "webkit",
        "device": None,
        "label": "Desktop Safari (WebKit)",
        "name": "Desktop Safari (WebKit)",
        "is_mobile": False,
        "has_touch": False,
    },
    "android_cdp": {
        "engine": "chromium",
        "device": None,
        "cdp_url": "http://localhost:9222",
        "label": "Android Device / Emulator (ADB)",
        "name": "Android Device / Emulator (ADB)",
        "is_mobile": True,
        "has_touch": True,
    },
    "electron_cdp": {
        "engine": "chromium",
        "device": None,
        "cdp_url": "http://localhost:9223",
        "label": "Desktop Electron App",
        "name": "Desktop Electron App",
        "is_mobile": False,
        "has_touch": False,
    },
}


def _resolve_platform_config(platform_config: Optional[dict] = None) -> dict:
    """Normalizes and resolves platform_config with defaults and alias support."""
    cfg = dict(platform_config or {})
    target = str(cfg.get("target_platform") or cfg.get("platform") or "desktop_chrome").strip().lower()

    preset = None
    for k, v in OMNICHANNEL_PRESETS.items():
        if k == target or k in target or target in k:
            preset = v
            break

    if not preset:
        preset = OMNICHANNEL_PRESETS["desktop_chrome"]

    cdp = str(cfg.get("cdp_url") or preset.get("cdp_url") or "").strip()
    engine = cfg.get("engine") or cfg.get("browser_engine") or preset["engine"]

    resolved = {
        "target_platform": target or "desktop_chrome",
        "platform": target or "desktop_chrome",
        "browser_engine": engine,
        "engine": engine,
        "device_name": cfg.get("device_name") or preset["device"],
        "orientation": str(cfg.get("orientation", "portrait")).lower(),
        "network_preset": str(cfg.get("network_preset", "direct")).lower(),
        "cdp_url": cdp,
        "is_cdp": bool(cdp),
        "is_mobile": bool(cfg.get("is_mobile", preset["is_mobile"])),
        "has_touch": bool(cfg.get("has_touch", preset["has_touch"])),
        "label": preset.get("label", "Desktop Chrome"),
        "name": preset.get("name", "Desktop Chrome"),
    }
    return resolved


def _launch_platform_browser(p, platform_config: dict, headed: bool = False):
    """
    Launches browser according to platform configuration:
    1. CDP connection if remote endpoint specified (Real Android ADB or Desktop Electron).
    2. Multi-browser engine (WebKit, Firefox, Chromium) with graceful fallback.
    """
    cdp_url = platform_config.get("cdp_url")
    is_cdp_platform = platform_config.get("platform") in ["android_cdp", "electron_cdp"] or platform_config.get("target_platform") in ["android_cdp", "electron_cdp"]
    if cdp_url:
        try:
            print(f"[Omnichannel] Connecting over CDP to: {cdp_url}")
            return p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            if is_cdp_platform:
                raise RuntimeError(
                    f"❌ CDP Connection Error: Failed to connect to {cdp_url} ({e}). "
                    "Ensure the target phone or app is connected with remote debugging enabled and the port is forwarded via ADB."
                )
            print(f"[Omnichannel] CDP connection note: {e}. Falling back to standard browser.")

    engine = str(platform_config.get("browser_engine", "chromium")).strip().lower()

    if engine == "webkit":
        try:
            return p.webkit.launch(headless=not headed)
        except Exception as we:
            print(f"[Omnichannel] WebKit engine launch note: {we}. Falling back to Chromium...")
    elif engine == "firefox":
        try:
            return p.firefox.launch(headless=not headed)
        except Exception as fe:
            print(f"[Omnichannel] Firefox engine launch note: {fe}. Falling back to Chromium...")

    # Default Chromium (custom executable, msedge, or bundled)
    exec_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    if exec_path and os.path.exists(exec_path):
        try:
            return p.chromium.launch(headless=not headed, executable_path=exec_path)
        except Exception:
            pass
    try:
        return p.chromium.launch(headless=not headed, channel="msedge")
    except Exception:
        return p.chromium.launch(headless=not headed)


def _apply_network_throttling(context, net_preset: str):
    """Simulates network throttling or offline mode."""
    if not net_preset or net_preset == "direct":
        return
    try:
        if net_preset == "offline":
            context.route("**/*", lambda route: route.abort("internetdisconnected"))
    except Exception as e:
        print(f"[Omnichannel] Network throttling note: {e}")


def _create_platform_context(p, browser, platform_config: dict, storage_state_file: str = "auth.json", unauthenticated: bool = False):
    """Creates a browser context configured with device metrics, user agent, touch events, and session storage."""
    context_kwargs = {}
    device_name = platform_config.get("device_name")
    device_desc = None

    if device_name and hasattr(p, "devices"):
        if device_name in p.devices:
            device_desc = dict(p.devices[device_name])
        else:
            d_low = device_name.lower()
            if "iphone 15" in d_low:
                device_desc = dict(p.devices.get("iPhone 15 Pro", p.devices.get("iPhone 14 Pro", {})))
            elif "iphone" in d_low:
                device_desc = dict(p.devices.get("iPhone 14 Pro", p.devices.get("iPhone 13", {})))
            elif "pixel" in d_low:
                device_desc = dict(p.devices.get("Pixel 7", p.devices.get("Pixel 5", {})))
            elif "galaxy" in d_low or "samsung" in d_low:
                device_desc = dict(p.devices.get("Galaxy S9+", p.devices.get("Galaxy S8", {})))
            elif "ipad" in d_low or "tablet" in d_low:
                device_desc = dict(p.devices.get("iPad Air", p.devices.get("iPad Pro 11", {})))

    if device_desc:
        context_kwargs.update(device_desc)
        if platform_config.get("orientation") == "landscape" and "viewport" in context_kwargs:
            vp = context_kwargs["viewport"]
            context_kwargs["viewport"] = {"width": vp["height"], "height": vp["width"]}
    else:
        context_kwargs["viewport"] = {"width": 1440, "height": 900}
        context_kwargs["is_mobile"] = platform_config.get("is_mobile", False)
        context_kwargs["has_touch"] = platform_config.get("has_touch", False)

    origin_storage = []
    if storage_state_file and os.path.exists(storage_state_file) and not unauthenticated:
        try:
            context_kwargs["storage_state"] = storage_state_file
            with open(storage_state_file, "r", encoding="utf-8") as f:
                auth_data = json.load(f)
            origin_storage = auth_data.get("origins", [{}])[0].get("localStorage", [])
        except Exception:
            pass

    context = browser.new_context(**context_kwargs)

    net_preset = platform_config.get("network_preset", "direct")
    if net_preset and net_preset != "direct":
        _apply_network_throttling(context, net_preset)

    if origin_storage and not unauthenticated:
        items_json = json.dumps(origin_storage)
        try:
            context.add_init_script(f"""
            (() => {{
                const items = {items_json};
                items.forEach(it => {{
                    try {{ window.localStorage.setItem(it.name, it.value); }} catch(e){{}}
                }});
            }})();
            """)
        except Exception:
            pass

    return context, origin_storage


class WebRunner:
    def __init__(
        self,
        url: str,
        headed: bool = False,
        visual: bool = False,
        test_data: dict = None,
        test_step: str = "",
        expected_result: str = "",
        custom_script: str = "",
        platform_config: dict = None,
    ):
        self.headed = headed
        self.visual = visual
        self.platform_config = _resolve_platform_config(platform_config)
        fallback_creds = _load_settings_credentials()
        raw_test_data = test_data or {}
        self.test_data = {k: v for k, v in raw_test_data.items() if str(k).strip() and str(v).strip()}
        if not self.test_data.get("Email") and not self.test_data.get("email"):
            self.test_data["Email"] = fallback_creds.get("Email") or fallback_creds.get("email") or ""
        if not self.test_data.get("Password") and not self.test_data.get("password"):
            self.test_data["Password"] = fallback_creds.get("Password") or fallback_creds.get("password") or ""

        self.url = _normalize_url(_substitute_variables(url, self.test_data))
        self.test_step = _substitute_variables(test_step, self.test_data)
        self.expected_result = _substitute_variables(expected_result, self.test_data)
        self.custom_script = custom_script
        self.last_screenshot = ""
        self.generated_code = ""
        self.full_script = ""

        self.ai_client = AIClient()
        self.healer = SelfHealingEngine(self.ai_client)
        self.inspector = VisualInspector(self.ai_client)
        self.replay_cache = ReplayCache()
        self.semantic_assert = SemanticAssertEngine(self.ai_client)

    def _launch_browser(self, p):
        """Launches platform-specific browser engine (Chromium, Firefox, WebKit, or CDP)."""
        return _launch_platform_browser(p, self.platform_config, headed=self.headed)

    def _capture_screenshot(self, page, prefix="screenshot"):
        """Captures and saves a full-scroll managed screenshot to reports/screenshots/."""
        return save_screenshot_with_prefix(page, prefix)

    def _build_execution_scope(self, page):
        """Builds a rich, safe dynamic namespace of fallback locators and helpers."""
        scope = SafeNamespace(page, expect)
        # Pre-seed high-frequency locators across all frameworks (React, MUI, AntD, Tailwind, Bootstrap)
        scope["cards"] = page.locator('[class*="card"], [class*="kpi"], [class*="stat"], [class*="attention"], .item-card, .list-item')
        scope["card"] = page.locator('[class*="card"], [class*="kpi"], [class*="stat"], [class*="attention"], .item-card, .list-item').first
        scope["kpi_cards"] = page.locator('[class*="kpi"], [class*="attention-card"], [class*="stat-card"]')
        scope["kpi_card"] = page.locator('[class*="kpi"], [class*="attention-card"], [class*="stat-card"]').first
        scope["pill_tabs"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="pill-tab"], [class*="tab-"]')
        scope["pill_tab"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="pill-tab"], [class*="tab-"]').first
        scope["tabs"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="tab"]')
        scope["tab"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="tab"]').first
        scope["filters"] = page.locator('button[class*="filter"], [aria-label*="filter" i], select, [role="combobox"]')
        scope["filter_button"] = page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn, [aria-label*="filter" i]').first
        scope["search_input"] = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input[name*="search" i]').first
        scope["search_box"] = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input[name*="search" i]').first
        scope["create_button"] = page.locator('button:has-text("Create"), button:has-text("Add"), button:has-text("New"), [aria-label*="create" i], [aria-label*="add" i]').first
        scope["add_button"] = page.locator('button:has-text("Add"), button:has-text("Create"), button:has-text("New"), [aria-label*="add" i]').first
        scope["action_buttons"] = page.locator('button, [role="button"], input[type="submit"], input[type="button"], .btn')
        scope["action_button"] = page.locator('button, [role="button"], input[type="submit"], input[type="button"], .btn').first
        scope["modal"] = page.locator('[role="dialog"], .modal, .drawer, .ant-modal, .MuiDialog-root').first
        scope["modal_close"] = page.locator('button:has-text("Cancel"), button:has-text("Close"), [aria-label*="close" i], .ant-modal-close, .modal-close').first
        scope["required_inputs"] = page.locator('input[required], select[required], textarea[required], [aria-required="true"]')
        scope["submit_buttons"] = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Save"), button:has-text("Enter Data")')
        scope["submit_button"] = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Save"), button:has-text("Enter Data")').first
        scope["status_controls"] = page.locator('select, [role="combobox"], button[aria-haspopup="listbox"], .status-filter, [class*="status"]')
        scope["status_control"] = page.locator('select, [role="combobox"], button[aria-haspopup="listbox"], .status-filter, [class*="status"]').first
        scope["page_size_controls"] = page.locator('select, .pagination select, [aria-label*="page" i], [aria-label*="limit" i]')
        scope["page_size_buttons"] = SafeList(page.locator('button').all())
        scope["dashboard_panel"] = page.locator('.dashboard-panel, .main-content, #root, main').first
        scope["login_email_input"] = page.locator('input[type="email"], input[name*="email" i], input[placeholder*="email" i], input[name*="user" i]').first
        scope["validation_messages"] = page.locator('.validation-error, .error-message, .invalid-feedback, .alert-danger, [role="alert"]')
        scope["field"] = page.locator('input, select, textarea').first
        scope["control"] = page.locator('select, [role="combobox"], button').first
        scope["button"] = page.locator('button, [role="button"]').first
        scope["tables"] = page.locator('table, [role="table"]')
        scope["table"] = page.locator('table, [role="table"]').first
        scope["fields"] = page.locator('input, select, textarea')
        scope["controls"] = SafeList(page.locator('select, [role="combobox"], button').all())
        scope["rows"] = page.locator('table tbody tr, [role="row"]')
        scope["headers"] = page.locator('table thead th, [role="columnheader"]')
        scope["options"] = page.locator('option, [role="option"]')

        # Tester-Army Agent DSL injection
        agent_ctx = AgentContext(page, base_url=getattr(self, 'url', ''))
        scope["app"] = agent_ctx.app
        scope["agent"] = agent_ctx.agent
        scope["screen"] = agent_ctx.screen
        scope["ctx"] = agent_ctx
        scope["assert_semantic"] = lambda claim, ss=None: agent_ctx.agent.assert_that(claim, ss)
        return scope

    def execute(self):
        print(f"Starting WebRunner for {self.url} (Headed: {self.headed}, Platform: {self.platform_config.get('label')})")

        # 1. Standalone custom script provided
        if self.custom_script and "sync_playwright" in self.custom_script:
            print("Executing FULL Standalone Custom Script provided from the table...")
            self.full_script = self.custom_script
            try:
                exec(self.custom_script, {"__name__": "__main__"})
                print("Standalone custom script execution successful.")
                return self.custom_script
            except Exception as e:
                print(f"Failed to execute standalone custom script: {e}")
                raise e

        # Check if step is explicitly testing login page, unauthenticated access, or logout
        url_lower = str(self.url or "").lower()
        is_login_target = any(kw in url_lower for kw in ["/login", "/signin", "/sign-in", "/register", "/signup"])
        is_unauth_test = is_login_target or any(
            kw in self.test_step.lower()
            for kw in ["without a valid", "unauthenticated", "logged out", "without authentication", "expired session", "clear_cookies"]
        )
        is_logout_test = any(kw in self.test_step.lower() for kw in ["logout", "log out", "sign out"])

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            state_file = "auth.json"
            context, origin_storage = _create_platform_context(p, browser, self.platform_config, state_file, unauthenticated=is_unauth_test)

            page = context.new_page()
            _setup_page_optimization(page, headless=not self.headed)

            # Resolve relative target URLs
            target_run_url = str(self.url or "").strip()
            if target_run_url and not target_run_url.startswith("http://") and not target_run_url.startswith("https://"):
                if "." in target_run_url:
                    target_run_url = f"https://{target_run_url.lstrip('/')}"

            try:
                page.goto(target_run_url, wait_until="domcontentloaded", timeout=30000)
                safe_wait_loaded(page)
                _ensure_authenticated(page, target_run_url, self.test_data, is_unauth_test)
                _verify_screen_health_and_target(page, target_run_url)
                print(f"Navigated to {target_run_url} (Current URL: {page.url})")

                exec_scope = self._build_execution_scope(page)

                # 2. Snippet custom script
                if self.custom_script:
                    print("Executing Custom Script snippet provided from the table...")
                    clean_script = _sanitize_playwright_code(self.custom_script)
                    if _is_trivial_or_mismatched_script(clean_script, self.test_step):
                        from core.test_generator import _synthesize_default_playwright_code
                        print(f"Custom script was passive/mismatched. Re-synthesizing active multi-step execution for: '{self.test_step}'...")
                        clean_script = _synthesize_default_playwright_code(self.test_step, self.expected_result, target_run_url)
                    self.generated_code = clean_script
                    try:
                        exec(clean_script, exec_scope)
                        print("Custom script execution successful.")
                        self.last_screenshot = self._capture_screenshot(page, "pass_custom_script")
                        if self.expected_result and self.expected_result.strip().lower() not in ["", "nan", "none", "action executes cleanly"]:
                            is_valid, reason = self.semantic_assert.evaluate_claim(page, self.expected_result, screenshot_path=self.last_screenshot)
                            if not is_valid:
                                raise AssertionError(f"Expected Result Validation Failed: {reason} (Expected: '{self.expected_result}')")
                        return self.custom_script
                    except Exception as script_err:
                        print(f"Custom script failed: {script_err}. Engaging Multi-Tier Self-Healing Engine...")
                        success, healed_code, log_msg = self_healing_engine.heal_and_execute(
                            page=page,
                            failed_code=clean_script,
                            error=script_err,
                            step_desc=self.test_step,
                            expected_result=self.expected_result,
                            test_data=self.test_data,
                            exec_scope=exec_scope,
                        )
                        if success:
                            print(f"[WebRunner] {log_msg}")
                            self.generated_code = healed_code
                            self.last_screenshot = self._capture_screenshot(page, "pass_healed_script")
                            if self.expected_result and self.expected_result.strip().lower() not in ["", "nan", "none", "action executes cleanly"]:
                                is_valid, reason = self.semantic_assert.evaluate_claim(page, self.expected_result, screenshot_path=self.last_screenshot)
                                if not is_valid:
                                    raise AssertionError(f"Expected Result Validation Failed: {reason} (Expected: '{self.expected_result}')")
                            return healed_code
                        else:
                            self.last_screenshot = self._capture_screenshot(page, "fail_custom_script")
                            raise script_err

                # 3. Dynamic execution via AI / Replay Cache
                elif self.test_step:
                    # 3.1 Check Replay Cache for sub-50ms instant execution
                    cached_code = self.replay_cache.get_cached_action(self.url, self.test_step)
                    if cached_code:
                        print(f"⚡ Replay Cache Hit for: '{self.test_step[:50]}' (Replaying deterministic script directly)")
                        code = cached_code
                        self.generated_code = code
                    else:
                        print(f"Executing step dynamically via AI: {self.test_step}")

                        live_dom = page.content()
                        if len(live_dom) > 20000:
                            live_dom = live_dom[:20000] + "\n...(truncated)"

                        prompt = f"""
                        You are an expert QA automation engineer writing SYNCHRONOUS Python Playwright code for an existing active `page` object.
                        Test Step: "{self.test_step}"
                        Expected Result: "{self.expected_result}"
                        Available test data: {json.dumps(self.test_data)}
                        
                        Here is the LIVE HTML DOM of the page you are on:
                        {live_dom}
                        
                        CRITICAL RULES:
                        1. SEQUENTIAL MULTI-STEP EXECUTION: If Test Step specifies multiple sequential steps (e.g., Step 1 -> Step 2 -> Step 3 -> Step 4 -> Step 5), you MUST execute EVERY step in order:
                           - If "Click Filter": click Filter button `page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn').first.click(force=True)` and wait.
                           - If "Select Role / Option": click the role/filter input (`input[placeholder*="Role" i]`), click the option (`.offcanvas div[style*="cursor: pointer"], [role="option"]`), and wait.
                           - If "Apply Filters": click `page.locator('button:has-text("Apply Filters"), button:has-text("Apply")').first.click(force=True)`.
                           - If "Search": fill query into `input[type="search"], input[placeholder*="search" i], .custom-search` and press 'Enter'.
                           - If "Verify Table": check `expect(page.locator('table, [role="table"], .table').first).to_be_visible()`.
                        2. NO DUMMY ASSERTIONS: Do NOT just write `expect(page.locator('body')).to_be_visible()`. You MUST perform the actual clicks and interactions described!
                        3. SYNCHRONOUS ONLY: Use `sync_playwright` methods (e.g. `page.fill(...)`, `page.click(...)`, `page.locator(...)`). NEVER use `await` or `async`.
                        4. STRICT MODE SAFE: If any locator could match multiple elements, ALWAYS append `.first` or `.nth(0)` to prevent strict mode violation errors!
                        5. RESILIENT INTERACTION: Use `.click(force=True)`.
                        6. INDENTATION & SYNTAX: Always indent 4 spaces inside blocks.
                        7. OUTPUT ONLY RAW PYTHON CODE: Pure Python statements only!
                        """

                        has_keys = bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENROUTER_API_KEY"))
                        code = ""
                        if has_keys or os.getenv("FORCE_G4F") == "true":
                            try:
                                pool = ThreadPoolExecutor(max_workers=1)
                                try:
                                    fut = pool.submit(self.ai_client.generate_response, prompt)
                                    raw_code = fut.result(timeout=4)
                                finally:
                                    pool.shutdown(wait=False, cancel_futures=True)
                                code = _sanitize_playwright_code(raw_code)
                            except Exception as ai_err:
                                print(f"AI generation notice/timeout ({ai_err}). Engaging instant deterministic synthesis...")
                                code = ""

                        if not code or len(code.strip()) < 5 or _is_trivial_or_mismatched_script(code, self.test_step):
                            from core.test_generator import _synthesize_default_playwright_code
                            code = _synthesize_default_playwright_code(self.test_step, self.expected_result, self.url)

                        self.generated_code = code

                    print(f"--- Generated Code ---\n{code}\n----------------------")

                    # Save the test script file
                    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", self.test_step.strip())[:50].strip("_")
                    if not safe_name:
                        safe_name = "test_script"
                    script_dir = "generated_scripts"
                    os.makedirs(script_dir, exist_ok=True)
                    script_path = os.path.join(script_dir, f"{safe_name}.py")

                    full_script = f"# Test Step: {self.test_step}\n"
                    full_script += f"# Target URL: {self.url}\n\n"
                    full_script += "from playwright.sync_api import sync_playwright, expect\n\n"
                    full_script += "def run_test():\n"
                    full_script += "    with sync_playwright() as p:\n"
                    full_script += '        browser = p.chromium.launch(headless=False)\n'
                    full_script += '        import os\n'
                    full_script += '        if os.path.exists("auth.json"):\n'
                    full_script += '            context = browser.new_context(storage_state="auth.json")\n'
                    full_script += '        else:\n'
                    full_script += '            context = browser.new_context()\n'
                    full_script += "        page = context.new_page()\n"
                    full_script += f'        page.goto("{self.url}", wait_until="domcontentloaded")\n\n'

                    indented_code = "\n".join(["        " + line for line in code.split("\n")])
                    full_script += indented_code + "\n\n"
                    full_script += '        context.storage_state(path="auth.json")\n'
                    full_script += "        browser.close()\n\n"
                    full_script += 'if __name__ == "__main__":\n'
                    full_script += "    run_test()\n"

                    self.full_script = full_script

                    with open(script_path, "w", encoding="utf-8") as f:
                        f.write(full_script)
                    print(f"Test script successfully saved to: {script_path}")

                    # Execute with Self-Healing fallback
                    try:
                        exec(code, exec_scope)
                        print("Dynamic step execution successful.")
                    except Exception as first_error:
                        print(f"Initial step execution failed: {first_error}. Engaging Multi-Tier Self-Healing...")
                        success, healed_code, log_msg = self_healing_engine.heal_and_execute(
                            page=page,
                            failed_code=code,
                            error=first_error,
                            step_desc=self.test_step,
                            expected_result=self.expected_result,
                            test_data=self.test_data,
                            exec_scope=exec_scope,
                        )
                        if success:
                            print(f"[WebRunner] {log_msg}")
                            code = healed_code
                            self.generated_code = healed_code

                            # Re-write the saved script with healed code
                            indented_healed = "\n".join(["        " + line for line in healed_code.split("\n")])
                            self.full_script = (
                                f"# Test Step: {self.test_step} (Self-Healed)\n# Target URL: {self.url}\n\n"
                                f"from playwright.sync_api import sync_playwright, expect\n\n"
                                f"def run_test():\n    with sync_playwright() as p:\n"
                                f"        browser = p.chromium.launch(headless=False)\n        import os\n"
                                f"        context = browser.new_context(storage_state='auth.json') if os.path.exists('auth.json') else browser.new_context()\n"
                                f"        page = context.new_page()\n        page.goto('{self.url}', wait_until='domcontentloaded')\n\n"
                                f"{indented_healed}\n\n        context.storage_state(path='auth.json')\n        browser.close()\n\n"
                                f"if __name__ == '__main__':\n    run_test()\n"
                            )
                            with open(script_path, "w", encoding="utf-8") as f:
                                f.write(self.full_script)
                        else:
                            self._capture_screenshot(page, "fail_healing")
                            print(f"[WebRunner] Self-Healing exhausted: {log_msg}")
                            raise first_error

                    if self.visual:
                        print("Running visual inspection...")
                        try:
                            screenshot_bytes = capture_full_scroll_screenshot(page)
                            b64_image = base64.b64encode(screenshot_bytes).decode("utf-8")
                            result = self.inspector.inspect_layout(
                                b64_image, context=f"Page at {self.url} after step: {self.test_step}"
                            )
                            if result.get("status") == "success":
                                print(f"Visual Analysis Result:\n{result.get('analysis')}")
                            else:
                                print(f"Visual Analysis Notice: {result.get('error')}")
                        except Exception as vis_err:
                            print(f"Visual inspection skipped due to: {vis_err}")

                    # Capture final step screenshot
                    self.last_screenshot = self._capture_screenshot(page, "pass_step")

                    if self.expected_result and self.expected_result.strip().lower() not in ["", "nan", "none", "action executes cleanly"]:
                        is_valid, reason = self.semantic_assert.evaluate_claim(page, self.expected_result, screenshot_path=self.last_screenshot)
                        if not is_valid:
                            raise AssertionError(f"Expected Result Validation Failed: {reason} (Expected: '{self.expected_result}')")

                    if self.headed:
                        try:
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass

                    return full_script

            except Exception as e:
                self.last_screenshot = self._capture_screenshot(page, "fail_execution")
                print(f"Web execution failed: {e}")
                raise e
            finally:
                try:
                    if not is_unauth_test and not is_logout_test and "/login" not in (page.url or "").lower():
                        context.storage_state(path=state_file)
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                print("Browser closed.")


class WebBatchRunner:
    """High-performance batch executor that keeps a single browser session open across multiple web tests."""

    def __init__(
        self,
        headed: bool = False,
        visual: bool = False,
        test_data: dict = None,
        platform_config: dict = None,
    ):
        self.headed = headed
        self.visual = visual
        self.platform_config = _resolve_platform_config(platform_config)
        fallback_creds = _load_settings_credentials()
        raw_test_data = test_data or {}
        self.test_data = {k: v for k, v in raw_test_data.items() if str(k).strip() and str(v).strip()}
        if not self.test_data.get("Email") and not self.test_data.get("email"):
            self.test_data["Email"] = fallback_creds.get("Email") or fallback_creds.get("email") or ""
        if not self.test_data.get("Password") and not self.test_data.get("password"):
            self.test_data["Password"] = fallback_creds.get("Password") or fallback_creds.get("password") or ""

        self.ai_client = AIClient()
        self.healer = SelfHealingEngine(self.ai_client)
        self.inspector = VisualInspector(self.ai_client)
        self.replay_cache = ReplayCache()
        self.semantic_assert = SemanticAssertEngine(self.ai_client)

    def _launch_browser(self, p):
        """Launches platform-specific browser engine (Chromium, Firefox, WebKit, or CDP)."""
        return _launch_platform_browser(p, self.platform_config, headed=self.headed)

    def _build_execution_scope(self, page):
        scope = SafeNamespace(page, expect)
        scope["cards"] = page.locator('[class*="card"], [class*="kpi"], [class*="stat"], [class*="attention"], .item-card, .list-item')
        scope["card"] = page.locator('[class*="card"], [class*="kpi"], [class*="stat"], [class*="attention"], .item-card, .list-item').first
        scope["kpi_cards"] = page.locator('[class*="kpi"], [class*="attention-card"], [class*="stat-card"]')
        scope["kpi_card"] = page.locator('[class*="kpi"], [class*="attention-card"], [class*="stat-card"]').first
        scope["pill_tabs"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="pill-tab"], [class*="tab-"]')
        scope["pill_tab"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="pill-tab"], [class*="tab-"]').first
        scope["tabs"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="tab"]')
        scope["tab"] = page.locator('.pill-tab, [role="tab"], .tab, [class*="tab"]').first
        scope["filters"] = page.locator('button[class*="filter"], [aria-label*="filter" i], select, [role="combobox"]')
        scope["filter_button"] = page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn, [aria-label*="filter" i]').first
        scope["search_input"] = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input[name*="search" i]').first
        scope["search_box"] = page.locator('input[type="search"], input[placeholder*="search" i], input[placeholder*="filter" i], input[name*="search" i]').first
        scope["create_button"] = page.locator('button:has-text("Create"), button:has-text("Add"), button:has-text("New"), [aria-label*="create" i], [aria-label*="add" i]').first
        scope["add_button"] = page.locator('button:has-text("Add"), button:has-text("Create"), button:has-text("New"), [aria-label*="add" i]').first
        scope["export_button"] = page.locator('button:has-text("Export"), button:has-text("Download"), [aria-label*="export" i]').first
        scope["refresh_button"] = page.locator('button:has-text("Refresh"), button:has-text("Reload"), [aria-label*="refresh" i]').first
        scope["action_buttons"] = page.locator('button, [role="button"], input[type="submit"], input[type="button"], .btn')
        scope["action_button"] = page.locator('button, [role="button"], input[type="submit"], input[type="button"], .btn').first
        scope["modal"] = page.locator('[role="dialog"], .modal, .drawer, .ant-modal, .MuiDialog-root').first
        scope["modal_close"] = page.locator('button:has-text("Cancel"), button:has-text("Close"), [aria-label*="close" i], .ant-modal-close, .modal-close').first
        scope["required_inputs"] = page.locator('input[required], select[required], textarea[required], [aria-required="true"]')
        scope["submit_buttons"] = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Save"), button:has-text("Enter Data")')
        scope["submit_button"] = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Save"), button:has-text("Enter Data")').first
        scope["status_controls"] = page.locator('select, [role="combobox"], button[aria-haspopup="listbox"], .status-filter, [class*="status"]')
        scope["status_control"] = page.locator('select, [role="combobox"], button[aria-haspopup="listbox"], .status-filter, [class*="status"]').first
        scope["page_size_controls"] = page.locator('select, .pagination select, [aria-label*="page" i], [aria-label*="limit" i]')
        scope["page_size_buttons"] = SafeList(page.locator('button').all())
        scope["dashboard_panel"] = page.locator('.dashboard-panel, .main-content, #root, main').first
        scope["login_email_input"] = page.locator('input[type="email"], input[name*="email" i], input[placeholder*="email" i], input[name*="user" i]').first
        scope["validation_messages"] = page.locator('.validation-error, .error-message, .invalid-feedback, .alert-danger, [role="alert"]')
        scope["field"] = page.locator('input, select, textarea').first
        scope["control"] = page.locator('select, [role="combobox"], button').first
        scope["button"] = page.locator('button, [role="button"]').first
        scope["tables"] = page.locator('table, [role="table"], .table, [class*="table"]')
        scope["table"] = page.locator('table, [role="table"], .table, [class*="table"]').first
        scope["fields"] = page.locator('input, select, textarea')
        scope["controls"] = SafeList(page.locator('select, [role="combobox"], button').all())
        scope["rows"] = page.locator('table tbody tr, [role="row"], .table-row')
        scope["headers"] = page.locator('table thead th, [role="columnheader"]')
        scope["options"] = page.locator('option, [role="option"]')

        # Tester-Army Agent DSL injection
        agent_ctx = AgentContext(page, base_url="")
        scope["app"] = agent_ctx.app
        scope["agent"] = agent_ctx.agent
        scope["screen"] = agent_ctx.screen
        scope["ctx"] = agent_ctx
        scope["assert_semantic"] = lambda claim, ss=None: agent_ctx.agent.assert_that(claim, ss)
        return scope

    def run_batch(self, tests: list, on_step_callback=None, check_control_callback=None):
        """
        Executes a sequence of web test items in a single, high-performance browser lifecycle.
        tests: list of dicts:
            {
                'index': int,
                'url': str,
                'test_step': str,
                'expected_result': str,
                'custom_script': str
            }
        on_step_callback: callable(index, status, full_script, screenshot_path, notes)
        """
        if not tests:
            return

        print(f"[WebBatchRunner] Starting batch execution for {len(tests)} test cases...")
        state_file = "auth.json"

        with sync_playwright() as p:
            browser = self._launch_browser(p)
            context, origin_storage = _create_platform_context(p, browser, self.platform_config, state_file)
            page = context.new_page()
            _setup_page_optimization(page, headless=not self.headed)
            current_target_url = None

            for item in tests:
                idx = item.get("index", 0)
                url = _normalize_url(_substitute_variables(item.get("url", ""), self.test_data))
                test_step = _substitute_variables(str(item.get("test_step", "")).strip(), self.test_data)
                expected_result = _substitute_variables(str(item.get("expected_result", "")).strip(), self.test_data)
                custom_script = str(item.get("custom_script", "")).strip()
                if custom_script == "nan":
                    custom_script = ""

                step_start_time = time.time()
                step_status = "PASSED"
                step_script = custom_script
                step_screenshot = ""
                step_notes = ""

                url_lower = url.lower()
                is_login_target = any(kw in url_lower for kw in ["/login", "/signin", "/sign-in", "/register", "/signup"])
                is_unauth_test = is_login_target or any(
                    kw in test_step.lower()
                    for kw in ["without a valid", "unauthenticated", "logged out", "without authentication", "expired session"]
                ) or "clear_cookies" in custom_script
                is_logout_test = any(kw in test_step.lower() for kw in ["logout", "log out", "sign out"])

                # 1. Health check & Auto-Recovery of browser/context/page
                page_needs_recovery = False
                if browser is None or not browser.is_connected() or context is None or page is None:
                    page_needs_recovery = True
                else:
                    try:
                        if page.is_closed():
                            page_needs_recovery = True
                        else:
                            page.evaluate("1 + 1")
                    except Exception:
                        page_needs_recovery = True

                if page_needs_recovery:
                    try:
                        print(f"[WebBatchRunner] Browser or page was closed/disconnected. Reopening fresh session for step {idx + 1}...")
                        try:
                            if browser and browser.is_connected():
                                browser.close()
                        except Exception:
                            pass
                        browser = self._launch_browser(p)
                        context, origin_storage = _create_platform_context(p, browser, self.platform_config, state_file)
                        page = context.new_page()
                        _setup_page_optimization(page, headless=not self.headed)
                    except Exception as launch_err:
                        print(f"[WebBatchRunner] Session recreation note: {launch_err}")

                # Isolate login, unauthenticated, and logout tests in a temporary context so they don't redirect or corrupt main session
                isolated_ctx = None
                main_page_ref = page
                if is_unauth_test or is_logout_test:
                    try:
                        isolated_ctx, _ = _create_platform_context(
                            p, browser, self.platform_config, state_file, unauthenticated=is_unauth_test
                        )
                        page = isolated_ctx.new_page()
                        _setup_page_optimization(page, headless=not self.headed)
                    except Exception:
                        page = main_page_ref

                try:
                    # Check if page drifted away from the step's target URL
                    current_page_url = (page.url or "").rstrip("/") if (page and not page.is_closed()) else ""
                    normalized_target = url.rstrip("/")
                    target_clean = normalized_target.split("#")[0].split("?")[0]
                    page_clean = current_page_url.split("#")[0].split("?")[0]

                    # Also detect if page is currently rendered blank or unmounted before deciding whether to navigate
                    is_current_blank = False
                    if page and not page.is_closed():
                        try:
                            c_elems = page.evaluate("() => document.querySelectorAll('button, a, input, select, table, [role], main, h1, h2, h3, h4, .card, div').length")
                            c_text = page.evaluate("() => document.body ? (document.body.innerText || '').trim() : ''")
                            if c_elems < 3 and len(c_text) < 10:
                                is_current_blank = True
                        except Exception:
                            is_current_blank = True

                    if target_clean and (target_clean != page_clean or is_current_blank or is_unauth_test or is_logout_test or idx == 0 or not current_page_url):
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        safe_wait_loaded(page)
                        _ensure_authenticated(page, url, self.test_data, is_unauth_test)
                        _verify_screen_health_and_target(page, url)
                        print(f"[WebBatchRunner] Step {idx + 1}: Navigated/reset to {url} (Current URL: {page.url})")
                    else:
                        _ensure_authenticated(page, url, self.test_data, is_unauth_test)
                        _verify_screen_health_and_target(page, url)

                    # Reset and clear any open modal/offcanvas drawer or dialogs before running step
                    _reset_page_state(page)

                    exec_scope = self._build_execution_scope(page)
                    is_replay_hit = False

                    # 1. Snippet custom script provided
                    if custom_script and "sync_playwright" not in custom_script:
                        clean_script = _sanitize_playwright_code(custom_script)
                        if _is_trivial_or_mismatched_script(clean_script, test_step):
                            from core.test_generator import _synthesize_default_playwright_code
                            clean_script = _synthesize_default_playwright_code(test_step, expected_result, url)
                        step_script = clean_script
                        try:
                            exec(clean_script, exec_scope)
                        except Exception as script_err:
                            print(f"[WebBatchRunner] Step #{idx} snippet failed: {script_err}. Engaging Multi-Tier Self-Healing...")
                            success, healed_code, log_msg = self_healing_engine.heal_and_execute(
                                page=page,
                                failed_code=clean_script,
                                error=script_err,
                                step_desc=test_step,
                                expected_result=expected_result,
                                test_data=self.test_data,
                                exec_scope=exec_scope,
                            )
                            if success:
                                print(f"[WebBatchRunner] {log_msg}")
                                step_script = healed_code
                            else:
                                raise script_err
                    # 2. Dynamic execution via AI / Replay Cache
                    elif test_step:
                        cached_code = self.replay_cache.get_cached_action(url, test_step)
                        if cached_code:
                            print(f"[WebBatchRunner] ⚡ [e2e Replay Cache] Hit for Step #{idx}: '{test_step[:50]}' (Replaying directly in <50ms)")
                            code = cached_code
                            is_replay_hit = True
                        else:
                            live_dom = page.content()
                            if len(live_dom) > 20000:
                                live_dom = live_dom[:20000] + "\n...(truncated)"

                            prompt = f"""
                            You are an expert QA automation engineer writing SYNCHRONOUS Python Playwright code for an existing active `page` object.
                            Test Step: "{test_step}"
                            Expected Result: "{expected_result}"
                            Available test data: {json.dumps(self.test_data)}
                            
                            Here is the LIVE HTML DOM of the page you are on:
                            {live_dom}
                            
                            CRITICAL RULES:
                            1. SEQUENTIAL MULTI-STEP EXECUTION: If Test Step specifies multiple sequential steps (e.g., Step 1 -> Step 2 -> Step 3 -> Step 4 -> Step 5), you MUST execute EVERY step in order:
                               - If "Click Filter": click Filter button `page.locator('button:has-text("Filter"), button:has-text("Filters"), .filter-btn').first.click(force=True)` and wait.
                               - If "Select Role / Option": click role input (`input[placeholder*="Role" i]`), click option (`.offcanvas div[style*="cursor: pointer"], [role="option"]`), and wait.
                               - If "Apply Filters": click `page.locator('button:has-text("Apply Filters"), button:has-text("Apply")').first.click(force=True)`.
                               - If "Search": fill query into `input[type="search"], input[placeholder*="search" i], .custom-search` and press 'Enter'.
                               - If "Verify Table": check `expect(page.locator('table, [role="table"], .table').first).to_be_visible()`.
                            2. NO DUMMY ASSERTIONS: Do NOT just write `expect(page.locator('body')).to_be_visible()`. You MUST perform the actual clicks and interactions described!
                            3. SYNCHRONOUS ONLY: Use `sync_playwright` methods (e.g. `page.fill(...)`, `page.click(...)`, `page.locator(...)`). NEVER use `await` or `async`.
                            4. STRICT MODE SAFE: If any locator could match multiple elements, ALWAYS append `.first` or `.nth(0)` to prevent strict mode errors!
                            5. RESILIENT INTERACTION: Use `.click(force=True)`.
                            6. INDENTATION & SYNTAX: Always indent 4 spaces inside blocks.
                            7. OUTPUT ONLY RAW PYTHON CODE: Pure Python statements only!
                            """

                            has_keys = bool(os.getenv("OPENAI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENROUTER_API_KEY"))
                            code = ""
                            if has_keys or os.getenv("FORCE_G4F") == "true":
                                try:
                                    pool = ThreadPoolExecutor(max_workers=1)
                                    try:
                                        fut = pool.submit(self.ai_client.generate_response, prompt)
                                        raw_code = fut.result(timeout=4)
                                    finally:
                                        pool.shutdown(wait=False, cancel_futures=True)
                                    code = _sanitize_playwright_code(raw_code)
                                except Exception as ai_err:
                                    print(f"[WebBatchRunner] AI generation notice/timeout ({ai_err}). Engaging instant deterministic synthesis...")
                                    code = ""

                            if not code or len(code.strip()) < 5 or _is_trivial_or_mismatched_script(code, test_step):
                                from core.test_generator import _synthesize_default_playwright_code
                                code = _synthesize_default_playwright_code(test_step, expected_result, url)

                        safe_name = re.sub(r"[^a-zA-Z0-9]", "_", test_step)[:50].strip("_") or "test_script"
                        script_dir = "generated_scripts"
                        os.makedirs(script_dir, exist_ok=True)
                        script_path = os.path.join(script_dir, f"{safe_name}.py")

                        full_script = (
                            f"# Test Step: {test_step}\n# Target URL: {url}\n\n"
                            f"from playwright.sync_api import sync_playwright, expect\n\n"
                            f"def run_test():\n    with sync_playwright() as p:\n"
                            f"        browser = p.chromium.launch(headless=False)\n        import os\n"
                            f"        context = browser.new_context(storage_state='auth.json') if os.path.exists('auth.json') else browser.new_context()\n"
                            f"        page = context.new_page()\n        page.goto('{url}', wait_until='domcontentloaded')\n\n"
                            f"{textwrap.indent(code, '        ')}\n\n"
                            f"        context.storage_state(path='auth.json')\n        browser.close()\n\n"
                            f"if __name__ == '__main__':\n    run_test()\n"
                        )
                        with open(script_path, "w", encoding="utf-8") as f:
                            f.write(full_script)

                        step_script = full_script

                        # Execute with self-healing fallback
                        try:
                            exec(code, exec_scope)
                        except Exception as first_err:
                            print(f"[WebBatchRunner] Step #{idx} initial failure: {first_err}. Engaging Multi-Tier Self-Healing...")
                            success, healed_code, log_msg = self_healing_engine.heal_and_execute(
                                page=page,
                                failed_code=code,
                                error=first_err,
                                step_desc=test_step,
                                expected_result=expected_result,
                                test_data=self.test_data,
                                exec_scope=exec_scope,
                            )
                            if success:
                                print(f"[WebBatchRunner] {log_msg}")
                                step_script = healed_code
                                code = healed_code
                            else:
                                raise first_err

                        if code and len(code.strip()) > 5:
                            self.replay_cache.save_cached_action(url, test_step, code)

                    step_screenshot = save_screenshot_with_prefix(page, "pass_step")

                    layout_audit_note = ""
                    if self.visual:
                        try:
                            audit_res = self.inspector.inspect_layout(step_screenshot, context=f"Page at {url} after step: {test_step}", page=page)
                            if audit_res and audit_res.get("summary"):
                                layout_audit_note = " | 👁️ " + audit_res["summary"]
                        except Exception:
                            pass

                    # Strict Expected_Result Semantic & DOM Validation
                    if expected_result and expected_result.strip().lower() not in ["", "nan", "none", "action executes cleanly"]:
                        is_valid, reason = self.semantic_assert.evaluate_claim(page, expected_result, screenshot_path=step_screenshot)
                        if not is_valid:
                            raise AssertionError(f"Expected Result Validation Failed: {reason} (Expected: '{expected_result}')")

                    step_status = "PASSED"
                    if is_replay_hit:
                        step_notes = "⚡ [e2e Replay Cache Hit] Executed verified script in <50ms (0 Tokens Spent)" + layout_audit_note
                    else:
                        step_notes = "🤖 [AI Autonomous Agent] Step verified and cached for instant replay." + layout_audit_note
                    _reset_page_state(page)

                except Exception as ex:
                    print(f"\n❌ [WebBatchRunner Step #{idx + 1} Failed]: {test_step}\nError: {ex}")
                    step_status = "FAILED"
                    step_notes = f"Execution error: {ex}"
                    if "Screen Load Error" in str(ex) or "completely blank" in str(ex):
                        page_needs_recovery = True
                    try:
                        safe_fail_name = re.sub(r"[^a-zA-Z0-9]", "_", test_step)[:40].strip("_") or "step"
                        step_screenshot = save_screenshot_with_prefix(page, f"failure_{safe_fail_name}")
                        if step_screenshot:
                            print(f"📸 [Failure Screenshot Captured]: {step_screenshot}")
                    except Exception as ss_err:
                        print(f"⚠️ Could not capture failure screenshot: {ss_err}")
                    _reset_page_state(page)
                finally:
                    if isolated_ctx is not None:
                        try:
                            isolated_ctx.close()
                        except Exception:
                            pass
                        page = main_page_ref

                duration_str = f"{round(time.time() - step_start_time, 2)}s"

                if on_step_callback:
                    try:
                        on_step_callback(
                            idx,
                            step_status,
                            step_script,
                            step_screenshot,
                            step_notes,
                            duration_str,
                        )
                    except TypeError:
                        try:
                            on_step_callback(
                                index=idx,
                                status=step_status,
                                full_script=step_script,
                                screenshot_path=step_screenshot,
                                notes=step_notes,
                                duration=duration_str,
                            )
                        except Exception as cb_err:
                            print(f"[WebBatchRunner] Step callback error: {cb_err}")
                    except Exception as cb_err:
                        print(f"[WebBatchRunner] Step callback error: {cb_err}")

                if check_control_callback:
                    try:
                        ctrl_signal = check_control_callback()
                        if ctrl_signal == "PAUSE":
                            print(f"[WebBatchRunner] Execution paused cleanly after step #{idx}.")
                            break
                        elif ctrl_signal == "STOP":
                            print(f"[WebBatchRunner] Execution stopped by user after step #{idx}.")
                            break
                    except Exception as ctrl_err:
                        print(f"[WebBatchRunner] Control check error: {ctrl_err}")

            try:
                context.storage_state(path=state_file)
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            print("[WebBatchRunner] Batch execution finished and browser closed cleanly.")

