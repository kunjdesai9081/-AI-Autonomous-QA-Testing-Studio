import json
import os
import re
import sys
import time
from datetime import datetime
from playwright.sync_api import sync_playwright

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.screenshot_utils import capture_full_scroll_screenshot
from core.adk_orchestrator import TestOrchestrator
from core.ai_client import AIClient


def _safe_settle(page, min_ms: int = 250, max_timeout: int = 4000):
    """Safely waits for DOMContentLoaded and network settlement without hanging on WebSockets/analytics."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=max_timeout)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(max_timeout, 1200))
    except Exception:
        pass
    if min_ms > 0:
        page.wait_for_timeout(min_ms)


def _dom_fingerprint(summary: dict, url: str) -> str:
    """Generates a stable functional fingerprint of the screen to prevent duplicate captures."""
    clean_url = str(url or "").split("?")[0].split("#")[0].rstrip("/").lower()
    headings = "".join(summary.get("headings", [])[:3]).lower()
    btns = "".join(summary.get("action_buttons", [])[:4]).lower()
    inps = "".join(summary.get("inputs", [])[:4]).lower()
    return f"{clean_url}|{headings}|{btns}|{inps}"


class AppFlowExplorer:
    """
    Autonomous and Manual Application Flow Discovery Engine:
    1. Explores web application routes, sub-tabs, configuration sub-pages, KPI cards, and action modals/drawers.
    2. Takes full-page screenshots for every discovered functionality, view state, and modal dialog.
    3. Builds a visual application flow map and synthesizes end-to-end test suites covering every interactive element.
    """

    def __init__(self, ai_client=None):
        self.ai_client = ai_client or AIClient()
        self.orchestrator = TestOrchestrator(ai_client=self.ai_client)

    def _launch_browser(self, p, headless: bool = True):
        if os.getenv("SHOW_BROWSER", "").lower() == "true":
            headless = False
        exec_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
        if exec_path and os.path.exists(exec_path):
            try:
                return p.chromium.launch(headless=headless, executable_path=exec_path)
            except Exception as e:
                print(f"Warning: Failed custom executable at {exec_path}: {e}")
        try:
            return p.chromium.launch(headless=headless, channel="msedge")
        except Exception:
            return p.chromium.launch(headless=headless)

    def _extract_page_summary(self, page):
        """Extracts title, headings, navigation links, tabs, buttons, config cards, KPIs, inputs, and tables from live view."""
        return page.evaluate("""
        () => {
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
            const isVis = el => {
                if (!el) return false;
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && el.offsetWidth > 0;
            };

            const title = document.title || '';
            const headings = Array.from(document.querySelectorAll('h1, h2, h3, h4, .page-title, .header-title, [class*="title"], [class*="heading"]'))
                .filter(isVis).map(h => clean(h.innerText)).filter(Boolean).slice(0, 8);
            
            const navLinks = [];
            const seenNav = new Set();
            
            const navSelectors = [
                'nav a', 'aside a', '.sidebar a', 'header a',
                '.nav-item', '[class*="nav-item"]', '[class*="menu-item"]',
                '.sidebar button', '.sidebar div', 'aside button', 'aside div',
                '[role="navigation"] button', '[role="navigation"] a'
            ].join(', ');
            
            document.querySelectorAll(navSelectors).forEach(el => {
                if (isVis(el)) {
                    const txt = clean(el.innerText || el.getAttribute('aria-label') || el.title || '');
                    const href = el.getAttribute('href') || '';
                    const lower = txt.toLowerCase();
                    if (txt && txt.length >= 3 && txt.length < 35 && !seenNav.has(lower) && 
                        !lower.includes('logout') && !lower.includes('sign out') && 
                        !lower.includes('collapse') && !lower.includes('close') &&
                        lower !== 'lc' && lower !== 'logo' && lower !== 'leadcrm') {
                        seenNav.add(lower);
                        navLinks.push({
                            text: txt,
                            href: href,
                            tag: el.tagName.toLowerCase(),
                            className: el.className || ''
                        });
                    }
                }
            });

            // Extract Config Cards & Sub-module Tiles
            const configCards = [];
            const seenCards = new Set();
            const configKeywords = [
                'status & phases', 'lead status', 'lead categories', 'lead category', 
                'source management', 'task type', 'rules management', 'branch management', 
                'employee management'
            ];
            
            document.querySelectorAll('h1, h2, h3, h4, [class*="title"], [class*="heading"], strong, b, div, a').forEach(el => {
                if (isVis(el) && el.children.length <= 1) {
                    const txt = clean(el.innerText || el.textContent);
                    const lower = txt.toLowerCase();
                    if (txt && txt.length >= 4 && txt.length <= 40 && !seenCards.has(lower) && !seenNav.has(lower)) {
                        if (configKeywords.some(k => lower.includes(k))) {
                            seenCards.add(lower);
                            configCards.push(txt);
                        }
                    }
                }
            });

            // Extract KPI & Attention Cards
            const kpiCards = [];
            const seenKpis = new Set();
            const kpiKeywords = ['overdue tasks', 'pending tasks', 'completed tasks', 'leads without action', 'win rate', 'total leads', 'open tasks'];
            document.querySelectorAll('[class*="card--interactive"], [class*="attention-card"], [class*="health-card"], [class*="kpi"], [class*="stat-card"]').forEach(el => {
                if (isVis(el)) {
                    const header = el.querySelector('[class*="title"], [class*="header"], h3, h4, strong') || el;
                    const txt = clean(header.innerText || header.textContent || el.innerText);
                    const lower = txt.toLowerCase();
                    if (txt && txt.length >= 3 && txt.length <= 35 && !seenKpis.has(lower)) {
                        if (kpiKeywords.some(k => lower.includes(k)) || el.className.includes('interactive') || el.className.includes('attention')) {
                            if (!lower.includes('require') && !lower.includes('due today') && !lower.includes('closed recently') && !lower.includes('no progress')) {
                                seenKpis.add(lower);
                                kpiCards.push(txt);
                            }
                        }
                    }
                }
            });

            const pillTabs = [];
            const seenTabs = new Set();
            document.querySelectorAll('.pill-tab, [role="tab"], .tab, [class*="pill-tab"], [class*="tab-"]').forEach(t => {
                if (isVis(t)) {
                    const txt = clean(t.innerText || t.getAttribute('aria-label') || '');
                    const lower = txt.toLowerCase();
                    if (txt && txt.length < 30 && !seenTabs.has(lower) && !seenNav.has(lower)) {
                        seenTabs.add(lower);
                        pillTabs.push(txt);
                    }
                }
            });

            const actionButtons = [];
            document.querySelectorAll('button, input[type="submit"], input[type="button"], .btn, [role="button"]').forEach(b => {
                if (isVis(b)) {
                    const txt = clean(b.innerText || b.value || b.getAttribute('aria-label') || b.title || '');
                    const lower = txt.toLowerCase();
                    if (txt && txt.length < 40 && !actionButtons.includes(txt) && 
                        !lower.includes('logout') && !lower.includes('sign out') && 
                        !lower.includes('collapse') && !lower.includes('close') &&
                        !lower.includes('delete') && !lower.includes('remove') &&
                        !lower.includes('purge') && !lower.includes('drop')) {
                        actionButtons.push(txt);
                    }
                }
            });

            const inputs = [];
            document.querySelectorAll('input:not([type="hidden"]), select, textarea').forEach(i => {
                if (isVis(i)) {
                    const p = clean(i.getAttribute('placeholder') || i.getAttribute('name') || i.getAttribute('aria-label') || i.id || i.type);
                    if (p && !inputs.includes(p) && p.length < 40) {
                        inputs.push(p);
                    }
                }
            });

            const tablesCount = document.querySelectorAll('table, [role="table"], .data-grid, .table-container, [class*="table"]').length;

            return {
                title: title,
                primary_heading: headings[0] || title || 'App Screen',
                headings: headings,
                nav_links: navLinks.slice(0, 25),
                config_cards: configCards.slice(0, 15),
                kpi_cards: kpiCards.slice(0, 10),
                pill_tabs: pillTabs.slice(0, 15),
                action_buttons: actionButtons.slice(0, 25),
                inputs: inputs.slice(0, 20),
                total_tables: tablesCount
            };
        }
        """)

    def _detect_and_explore_modals(self, page, screen_name: str, reports_dir: str, screen_idx: int) -> list:
        """
        Discovers and triggers action buttons that open modal dialogs or drawers (e.g. Create Lead, Add Customer, Filters, Add Status).
        Captures dedicated modal snapshots and extracts full form element trees before safely dismissing.
        """
        discovered_modals = []
        
        modal_trigger_keywords = [
            "create", "add", "new", "filter", "filters", "edit", 
            "configure", "dashboard configuration", "branch filters", "import", "export"
        ]

        summary = self._extract_page_summary(page)
        action_buttons = summary.get("action_buttons", [])

        for btn_text in action_buttons:
            lower = btn_text.lower()
            if any(k in lower for k in modal_trigger_keywords) and not any(k in lower for k in ["delete", "remove", "logout", "cancel", "close", "save", "submit"]):
                try:
                    btn_loc = page.locator(f'button:has-text("{btn_text}"), a:has-text("{btn_text}"), .btn:has-text("{btn_text}"), [role="button"]:has-text("{btn_text}")').first
                    if btn_loc.count() > 0 and btn_loc.is_visible():
                        print(f"[Flow Explorer] Triggering action/modal button: '{btn_text}' on '{screen_name}'")
                        btn_loc.click(force=True)
                        _safe_settle(page, min_ms=300)

                        modal_info = page.evaluate("""() => {
                            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                            const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                            
                            const modalSelectors = [
                                '[role="dialog"]', '.modal', '.modal-content', '.drawer', 
                                '.drawer-content', '.ant-modal', '.chakra-modal', 'dialog', 
                                '.popup', '.slide-over', '.popup-container', '.dialog-overlay'
                            ];
                            let modalEl = null;
                            for (const s of modalSelectors) {
                                const el = document.querySelector(s);
                                if (isVis(el)) { modalEl = el; break; }
                            }
                            const container = modalEl || document.body;
                            
                            const inputs = [];
                            container.querySelectorAll('input:not([type="hidden"]), select, textarea').forEach(i => {
                                if (isVis(i)) {
                                    const p = clean(i.getAttribute('placeholder') || i.getAttribute('name') || i.getAttribute('aria-label') || i.id || i.type);
                                    if (p && !inputs.includes(p)) inputs.push(p);
                                }
                            });
                            
                            const buttons = [];
                            container.querySelectorAll('button, input[type="submit"], input[type="button"], .btn').forEach(b => {
                                if (isVis(b)) {
                                    const t = clean(b.innerText || b.value || b.getAttribute('aria-label'));
                                    if (t && !buttons.includes(t) && t.length < 35) buttons.push(t);
                                }
                            });
                            
                            const headings = Array.from(container.querySelectorAll('h1, h2, h3, h4, .title, .modal-title, .drawer-title'))
                                .filter(isVis).map(h => clean(h.innerText)).filter(Boolean);

                            return {
                                is_modal: Boolean(modalEl) || inputs.length > 0,
                                modal_name: headings[0] || '',
                                inputs: inputs.slice(0, 15),
                                buttons: buttons.slice(0, 15),
                                headings: headings.slice(0, 5)
                            };
                        }""")

                        if modal_info.get("is_modal") or len(modal_info.get("inputs", [])) > 0:
                            sub_modal_name = modal_info.get("modal_name") or f"{screen_name} - {btn_text} Modal"
                            sub_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{btn_text}")[:25].strip("_")
                            sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            modal_ss = os.path.join(reports_dir, f"modal_{screen_idx}_{sub_slug}_{sub_ts}.png")
                            
                            try:
                                capture_full_scroll_screenshot(page, modal_ss)
                            except Exception:
                                pass

                            modal_rec = {
                                "type": "modal",
                                "name": sub_modal_name,
                                "trigger": btn_text,
                                "screenshot": modal_ss,
                                "url": page.url,
                                "headings": modal_info.get("headings", []),
                                "inputs": modal_info.get("inputs", []),
                                "action_buttons": modal_info.get("buttons", []),
                            }
                            discovered_modals.append(modal_rec)
                            print(f"[Flow Explorer] Captured Modal: '{sub_modal_name}' with {len(modal_info['inputs'])} inputs, {len(modal_info['buttons'])} buttons.")

                        # Dismiss modal safely
                        dismissed = False
                        close_btn = page.locator('[aria-label="Close" i], .close, .modal-close, button:has-text("Cancel"), button:has-text("Close"), svg[aria-label="Close"]').first
                        if close_btn.count() > 0 and close_btn.is_visible():
                            try:
                                close_btn.click(force=True)
                                _safe_settle(page, min_ms=250)
                                dismissed = True
                            except Exception:
                                pass

                        if not dismissed:
                            page.keyboard.press("Escape")
                            _safe_settle(page, min_ms=250)

                        # Non-destructive backdrop & overlay safety cleanup to prevent blocked clicks
                        try:
                            page.evaluate("""() => {
                                document.querySelectorAll('.modal-backdrop, [class*="backdrop"], .dialog-overlay').forEach(el => {
                                    el.style.display = 'none';
                                    el.style.pointerEvents = 'none';
                                });
                            }""")
                        except Exception:
                            pass

                except Exception as me:
                    print(f"[Flow Explorer] Note during modal exploration of '{btn_text}': {me}")
                    continue

        return discovered_modals

    def _explore_config_sub_pages(self, page, screen_name: str, reports_dir: str, screen_idx: int, config_base_url: str) -> list:
        """Deeply explores all Configuration sub-modules (Lead Status & Phases, Lead Category, Source Management, etc.)."""
        discovered_sub_configs = []
        target_modules = [
            "Lead Status", "Lead Category", "Source Management", 
            "Task Type", "Rules Management", "Branch Management", "Employee Management"
        ]
        
        print(f"[Flow Explorer] Exploring {len(target_modules)} configuration modules on '{screen_name}'...")

        for idx, mod_kw in enumerate(target_modules, 1):
            try:
                # Ensure we are on root configuration page
                if page.url.rstrip("/") != config_base_url.rstrip("/"):
                    page.goto(config_base_url, wait_until="domcontentloaded", timeout=12000)
                    _safe_settle(page, min_ms=250)

                # Locate the innermost card tile
                card_loc = page.locator(f'div:has-text("{mod_kw}"), span:has-text("{mod_kw}"), a:has-text("{mod_kw}")').last
                if card_loc.count() > 0 and card_loc.is_visible():
                    print(f"[Flow Explorer] Navigating into Config Module: '{mod_kw}'")
                    card_loc.click(force=True)
                    _safe_settle(page, min_ms=350)

                    sub_summary = self._extract_page_summary(page)
                    cfg_heading = sub_summary.get("primary_heading") or f"Config {mod_kw}"
                    sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    cfg_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"config_{mod_kw}")[:25].strip("_")
                    cfg_ss = os.path.join(reports_dir, f"config_{screen_idx}_{idx}_{cfg_slug}_{sub_ts}.png")

                    try:
                        capture_full_scroll_screenshot(page, cfg_ss)
                    except Exception:
                        pass

                    # Also explore any modals inside this sub-config page (e.g. 'Add Status', 'Branch Filters')
                    inner_modals = self._detect_and_explore_modals(page, cfg_heading, reports_dir, screen_idx)

                    cfg_record = {
                        "type": "config_module",
                        "name": f"Config: {cfg_heading}",
                        "trigger": mod_kw,
                        "url": page.url,
                        "screenshot": cfg_ss,
                        "headings": sub_summary.get("headings", []),
                        "action_buttons": sub_summary.get("action_buttons", []),
                        "inputs": sub_summary.get("inputs", []),
                        "tables_count": sub_summary.get("total_tables", 0),
                        "inner_modals": inner_modals,
                    }
                    discovered_sub_configs.append(cfg_record)
                    print(f"[Flow Explorer] Captured Config Sub-Module '{cfg_heading}' at {page.url} ({len(cfg_record['action_buttons'])} btns, {len(cfg_record['inputs'])} inputs)")

                    # Return back to configuration base view
                    page.goto(config_base_url, wait_until="domcontentloaded", timeout=12000)
                    _safe_settle(page, min_ms=250)

            except Exception as ce:
                print(f"[Flow Explorer] Note exploring config module '{mod_kw}': {ce}")
                try:
                    page.goto(config_base_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                continue

        return discovered_sub_configs

    def _explore_dashboard_kpis(self, page, screen_name: str, reports_dir: str, screen_idx: int, dash_url: str) -> list:
        """Explores interactive KPI and attention cards on the Dashboard."""
        discovered_kpis = []
        summary = self._extract_page_summary(page)
        kpi_cards = summary.get("kpi_cards", [])

        for kpi in kpi_cards:
            if not kpi or len(kpi) < 3:
                continue
            try:
                kpi_loc = page.locator(f'[class*="interactive"]:has-text("{kpi}"), .attention-card:has-text("{kpi}"), .health-card:has-text("{kpi}")').first
                if kpi_loc.count() > 0 and kpi_loc.is_visible():
                    print(f"[Flow Explorer] Exploring Dashboard KPI Card: '{kpi}'")
                    kpi_loc.click(force=True)
                    _safe_settle(page, min_ms=350)

                    kpi_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    kpi_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"kpi_{kpi}")[:25].strip("_")
                    kpi_ss = os.path.join(reports_dir, f"kpi_{screen_idx}_{kpi_slug}_{kpi_ts}.png")

                    try:
                        capture_full_scroll_screenshot(page, kpi_ss)
                    except Exception:
                        pass

                    kpi_summary = self._extract_page_summary(page)
                    kpi_record = {
                        "type": "kpi_card",
                        "name": f"Dashboard KPI: {kpi}",
                        "trigger": kpi,
                        "url": page.url,
                        "screenshot": kpi_ss,
                        "headings": kpi_summary.get("headings", []),
                        "action_buttons": kpi_summary.get("action_buttons", []),
                        "inputs": kpi_summary.get("inputs", []),
                    }
                    discovered_kpis.append(kpi_record)

                    # Return to Dashboard
                    page.goto(dash_url, wait_until="domcontentloaded", timeout=12000)
                    _safe_settle(page, min_ms=250)
            except Exception as ke:
                print(f"[Flow Explorer] Note exploring KPI '{kpi}': {ke}")
                try:
                    page.goto(dash_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                continue

        return discovered_kpis

    def _explore_screen_sub_tabs(self, page, screen_name: str, reports_dir: str, screen_idx: int) -> list:
        """Explores ownership tabs (All, Mine, Team, Employee) and time filters (Today, Week, Month)."""
        discovered_tabs = []
        summary = self._extract_page_summary(page)
        pill_tabs = summary.get("pill_tabs", [])

        for tab_text in pill_tabs:
            if not tab_text or len(tab_text) > 30:
                continue
            try:
                tab_loc = page.locator(f'button:has-text("{tab_text}"), a:has-text("{tab_text}"), .pill-tab:has-text("{tab_text}"), [role="tab"]:has-text("{tab_text}")').first
                if tab_loc.count() > 0 and tab_loc.is_visible():
                    tab_loc.click(force=True)
                    _safe_settle(page, min_ms=300)
                    
                    sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    tab_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{tab_text}")[:25].strip("_")
                    tab_ss = os.path.join(reports_dir, f"tab_{screen_idx}_{tab_slug}_{sub_ts}.png")
                    
                    try:
                        capture_full_scroll_screenshot(page, tab_ss)
                    except Exception:
                        pass

                    tab_summary = self._extract_page_summary(page)
                    tab_rec = {
                        "type": "tab",
                        "name": f"{screen_name} - {tab_text} Tab",
                        "tab": tab_text,
                        "url": page.url,
                        "screenshot": tab_ss,
                        "tables_count": tab_summary.get("total_tables", 0),
                        "action_buttons": tab_summary.get("action_buttons", []),
                        "inputs": tab_summary.get("inputs", []),
                    }
                    discovered_tabs.append(tab_rec)
            except Exception as te:
                print(f"[Flow Explorer] Note during tab exploration of '{tab_text}': {te}")
                continue

        return discovered_tabs

    def explore_app_flows(self, start_url: str, max_screens: int = 100, explore_deep_subfeatures: bool = True, on_progress_callback=None):
        """
        Autonomously discovers and navigates across internal app flows, tabs, configuration modules,
        Dashboard KPI cards, and action modals/drawers, capturing full screenshots and element trees.
        """
        reports_dir = os.path.join("reports", "screenshots", "flows")
        os.makedirs(reports_dir, exist_ok=True)

        discovered_screens = []
        visited_urls = set()
        seen_screen_signatures = set()
        effective_max = max_screens if max_screens and max_screens > 0 else 100

        print(f"[Flow Explorer] Starting deep autonomous exploration from: {start_url} (Max Screens/Views: {effective_max})")
        if on_progress_callback:
            on_progress_callback(0, f"Launching browser session for {start_url}...", None)

        with sync_playwright() as p:
            browser = self._launch_browser(p, headless=True)
            if os.path.exists("auth.json"):
                try:
                    context = browser.new_context(storage_state="auth.json")
                    with open("auth.json", "r", encoding="utf-8") as f:
                        auth_data = json.load(f)
                    origin_storage = auth_data.get("origins", [{}])[0].get("localStorage", [])
                    if origin_storage:
                        init_js = ";\n".join([
                            f'localStorage.setItem({json.dumps(item["name"])}, {json.dumps(item["value"])});'
                            for item in origin_storage
                        ])
                        context.add_init_script(init_js)
                except Exception:
                    context = browser.new_context()
            else:
                context = browser.new_context()

            page = context.new_page()
            try:
                page.set_default_timeout(8000)
                page.set_default_navigation_timeout(15000)
                page.on("dialog", lambda dialog: dialog.accept())
            except Exception:
                pass

            try:
                # 1. Capture Root / Start Screen (e.g. Dashboard)
                page.goto(start_url, wait_until="domcontentloaded", timeout=20000)
                _safe_settle(page, min_ms=400)
                try:
                    from runners.web_runner import _ensure_authenticated
                    _ensure_authenticated(page, start_url, {})
                except Exception:
                    pass
                _safe_settle(page, min_ms=300)
                visited_urls.add(page.url.rstrip("/"))
                
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                root_ss = os.path.join(reports_dir, f"screen_1_root_{ts}.png")
                try:
                    capture_full_scroll_screenshot(page, root_ss)
                except Exception:
                    pass

                summary = self._extract_page_summary(page)
                root_name = summary.get("primary_heading") or summary.get("title") or "Dashboard"
                root_fp = _dom_fingerprint(summary, page.url)
                seen_screen_signatures.add(root_fp)
                seen_screen_signatures.add(root_name.lower())
                seen_screen_signatures.add(page.url.rstrip("/").lower())
                
                # Deep sub-feature, tab, KPI & modal inspection for root screen
                root_sub_features = []
                if explore_deep_subfeatures:
                    root_sub_features.extend(self._explore_screen_sub_tabs(page, root_name, reports_dir, 1))
                    root_sub_features.extend(self._detect_and_explore_modals(page, root_name, reports_dir, 1))
                    if "dashboard" in page.url.lower():
                        root_sub_features.extend(self._explore_dashboard_kpis(page, root_name, reports_dir, 1, start_url))

                root_record = {
                    "step_num": 1,
                    "name": root_name,
                    "url": page.url,
                    "screenshot": root_ss,
                    "title": summary.get("title", ""),
                    "headings": summary.get("headings", []),
                    "action_buttons": summary.get("action_buttons", []),
                    "inputs": summary.get("inputs", []),
                    "nav_links": summary.get("nav_links", []),
                    "pill_tabs": summary.get("pill_tabs", []),
                    "config_cards": summary.get("config_cards", []),
                    "kpi_cards": summary.get("kpi_cards", []),
                    "tables_count": summary.get("total_tables", 0),
                    "sub_features": root_sub_features,
                }
                discovered_screens.append(root_record)

                if on_progress_callback:
                    on_progress_callback(1, f"Captured Screen #1: {root_record['name']} (+{len(root_sub_features)} sub-features/KPIs)", root_record)

                # 2. Extract candidate navigation links/tabs for flow exploration
                candidate_nav_items = summary.get("nav_links", [])
                print(f"[Flow Explorer] Found {len(candidate_nav_items)} candidate navigation items on root screen.")

                # 3. Explore sub-pages / modules
                screen_idx = 2
                for candidate in candidate_nav_items:
                    if len(discovered_screens) >= effective_max:
                        break

                    cand_name = candidate.get("text", "").strip()
                    cand_href = str(candidate.get("href", "")).strip()
                    if not cand_name or cand_name.lower() in seen_screen_signatures:
                        continue

                    try:
                        prev_url = page.url
                        navigated = False
                        # Strategy A: Direct Route navigation if valid href exists
                        if cand_href and not cand_href.startswith("#") and not cand_href.startswith("javascript:"):
                            full_href = cand_href if cand_href.startswith("http") else start_url.rstrip("/") + ("/" + cand_href.lstrip("/"))
                            if full_href.rstrip("/") not in visited_urls:
                                print(f"[Flow Explorer] Navigating via URL to: {full_href} ({cand_name})")
                                page.goto(full_href, wait_until="domcontentloaded", timeout=12000)
                                _safe_settle(page, min_ms=350)
                                visited_urls.add(page.url.rstrip("/"))
                                navigated = True

                        selectors = [
                            f'.nav-item:has-text("{cand_name}")',
                            f'[class*="nav-item"]:has-text("{cand_name}")',
                            f'aside button:has-text("{cand_name}"), aside div:has-text("{cand_name}"), aside a:has-text("{cand_name}")',
                            f'.sidebar button:has-text("{cand_name}"), .sidebar div:has-text("{cand_name}"), .sidebar a:has-text("{cand_name}")',
                            f'button:has-text("{cand_name}")',
                            f'a:has-text("{cand_name}")',
                            f'.pill-tab:has-text("{cand_name}")',
                            f'[role="tab"]:has-text("{cand_name}")'
                        ]

                        # Strategy B: SPA Click navigation on nav-item, tab, or button
                        if not navigated:
                            for sel in selectors:
                                try:
                                    loc = page.locator(sel).first
                                    if loc.count() > 0 and loc.is_visible():
                                        print(f"[Flow Explorer] Clicking SPA navigation target '{cand_name}' with selector: {sel}")
                                        loc.click(force=True)
                                        _safe_settle(page, min_ms=400)
                                        navigated = True
                                        visited_urls.add(page.url.rstrip("/"))
                                        break
                                except Exception:
                                    continue

                        # Strategy C: Return-to-base fallback if candidate not found on current child page
                        if not navigated and page.url.rstrip("/").lower() != start_url.rstrip("/").lower():
                            try:
                                print(f"[Flow Explorer] Target '{cand_name}' not on child screen. Returning to root to locate...")
                                page.goto(start_url, wait_until="domcontentloaded", timeout=12000)
                                _safe_settle(page, min_ms=350)
                                for sel in selectors:
                                    loc = page.locator(sel).first
                                    if loc.count() > 0 and loc.is_visible():
                                        print(f"[Flow Explorer] Located '{cand_name}' on root screen. Clicking...")
                                        loc.click(force=True)
                                        _safe_settle(page, min_ms=400)
                                        navigated = True
                                        visited_urls.add(page.url.rstrip("/"))
                                        break
                            except Exception as ret_err:
                                print(f"[Flow Explorer] Return-to-base note: {ret_err}")

                        if navigated:
                            # Re-authenticate if navigation redirected to /login
                            curr_url_lower = page.url.lower()
                            if "/login" in curr_url_lower and "/login" not in start_url.lower():
                                try:
                                    from runners.web_runner import _ensure_authenticated
                                    _ensure_authenticated(page, start_url, {})
                                    if cand_href and cand_href.startswith("http"):
                                        page.goto(cand_href, wait_until="domcontentloaded", timeout=10000)
                                        _safe_settle(page, min_ms=350)
                                except Exception:
                                    pass

                            sub_summary = self._extract_page_summary(page)

                            # Blank screen / unrendered protection
                            if not sub_summary.get("headings") and not sub_summary.get("action_buttons") and not sub_summary.get("inputs") and not sub_summary.get("nav_links"):
                                page.wait_for_timeout(800)
                                sub_summary = self._extract_page_summary(page)
                                if not sub_summary.get("headings") and not sub_summary.get("action_buttons") and not sub_summary.get("inputs") and not sub_summary.get("nav_links"):
                                    print(f"[Flow Explorer] Skipping unrendered/blank screen: '{cand_name}' at {page.url}")
                                    continue

                            screen_title = sub_summary.get("primary_heading") or cand_name
                            current_clean_url = page.url.rstrip("/").lower()
                            dom_sig = _dom_fingerprint(sub_summary, page.url)

                            if dom_sig in seen_screen_signatures or (cand_name.lower() in seen_screen_signatures and prev_url.rstrip("/").lower() == current_clean_url):
                                print(f"[Flow Explorer] Skipping duplicate view: {cand_name}")
                                continue

                            seen_screen_signatures.add(cand_name.lower())
                            seen_screen_signatures.add(current_clean_url)
                            seen_screen_signatures.add(dom_sig)

                            sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_slug = re.sub(r"[^a-zA-Z0-9]", "_", cand_name)[:20].strip("_")
                            sub_ss = os.path.join(reports_dir, f"screen_{screen_idx}_{safe_slug}_{sub_ts}.png")
                            try:
                                capture_full_scroll_screenshot(page, sub_ss)
                            except Exception:
                                pass

                            # Deep Sub-Feature & Modal Exploration on this screen
                            sub_features = []
                            if explore_deep_subfeatures:
                                sub_features.extend(self._explore_screen_sub_tabs(page, cand_name, reports_dir, screen_idx))
                                sub_features.extend(self._detect_and_explore_modals(page, cand_name, reports_dir, screen_idx))
                                
                                # If this is the Configuration screen, deeply crawl all sub-config modules!
                                if "configuration" in cand_name.lower() or "config" in page.url.lower():
                                    sub_features.extend(self._explore_config_sub_pages(page, cand_name, reports_dir, screen_idx, page.url))

                            screen_record = {
                                "step_num": screen_idx,
                                "name": cand_name or screen_title or f"Screen #{screen_idx}",
                                "url": page.url,
                                "screenshot": sub_ss,
                                "title": sub_summary.get("title", ""),
                                "headings": sub_summary.get("headings", []),
                                "action_buttons": sub_summary.get("action_buttons", []),
                                "inputs": sub_summary.get("inputs", []),
                                "nav_links": sub_summary.get("nav_links", []),
                                "pill_tabs": sub_summary.get("pill_tabs", []),
                                "config_cards": sub_summary.get("config_cards", []),
                                "kpi_cards": sub_summary.get("kpi_cards", []),
                                "tables_count": sub_summary.get("total_tables", 0),
                                "sub_features": sub_features,
                            }
                            discovered_screens.append(screen_record)
                            if on_progress_callback:
                                on_progress_callback(screen_idx, f"Captured Screen #{screen_idx}: {screen_record['name']} (+{len(sub_features)} sub-features/modals)", screen_record)
                            screen_idx += 1

                    except Exception as nav_err:
                        print(f"[Flow Explorer] Note during exploring {cand_name}: {nav_err}")
                        continue

            except Exception as e:
                print(f"[Flow Explorer] Error during flow exploration: {e}")
            finally:
                browser.close()

        flow_data = {
            "start_url": start_url,
            "total_screens": len(discovered_screens),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "screens": discovered_screens,
        }
        return flow_data

    def capture_single_screen(self, url: str, screen_name: str = "") -> dict:
        """Manually captures a specific single page or flow state."""
        reports_dir = os.path.join("reports", "screenshots", "flows")
        os.makedirs(reports_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9]", "_", screen_name or "manual_screen")[:25].strip("_")
        ss_path = os.path.join(reports_dir, f"manual_{slug}_{ts}.png")

        with sync_playwright() as p:
            browser = self._launch_browser(p, headless=True)
            if os.path.exists("auth.json"):
                try:
                    context = browser.new_context(storage_state="auth.json")
                    with open("auth.json", "r", encoding="utf-8") as f:
                        auth_data = json.load(f)
                    origin_storage = auth_data.get("origins", [{}])[0].get("localStorage", [])
                    if origin_storage:
                        init_js = ";\n".join([
                            f'localStorage.setItem({json.dumps(item["name"])}, {json.dumps(item["value"])});'
                            for item in origin_storage
                        ])
                        context.add_init_script(init_js)
                except Exception:
                    context = browser.new_context()
            else:
                context = browser.new_context()

            page = context.new_page()
            try:
                page.set_default_timeout(8000)
                page.set_default_navigation_timeout(15000)
                page.on("dialog", lambda dialog: dialog.accept())
            except Exception:
                pass
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(800)
                try:
                    from runners.web_runner import _ensure_authenticated
                    _ensure_authenticated(page, url, {})
                except Exception:
                    pass
                capture_full_scroll_screenshot(page, ss_path)
                summary = self._extract_page_summary(page)
                return {
                    "name": screen_name or summary.get("primary_heading") or summary.get("title") or "Manual Screen",
                    "url": page.url,
                    "screenshot": ss_path,
                    "title": summary.get("title", ""),
                    "headings": summary.get("headings", []),
                    "action_buttons": summary.get("action_buttons", []),
                    "inputs": summary.get("inputs", []),
                    "nav_links": summary.get("nav_links", []),
                    "pill_tabs": summary.get("pill_tabs", []),
                    "config_cards": summary.get("config_cards", []),
                    "kpi_cards": summary.get("kpi_cards", []),
                    "tables_count": summary.get("total_tables", 0),
                    "sub_features": [],
                }
            finally:
                browser.close()

    def launch_interactive_recorder(self, start_url: str, timeout_seconds: int = 300) -> dict:
        """
        Launches a live, visible (headed) browser session allowing the user to navigate freely,
        open modals/drawers, switch tabs, and capture screens interactively with an injected floating toolbar.
        Uses a resilient DOM event-queue pattern to prevent Playwright sync loop deadlocks.
        """
        reports_dir = os.path.join("reports", "screenshots", "flows")
        os.makedirs(reports_dir, exist_ok=True)

        captured_screens = []
        is_finished = False

        with sync_playwright() as p:
            browser = self._launch_browser(p, headless=False)
            if os.path.exists("auth.json"):
                try:
                    context = browser.new_context(storage_state="auth.json")
                    with open("auth.json", "r", encoding="utf-8") as f:
                        auth_data = json.load(f)
                    origin_storage = auth_data.get("origins", [{}])[0].get("localStorage", [])
                    if origin_storage:
                        init_js = ";\n".join([
                            f'localStorage.setItem({json.dumps(item["name"])}, {json.dumps(item["value"])});'
                            for item in origin_storage
                        ])
                        context.add_init_script(init_js)
                except Exception:
                    context = browser.new_context()
            else:
                context = browser.new_context()

            page = context.new_page()

            recorder_dock_js = """
            () => {
                if (document.getElementById('ai-flow-recorder-dock')) return;
                const dock = document.createElement('div');
                dock.id = 'ai-flow-recorder-dock';
                dock.style.position = 'fixed';
                dock.style.bottom = '24px';
                dock.style.right = '24px';
                dock.style.zIndex = '99999999';
                dock.style.background = '#181824';
                dock.style.color = '#FFFFFF';
                dock.style.padding = '12px 18px';
                dock.style.borderRadius = '14px';
                dock.style.boxShadow = '0 10px 35px rgba(0,0,0,0.6)';
                dock.style.fontFamily = 'system-ui, -apple-system, sans-serif';
                dock.style.display = 'flex';
                dock.style.alignItems = 'center';
                dock.style.gap = '10px';
                dock.style.border = '2px solid #6366F1';
                dock.style.cursor = 'move';
                dock.style.userSelect = 'none';

                dock.innerHTML = `
                    <div style="font-weight: 700; display: flex; align-items: center; gap: 6px; font-size: 13px;">
                        <span style="color: #EF4444; font-size: 14px;">●</span> Flow Recorder
                        <span id="ai-snap-count" style="background: #3730A3; color: #E0E7FF; font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 700;">0 Screens</span>
                    </div>
                    <input id="ai-screen-label-input" type="text" placeholder="Screen / Modal Name" 
                        style="background: #252538; color: white; border: 1px solid #4F46E5; border-radius: 8px; padding: 7px 12px; font-size: 13px; width: 180px; outline: none;" />
                    <button id="ai-snap-btn" title="Shortcut: Ctrl+Shift+S" style="background: #4F46E5; color: white; border: none; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.2s;">
                        📸 Capture Screen
                    </button>
                    <button id="ai-finish-btn" title="Shortcut: Ctrl+Shift+Enter" style="background: #10B981; color: white; border: none; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.2s;">
                        ✅ Done & Save
                    </button>
                    <span id="ai-snap-feedback" style="font-size: 12px; color: #34D399; display: none; font-weight: 600;">Saved!</span>
                `;
                document.body.appendChild(dock);

                // Dragging Support
                let isDragging = false, startX, startY, initialX, initialY;
                dock.addEventListener('mousedown', e => {
                    if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return;
                    isDragging = true;
                    startX = e.clientX;
                    startY = e.clientY;
                    const rect = dock.getBoundingClientRect();
                    initialX = rect.left;
                    initialY = rect.top;
                });
                window.addEventListener('mousemove', e => {
                    if (!isDragging) return;
                    dock.style.right = 'auto';
                    dock.style.bottom = 'auto';
                    dock.style.left = (initialX + (e.clientX - startX)) + 'px';
                    dock.style.top = (initialY + (e.clientY - startY)) + 'px';
                });
                window.addEventListener('mouseup', () => isDragging = false);

                // Button Event Listeners via DOM attributes
                const snapBtn = document.getElementById('ai-snap-btn');
                const labelInput = document.getElementById('ai-screen-label-input');
                const finishBtn = document.getElementById('ai-finish-btn');

                snapBtn.addEventListener('click', () => {
                    snapBtn.innerText = 'Capturing...';
                    snapBtn.disabled = true;
                    dock.setAttribute('data-label', labelInput.value.trim());
                    dock.setAttribute('data-action', 'snapshot');
                });

                finishBtn.addEventListener('click', () => {
                    finishBtn.innerText = 'Saving...';
                    finishBtn.disabled = true;
                    dock.setAttribute('data-action', 'finish');
                });

                // Global Keyboard Shortcuts
                document.addEventListener('keydown', e => {
                    if (e.ctrlKey && e.shiftKey && (e.key === 'S' || e.key === 's')) {
                        e.preventDefault();
                        if (snapBtn && !snapBtn.disabled) snapBtn.click();
                    }
                    if (e.ctrlKey && e.shiftKey && e.key === 'Enter') {
                        e.preventDefault();
                        if (finishBtn && !finishBtn.disabled) finishBtn.click();
                    }
                });
            }
            """

            def do_snapshot(custom_name: str = ""):
                try:
                    summary = self._extract_page_summary(page)
                    screen_name = custom_name or summary.get("primary_heading") or f"Screen #{len(captured_screens) + 1}"
                    step_idx = len(captured_screens) + 1
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_slug = re.sub(r"[^a-zA-Z0-9]", "_", screen_name)[:20].strip("_")
                    ss_path = os.path.join(reports_dir, f"interactive_{step_idx}_{safe_slug}_{ts}.png")

                    capture_full_scroll_screenshot(page, ss_path)

                    rec = {
                        "step_num": step_idx,
                        "name": screen_name,
                        "url": page.url,
                        "screenshot": ss_path,
                        "title": summary.get("title", ""),
                        "headings": summary.get("headings", []),
                        "action_buttons": summary.get("action_buttons", []),
                        "inputs": summary.get("inputs", []),
                        "nav_links": summary.get("nav_links", []),
                        "pill_tabs": summary.get("pill_tabs", []),
                        "config_cards": summary.get("config_cards", []),
                        "kpi_cards": summary.get("kpi_cards", []),
                        "tables_count": summary.get("total_tables", 0),
                        "sub_features": [],
                    }
                    captured_screens.append(rec)
                    print(f"[Flow Recorder] Captured screen #{step_idx}: '{screen_name}' -> {ss_path}")
                    return True
                except Exception as snap_err:
                    print(f"[Flow Recorder] Snapshot error: {snap_err}")
                    return False

            try:
                page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1000)
                try:
                    page.evaluate(recorder_dock_js)
                except Exception:
                    pass

                # Capture root screen by default
                do_snapshot("Initial Screen / Dashboard")

                start_t = time.time()
                while time.time() - start_t < timeout_seconds and not is_finished:
                    try:
                        if page.is_closed():
                            break

                        # Check if dock is present and if user triggered action
                        action_data = page.evaluate("""() => {
                            const dock = document.getElementById('ai-flow-recorder-dock');
                            if (!dock) return { has_dock: false, action: null };
                            const action = dock.getAttribute('data-action');
                            const label = dock.getAttribute('data-label') || '';
                            if (action) {
                                dock.removeAttribute('data-action');
                                dock.removeAttribute('data-label');
                            }
                            return { has_dock: true, action: action, label: label };
                        }""")

                        if not action_data.get("has_dock"):
                            try:
                                page.evaluate(recorder_dock_js)
                            except Exception:
                                pass

                        action = action_data.get("action")
                        if action == "snapshot":
                            label = action_data.get("label", "")
                            # Hide dock during screenshot
                            page.evaluate("() => { const d = document.getElementById('ai-flow-recorder-dock'); if (d) d.style.display = 'none'; }")
                            do_snapshot(label)
                            # Restore dock with feedback and live count
                            count_str = f"{len(captured_screens)} Screens"
                            page.evaluate(f"""() => {{
                                const d = document.getElementById('ai-flow-recorder-dock');
                                if (d) {{
                                    d.style.display = 'flex';
                                    const cnt = document.getElementById('ai-snap-count');
                                    if (cnt) cnt.innerText = '{count_str}';
                                    const fb = document.getElementById('ai-snap-feedback');
                                    if (fb) {{
                                        fb.style.display = 'inline';
                                        setTimeout(() => {{ fb.style.display = 'none'; }}, 2000);
                                    }}
                                    const btn = document.getElementById('ai-snap-btn');
                                    if (btn) {{ btn.innerText = '📸 Capture Screen'; btn.disabled = false; }}
                                    const inp = document.getElementById('ai-screen-label-input');
                                    if (inp) inp.value = '';
                                }}
                            }}""")

                        elif action == "finish":
                            is_finished = True
                            print("[Flow Recorder] Finished recording via toolbar.")
                            break

                        page.wait_for_timeout(400)
                    except Exception as loop_err:
                        err_str = str(loop_err).lower()
                        if "closed" in err_str or "target" in err_str:
                            break
                        page.wait_for_timeout(400)

                try:
                    context.storage_state(path="auth.json")
                except Exception:
                    pass

            except Exception as e:
                print(f"[Flow Recorder] Session notice: {e}")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        return {
            "start_url": start_url,
            "total_screens": len(captured_screens),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "screens": captured_screens,
        }


flow_explorer = AppFlowExplorer()

