import json
import os
import re
import sys
import time
from urllib.parse import urljoin
from datetime import datetime
from typing import Optional, List, Dict, Any
from playwright.sync_api import sync_playwright

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.screenshot_utils import capture_full_scroll_screenshot
from core.adk_orchestrator import TestOrchestrator
from core.ai_client import AIClient
from core.security_guardrails import (
    validate_target_url_security,
    classify_action_intent,
    ActionSafetyCategory,
)


def _safe_settle(page, min_ms: int = 250, max_timeout: int = 4000):
    """Safely waits for DOMContentLoaded, network settlement, and dynamic hydration without hanging on WebSockets/analytics."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=max_timeout)
    except Exception:
        pass
    # Auto-click Dev Tunnels interstitial warning continue button if present
    try:
        if "devtunnels.ms" in (page.url or "").lower():
            cont_btn = page.locator('button:has-text("Continue"), input[value="Continue"], #continue-button, [role="button"]:has-text("Continue")').first
            if cont_btn.count() > 0 and cont_btn.is_visible():
                cont_btn.click()
                page.wait_for_timeout(800)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=min(max_timeout, 3500))
    except Exception:
        pass
    # Wait for dynamic table/skeleton hydration to complete
    try:
        page.wait_for_selector('.skeleton, .animate-pulse, [data-loading="true"], .spinner-border, .ant-spin', state='detached', timeout=2000)
    except Exception:
        pass
    if min_ms > 0:
        page.wait_for_timeout(min_ms)


def _clean_stable_url(url: str) -> str:
    """Normalizes URL by stripping volatile query parameters (utm_*, timestamp, session_id, _t) and preserving semantic SPA routes."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        p = urlparse(str(url).strip())
        volatile_keys = {"timestamp", "session_id", "sessionid", "nonce", "token", "random", "_t", "cb", "bust", "v", "_"}
        filtered_q = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            k_low = k.lower()
            if k_low.startswith("utm_") or k_low in volatile_keys:
                continue
            filtered_q.append((k, v))
        filtered_q.sort()
        new_query = urlencode(filtered_q)
        fragment = p.fragment
        clean_fragment = fragment if (fragment and ("/" in fragment or fragment.startswith("!"))) else ""
        return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), p.params, new_query, clean_fragment)).lower()
    except Exception:
        return str(url).split("?")[0].rstrip("/").lower()


def _dom_fingerprint(summary: dict, url: str) -> str:
    """Generates a stable functional fingerprint of the screen to prevent duplicate captures and crawl loops."""
    clean_url = _clean_stable_url(url)
    headings = "".join(summary.get("headings", [])[:3]).lower()
    btns = "".join(summary.get("action_buttons", [])[:4]).lower()
    inps = "".join(summary.get("inputs", [])[:4]).lower()
    active_tab = str(summary.get("active_tab", "")).lower()
    modal_flag = "modal" if summary.get("is_modal_open") else "page"
    return f"{clean_url}|{headings}|{btns}|{inps}|{active_tab}|{modal_flag}"


def _is_safe_internal_link(url_str: str, base_netloc: Optional[str] = None) -> bool:
    """Verifies that the URL target is an internal, safe, non-destructive navigation route."""
    if not url_str or url_str.startswith("#") or url_str.startswith("javascript:") or url_str.startswith("mailto:") or url_str.startswith("tel:"):
        return False
    from urllib.parse import urlparse
    p = urlparse(url_str)
    if base_netloc and p.netloc and p.netloc.lower() != base_netloc.lower():
        return False
    low = url_str.lower()
    if any(k in low for k in ["logout", "signout", "sign-out", "delete", "destroy", "purge", ".pdf", ".zip", ".xlsx"]):
        return False
    is_safe, _ = validate_target_url_security(url_str, allow_local_dev=True)
    if not is_safe:
        return False
    return True


def _href_to_title(h: str) -> str:
    """Generates a clean route title from href (e.g. /box-detail/123 -> Box Detail 123)."""
    if not h or h == "/" or h.startswith("#"):
        return ""
    parts = h.split("?")[0].split("#")[0].strip("/").split("/")
    last = parts[-1] if parts else ""
    return re.sub(r"[-_]+", " ", last).title().strip()


def _deduplicate_sub_features(sub_features: list) -> list:
    """Deduplicates sub-features, modals, and tabs while preserving discovery order."""
    unique_list = []
    seen = set()
    for item in sub_features:
        if isinstance(item, dict):
            k = (
                str(item.get("type", "")).strip().lower(),
                str(item.get("name", "")).strip().lower(),
                str(item.get("trigger", "")).strip().lower(),
                str(item.get("url", "")).strip().lower()
            )
            if k not in seen:
                seen.add(k)
                unique_list.append(item)
        else:
            s_val = str(item).strip().lower()
            if s_val not in seen:
                seen.add(s_val)
                unique_list.append(item)
    return unique_list


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
        self.base_netloc = ""

    def _launch_browser(self, p, headless: Optional[bool] = None):
        if headless is None:
            # Respect environment or default to False if SHOW_BROWSER is true
            headless = os.getenv("SHOW_BROWSER", "false").lower() != "true"
        
        exec_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]

        # 1. Custom executable path if specified
        if exec_path and os.path.exists(exec_path):
            try:
                print(f"[Flow Explorer] Launching custom browser at: {exec_path} (headless={headless})")
                return p.chromium.launch(headless=headless, executable_path=exec_path, args=launch_args)
            except Exception as e:
                print(f"[Flow Explorer] Warning: Failed custom executable at {exec_path}: {e}")

        # 2. Try Chrome channel
        try:
            print(f"[Flow Explorer] Launching Google Chrome (channel='chrome', headless={headless})...")
            return p.chromium.launch(headless=headless, channel="chrome", args=launch_args)
        except Exception as e_chrome:
            print(f"[Flow Explorer] Note: Chrome channel launch note: {e_chrome}")

        # 3. Try Edge channel (default on Windows)
        try:
            print(f"[Flow Explorer] Launching Microsoft Edge (channel='msedge', headless={headless})...")
            return p.chromium.launch(headless=headless, channel="msedge", args=launch_args)
        except Exception as e_edge:
            print(f"[Flow Explorer] Note: Edge channel launch note: {e_edge}")

        # 4. Try bundled Playwright Chromium
        try:
            print(f"[Flow Explorer] Launching bundled Chromium (headless={headless})...")
            return p.chromium.launch(headless=headless, args=launch_args)
        except Exception as e_bundled:
            print(f"[Flow Explorer] Bundled Chromium launch note: {e_bundled}")

        # 5. Headed fallback to headless if headed mode failed due to display context
        if not headless:
            print("[Flow Explorer] Headed browser launch failed. Retrying in headless mode...")
            try:
                return p.chromium.launch(headless=True, channel="chrome", args=launch_args)
            except Exception:
                try:
                    return p.chromium.launch(headless=True, channel="msedge", args=launch_args)
                except Exception:
                    return p.chromium.launch(headless=True, args=launch_args)

        raise RuntimeError("Unable to launch any Chromium browser channel (Chrome, Edge, or bundled). Please ensure Google Chrome or Microsoft Edge is installed.")

    def _expand_slide_menus_and_accordions(self, page) -> int:
        """
        Expands collapsed slide menus, offcanvas drawers, hamburger toggles,
        and collapsible sidebar accordions/submenus to expose all hidden module links.
        Returns count of expanded toggles.
        """
        try:
            count = page.evaluate("""() => {
                let expandedCount = 0;
                const isVis = el => {
                    if (!el) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0;
                };

                // 1. Hamburger / Slide menu toggles
                const menuToggleSelectors = [
                    'button[aria-label*="menu" i]', 'button.navbar-toggler', '.hamburger',
                    '[class*="menu-toggle"]', '[class*="sidebar-toggle"]', '[data-bs-toggle="offcanvas"]',
                    '[class*="drawer-toggle"]', 'button:has(svg[class*="menu" i])', 'button:has([class*="menu" i])',
                    '.sidebar-collapse', '.menu-collapse', '[aria-label*="expand" i]'
                ];
                for (const sel of menuToggleSelectors) {
                    document.querySelectorAll(sel).forEach(btn => {
                        const ariaExp = btn.getAttribute('aria-expanded');
                        const title = (btn.getAttribute('title') || btn.getAttribute('aria-label') || '').toLowerCase();
                        // Never click a button whose action is to collapse an already expanded sidebar
                        if (title.includes('collapse') || title.includes('close')) {
                            return;
                        }
                        const isAlreadyExpanded = Boolean(document.querySelector(
                            '.sidebar-expanded, [class*="sidebar-expanded"], .offcanvas.show, .drawer.open, .ant-drawer-open, [class*="sidebar-open"], [class*="menu-open"]'
                        ));
                        if (isAlreadyExpanded) {
                            return;
                        }
                        if (isVis(btn) && (ariaExp === 'false' || !ariaExp)) {
                            const isDrawerClosed = !document.querySelector('.offcanvas.show, .drawer.open, .ant-drawer-open, [class*="sidebar-open"], [class*="menu-open"]');
                            if (isDrawerClosed) {
                                try { btn.click(); expandedCount++; } catch(e){}
                            }
                        }
                    });
                }

                // 2. Collapsible Submenu & Accordion headers in Sidebar
                const accordionSelectors = [
                    '[data-bs-toggle="collapse"][aria-expanded="false"]',
                    '.ant-menu-submenu-title',
                    '[class*="nav-group-toggle"][aria-expanded="false"]',
                    '[class*="submenu-toggle"][aria-expanded="false"]',
                    '.accordion-button.collapsed',
                    '[role="button"][aria-expanded="false"]',
                    '.sidebar [aria-expanded="false"]',
                    'aside [aria-expanded="false"]',
                    'nav [aria-expanded="false"]',
                    '.menu-item-has-children > a',
                    '.has-arrow',
                    '.pcoded-hasmenu:not(.pcoded-trigger):not(.active) > a',
                    '[class*="hasmenu"]:not([class*="trigger"]):not([class*="active"]) > a',
                    'li:has(ul[class*="submenu"]):not(.pcoded-trigger):not(.active) > a',
                    'li:has(ul):not(.active):not(.open) > a[class*="nav-link"]',
                    '.treeview:not(.active) > a',
                    '.has-treeview:not(.menu-open) > a'
                ];
                for (const sel of accordionSelectors) {
                    document.querySelectorAll(sel).forEach(hdr => {
                        if (isVis(hdr)) {
                            try { hdr.click(); expandedCount++; } catch(e){}
                        }
                    });
                }

                return expandedCount;
            }""")
            if count > 0:
                _safe_settle(page, min_ms=350)
            return count
        except Exception as e:
            print(f"[Flow Explorer] Slide menu expansion note: {e}")
            return 0

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
                .filter(isVis).map(h => clean(h.innerText)).filter(Boolean).slice(0, 15);
            
            const navLinks = [];
            const seenNav = new Set();
            
            const navSelectors = [
                'nav a', 'aside a', '.sidebar a', 'header a',
                '.pcoded-submenu a', '[class*="submenu"] a',
                'a.nav-link', '.nav-item > a', '[class*="nav-item"] > a', '[class*="menu-item"] > a',
                '.sidebar button', 'aside button', '[role="menuitem"]',
                '.offcanvas a', '.drawer a', '.ant-drawer a', '.MuiDrawer-root a',
                '[class*="slide-menu"] a', '[class*="drawer"] a', '[class*="sidebar"] a',
                '[class*="menu"] a', '.navbar-nav a', '[role="navigation"] a',
                '[role="tab"]', '[role="link"]', '[data-route]', '[routerlink]', '[to]',
                // Direct SPA nav items without inner <a> (e.g. LeadCRM div.nav-item, React/Vue SPAs)
                '.sidebar-nav > *', '[class*="sidebar-nav"] > *',
                '.sidebar .nav-item', 'aside .nav-item', 'nav .nav-item',
                '.sidebar [class*="nav-item"]', 'aside [class*="nav-item"]', 'nav [class*="nav-item"]',
                '.sidebar [class*="menu-item"]', 'aside [class*="menu-item"]', 'nav [class*="menu-item"]',
                '[class*="sidebar"] [class*="nav-item"]', '[class*="sidebar"] [class*="menu-item"]',
                '.nav-item', '[class*="nav-item"]:not(li)', '[class*="menu-item"]:not(li)'
            ].join(', ');

            // Helper to format clean route title from href (e.g. /box-list -> "Box List")
            const hrefToTitle = h => {
                if (!h || h === '/' || h.startsWith('#')) return '';
                const parts = h.split('?')[0].split('#')[0].replace(/^\\/+/, '').split('/');
                const last = parts[parts.length - 1] || '';
                return last.replace(/[-_]+/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase()).trim();
            };

            const processCandidate = (el, forceHref) => {
                let href = forceHref || el.getAttribute('href') || el.getAttribute('to') || el.getAttribute('routerlink') || el.getAttribute('data-route') || '';
                if (!href && (el.tagName === 'LI' || el.tagName === 'DIV')) {
                    const childA = el.querySelector('a[href]');
                    if (childA) href = childA.getAttribute('href') || '';
                }
                // For clickable buttons without href, require visibility; for direct route links, discover even if in collapsible container
                if (!href && !isVis(el)) return;

                let txt = clean(el.innerText || el.getAttribute('aria-label') || el.title || el.getAttribute('data-tooltip') || '');
                if (!txt && href) {
                    txt = hrefToTitle(href);
                }
                const lower = (txt || '').toLowerCase();
                // Filter out bare profile initials or avatar text containers or sidebar toggles with no href
                if (!href && (/^(au|pe|admin user|administrator|profile|my profile|account|user|collapse sidebar|collapse)$/i.test(lower) || /^[a-z]{1,3}$/i.test(lower) || lower.includes('collapse') || lower.includes('sidebar-toggle'))) {
                    return;
                }
                if (txt && txt.length >= 2 && txt.length < 60 && 
                    !lower.includes('logout') && !lower.includes('sign out') && 
                    lower !== 'logo' && !lower.includes('terms') && !lower.includes('privacy')) {
                    
                    // Uniqueness key: If href exists, key by normalized path; otherwise key by text
                    const normH = href ? href.split('?')[0].split('#')[0].replace(/\\/+$/, '').toLowerCase() : '';
                    const navKey = normH ? ('HREF:' + normH) : ('TXT:' + lower);
                    if (seenNav.has(navKey)) {
                        return;
                    }
                    seenNav.add(navKey);
                    navLinks.push({
                        text: txt,
                        href: href,
                        tag: el.tagName.toLowerCase(),
                        className: el.className || ''
                    });
                }
            };

            document.querySelectorAll(navSelectors).forEach(el => processCandidate(el));

            // Document-wide internal route discovery: cards, content links, data tables, and router attributes
            document.querySelectorAll('a[href], [data-route], [routerlink], [to]').forEach(el => {
                const h = (el.getAttribute('href') || el.getAttribute('to') || el.getAttribute('routerlink') || el.getAttribute('data-route') || '').trim();
                if (h && !h.startsWith('#') && !h.startsWith('javascript:') && !h.startsWith('mailto:') && !h.startsWith('tel:')) {
                    const lowH = h.toLowerCase();
                    if (!lowH.endsWith('.css') && !lowH.endsWith('.js') && !lowH.endsWith('.png') && !lowH.endsWith('.jpg') && !lowH.endsWith('.svg') && !lowH.endsWith('.ico') && !lowH.endsWith('.pdf')) {
                        processCandidate(el, h);
                    }
                }
            });

            // Extract Universal Module Cards & Config Tiles
            const configCards = [];
            const seenCards = new Set();
            
            const isConfigHub = /config|setting|setup|admin|manage|preference|options/i.test(window.location.href) || 
                                /config|setting|setup|admin/i.test(document.title);

            const cardModuleSelectors = [
                '[class*="settings-card"]', '[class*="config-card"]', '[class*="admin-tile"]',
                '[class*="hub-card"]', '[data-module]', '[class*="settings-list"] > *',
                '[class*="admin-card"]', '[class*="setting-item"]', '[class*="config-item"]',
                '.settings-grid > *', '.admin-grid > *', '[class*="config-box"]',
                '[class*="settings-box"]'
            ];

            if (isConfigHub) {
                cardModuleSelectors.push(
                    'a[href*="/config"]', 'a[href*="/setting"]', 'a[href*="/admin"]',
                    'a[href*="config"]', 'a[href*="setting"]',
                    '[class*="card"][class*="pointer"]', '[class*="card"]:has(h2, h3, h4, h5)',
                    '[role="region"] [class*="card"]', 'main [class*="grid"] > div',
                    '.content [class*="grid"] > div', '#root [class*="grid"] > div',
                    '[role="main"] [class*="grid"] > div', '[class*="grid"] > a',
                    '[class*="grid"] > [role="button"]'
                );
            }

            const boilerplateSkip = new Set([
                'save', 'submit', 'cancel', 'close', 'delete', 'ok', 'apply', 'reset', 
                'filter', 'search', 'loading', 'confirm', 'back', 'next', 'previous'
            ]);

            document.querySelectorAll(cardModuleSelectors.join(', ')).forEach(card => {
                if (!isVis(card)) return;
                const rect = card.getBoundingClientRect();
                if (rect.width < 40 || rect.height < 25 || rect.width > 1200 || rect.height > 800) return;

                // Ignore if it is part of sidebar or top navbar
                if (card.closest('nav, aside, .sidebar, header, [role="navigation"]')) return;

                let titleTxt = '';
                const headEl = card.querySelector('h1, h2, h3, h4, h5, h6, [class*="title"], [class*="heading"], [class*="name"], strong, b');
                if (headEl && isVis(headEl)) {
                    titleTxt = clean(headEl.innerText || headEl.textContent);
                } else {
                    const directA = card.tagName === 'A' ? card : card.querySelector('a, button, [role="button"]');
                    if (directA && isVis(directA)) {
                        titleTxt = clean(directA.innerText || directA.getAttribute('aria-label') || directA.title);
                    } else if (card.children.length <= 3) {
                        titleTxt = clean(card.innerText || card.textContent);
                    }
                }

                if (titleTxt && titleTxt.length >= 3 && titleTxt.length <= 50) {
                    const lower = titleTxt.toLowerCase();
                    if (!seenCards.has(lower) && !seenNav.has(lower) && !boilerplateSkip.has(lower)) {
                        seenCards.add(lower);
                        configCards.push(titleTxt);
                    }
                }
            });

            // Extract KPI, Metric & Attention Cards using multi-factor heuristic evidence scoring
            const kpiCards = [];
            const seenKpis = new Set();
            
            const cardSelectors = [
                '[class*="kpi"]', '[class*="stat"]', '[class*="metric"]', '[class*="summary-card"]',
                '[class*="card"]', '[class*="tile"]', '[class*="widget"]', '[class*="panel"]',
                '[role="region"]', '[data-testid*="kpi"]', '[data-testid*="stat"]', '[data-testid*="metric"]',
                '[class*="health-card"]', '[class*="attention-card"]'
            ].join(', ');

            const valueRegex = /^[\\$€£₹¥]?\\s*[\\d,\\.]+\\s*[%kKmMbBtT]?$|^[\\d,\\.]+\\s*\\/\\s*[\\d,\\.]+$|^\\d+\\s*(ms|s|m|h|d|days?|hrs?|mins?)$/i;
            const trendRegex = /^([+\\-↑↓▲▼]?\\s*\\d+(\\.\\d+)?%|[+\\-]\\d+|healthy|critical|warning|active|good|poor)$/i;

            document.querySelectorAll(cardSelectors).forEach(card => {
                if (!isVis(card)) return;
                const rect = card.getBoundingClientRect();
                if (rect.width > 650 || rect.height > 450 || rect.width < 50 || rect.height < 35) return;

                const cardClass = (card.className || '').toString().toLowerCase();
                let score = 0;
                if (/kpi|metric|stat|tile|summary|health|attention/.test(cardClass)) score += 2;
                if (/card|widget|box|panel/.test(cardClass)) score += 1;

                let bestValText = '';
                let bestLabelText = '';
                let trendText = '';
                let prominentValFound = false;

                const allDescendants = card.querySelectorAll('*');
                if (allDescendants.length > 40) return; // Skip large complex forms or data grids

                allDescendants.forEach(el => {
                    if (!isVis(el) || el.children.length > 0) return;
                    const txt = clean(el.innerText || el.textContent);
                    if (!txt) return;

                    if (txt.length <= 18 && valueRegex.test(txt)) {
                        const style = window.getComputedStyle(el);
                        const fSize = parseFloat(style.fontSize) || 14;
                        const fWeight = parseInt(style.fontWeight) || 400;
                        if (fSize >= 18 || fWeight >= 600) {
                            prominentValFound = true;
                            bestValText = txt;
                        } else if (!bestValText) {
                            bestValText = txt;
                        }
                    } else if (txt.length <= 15 && trendRegex.test(txt)) {
                        trendText = txt;
                    } else if (txt.length >= 3 && txt.length <= 40 && !bestLabelText) {
                        const lower = txt.toLowerCase();
                        if (!/^(view all|details|more|click|learn more|refresh|export|filter|search)$/.test(lower)) {
                            bestLabelText = txt;
                        }
                    }
                });

                if (bestValText) score += 2;
                if (bestLabelText) score += 2;
                if (prominentValFound) score += 1;
                if (trendText) score += 1;

                const isInteractive = card.matches('a, button, [role="button"]') ||
                                     card.querySelector('a, button, [role="button"]') !== null ||
                                     window.getComputedStyle(card).cursor === 'pointer';
                if (isInteractive) score += 1;

                if (score >= 4 && bestLabelText) {
                    const kpiKey = (bestLabelText + '|' + bestValText).toLowerCase();
                    if (!seenKpis.has(kpiKey)) {
                        seenKpis.add(kpiKey);
                        kpiCards.push({
                            label: bestLabelText,
                            value: bestValText,
                            trend: trendText,
                            score: score,
                            confidence: score >= 6 ? 'HIGH' : 'MEDIUM',
                            is_interactive: isInteractive,
                            text: bestValText ? `${bestLabelText}: ${bestValText}` : bestLabelText
                        });
                    }
                }
            });

            const pillTabs = [];
            const seenTabs = new Set();
            const tabSelectors = [
                '.pill-tab', '[role="tab"]', '.tab', '[class*="pill-tab"]', '[class*="tab-"]',
                '.nav-tabs button', '.nav-tabs a', '[data-bs-toggle="tab"]', '[data-bs-toggle="pill"]',
                '.ant-tabs-tab', '.MuiTab-root', 'button[id*="tab"]', '.nav-link[role="tab"]'
            ].join(', ');
            document.querySelectorAll(tabSelectors).forEach(t => {
                if (isVis(t)) {
                    const txt = clean(t.innerText || t.getAttribute('aria-label') || '');
                    const lower = txt.toLowerCase();
                    if (txt && txt.length < 40 && !seenTabs.has(lower) && !seenNav.has(lower)) {
                        seenTabs.add(lower);
                        pillTabs.push(txt);
                    }
                }
            });

            const actionButtons = [];
            document.querySelectorAll('button, input[type="submit"], input[type="button"], .btn, [role="button"]').forEach(b => {
                if (isVis(b)) {
                    // Do not treat navigation or sidebar toggles as content action buttons
                    if (b.closest('aside, nav, .sidebar, [role="navigation"]')) return;
                    const txt = clean(b.innerText || b.value || b.getAttribute('aria-label') || b.title || '');
                    const lower = txt.toLowerCase();
                    if (txt && txt.length < 40 && !actionButtons.includes(txt) && 
                        !lower.includes('logout') && !lower.includes('sign out') && 
                        !lower.includes('delete') && !lower.includes('remove') &&
                        !lower.includes('purge') && !lower.includes('drop')) {
                        actionButtons.push(txt);
                    }
                }
            });

            const inputs = [];
            document.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"]').forEach(i => {
                if (isVis(i)) {
                    const p = clean(i.getAttribute('placeholder') || i.getAttribute('name') || i.getAttribute('aria-label') || i.id || i.type);
                    if (p && !inputs.includes(p) && p.length < 40) {
                        inputs.push(p);
                    }
                }
            });

            const tablesCount = document.querySelectorAll('table, [role="table"], .data-grid, .table-container, [class*="table"]').length;

            // Active Tab detection
            let activeTab = '';
            const activeTabEl = document.querySelector('[role="tab"][aria-selected="true"], .nav-link.active, .tab.active, .pill-tab.active, .ant-tabs-tab-active');
            if (activeTabEl && isVis(activeTabEl)) {
                activeTab = clean(activeTabEl.innerText || activeTabEl.getAttribute('aria-label') || '');
            }

            // Visible Modal detection
            const modalEl = document.querySelector('[role="dialog"]:not([aria-hidden="true"]), .modal.show, .modal.in, dialog[open], .drawer.open, .ant-modal-open');
            const isModalOpen = Boolean(modalEl && isVis(modalEl));

            return {
                title: title,
                primary_heading: headings[0] || title || 'App Screen',
                headings: headings,
                active_tab: activeTab,
                is_modal_open: isModalOpen,
                nav_links: navLinks.slice(0, 150),
                config_cards: configCards.slice(0, 50),
                kpi_cards: kpiCards.slice(0, 30),
                pill_tabs: pillTabs.slice(0, 50),
                action_buttons: actionButtons.slice(0, 80),
                inputs: inputs.slice(0, 60),
                total_tables: tablesCount
            };
        }
        """)

    def _detect_and_explore_modals(self, page, screen_name: str, reports_dir: str, screen_idx: int) -> list:
        """
        Discovers and triggers action buttons that open modal dialogs, slide-overs, or drawers (e.g. Create Lead, Add Customer, Filters, Add Status).
        Captures dedicated modal snapshots and extracts full form element trees before safely dismissing.
        Also captures child action screens if an action button navigates to a dedicated child form route.
        """
        discovered_modals = []
        
        modal_trigger_keywords = [
            "create", "add", "new", "filter", "filters", "edit", 
            "configure", "config", "import", "export", "upload", "invite",
            "assign", "schedule", "options", "customize", "generate",
            "clone", "duplicate", "connect", "link", "sync"
        ]

        summary = self._extract_page_summary(page)
        action_buttons = list(summary.get("action_buttons", []))

        # Dynamically discover explicit modal / drawer trigger attributes
        try:
            attr_modal_triggers = page.evaluate("""() => {
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const isVis = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && window.getComputedStyle(el).display !== 'none';
                };
                const triggers = [];
                const sels = [
                    '[data-bs-toggle="modal"]', '[data-toggle="modal"]', '[aria-haspopup="dialog"]',
                    '[data-drawer-target]', '[data-modal-target]', '[data-bs-toggle="offcanvas"]',
                    '[data-dialog-trigger]', '[data-state="closed"][aria-haspopup]',
                    'button:has(svg.lucide-plus)', 'button:has(svg.lucide-filter)',
                    'button:has(svg.lucide-sliders-horizontal)', 'button[aria-haspopup="dialog"]'
                ];
                document.querySelectorAll(sels.join(', ')).forEach(el => {
                    // Exclude sidebar or header elements
                    if (el.closest('aside, nav, .sidebar, header, [role="navigation"]')) return;
                    if (isVis(el)) {
                        const txt = clean(el.innerText || el.getAttribute('aria-label') || el.title || '');
                        if (txt && !triggers.includes(txt) && txt.length <= 40) {
                            triggers.push(txt);
                        }
                    }
                });
                return triggers;
            }""")
            for trg in (attr_modal_triggers or []):
                if trg not in action_buttons and classify_action_intent(trg) != ActionSafetyCategory.DESTRUCTIVE:
                    action_buttons.append(trg)
        except Exception:
            pass

        seen_triggers = set()
        for btn_text in action_buttons:
            # L-06 Action Safety Shield: Block destructive or irreversible state-changing actions
            intent = classify_action_intent(btn_text)
            if intent == ActionSafetyCategory.DESTRUCTIVE:
                print(f"[Flow Explorer] Safety Shield: Skipping destructive action button '{btn_text}' during discovery")
                continue

            btn_norm = btn_text.strip().lower()
            if not btn_norm or btn_norm in seen_triggers:
                continue

            matches_kw = any(k in btn_norm for k in modal_trigger_keywords) or btn_norm == "+" or btn_norm.startswith("+ ")
            is_boilerplate = any(b in btn_norm for b in ["delete", "remove", "logout", "cancel", "close", "save", "submit", "apply", "reset"])
            
            if matches_kw and not is_boilerplate:
                seen_triggers.add(btn_norm)
                try:
                    safe_btn = btn_text.replace('"', '\\"')
                    btn_loc = page.locator(
                        f'main button:has-text("{safe_btn}"), .content button:has-text("{safe_btn}"), button:has-text("{safe_btn}"), '
                        f'a:has-text("{safe_btn}"), [role="button"]:has-text("{safe_btn}"), .btn:has-text("{safe_btn}")'
                    ).filter(has_not=page.locator('aside *, nav *, .sidebar *')).first

                    if btn_loc.count() == 0 or not btn_loc.is_visible():
                        btn_loc = page.locator(f'button:has-text("{safe_btn}"), a:has-text("{safe_btn}"), [role="button"]:has-text("{safe_btn}")').first

                    if btn_loc.count() > 0 and btn_loc.is_visible():
                        orig_modal_url = page.url
                        print(f"[Flow Explorer] Triggering action/modal button: '{btn_text}' on '{screen_name}'")
                        btn_loc.click(force=True)
                        _safe_settle(page, min_ms=350)

                        # Case 1: Clicking navigated away to a child action route (e.g. /leads/create)
                        if page.url.rstrip("/").lower() != orig_modal_url.rstrip("/").lower():
                            new_route_url = page.url
                            print(f"[Flow Explorer] Button '{btn_text}' navigated to child action route: {new_route_url}")
                            child_sum = self._extract_page_summary(page)
                            child_name = child_sum.get("primary_heading") or f"{screen_name} - {btn_text}"
                            child_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            child_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{btn_text}")[:25].strip("_")
                            child_ss = os.path.join(reports_dir, f"action_route_{screen_idx}_{child_slug}_{child_ts}.png")
                            try:
                                capture_full_scroll_screenshot(page, child_ss)
                            except Exception:
                                pass
                            
                            child_rec = {
                                "type": "action_route",
                                "name": child_name,
                                "trigger": btn_text,
                                "url": new_route_url,
                                "screenshot": child_ss,
                                "headings": child_sum.get("headings", []),
                                "inputs": child_sum.get("inputs", []),
                                "action_buttons": child_sum.get("action_buttons", []),
                                "tables_count": child_sum.get("total_tables", 0)
                            }
                            discovered_modals.append(child_rec)
                            
                            # Return back to original screen
                            back_btn = page.locator('button:has-text("Back"), a:has-text("Back"), [aria-label*="Back" i]').first
                            if back_btn.count() > 0 and back_btn.is_visible():
                                back_btn.click(force=True)
                                _safe_settle(page, min_ms=300)
                            else:
                                page.go_back()
                                _safe_settle(page, min_ms=300)
                            if page.url.rstrip("/").lower() != orig_modal_url.rstrip("/").lower():
                                page.goto(orig_modal_url, wait_until="domcontentloaded", timeout=12000)
                                _safe_settle(page, min_ms=300)
                            continue

                        # Case 2: In-Page Modal, Drawer, or Popover dialog
                        modal_info = page.evaluate("""() => {
                            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                            const isVis = el => {
                                if (!el) return false;
                                const r = el.getBoundingClientRect();
                                return r.width > 40 && r.height > 40 && 
                                       window.getComputedStyle(el).display !== 'none' && 
                                       window.getComputedStyle(el).visibility !== 'hidden';
                            };
                            
                            const modalSelectors = [
                                '[role="dialog"]:not([aria-hidden="true"])',
                                '[role="alertdialog"]:not([aria-hidden="true"])',
                                'dialog[open]',
                                '.modal.show', '.modal.in', '.modal[style*="display: block"]',
                                '.ant-modal-wrap:not([style*="display: none"]) .ant-modal',
                                '.ant-modal:not([style*="display: none"])',
                                '.chakra-modal__content',
                                '.MuiDialog-root:not([aria-hidden="true"]) [role="dialog"]',
                                '.MuiModal-root:not([aria-hidden="true"]) [role="dialog"]',
                                '[data-headlessui-state="open"][role="dialog"]',
                                'div[data-state="open"][role="dialog"]',
                                '.drawer.open, .drawer.show, [class*="drawer"][class*="open"], [class*="drawer"][class*="show"]',
                                '[data-radix-popper-content-wrapper]',
                                '[class*="popover"]:not([class*="hidden"]):not([style*="display: none"])',
                                'div.shadow-xl.z-50:not(header):not(nav):not(aside)',
                                'div[class*="shadow"][class*="z-50"]:not(header):not(nav):not(aside)',
                                'div.fixed.inset-0.flex:not(aside):not(header):not(nav):not([id="root"]):has(button, input, h1, h2, h3, h4, h5)',
                                'div[class*="fixed"][class*="z-"]:not(aside):not(header):not(nav):not([id="root"]):has(button, input, h1, h2, h3, h4, h5)'
                            ];
                            
                            let modalEl = null;
                            for (const s of modalSelectors) {
                                const found = Array.from(document.querySelectorAll(s)).filter(isVis);
                                if (found.length > 0) {
                                    modalEl = found[found.length - 1];
                                    break;
                                }
                            }
                            
                            if (!modalEl) {
                                return { is_modal: false, inputs: [], buttons: [], headings: [] };
                            }
                            
                            const inputs = [];
                            modalEl.querySelectorAll('input:not([type="hidden"]), select, textarea, [role="combobox"], [role="checkbox"], [role="radio"]').forEach(i => {
                                if (isVis(i)) {
                                    const p = clean(i.getAttribute('placeholder') || i.getAttribute('name') || i.getAttribute('aria-label') || i.id || i.type);
                                    if (p && !inputs.includes(p) && p.length < 40) inputs.push(p);
                                }
                            });
                            if (inputs.length === 0) {
                                modalEl.querySelectorAll('label').forEach(lbl => {
                                    if (isVis(lbl)) {
                                        const t = clean(lbl.innerText);
                                        if (t && !inputs.includes(t) && t.length < 50) inputs.push(t);
                                    }
                                });
                            }
                            
                            const buttons = [];
                            modalEl.querySelectorAll('button, input[type="submit"], input[type="button"], .btn, [role="button"]').forEach(b => {
                                if (isVis(b)) {
                                    const t = clean(b.innerText || b.value || b.getAttribute('aria-label'));
                                    if (t && !buttons.includes(t) && t.length < 35) buttons.push(t);
                                }
                            });
                            
                            const headings = Array.from(modalEl.querySelectorAll('h1, h2, h3, h4, h5, h6, .title, .modal-title, .drawer-title, strong, [class*="title"], [class*="heading"]'))
                                .filter(isVis).map(h => clean(h.innerText)).filter(Boolean);

                            const modalName = headings[0] || modalEl.getAttribute('aria-label') || '';

                            return {
                                is_modal: true,
                                modal_name: modalName,
                                inputs: inputs.slice(0, 25),
                                buttons: buttons.slice(0, 25),
                                headings: headings.slice(0, 10)
                            };
                        }""")

                        if modal_info.get("is_modal"):
                            sub_modal_name = modal_info.get("modal_name")
                            if not sub_modal_name or sub_modal_name.lower() == screen_name.lower():
                                sub_modal_name = f"{screen_name} - {btn_text} Dialog"
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
                            print(f"[Flow Explorer] Captured True Modal: '{sub_modal_name}' with {len(modal_info['inputs'])} inputs, {len(modal_info['buttons'])} buttons.")

                            # Dismiss modal safely
                            dismissed = False
                            try:
                                close_btn = page.locator(
                                    '[role="dialog"] button:has(svg), .modal button:has(svg), .drawer button:has(svg), '
                                    'div.fixed button:has(svg), div.shadow-xl.z-50 button:has(svg), [role="dialog"] [aria-label*="close" i], '
                                    'button:has-text("Cancel"), button:has-text("Close"), [role="dialog"] button:has-text("Cancel"), '
                                    '[role="dialog"] .close, [role="dialog"] [data-dismiss="modal"], '
                                    '.drawer [aria-label*="close" i], .modal [aria-label*="close" i]'
                                ).first
                                if close_btn.count() > 0 and close_btn.is_visible():
                                    close_btn.click(force=True)
                                    _safe_settle(page, min_ms=250)
                                    dismissed = True
                            except Exception:
                                pass

                            if not dismissed:
                                page.keyboard.press("Escape")
                                _safe_settle(page, min_ms=250)

                            # If floating popover / filter panel is still open, toggle it closed with the trigger button
                            try:
                                still_open = page.evaluate("""() => {
                                    const s = '[role="dialog"]:not([aria-hidden="true"]), [data-radix-popper-content-wrapper], div.shadow-xl.z-50, [class*="popover"]';
                                    const el = document.querySelector(s);
                                    return el && el.offsetWidth > 40 && el.offsetHeight > 40;
                                }""")
                                if still_open and btn_loc.count() > 0 and btn_loc.is_visible():
                                    btn_loc.click(force=True)
                                    _safe_settle(page, min_ms=250)
                            except Exception:
                                pass

                            # Safe backdrop cleanup without ever touching root layout containers
                            try:
                                page.evaluate("""() => {
                                    document.querySelectorAll('.modal-backdrop, .offcanvas-backdrop, .drawer-overlay').forEach(el => {
                                        el.classList.remove('open', 'show');
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

    def _explore_config_sub_pages(self, page, screen_name: str, reports_dir: str, screen_idx: int, config_base_url: str, candidate_modules: Optional[list] = None) -> list:
        """
        Deeply explores module cards, configuration tiles, and settings sections across any web application.
        Navigates into each unique sub-module tile, captures snapshots, form elements and inner modals,
        and uses resilient multi-tier back-navigation to return to base view.
        """
        discovered_sub_configs = []
        
        # If candidate modules not supplied, discover dynamically from page summary
        if candidate_modules is None:
            curr_summary = self._extract_page_summary(page)
            candidate_modules = curr_summary.get("config_cards", [])

        # Filter out destructive actions and limit to max 12 unique modules to balance depth and speed
        target_modules = []
        seen_targets = set()
        for mod in (candidate_modules or []):
            mod_str = str(mod).strip()
            mod_low = mod_str.lower()
            if mod_str and mod_low not in seen_targets and len(mod_str) <= 45:
                if classify_action_intent(mod_str) != ActionSafetyCategory.DESTRUCTIVE:
                    seen_targets.add(mod_low)
                    target_modules.append(mod_str)
                    if len(target_modules) >= 12:
                        break

        if not target_modules:
            return discovered_sub_configs

        print(f"[Flow Explorer] Dynamically exploring {len(target_modules)} module tiles on '{screen_name}'...")

        for idx, mod_kw in enumerate(target_modules, 1):
            try:
                # Ensure we are on root configuration / hub page
                if page.url.rstrip("/") != config_base_url.rstrip("/"):
                    page.goto(config_base_url, wait_until="domcontentloaded", timeout=12000)
                    _safe_settle(page, min_ms=250)

                # Locate the tile / card element
                card_loc = page.locator(
                    f'h1:has-text("{mod_kw}"), h2:has-text("{mod_kw}"), h3:has-text("{mod_kw}"), h4:has-text("{mod_kw}"), h5:has-text("{mod_kw}"), a:has-text("{mod_kw}"), button:has-text("{mod_kw}"), [role="button"]:has-text("{mod_kw}")'
                ).filter(has_text=re.compile(rf'^{re.escape(mod_kw)}$', re.IGNORECASE))
                
                if card_loc.count() == 0 or not card_loc.first.is_visible():
                    card_loc = page.locator(
                        f'a:has-text("{mod_kw}"), [role="button"]:has-text("{mod_kw}"), button:has-text("{mod_kw}"), div:has-text("{mod_kw}")'
                    ).filter(has_text=mod_kw).last

                if card_loc.count() > 0 and card_loc.first.is_visible():
                    print(f"[Flow Explorer] Navigating into Module Tile: '{mod_kw}'")
                    card_loc.first.click(force=True)
                    _safe_settle(page, min_ms=400)

                    sub_summary = self._extract_page_summary(page)
                    cfg_heading = sub_summary.get("primary_heading") or f"Module: {mod_kw}"
                    sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    cfg_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"module_{mod_kw}")[:25].strip("_")
                    cfg_ss = os.path.join(reports_dir, f"module_{screen_idx}_{idx}_{cfg_slug}_{sub_ts}.png")

                    try:
                        capture_full_scroll_screenshot(page, cfg_ss)
                    except Exception:
                        pass

                    # Also explore any modals inside this sub-module
                    inner_modals = self._detect_and_explore_modals(page, cfg_heading, reports_dir, screen_idx)

                    cfg_record = {
                        "type": "config_module",
                        "name": f"Module: {cfg_heading}",
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
                    print(f"[Flow Explorer] Captured Sub-Module '{cfg_heading}' at {page.url} ({len(cfg_record['action_buttons'])} btns, {len(cfg_record['inputs'])} inputs)")

                    # Return back to base view using 3-tier back-navigation
                    if page.url.rstrip("/") != config_base_url.rstrip("/"):
                        back_btn = page.locator('button:has-text("Back"), a:has-text("Back"), [aria-label*="Back" i]').first
                        if back_btn.count() > 0 and back_btn.is_visible():
                            back_btn.click(force=True)
                            _safe_settle(page, min_ms=300)
                        else:
                            page.go_back()
                            _safe_settle(page, min_ms=300)

                    if page.url.rstrip("/").lower() != config_base_url.rstrip("/").lower():
                        page.goto(config_base_url, wait_until="domcontentloaded", timeout=12000)
                        _safe_settle(page, min_ms=250)

                    # Neutralize any drawer/backdrop overlay leftover
                    try:
                        page.evaluate("""() => {
                            document.querySelectorAll('.drawer-overlay, .modal-backdrop, [class*="overlay"][class*="open"], [class*="backdrop"]').forEach(el => {
                                el.classList.remove('open', 'show', 'active');
                                el.style.display = 'none';
                                el.style.pointerEvents = 'none';
                            });
                        }""")
                    except Exception:
                        pass

            except Exception as ce:
                print(f"[Flow Explorer] Note exploring module tile '{mod_kw}': {ce}")
                try:
                    page.goto(config_base_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                continue

        return discovered_sub_configs

    def _bounded_lazy_content_scroll(self, page, max_steps: int = 6, max_duration_ms: int = 3000) -> dict:
        """
        Progressively scrolls the viewport in 75% steps to reveal lazy-loaded charts,
        metric cards, and data-grid rows, halting cleanly on infinite-scroll pages.
        """
        try:
            return page.evaluate("""(args) => {
                const maxSteps = args.maxSteps || 6;
                const maxDurationMs = args.maxDurationMs || 3000;
                const startTime = Date.now();
                
                let prevHeight = document.body.scrollHeight || document.documentElement.scrollHeight;
                let stableHeightCount = 0;
                let stepsTaken = 0;
                let changedContent = false;
                
                for (let i = 0; i < maxSteps; i++) {
                    if (Date.now() - startTime >= maxDurationMs) break;
                    const viewH = window.innerHeight || 800;
                    window.scrollBy({ top: Math.floor(viewH * 0.75), behavior: 'instant' });
                    stepsTaken++;
                    
                    const curHeight = document.body.scrollHeight || document.documentElement.scrollHeight;
                    if (curHeight > prevHeight + 50) {
                        changedContent = true;
                        stableHeightCount = 0;
                        prevHeight = curHeight;
                    } else {
                        stableHeightCount++;
                    }
                    if (stableHeightCount >= 2) break;
                }

                // Restore scroll to top
                window.scrollTo({ top: 0, behavior: 'instant' });
                return {
                    stepsTaken: stepsTaken,
                    changedContent: changedContent,
                    totalHeight: prevHeight
                };
            }""", {"maxSteps": max_steps, "maxDurationMs": max_duration_ms})
        except Exception:
            return {"stepsTaken": 0, "changedContent": False, "totalHeight": 0}

    def _explore_kpi_cards(self, page, screen_name: str, reports_dir: str, screen_idx: int, base_url: str) -> list:
        """Explores interactive KPI and metric cards on any active screen with baseline restoration."""
        discovered_kpis = []
        summary = self._extract_page_summary(page)
        kpi_cards = summary.get("kpi_cards", [])

        for kpi in kpi_cards:
            kpi_lbl = kpi.get("label", str(kpi)) if isinstance(kpi, dict) else str(kpi)
            kpi_val = kpi.get("value", "") if isinstance(kpi, dict) else ""
            is_interactive = kpi.get("is_interactive", True) if isinstance(kpi, dict) else True

            if not kpi_lbl or len(kpi_lbl) < 2:
                continue

            try:
                kpi_loc = page.locator('a, button, [role="button"], [class*="card"], [class*="kpi"], [class*="stat"], [class*="tile"]').filter(has_text=kpi_lbl).first
                if kpi_loc.count() == 0:
                    kpi_loc = page.get_by_text(kpi_lbl).first

                if kpi_loc.count() > 0 and kpi_loc.is_visible() and is_interactive:
                    print(f"[Flow Explorer] Exploring KPI Card: '{kpi_lbl}' on '{screen_name}'")
                    init_url = page.url
                    kpi_loc.click(force=True)
                    _safe_settle(page, min_ms=350)

                    kpi_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    kpi_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"kpi_{kpi_lbl}")[:25].strip("_")
                    kpi_ss = os.path.join(reports_dir, f"kpi_{screen_idx}_{kpi_slug}_{kpi_ts}.png")

                    try:
                        capture_full_scroll_screenshot(page, kpi_ss)
                    except Exception:
                        pass

                    kpi_summary = self._extract_page_summary(page)
                    kpi_record = {
                        "type": "kpi_card",
                        "name": f"KPI: {kpi_lbl}" + (f" ({kpi_val})" if kpi_val else ""),
                        "trigger": kpi_lbl,
                        "value": kpi_val,
                        "url": page.url,
                        "screenshot": kpi_ss,
                        "headings": kpi_summary.get("headings", []),
                        "action_buttons": kpi_summary.get("action_buttons", []),
                        "inputs": kpi_summary.get("inputs", []),
                    }
                    discovered_kpis.append(kpi_record)

                    # Baseline Restoration: Return to original screen
                    if page.url != init_url:
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=5000)
                            _safe_settle(page, min_ms=250)
                        except Exception:
                            page.goto(base_url, wait_until="domcontentloaded", timeout=12000)
                            _safe_settle(page, min_ms=250)
                    else:
                        page.keyboard.press("Escape")
                        _safe_settle(page, min_ms=150)
            except Exception as ke:
                print(f"[Flow Explorer] Note exploring KPI '{kpi_lbl}': {ke}")
                try:
                    page.goto(base_url, wait_until="domcontentloaded")
                except Exception:
                    pass
                continue

        return discovered_kpis

    # Backward compatibility alias
    _explore_dashboard_kpis = _explore_kpi_cards

    def _explore_screen_sub_tabs(self, page, screen_name: str, reports_dir: str, screen_idx: int) -> list:
        """Explores all screen tabs (pill tabs, tab panels, sub-tabs, filter tabs) across the active screen."""
        discovered_tabs = []
        summary = self._extract_page_summary(page)
        pill_tabs = summary.get("pill_tabs", [])

        for tab_text in pill_tabs:
            if not tab_text or len(tab_text) > 40:
                continue
            try:
                tab_selectors = [
                    f'button:has-text("{tab_text}")',
                    f'a:has-text("{tab_text}")',
                    f'.pill-tab:has-text("{tab_text}")',
                    f'[role="tab"]:has-text("{tab_text}")',
                    f'.nav-link:has-text("{tab_text}")',
                    f'.ant-tabs-tab:has-text("{tab_text}")',
                    f'.MuiTab-root:has-text("{tab_text}")',
                ]
                tab_loc = None
                for t_sel in tab_selectors:
                    try:
                        cand = page.locator(t_sel).first
                        if cand.count() > 0:
                            tab_loc = cand
                            break
                    except Exception:
                        continue

                if tab_loc and tab_loc.count() > 0:
                    try:
                        tab_loc.scroll_into_view_if_needed(timeout=1000)
                    except Exception:
                        pass
                    tab_loc.click(force=True)
                    _safe_settle(page, min_ms=350)
                    
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

                    # Check if clicking this tab opened a popup drawer, datepicker, or modal dialog (e.g. Custom Date drawer)
                    try:
                        has_overlay = page.evaluate("""() => {
                            const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                            const ov = document.querySelector('.drawer-overlay.open, .drawer.open, [role="dialog"], .modal.show, [class*="drawer"][class*="open"], [class*="overlay"][class*="open"]');
                            return isVis(ov);
                        }""")
                        if has_overlay:
                            close_btn = page.locator('.drawer button:has(svg), .drawer-overlay button:has(svg), button:has(svg.lucide-x), [aria-label*="close" i], button:has-text("Cancel"), button:has-text("Close"), .close').first
                            if close_btn.count() > 0 and close_btn.is_visible():
                                close_btn.click(force=True)
                                _safe_settle(page, min_ms=250)
                            page.keyboard.press("Escape")
                            _safe_settle(page, min_ms=200)
                            page.evaluate("""() => {
                                document.querySelectorAll('.drawer-overlay, .modal-backdrop, [class*="backdrop"]').forEach(el => {
                                    el.style.display = 'none';
                                    el.style.pointerEvents = 'none';
                                    el.classList.remove('open');
                                });
                            }""")
                    except Exception:
                        pass
            except Exception as te:
                print(f"[Flow Explorer] Note during tab exploration of '{tab_text}': {te}")
                continue

        # Restore default/first tab and clean up any remaining tab overlays
        try:
            if pill_tabs:
                first_tab = page.locator(f'.pill-tab:has-text("{pill_tabs[0]}"), button:has-text("{pill_tabs[0]}")').first
                if first_tab.count() > 0 and first_tab.is_visible():
                    first_tab.click(force=True)
                    _safe_settle(page, min_ms=200)
            page.evaluate("""() => {
                document.querySelectorAll('.drawer-overlay.open, .drawer-overlay, .modal-backdrop, [class*="backdrop"]').forEach(el => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                    el.classList.remove('open');
                });
            }""")
        except Exception:
            pass

        return discovered_tabs

    def _explore_screen_dropdowns(self, page, screen_name: str, reports_dir: str, screen_idx: int) -> list:
        """
        Discovers dropdown triggers, select elements, and comboboxes.
        Opens each dropdown to reveal its available options/menu items,
        captures dedicated snapshots, and records the selectable values for test generation.
        """
        discovered_dropdowns = []
        try:
            # 1. Native <select> elements: Extract options directly
            native_selects = page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('select').forEach((sel, idx) => {
                    const isVis = sel.offsetWidth > 0 && sel.offsetHeight > 0;
                    if (isVis) {
                        const name = sel.getAttribute('name') || sel.getAttribute('aria-label') || sel.id || `Select_${idx + 1}`;
                        const opts = Array.from(sel.querySelectorAll('option')).map(o => (o.innerText || o.value || '').trim()).filter(Boolean);
                        if (opts.length > 0) {
                            results.push({ name: name, options: opts.slice(0, 20) });
                        }
                    }
                });
                return results;
            }""")
            for ns in native_selects:
                discovered_dropdowns.append({
                    "type": "dropdown",
                    "name": f"{screen_name} - {ns['name']} Dropdown",
                    "label": ns["name"],
                    "options": ns["options"],
                    "url": page.url,
                    "screenshot": "",
                })

            # 2. Custom SPA Dropdowns (Bootstrap, AntD, MUI, Radix UI, Tailwind)
            custom_triggers = page.evaluate("""() => {
                const triggers = [];
                const isVis = el => {
                    if (!el) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0;
                };
                const sel = [
                    '[data-bs-toggle="dropdown"]', '.dropdown-toggle', '[role="combobox"]',
                    '.ant-select-selector', '.MuiSelect-select', 'button[aria-haspopup="true"]',
                    'button[aria-haspopup="listbox"]', 'button[data-state]',
                    '[class*="dropdown-trigger"]', '[class*="select-trigger"]',
                    'button:has(svg.lucide-chevron-down)', 'button:has(svg.lucide-chevrons-up-down)'
                ].join(', ');
                document.querySelectorAll(sel).forEach((el, idx) => {
                    if (isVis(el)) {
                        const txt = (el.innerText || el.getAttribute('aria-label') || el.title || '').replace(/\\s+/g, ' ').trim();
                        if (txt && txt.length < 35 && !txt.toLowerCase().includes('user') && !txt.toLowerCase().includes('profile')) {
                            triggers.push({ index: idx, text: txt });
                        }
                    }
                });
                return triggers;
            }""")

            for trigger in custom_triggers:
                t_text = trigger["text"]
                try:
                    trig_loc = page.locator(f'[data-bs-toggle="dropdown"]:has-text("{t_text}"), .dropdown-toggle:has-text("{t_text}"), [role="combobox"]:has-text("{t_text}"), button[aria-haspopup="true"]:has-text("{t_text}"), button:has-text("{t_text}")').first
                    if trig_loc.count() > 0 and trig_loc.is_visible():
                        trig_loc.click(force=True)
                        _safe_settle(page, min_ms=300)

                        # Extract dropdown menu options
                        opts = page.evaluate("""() => {
                            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                            const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                            const optionSels = [
                                '[role="option"]', '[role="listbox"] [role="option"]',
                                '[data-radix-collection-item]', '[data-radix-popper-content-wrapper] [role="option"]',
                                '.dropdown-menu .dropdown-item', '.ant-select-item-option',
                                '[class*="dropdown-item"]', '.MuiMenuItem-root', 'li[role="option"]',
                                '.dropdown-menu a', '.dropdown-menu button', '[class*="popover"] [role="option"]',
                                '[class*="popover"] button'
                            ];
                            let items = [];
                            for (const s of optionSels) {
                                document.querySelectorAll(s).forEach(o => {
                                    if (isVis(o)) {
                                        const t = clean(o.innerText || o.getAttribute('data-value') || '');
                                        if (t && !items.includes(t) && t.length < 50) items.push(t);
                                    }
                                });
                                if (items.length > 0) break;
                            }
                            return items.slice(0, 20);
                        }""")

                        if opts:
                            dd_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{t_text}")[:20].strip("_")
                            dd_ss = os.path.join(reports_dir, f"dropdown_{screen_idx}_{safe_slug}_{dd_ts}.png")
                            try:
                                page.screenshot(path=dd_ss, full_page=False)
                            except Exception:
                                pass

                            discovered_dropdowns.append({
                                "type": "dropdown",
                                "name": f"{screen_name} - {t_text} Dropdown",
                                "label": t_text,
                                "options": opts,
                                "screenshot": dd_ss,
                                "url": page.url,
                            })

                        # Safely close dropdown with Escape or toggle click
                        page.keyboard.press("Escape")
                        _safe_settle(page, min_ms=200)
                        try:
                            still_open_dd = page.evaluate("""() => {
                                const popup = document.querySelector('[role="listbox"], [data-radix-popper-content-wrapper], [class*="popover"]');
                                return popup && popup.offsetWidth > 0 && popup.offsetHeight > 0;
                            }""")
                            if still_open_dd and trig_loc.count() > 0 and trig_loc.is_visible():
                                trig_loc.click(force=True)
                                _safe_settle(page, min_ms=200)
                        except Exception:
                            pass
                except Exception as d_err:
                    print(f"[Flow Explorer] Dropdown probe note for '{t_text}': {d_err}")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
        except Exception as ge:
            print(f"[Flow Explorer] General dropdown probe note: {ge}")

        return discovered_dropdowns

    def _explore_screen_drilldowns(
        self,
        page,
        screen_name: str,
        reports_dir: str,
        screen_idx: int,
        discovered_screens: list,
        seen_screen_signatures: set,
        visited_urls: set,
        queued_urls: set,
        queue,
        cand_depth: int,
        on_progress_callback=None,
    ) -> list:
        """
        Discovers table row action buttons, drill-down cards, and child views (e.g. 'Units Detail', 'View Details', 'Orders', 'Manage').
        Clicks each unique drilldown target:
        - If the URL changes, captures the child screen into discovered_screens and navigates back.
        - If the URL does not change, inspects open drawers/modals and dismisses safely.
        """
        drilldown_features = []
        drilldown_keywords = [
            "units detail", "detail", "details", "manage", "view", "open", "edit",
            "inspect", "show", "preview", "history", "units", "items",
            "profile", "configure", "customize", "explore", "orders", "logs", "audit",
            "records", "expand", "sub-", "status", "info", "more"
        ]

        summary = self._extract_page_summary(page)
        action_buttons = summary.get("action_buttons", [])

        # Find unique candidates
        candidates = []
        for btn_text in action_buttons:
            low = btn_text.lower().strip()
            if any(k in low for k in drilldown_keywords) and not any(k in low for k in ["delete", "remove", "logout", "cancel", "close", "filter", "branch", "branches"]):
                if btn_text not in candidates:
                    candidates.append(btn_text)

        # Also sample action buttons directly from the first 2 rows of any table or data grid
        try:
            table_row_actions = page.evaluate("""() => {
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const rowButtons = [];
                const rows = document.querySelectorAll('table tbody tr, [role="row"]:not([role="rowheader"]), [data-row-key]');
                for (let i = 0; i < Math.min(rows.length, 3); i++) {
                    const row = rows[i];
                    if (!isVis(row)) continue;
                    row.querySelectorAll('button, a, [role="button"]').forEach(b => {
                        if (!isVis(b)) return;
                        const t = clean(b.innerText || b.getAttribute('aria-label') || b.title || '');
                        if (t && t.length >= 2 && t.length <= 30) {
                            const low = t.toLowerCase();
                            if (!rowButtons.includes(t) && !low.includes('delete') && !low.includes('remove') && !low.includes('trash') && !low.includes('close')) {
                                rowButtons.push(t);
                            }
                        }
                    });
                }
                return rowButtons;
            }""")
            for t_btn in (table_row_actions or []):
                if t_btn not in candidates and classify_action_intent(t_btn) != ActionSafetyCategory.DESTRUCTIVE:
                    candidates.append(t_btn)
        except Exception:
            pass

        # Prioritize explicit drill-down navigation actions (units, detail, view, items, branches) ahead of form modals (edit, add)
        def _drilldown_score(t: str) -> int:
            low = t.lower()
            if any(k in low for k in ["unit", "detail", "view", "item", "branch", "history", "order", "profile", "manage"]):
                return 0
            return 1

        candidates.sort(key=_drilldown_score)

        # Limit to max 4 unique drilldown buttons per screen to prevent infinite loops while exploring child views
        for btn_text in candidates[:4]:
            intent = classify_action_intent(btn_text)
            if intent == ActionSafetyCategory.DESTRUCTIVE:
                continue

            try:
                btn_loc = page.locator(f'button:has-text("{btn_text}"), a:has-text("{btn_text}"), [role="button"]:has-text("{btn_text}")').first
                if btn_loc.count() > 0 and btn_loc.is_visible():
                    orig_url = page.url
                    print(f"[Flow Explorer] Testing drill-down action '{btn_text}' on '{screen_name}'...")
                    btn_loc.click(force=True)
                    _safe_settle(page, min_ms=500)
                    new_url = page.url

                    # Check if URL changed -> It's a new child screen!
                    if new_url.rstrip("/").lower() != orig_url.rstrip("/").lower():
                        child_summary = self._extract_page_summary(page)
                        child_title = child_summary.get("primary_heading") or f"{screen_name} - {btn_text}"
                        child_fp = _dom_fingerprint(child_summary, new_url)
                        stable_child_url = _clean_stable_url(new_url)
                        already_captured_urls = {_clean_stable_url(s.get("url", "")) for s in discovered_screens}

                        if child_fp not in seen_screen_signatures and stable_child_url not in visited_urls and stable_child_url not in already_captured_urls:
                            seen_screen_signatures.add(child_fp)
                            seen_screen_signatures.add(stable_child_url)
                            seen_screen_signatures.add(child_title.lower())
                            visited_urls.add(stable_child_url)

                            next_screen_idx = len(discovered_screens) + 1
                            child_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{btn_text}")[:22].strip("_")
                            child_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            child_ss = os.path.join(reports_dir, f"screen_{next_screen_idx}_{child_slug}_{child_ts}.png")
                            try:
                                capture_full_scroll_screenshot(page, child_ss)
                            except Exception:
                                pass

                            # Deep explore sub-features of the new child screen
                            child_sub_features = []
                            child_sub_features.extend(self._detect_and_explore_modals(page, child_title, reports_dir, next_screen_idx))
                            child_sub_features.extend(self._explore_screen_sub_tabs(page, child_title, reports_dir, next_screen_idx))

                            child_record = {
                                "step_num": next_screen_idx,
                                "name": child_title,
                                "url": new_url,
                                "depth": cand_depth + 1,
                                "screenshot": child_ss,
                                "title": child_summary.get("title", ""),
                                "headings": child_summary.get("headings", []),
                                "action_buttons": child_summary.get("action_buttons", []),
                                "inputs": child_summary.get("inputs", []),
                                "nav_links": child_summary.get("nav_links", []),
                                "pill_tabs": child_summary.get("pill_tabs", []),
                                "config_cards": child_summary.get("config_cards", []),
                                "kpi_cards": child_summary.get("kpi_cards", []),
                                "tables_count": child_summary.get("total_tables", 0),
                                "sub_features": _deduplicate_sub_features(child_sub_features),
                            }
                            discovered_screens.append(child_record)
                            print(f"[Flow Explorer] Captured Child Screen #{next_screen_idx}: '{child_title}' at {new_url} (+{len(child_sub_features)} sub-features)")
                            if on_progress_callback:
                                on_progress_callback(next_screen_idx, f"Captured Child Screen #{next_screen_idx}: {child_title}", child_record)

                            # Enqueue child links from this new screen
                            for ch_cand in child_summary.get("nav_links", []):
                                ch_n = ch_cand.get("text", "").strip()
                                ch_h = str(ch_cand.get("href", "")).strip()
                                if ch_h:
                                    ch_u = urljoin(new_url, ch_h)
                                    if _is_safe_internal_link(ch_u, self.base_netloc):
                                        stable_ch = _clean_stable_url(ch_u)
                                        if stable_ch not in queued_urls and stable_ch not in visited_urls and stable_ch not in already_captured_urls:
                                            queued_urls.add(stable_ch)
                                            queue.append((ch_n or _href_to_title(ch_h), ch_h, ch_u, cand_depth + 2, new_url))
                                elif ch_n and ch_n.lower() not in seen_screen_signatures:
                                    queue.append((ch_n, ch_h, "", cand_depth + 2, new_url))

                        # Return back to origin screen
                        back_btn = page.locator('button:has-text("Back"), a:has-text("Back"), [aria-label*="Back" i]').first
                        if back_btn.count() > 0 and back_btn.is_visible():
                            back_btn.click(force=True)
                            _safe_settle(page, min_ms=400)
                        else:
                            page.go_back()
                            _safe_settle(page, min_ms=400)

                        if page.url.rstrip("/").lower() != orig_url.rstrip("/").lower():
                            page.goto(orig_url, wait_until="domcontentloaded", timeout=12000)
                            _safe_settle(page, min_ms=300)

                    else:
                        # URL did not change -> Check if inline drawer/modal opened
                        modal_info = page.evaluate("""() => {
                            const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                            const m = document.querySelector('[role="dialog"], .modal, .drawer, .drawer-content, .ant-modal, dialog, .fixed.inset-0');
                            return { has_dialog: isVis(m) };
                        }""")
                        if modal_info.get("has_dialog"):
                            drilldown_features.append({
                                "type": "modal",
                                "name": f"{screen_name} - {btn_text} Panel",
                                "trigger": btn_text,
                                "url": page.url,
                            })
                            # Cleanly dismiss drawer / modal
                            try:
                                close_btn = page.locator('[role="dialog"] button:has(svg), .modal button:has(svg), .drawer button:has(svg), div.shadow-xl.z-50 button:has(svg), button:has(svg.lucide-x), button:has(svg[class*="close" i]), button:has(svg[class*="x" i]), [aria-label*="close" i], [aria-label*="cancel" i], button:has-text("Close"), button:has-text("Cancel"), .modal-close, .close, [data-dismiss="modal"]').first
                                if close_btn.count() > 0 and close_btn.is_visible():
                                    close_btn.click(force=True)
                                    _safe_settle(page, min_ms=300)
                            except Exception:
                                pass
                            page.keyboard.press("Escape")
                            _safe_settle(page, min_ms=250)
                            
                            # Backdrop & overlay safety shield to prevent blocked pointer events
                            try:
                                page.evaluate("""() => {
                                    document.querySelectorAll('.modal-backdrop, .offcanvas-backdrop, [class*="backdrop"], .fixed.inset-0').forEach(e => {
                                        e.style.display = 'none';
                                        e.style.pointerEvents = 'none';
                                    });
                                }""")
                            except Exception:
                                pass
                            _safe_settle(page, min_ms=200)

            except Exception as de:
                print(f"[Flow Explorer] Drilldown exploration note for '{btn_text}': {de}")
                continue

        # Sample and test clickable table rows directly (SPAs with tr[onClick] => navigate)
        try:
            table_row_candidates = page.evaluate("""() => {
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const results = [];
                const rows = document.querySelectorAll('table tbody tr, [role="row"]:not([role="rowheader"]), [data-row-key]');
                
                let count = 0;
                for (let i = 0; i < rows.length && count < 2; i++) {
                    const row = rows[i];
                    if (!isVis(row)) continue;
                    const cells = row.querySelectorAll('td, [role="cell"]');
                    if (cells.length === 0) continue;
                    const fullTxt = clean(row.innerText);
                    if (!fullTxt || fullTxt.toLowerCase().includes('no data') || fullTxt.toLowerCase().includes('no records')) continue;
                    
                    const firstCell = clean(cells[0].innerText || cells[1]?.innerText || '').slice(0, 30);
                    const label = firstCell || `Row #${i+1}`;
                    
                    const style = window.getComputedStyle(row);
                    const isClickable = style.cursor === 'pointer' || 
                                        row.classList.contains('cursor-pointer') || 
                                        row.hasAttribute('onclick') || 
                                        row.getAttribute('role') === 'button' ||
                                        row.hasAttribute('data-href');
                                        
                    results.push({
                        index: i,
                        label: label,
                        is_clickable: Boolean(isClickable)
                    });
                    count++;
                }
                return results;
            }""")
        except Exception:
            table_row_candidates = []

        for r_info in (table_row_candidates or []):
            if not r_info.get("is_clickable") and r_info.get("index", 0) > 0:
                continue
            r_idx = r_info.get("index", 0)
            r_label = r_info.get("label", f"Row #{r_idx+1}")
            
            try:
                row_loc = page.locator('table tbody tr, [role="row"]:not([role="rowheader"]), [data-row-key]').nth(r_idx)
                if row_loc.count() > 0 and row_loc.is_visible():
                    orig_url = page.url
                    print(f"[Flow Explorer] Testing table row drilldown '{r_label}' (row #{r_idx}) on '{screen_name}'...")
                    row_cell = row_loc.locator('td, [role="cell"]').first
                    if row_cell.count() > 0 and row_cell.is_visible():
                        row_cell.click(force=True)
                    else:
                        row_loc.click(force=True)
                    _safe_settle(page, min_ms=500)
                    new_url = page.url
                    
                    # If URL changed -> child detail screen
                    if new_url.rstrip("/").lower() != orig_url.rstrip("/").lower():
                        child_summary = self._extract_page_summary(page)
                        child_title = child_summary.get("primary_heading") or f"{screen_name} - {r_label} Detail"
                        child_fp = _dom_fingerprint(child_summary, new_url)
                        stable_child_url = _clean_stable_url(new_url)
                        already_captured_urls = {_clean_stable_url(s.get("url", "")) for s in discovered_screens}

                        if child_fp not in seen_screen_signatures and stable_child_url not in visited_urls and stable_child_url not in already_captured_urls:
                            seen_screen_signatures.add(child_fp)
                            seen_screen_signatures.add(stable_child_url)
                            seen_screen_signatures.add(child_title.lower())
                            visited_urls.add(stable_child_url)

                            next_screen_idx = len(discovered_screens) + 1
                            child_slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{screen_name}_{r_label}")[:22].strip("_")
                            child_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            child_ss = os.path.join(reports_dir, f"screen_{next_screen_idx}_{child_slug}_{child_ts}.png")
                            try:
                                capture_full_scroll_screenshot(page, child_ss)
                            except Exception:
                                pass

                            child_sub_features = []
                            child_sub_features.extend(self._detect_and_explore_modals(page, child_title, reports_dir, next_screen_idx))
                            child_sub_features.extend(self._explore_screen_sub_tabs(page, child_title, reports_dir, next_screen_idx))
                            child_sub_features.extend(self._explore_screen_dropdowns(page, child_title, reports_dir, next_screen_idx))

                            child_record = {
                                "step_num": next_screen_idx,
                                "name": child_title,
                                "url": new_url,
                                "depth": cand_depth + 1,
                                "screenshot": child_ss,
                                "title": child_summary.get("title", ""),
                                "headings": child_summary.get("headings", []),
                                "action_buttons": child_summary.get("action_buttons", []),
                                "inputs": child_summary.get("inputs", []),
                                "nav_links": child_summary.get("nav_links", []),
                                "pill_tabs": child_summary.get("pill_tabs", []),
                                "config_cards": child_summary.get("config_cards", []),
                                "kpi_cards": child_summary.get("kpi_cards", []),
                                "tables_count": child_summary.get("total_tables", 0),
                                "sub_features": _deduplicate_sub_features(child_sub_features),
                            }
                            discovered_screens.append(child_record)
                            print(f"[Flow Explorer] Captured Table Detail Screen #{next_screen_idx}: '{child_title}' at {new_url} (+{len(child_sub_features)} sub-features)")
                            if on_progress_callback:
                                on_progress_callback(next_screen_idx, f"Captured Table Detail Screen #{next_screen_idx}: {child_title}", child_record)

                            # Enqueue child links from this new screen
                            for ch_cand in child_summary.get("nav_links", []):
                                ch_n = ch_cand.get("text", "").strip()
                                ch_h = str(ch_cand.get("href", "")).strip()
                                if ch_h:
                                    ch_u = urljoin(new_url, ch_h)
                                    if _is_safe_internal_link(ch_u, self.base_netloc):
                                        stable_ch = _clean_stable_url(ch_u)
                                        if stable_ch not in queued_urls and stable_ch not in visited_urls and stable_ch not in already_captured_urls:
                                            queued_urls.add(stable_ch)
                                            queue.append((ch_n or _href_to_title(ch_h), ch_h, ch_u, cand_depth + 2, new_url))
                                elif ch_n and ch_n.lower() not in seen_screen_signatures:
                                    queue.append((ch_n, ch_h, "", cand_depth + 2, new_url))

                            # Deep drill-down inspection for nested detail routes (e.g. 'View Detail' on inquiry detail page)
                            drilldown_features.extend(self._explore_screen_drilldowns(
                                page, child_title, reports_dir, next_screen_idx, discovered_screens,
                                seen_screen_signatures, visited_urls, queued_urls, queue, cand_depth + 1, on_progress_callback
                            ))

                        # Return back to origin table screen
                        back_btn = page.locator('button:has-text("Go Back"), button:has-text("Back"), a:has-text("Back"), [aria-label*="Back" i]').first
                        if back_btn.count() > 0 and back_btn.is_visible():
                            back_btn.click(force=True)
                            _safe_settle(page, min_ms=400)
                        else:
                            page.go_back()
                            _safe_settle(page, min_ms=400)

                        if page.url.rstrip("/").lower() != orig_url.rstrip("/").lower():
                            page.goto(orig_url, wait_until="domcontentloaded", timeout=12000)
                            _safe_settle(page, min_ms=300)

                    else:
                        # URL did not change -> check if inline drawer or preview panel opened
                        modal_info = page.evaluate("""() => {
                            const isVis = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
                            const m = document.querySelector('[role="dialog"], [data-radix-popper-content-wrapper], .modal, .drawer, .drawer-content, .ant-modal, dialog, .fixed.inset-0');
                            return { has_dialog: isVis(m) };
                        }""")
                        if modal_info.get("has_dialog"):
                            drilldown_features.append({
                                "type": "modal",
                                "name": f"{screen_name} - {r_label} Row Panel",
                                "trigger": f"Row #{r_idx+1}",
                                "url": page.url,
                            })
                            try:
                                close_btn = page.locator('[role="dialog"] button:has(svg), .modal button:has(svg), .drawer button:has(svg), div.shadow-xl.z-50 button:has(svg), button:has(svg.lucide-x), button:has(svg[class*="close" i]), button:has(svg[class*="x" i]), [aria-label*="close" i], [aria-label*="cancel" i], button:has-text("Close"), button:has-text("Cancel"), .modal-close, .close, [data-dismiss="modal"]').first
                                if close_btn.count() > 0 and close_btn.is_visible():
                                    close_btn.click(force=True)
                                    _safe_settle(page, min_ms=300)
                            except Exception:
                                pass
                            page.keyboard.press("Escape")
                            _safe_settle(page, min_ms=250)
                            try:
                                page.evaluate("""() => {
                                    document.querySelectorAll('.modal-backdrop, .offcanvas-backdrop, [class*="backdrop"], .fixed.inset-0').forEach(e => {
                                        e.style.display = 'none';
                                        e.style.pointerEvents = 'none';
                                    });
                                }""")
                            except Exception:
                                pass
                            _safe_settle(page, min_ms=200)

            except Exception as re_err:
                print(f"[Flow Explorer] Table row drilldown note for '{r_label}': {re_err}")
                continue

        return drilldown_features

    def explore_app_flows(self, start_url: str, max_screens: int = 0, max_depth: int = 3, explore_deep_subfeatures: bool = True, on_progress_callback=None, headless: Optional[bool] = None):
        """
        Autonomously discovers and navigates across internal app flows, tabs, configuration modules,
        slide menus, dropdowns, KPI cards, and action modals/drawers using a bounded BFS route queue,
        capturing full screenshots and element trees.
        """
        reports_dir = os.path.join("reports", "screenshots", "flows")
        os.makedirs(reports_dir, exist_ok=True)

        discovered_screens = []
        visited_urls = set()
        queued_urls = set()
        seen_screen_signatures = set()
        effective_max = max_screens if max_screens and max_screens > 0 else 999999
        effective_depth = 999999 if (max_depth is None or max_depth <= 0) else max_depth
        from urllib.parse import urlparse
        self.base_netloc = urlparse(start_url).netloc.lower()

        print(f"[Flow Explorer] Starting deep autonomous exploration from: {start_url} (Max Screens: {'Unlimited' if effective_max >= 999999 else effective_max}, Max Depth: {'Unlimited' if effective_depth >= 999999 else effective_depth})")
        if on_progress_callback:
            on_progress_callback(0, f"Launching browser session for {start_url}...", None)

        with sync_playwright() as p:
            browser = self._launch_browser(p, headless=headless)
            if os.path.exists("auth.json"):
                try:
                    context = browser.new_context(storage_state="auth.json")
                    with open("auth.json", "r", encoding="utf-8") as f:
                        auth_data = json.load(f)
                    from urllib.parse import urlparse
                    target_host = urlparse(start_url).netloc.lower()
                    for origin_entry in auth_data.get("origins", []):
                        orig = str(origin_entry.get("origin", "")).lower()
                        if not orig or target_host in orig or len(auth_data.get("origins", [])) == 1:
                            storage_items = origin_entry.get("localStorage", [])
                            if storage_items:
                                init_js = ";\n".join([
                                    f'try {{ localStorage.setItem({json.dumps(item["name"])}, {json.dumps(item["value"])}); }} catch(e){{}}'
                                    for item in storage_items if item.get("name")
                                ])
                                context.add_init_script(init_js)
                except Exception as ae:
                    print(f"[Flow Explorer] Note loading auth.json state: {ae}")
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
                root_landing_url = page.url or start_url
                visited_urls.add(_clean_stable_url(root_landing_url))
                queued_urls.add(_clean_stable_url(root_landing_url))

                # Expand slide menus, drawers, and sidebar accordions to expose all sub-modules
                try:
                    self._expand_slide_menus_and_accordions(page)
                except Exception:
                    pass
                _safe_settle(page, min_ms=250)

                # Bounded lazy-scroll pass to reveal dynamic widgets & KPI cards
                self._bounded_lazy_content_scroll(page, max_steps=4, max_duration_ms=2500)
                _safe_settle(page, min_ms=250)
                
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
                seen_screen_signatures.add(_clean_stable_url(page.url))
                
                # Deep sub-feature, tab, dropdown, KPI & modal inspection for root screen
                root_sub_features = []
                if explore_deep_subfeatures:
                    root_sub_features.extend(self._explore_screen_sub_tabs(page, root_name, reports_dir, 1))
                    root_sub_features.extend(self._detect_and_explore_modals(page, root_name, reports_dir, 1))
                    root_sub_features.extend(self._explore_screen_dropdowns(page, root_name, reports_dir, 1))
                    # Universal KPI discovery on root screen (no "dashboard" URL gating!)
                    root_sub_features.extend(self._explore_kpi_cards(page, root_name, reports_dir, 1, start_url))
                    # Tile / module card exploration on configuration and settings hubs
                    root_is_hub = any(k in (root_name.lower() + " " + page.url.lower()) for k in [
                        "config", "setting", "setup", "admin", "preference", "options"
                    ])
                    if root_is_hub and summary.get("config_cards"):
                        root_sub_features.extend(self._explore_config_sub_pages(
                            page, root_name, reports_dir, 1, start_url, summary.get("config_cards", [])
                        ))

                root_record = {
                    "step_num": 1,
                    "name": root_name,
                    "url": page.url,
                    "depth": 0,
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
                    "sub_features": _deduplicate_sub_features(root_sub_features),
                }
                discovered_screens.append(root_record)

                if on_progress_callback:
                    on_progress_callback(1, f"Captured Screen #1 (Root): {root_record['name']} (+{len(root_sub_features)} sub-features/KPIs)", root_record)

                # 2. Initialize Bounded BFS Route Queue
                from collections import deque
                from urllib.parse import urlparse

                start_parsed = urlparse(start_url)

                def _is_safe_internal_link(url_str: str) -> bool:
                    if not url_str or url_str.startswith("#") or url_str.startswith("javascript:") or url_str.startswith("mailto:"):
                        return False
                    p = urlparse(url_str)
                    if p.netloc and p.netloc.lower() != start_parsed.netloc.lower():
                        return False
                    low = url_str.lower()
                    if any(k in low for k in ["logout", "signout", "sign-out", "delete", "destroy", "purge", ".pdf", ".zip", ".xlsx"]):
                        return False
                    is_safe, _ = validate_target_url_security(url_str, allow_local_dev=True)
                    if not is_safe:
                        return False
                    return True

                queue = deque()
                for cand in summary.get("nav_links", []):
                    c_name = cand.get("text", "").strip()
                    c_href = str(cand.get("href", "")).strip()
                    if classify_action_intent(c_name) == ActionSafetyCategory.DESTRUCTIVE:
                        continue
                    full_u = urljoin(root_landing_url, c_href) if c_href else ""
                    if full_u and _is_safe_internal_link(full_u):
                        stable_u = _clean_stable_url(full_u)
                        if stable_u not in queued_urls and stable_u not in visited_urls:
                            queued_urls.add(stable_u)
                            queue.append((c_name, c_href, full_u, 1, root_landing_url))
                    elif c_name and c_name.lower() not in seen_screen_signatures:
                        queue.append((c_name, c_href, "", 1, root_landing_url))

                print(f"[Flow Explorer] Initialized BFS queue with {len(queue)} candidate routes from root screen.")

                # Explore drill-down views and detail routes from the root screen
                if explore_deep_subfeatures:
                    root_sub_features.extend(self._explore_screen_drilldowns(
                        page, root_name, reports_dir, 1, discovered_screens,
                        seen_screen_signatures, visited_urls, queued_urls, queue, 0, on_progress_callback
                    ))
                    root_record["sub_features"] = _deduplicate_sub_features(root_sub_features)

                # 3. Explore sub-pages / modules recursively using BFS queue
                print(f"[Flow Explorer] Entering BFS queue loop with {len(queue)} items...")
                while queue and len(discovered_screens) < effective_max:
                    cand_name, cand_href, full_url, cand_depth, parent_url = queue.popleft()
                    print(f"[Flow Explorer] Evaluating queue candidate: '{cand_name}' (href='{cand_href}', full='{full_url}') on page: {page.url}")
                    if cand_depth > effective_depth:
                        print(f"[Flow Explorer] Skipping candidate '{cand_name}' due to depth limit ({cand_depth} > {effective_depth})")
                        continue
                    if full_url:
                        stable_full = _clean_stable_url(full_url)
                        if stable_full in visited_urls or stable_full in {_clean_stable_url(s.get("url", "")) for s in discovered_screens}:
                            print(f"[Flow Explorer] Skipping candidate '{cand_name}' because full URL '{stable_full}' was already visited/captured")
                            continue
                    else:
                        if cand_name.lower() in seen_screen_signatures:
                            print(f"[Flow Explorer] Skipping candidate '{cand_name}' because '{cand_name.lower()}' is in seen_screen_signatures")
                            continue
                    if classify_action_intent(cand_name) == ActionSafetyCategory.DESTRUCTIVE:
                        print(f"[Flow Explorer] Safety Shield: Skipping destructive navigation candidate '{cand_name}'")
                        continue

                    try:
                        prev_url = page.url
                        prev_summary = self._extract_page_summary(page)
                        prev_dom_sig = _dom_fingerprint(prev_summary, prev_url)
                        navigated = False

                        safe_cname = cand_name.replace('"', '\\"')
                        leaf_selectors = []
                        if cand_href and not cand_href.startswith("#") and not cand_href.startswith("javascript:"):
                            leaf_selectors.extend([
                                f'a[href="{cand_href}"]',
                                f'a[href$="{cand_href}"]',
                                f'[data-route="{cand_href}"]',
                                f'[to="{cand_href}"]',
                            ])
                        leaf_selectors.extend([
                            f'.pcoded-submenu a:has-text("{safe_cname}")',
                            f'[class*="submenu"] a:has-text("{safe_cname}")',
                            f'.sidebar a:has-text("{safe_cname}"), aside a:has-text("{safe_cname}"), nav a:has-text("{safe_cname}")',
                            f'a.nav-link:has-text("{safe_cname}")',
                            f'a:has-text("{safe_cname}")',
                            f'button:has-text("{safe_cname}")',
                            f'.sidebar button:has-text("{safe_cname}"), aside button:has-text("{safe_cname}")',
                            f'[role="tab"]:has-text("{safe_cname}"), .pill-tab:has-text("{safe_cname}")',
                            f'.nav-item > a:has-text("{safe_cname}")',
                            f'.nav-item:has-text("{safe_cname}")',
                            f'[class*="nav-item"]:has-text("{safe_cname}")',
                            f'[class*="menu-item"]:has-text("{safe_cname}")',
                            f'.sidebar [class*="item"]:has-text("{safe_cname}")',
                            f'aside [class*="item"]:has-text("{safe_cname}")',
                            f'nav [class*="item"]:has-text("{safe_cname}")',
                            f'[title="{safe_cname}"]',
                            f'[aria-label="{safe_cname}"]',
                            f'.nav-item[title="{safe_cname}"]',
                            f'[class*="nav-item"][title="{safe_cname}"]',
                            f'[class*="menu-item"][title="{safe_cname}"]',
                        ])

                        # Strategy 1: SPA In-Page Click navigation (Primary method for React, Vue, Angular, S3-hosted SPAs)
                        for sel in leaf_selectors:
                            try:
                                loc = page.locator(sel).last if ("nav-item" not in sel and "title=" not in sel) else page.locator(sel).first
                                if loc.count() > 0:
                                    if not loc.is_visible():
                                        self._expand_slide_menus_and_accordions(page)
                                        _safe_settle(page, min_ms=250)
                                    if not loc.is_visible():
                                        parent_hdr = page.locator(f'li:has(*:has-text("{safe_cname}")) > a, .pcoded-hasmenu:has(*:has-text("{safe_cname}")) > a').first
                                        if parent_hdr.count() > 0 and parent_hdr.is_visible():
                                            parent_hdr.click(force=True)
                                            _safe_settle(page, min_ms=250)
                                    if loc.is_visible():
                                        print(f"[Flow Explorer] Clicking SPA navigation target '{cand_name}' (depth={cand_depth})")
                                        loc.click(force=True)
                                        _safe_settle(page, min_ms=400)
                                        navigated = True
                                        visited_urls.add(_clean_stable_url(page.url))
                                        break
                            except Exception as e_sel:
                                continue

                        # Strategy 2: Return-to-base fallback if candidate was not visible on current sub-page
                        if not navigated and page.url.rstrip("/").lower() != root_landing_url.rstrip("/").lower():
                            try:
                                print(f"[Flow Explorer] Target '{cand_name}' not on child screen. Returning to root to locate...")
                                page.goto(root_landing_url, wait_until="domcontentloaded", timeout=12000)
                                _safe_settle(page, min_ms=350)
                                self._expand_slide_menus_and_accordions(page)
                                _safe_settle(page, min_ms=250)
                                for sel in leaf_selectors:
                                    loc = page.locator(sel).last if "nav-item" not in sel else page.locator(sel).first
                                    if loc.count() > 0:
                                        if not loc.is_visible():
                                            self._expand_slide_menus_and_accordions(page)
                                            _safe_settle(page, min_ms=250)
                                        if not loc.is_visible():
                                            parent_hdr = page.locator(f'li:has(*:has-text("{safe_cname}")) > a, .pcoded-hasmenu:has(*:has-text("{safe_cname}")) > a').first
                                            if parent_hdr.count() > 0 and parent_hdr.is_visible():
                                                parent_hdr.click(force=True)
                                                _safe_settle(page, min_ms=250)
                                        if loc.is_visible():
                                            print(f"[Flow Explorer] Located '{cand_name}' on root screen. Clicking...")
                                            loc.click(force=True)
                                            _safe_settle(page, min_ms=400)
                                            navigated = True
                                            visited_urls.add(_clean_stable_url(page.url))
                                            break
                            except Exception as ret_err:
                                print(f"[Flow Explorer] Return-to-base note: {ret_err}")

                        # Strategy 3: Direct URL navigation fallback if in-page clicks were not viable
                        if not navigated and full_url and _is_safe_internal_link(full_url):
                            stable_full = _clean_stable_url(full_url)
                            if stable_full not in visited_urls and stable_full not in {_clean_stable_url(s.get("url", "")) for s in discovered_screens}:
                                print(f"[Flow Explorer] Direct URL fallback (depth={cand_depth}) to: {full_url} ({cand_name})")
                                page.goto(full_url, wait_until="domcontentloaded", timeout=12000)
                                _safe_settle(page, min_ms=350)
                                visited_urls.add(stable_full)
                                navigated = True

                        if navigated:
                            # Re-authenticate if navigation redirected to /login
                            curr_url_lower = page.url.lower()
                            if any(kw in curr_url_lower for kw in ["/login", "/signin", "/sign-in"]):
                                try:
                                    from runners.web_runner import _ensure_authenticated
                                    _ensure_authenticated(page, root_landing_url, {})
                                    if full_url and full_url.startswith("http"):
                                        page.goto(full_url, wait_until="domcontentloaded", timeout=10000)
                                        _safe_settle(page, min_ms=350)
                                except Exception:
                                    pass

                            # Expand slide menus, drawers, and accordions BEFORE extracting page summary to reveal all hidden submenus
                            try:
                                self._expand_slide_menus_and_accordions(page)
                            except Exception:
                                pass
                            _safe_settle(page, min_ms=250)

                            # Bounded lazy-scroll pass to settle dynamic widgets
                            self._bounded_lazy_content_scroll(page, max_steps=4, max_duration_ms=2500)
                            _safe_settle(page, min_ms=250)

                            sub_summary = self._extract_page_summary(page)

                            # Blank screen / unrendered protection
                            if not sub_summary.get("headings") and not sub_summary.get("action_buttons") and not sub_summary.get("inputs") and not sub_summary.get("nav_links"):
                                page.wait_for_timeout(800)
                                sub_summary = self._extract_page_summary(page)
                                if not sub_summary.get("headings") and not sub_summary.get("action_buttons") and not sub_summary.get("inputs") and not sub_summary.get("nav_links"):
                                    print(f"[Flow Explorer] Skipping unrendered/blank screen: '{cand_name}' at {page.url}")
                                    continue

                            screen_title = sub_summary.get("primary_heading") or cand_name
                            current_stable_url = _clean_stable_url(page.url)
                            prev_stable_url = _clean_stable_url(prev_url)
                            root_stable_url = _clean_stable_url(root_landing_url)
                            already_captured_urls = {_clean_stable_url(s.get("url", "")) for s in discovered_screens}
                            is_dialog_open = bool(sub_summary.get("is_modal_open"))
                            dom_sig = _dom_fingerprint(sub_summary, page.url)

                            # Duplicate Check 1: Target URL was already captured and DOM fingerprint is not distinct
                            if current_stable_url in already_captured_urls and not is_dialog_open and dom_sig in seen_screen_signatures:
                                print(f"[Flow Explorer] Skipping already-captured screen URL & DOM: {current_stable_url} ('{cand_name}')")
                                continue

                            # Duplicate Check 2: URL did not change from previous screen and DOM fingerprint remained identical
                            if current_stable_url == prev_stable_url and not is_dialog_open and dom_sig in seen_screen_signatures:
                                print(f"[Flow Explorer] Skipping unnavigated target '{cand_name}' (URL and DOM remained identical)")
                                continue

                            # Duplicate Check 3: Candidate navigation looped back to root landing view state
                            if current_stable_url == root_stable_url and not is_dialog_open and dom_sig in seen_screen_signatures:
                                print(f"[Flow Explorer] Skipping root landing URL return for candidate '{cand_name}'")
                                continue

                            # Duplicate Check 4: DOM functional fingerprint was already captured
                            if dom_sig in seen_screen_signatures:
                                print(f"[Flow Explorer] Skipping duplicate DOM view state for '{cand_name}' at {page.url}")
                                continue

                            visited_urls.add(current_stable_url)
                            seen_screen_signatures.add(cand_name.lower())
                            seen_screen_signatures.add(screen_title.lower())
                            seen_screen_signatures.add(current_stable_url)
                            seen_screen_signatures.add(dom_sig)

                            next_screen_idx = len(discovered_screens) + 1
                            sub_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_slug = re.sub(r"[^a-zA-Z0-9]", "_", cand_name)[:20].strip("_")
                            sub_ss = os.path.join(reports_dir, f"screen_{next_screen_idx}_{safe_slug}_{sub_ts}.png")
                            try:
                                capture_full_scroll_screenshot(page, sub_ss)
                            except Exception:
                                pass

                            # Deep Sub-Feature, Tabs, Dropdowns & Modal Exploration on this screen
                            sub_features = []
                            if explore_deep_subfeatures:
                                sub_features.extend(self._explore_screen_sub_tabs(page, cand_name, reports_dir, next_screen_idx))
                                sub_features.extend(self._detect_and_explore_modals(page, cand_name, reports_dir, next_screen_idx))
                                sub_features.extend(self._explore_screen_dropdowns(page, cand_name, reports_dir, next_screen_idx))
                                # Universal KPI exploration on every screen
                                sub_features.extend(self._explore_kpi_cards(page, cand_name, reports_dir, next_screen_idx, page.url))
                                
                                # Deep crawl sub-modules / tiles on settings and configuration hubs
                                cand_is_hub = any(k in (cand_name.lower() + " " + page.url.lower()) for k in [
                                    "config", "setting", "setup", "admin", "preference", "options"
                                ])
                                if cand_is_hub and sub_summary.get("config_cards"):
                                    sub_features.extend(self._explore_config_sub_pages(
                                        page, cand_name, reports_dir, next_screen_idx, page.url, sub_summary.get("config_cards", [])
                                    ))

                                # Deep drill-down inspection for row actions & child detail views
                                sub_features.extend(self._explore_screen_drilldowns(
                                    page, cand_name, reports_dir, next_screen_idx, discovered_screens,
                                    seen_screen_signatures, visited_urls, queued_urls, queue, cand_depth, on_progress_callback
                                ))

                            actual_screen_idx = len(discovered_screens) + 1
                            screen_record = {
                                "step_num": actual_screen_idx,
                                "name": cand_name or screen_title or f"Screen #{actual_screen_idx}",
                                "url": page.url,
                                "depth": cand_depth,
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
                                "sub_features": _deduplicate_sub_features(sub_features),
                            }
                            discovered_screens.append(screen_record)
                            if on_progress_callback:
                                on_progress_callback(actual_screen_idx, f"Captured Screen #{actual_screen_idx} (d={cand_depth}): {screen_record['name']} (+{len(sub_features)} sub-features/KPIs)", screen_record)

                            # Recursive child link discovery: Enqueue child routes if within max depth
                            if cand_depth < effective_depth and len(discovered_screens) < effective_max:
                                for child_cand in sub_summary.get("nav_links", []):
                                    ch_name = child_cand.get("text", "").strip()
                                    ch_href = str(child_cand.get("href", "")).strip()
                                    if classify_action_intent(ch_name) == ActionSafetyCategory.DESTRUCTIVE:
                                        continue
                                    ch_full = urljoin(page.url, ch_href) if ch_href else ""
                                    if ch_full and _is_safe_internal_link(ch_full):
                                        stable_ch = _clean_stable_url(ch_full)
                                        if stable_ch not in queued_urls and stable_ch not in visited_urls and stable_ch not in already_captured_urls:
                                            queued_urls.add(stable_ch)
                                            queue.append((ch_name or hrefToTitle(ch_href), ch_href, ch_full, cand_depth + 1, page.url))
                                    elif ch_name and ch_name.lower() not in seen_screen_signatures:
                                        queue.append((ch_name, ch_href, "", cand_depth + 1, page.url))

                    except Exception as nav_err:
                        print(f"[Flow Explorer] Note during exploring {cand_name}: {nav_err}")
                        continue

            except Exception as e:
                print(f"[Flow Explorer] Error during flow exploration: {e}")
            finally:
                browser.close()

        # Strictly re-index all discovered screens sequentially and monotonically (1, 2, 3...)
        for idx, s in enumerate(discovered_screens, 1):
            s["step_num"] = idx

        flow_data = {
            "start_url": start_url,
            "total_screens": len(discovered_screens),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "screens": discovered_screens,
        }
        return flow_data

    def capture_single_screen(self, url: str, screen_name: str = "", headless: Optional[bool] = None) -> dict:
        """Manually captures a specific single page or flow state."""
        reports_dir = os.path.join("reports", "screenshots", "flows")
        os.makedirs(reports_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9]", "_", screen_name or "manual_screen")[:25].strip("_")
        ss_path = os.path.join(reports_dir, f"manual_{slug}_{ts}.png")

        with sync_playwright() as p:
            browser = self._launch_browser(p, headless=headless)
            if os.path.exists("auth.json"):
                try:
                    context = browser.new_context(storage_state="auth.json")
                    with open("auth.json", "r", encoding="utf-8") as f:
                        auth_data = json.load(f)
                    from urllib.parse import urlparse
                    target_host = urlparse(url).netloc.lower()
                    for origin_entry in auth_data.get("origins", []):
                        orig = str(origin_entry.get("origin", "")).lower()
                        if not orig or target_host in orig or len(auth_data.get("origins", [])) == 1:
                            storage_items = origin_entry.get("localStorage", [])
                            if storage_items:
                                init_js = ";\n".join([
                                    f'try {{ localStorage.setItem({json.dumps(item["name"])}, {json.dumps(item["value"])}); }} catch(e){{}}'
                                    for item in storage_items if item.get("name")
                                ])
                                context.add_init_script(init_js)
                except Exception as ae:
                    print(f"[Flow Explorer] Note loading auth.json state: {ae}")
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

    def launch_interactive_recorder(self, start_url: str, timeout_seconds: int = 7200) -> dict:
        """
        Launches a live, visible (headed) browser session allowing the user to navigate freely,
        open slide menus/drawers, switch tabs, and capture screens interactively with an injected floating toolbar.
        Preserves open slide menus without closing them and supports unlimited screenshots.
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
                        <span id="ai-snap-count" style="background: #3730A3; color: #E0E7FF; font-size: 11px; padding: 2px 7px; border-radius: 10px; font-weight: 700;">0 Screens (No Limit)</span>
                    </div>
                    <input id="ai-screen-label-input" type="text" placeholder="Screen / Slide Menu / Modal Name" 
                        style="background: #252538; color: white; border: 1px solid #4F46E5; border-radius: 8px; padding: 7px 12px; font-size: 13px; width: 200px; outline: none;" />
                    <button id="ai-snap-btn" title="Shortcut: Ctrl+Shift+S" style="background: #4F46E5; color: white; border: none; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.2s;">
                        📸 Capture Screen
                    </button>
                    <button id="ai-finish-btn" title="Shortcut: Ctrl+Shift+Enter" style="background: #10B981; color: white; border: none; border-radius: 8px; padding: 7px 14px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.2s;">
                        ✅ Done & Save
                    </button>
                    <span id="ai-snap-feedback" style="font-size: 12px; color: #34D399; display: none; font-weight: 600;">Saved!</span>
                `;
                document.body.appendChild(dock);

                // Stop event propagation so clicking the dock or capture button NEVER triggers click-outside listeners that collapse slide menus/drawers
                const stopProp = e => e.stopPropagation();
                dock.addEventListener('mousedown', stopProp);
                dock.addEventListener('mouseup', stopProp);
                dock.addEventListener('click', stopProp);
                dock.addEventListener('pointerdown', stopProp);

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

                snapBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    snapBtn.innerText = 'Capturing...';
                    snapBtn.disabled = true;
                    dock.setAttribute('data-label', labelInput.value.trim());
                    dock.setAttribute('data-action', 'snapshot');
                });

                finishBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    e.preventDefault();
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

                    # Check if an open slide menu, drawer, or modal dialog is currently active
                    is_drawer_or_modal_open = False
                    try:
                        is_drawer_or_modal_open = page.evaluate("""() => {
                            const isVis = el => {
                                if (!el) return false;
                                const s = window.getComputedStyle(el);
                                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && el.offsetWidth > 0;
                            };
                            const drawerSelectors = [
                                '[role="dialog"]', '.offcanvas.show', '.modal.show', '.drawer.open',
                                '.ant-drawer-open', '.MuiDrawer-root', '[class*="slide-menu"]',
                                '[class*="sidebar-open"]', '[class*="drawer"][class*="open"]',
                                '[aria-modal="true"]', '.show[class*="offcanvas"]', '.show[class*="drawer"]'
                            ];
                            return drawerSelectors.some(sel => isVis(document.querySelector(sel)));
                        }""")
                    except Exception:
                        is_drawer_or_modal_open = False

                    if is_drawer_or_modal_open:
                        try:
                            page.screenshot(path=ss_path, full_page=False)
                        except Exception:
                            capture_full_scroll_screenshot(page, ss_path)
                    else:
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
                            count_str = f"{len(captured_screens)} Screens (No Limit)"
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

