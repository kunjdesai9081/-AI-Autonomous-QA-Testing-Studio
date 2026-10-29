import json
import os
import shutil
import tempfile
import time
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def save_auth_target_url(target_url: str, auth_path: str = "auth.json") -> bool:
    """
    Saves or updates the target URL associated with an active session in auth.json and companion metadata.
    """
    if not target_url or not str(target_url).strip():
        return False
    target_url = str(target_url).strip()
    success = False
    if os.path.exists(auth_path):
        try:
            with open(auth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["target_url"] = target_url
            with open(auth_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            success = True
        except Exception as e:
            print(f"[Session Capture] Warning: could not write target_url into {auth_path}: {e}")

    meta_path = auth_path[:-5] + "_meta.json" if auth_path.endswith(".json") else "auth_meta.json"
    try:
        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump({"target_url": target_url, "updated_at": time.time()}, mf, indent=2)
        success = True
    except Exception as me:
        print(f"[Session Capture] Warning: could not write target_url into {meta_path}: {me}")

    return success


def clear_auth_session(auth_path: str = "auth.json") -> bool:
    """
    Completely removes the active session file and companion metadata.
    """
    cleared = False
    meta_path = auth_path[:-5] + "_meta.json" if auth_path.endswith(".json") else "auth_meta.json"
    for path in [auth_path, meta_path]:
        if os.path.exists(path):
            try:
                os.remove(path)
                cleared = True
            except Exception:
                pass
    return cleared


def get_auth_session_summary(auth_path: str = "auth.json") -> dict:
    """
    Inspects auth.json to verify validity and extract user details
    (name, role, email, cookies, target_url) to display rich live status in the UI.
    """
    if not auth_path or not os.path.exists(auth_path) or os.path.getsize(auth_path) < 10:
        return {"active": False, "summary": "No active session saved", "user": "", "role": "", "target_url": ""}

    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_name = ""
        user_role = ""
        user_email = ""
        origin_url = ""
        target_url = data.get("target_url", "")

        meta_path = auth_path[:-5] + "_meta.json" if auth_path.endswith(".json") else "auth_meta.json"
        if not target_url and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as mf:
                    target_url = json.load(mf).get("target_url", "")
            except Exception:
                pass

        # Check origins and localStorage
        origins = data.get("origins", [])
        if origins:
            origin_url = origins[0].get("origin", "")
            if not target_url and origin_url:
                target_url = origin_url
            for orig in origins:
                for item in orig.get("localStorage", []):
                    item_name = str(item.get("name", "")).lower()
                    val = str(item.get("value", ""))
                    if any(k in item_name for k in ["user", "auth", "profile", "account", "login", "store"]):
                        try:
                            parsed = json.loads(val)
                            if isinstance(parsed, dict):
                                u = parsed.get("state", {}).get("user", {}) if "state" in parsed else parsed.get("user", parsed)
                                if isinstance(u, dict):
                                    user_name = user_name or u.get("name") or u.get("userName") or u.get("username") or ""
                                    user_role = user_role or u.get("role") or u.get("role_name") or ""
                                    user_email = user_email or u.get("email") or ""
                        except Exception:
                            pass

        cookies = data.get("cookies", [])
        if not target_url and cookies:
            for c in cookies:
                d = str(c.get("domain", "")).lstrip(".")
                if d and "." in d:
                    target_url = f"https://{d}/login" if "cloudfront" in d or "login" in d else f"https://{d}"
                    break

        has_tokens = False
        if origins:
            for orig in origins:
                if len(orig.get("localStorage", [])) > 0:
                    has_tokens = True
                    break

        if user_name:
            role_part = f" ({user_role})" if user_role else ""
            summary = f"Logged in as {user_name}{role_part}"
        elif user_email:
            summary = f"Logged in as {user_email}"
        elif has_tokens:
            summary = "Session Active (localStorage tokens ready)"
        elif cookies:
            summary = f"Session Active ({len(cookies)} cookies stored)"
        else:
            return {"active": False, "summary": "No active session saved", "user": "", "role": "", "target_url": ""}

        return {
            "active": True,
            "summary": summary,
            "user": user_name or user_email,
            "role": user_role,
            "cookies_count": len(cookies),
            "origin": origin_url,
            "target_url": target_url,
        }
    except Exception:
        return {"active": False, "summary": "No active session saved", "user": "", "role": "", "target_url": ""}


def import_session_from_browser(
    target_url: str = None,
    output_auth_path: str = "auth.json",
    profile_name: str = None,
) -> dict:
    """
    Imports active authenticated session (localStorage and cookies) directly from
    the user's existing Chrome or Edge profile into auth.json without requiring re-login.
    """
    if sync_playwright is None:
        return {"status": "error", "message": "Playwright is not available in active Python environment."}

    user_data_dirs = [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
    ]

    candidate_profiles = []
    for base_dir in user_data_dirs:
        if os.path.exists(base_dir):
            for item in os.listdir(base_dir):
                full_p = os.path.join(base_dir, item)
                if os.path.isdir(full_p) and (item.startswith("Profile") or item == "Default"):
                    ls_p = os.path.join(full_p, "Local Storage")
                    if os.path.exists(ls_p):
                        candidate_profiles.append((os.path.getmtime(full_p), base_dir, item))

    # Sort most recently modified first
    candidate_profiles.sort(key=lambda x: x[0], reverse=True)

    if profile_name:
        candidate_profiles = [cp for cp in candidate_profiles if cp[2].lower() == profile_name.lower()] + [
            cp for cp in candidate_profiles if cp[2].lower() != profile_name.lower()
        ]

    if not candidate_profiles:
        return {"status": "error", "message": "No Chrome or Edge user profiles detected on this machine."}

    origin = ""
    netloc = ""
    if target_url and str(target_url).startswith("http"):
        parsed = urlparse(target_url)
        netloc = parsed.netloc
        origin = f"{parsed.scheme}://{netloc}"

    for _, base_dir, prof in candidate_profiles:
        src_profile = os.path.join(base_dir, prof)
        temp_dir = os.path.join(tempfile.gettempdir(), f"pw_import_{prof}_{os.getpid()}")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(os.path.join(temp_dir, "Default"), exist_ok=True)

        try:
            # Copy Local Storage
            src_ls = os.path.join(src_profile, "Local Storage")
            dst_ls = os.path.join(temp_dir, "Default", "Local Storage")
            if os.path.exists(src_ls):
                shutil.copytree(src_ls, dst_ls, ignore_dangling_symlinks=True)

            # Copy cookies if accessible (non-blocking)
            src_cookies = os.path.join(src_profile, "Network", "Cookies")
            dst_net = os.path.join(temp_dir, "Default", "Network")
            os.makedirs(dst_net, exist_ok=True)
            if os.path.exists(src_cookies):
                try:
                    with open(src_cookies, "rb") as fin:
                        with open(os.path.join(dst_net, "Cookies"), "wb") as fout:
                            fout.write(fin.read())
                except Exception:
                    pass

            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=temp_dir,
                    headless=True,
                    channel="msedge"
                )
                page = context.new_page()
                dest = origin or target_url or "http://localhost"
                try:
                    page.goto(dest + "/dashboard" if origin else dest, wait_until="domcontentloaded", timeout=12000)
                except Exception:
                    try:
                        page.goto(dest, wait_until="domcontentloaded", timeout=12000)
                    except Exception:
                        pass
                page.wait_for_timeout(1000)

                temp_auth = os.path.join(temp_dir, "state.json")
                context.storage_state(path=temp_auth)
                context.close()

                if os.path.exists(temp_auth) and os.path.getsize(temp_auth) > 50:
                    with open(temp_auth, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    # Check if tokens exist in this profile
                    has_relevant = False
                    for orig in data.get("origins", []):
                        if not netloc or netloc in orig.get("origin", ""):
                            for item in orig.get("localStorage", []):
                                nm = str(item.get("name", "")).lower()
                                if any(k in nm for k in ["token", "user", "auth", "session"]):
                                    has_relevant = True
                                    break
                    if not has_relevant and data.get("cookies"):
                        has_relevant = True

                    if has_relevant:
                        shutil.copy2(temp_auth, output_auth_path)
                        effective_url = target_url or (origin if origin else None)
                        if effective_url:
                            save_auth_target_url(effective_url, auth_path=output_auth_path)
                        summary_info = get_auth_session_summary(output_auth_path)
                        browser_name = "Chrome" if "Google" in base_dir else "Edge"
                        return {
                            "status": "success",
                            "message": f"Successfully imported active session for {summary_info.get('user', 'user')} from {browser_name} ({prof})!",
                            "profile": prof,
                            "browser": browser_name,
                            "summary": summary_info,
                        }
        except Exception as e:
            print(f"[Session Import] Note on {prof}: {e}")
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    target_desc = f" for {netloc}" if netloc else ""
    return {"status": "error", "message": f"No active login session found{target_desc} across local browser profiles."}


def capture_interactive_session(
    target_url: str,
    output_auth_path: str = "auth.json",
    timeout_seconds: int = 60,
    test_data: dict = None,
    auto_submit: bool = True,
    headless: bool = False,
) -> dict:
    """
    Launches a visible browser to target_url. Automatically pre-fills username/phone and password
    from configured test variables so the user doesn't have to type manually.
    Captures and saves cookies and localStorage state to auth.json once authenticated,
    and automatically closes the browser as soon as login is detected.
    """
    if not target_url or not target_url.startswith("http"):
        return {"status": "error", "message": "Please provide a valid HTTP/HTTPS URL."}

    print(f"[Session Capture] Launching browser for {target_url}...")
    saved = False
    error_msg = ""

    # Load credentials from test_data or config/settings.json
    creds = {}
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for var in data.get("test_variables", []):
                    k = str(var.get("Key", "")).strip()
                    v = str(var.get("Value", "")).strip()
                    if k and v:
                        creds[k] = v
    except Exception:
        pass
    if test_data and isinstance(test_data, dict):
        for k, v in test_data.items():
            if str(k).strip() and str(v).strip():
                creds[str(k).strip()] = str(v).strip()

    # Determine password and user identifier
    password = ""
    for k, v in creds.items():
        if "pass" in k.lower():
            password = v
            break
    identifier = ""
    for k in ["Phone", "phone", "Mobile", "mobile", "Email", "email", "Username", "username", "User", "user", "Login", "login"]:
        if k in creds and creds[k]:
            identifier = creds[k]
            break
    if not identifier:
        for k, v in creds.items():
            if "pass" not in k.lower() and v:
                identifier = v
                break

    # Pre-check if an active session already exists in auth.json
    initial_state = None
    if os.path.exists(output_auth_path) and os.path.getsize(output_auth_path) > 50:
        try:
            summary_info = get_auth_session_summary(output_auth_path)
            if summary_info.get("active"):
                initial_state = output_auth_path
                print(f"[Session Capture] Reusing existing state from {output_auth_path}.")
        except Exception:
            pass

    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed in the active Python environment. Please run via .\\venv\\Scripts\\python.exe")

    with sync_playwright() as p:
        exec_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
        browser = None
        if exec_path and os.path.exists(exec_path):
            try:
                browser = p.chromium.launch(headless=headless, executable_path=exec_path)
            except Exception as e:
                print(f"Warning: Failed custom executable at {exec_path}: {e}")

        if not browser:
            for ch in ["msedge", "chrome"]:
                try:
                    browser = p.chromium.launch(headless=headless, channel=ch)
                    break
                except Exception:
                    pass
        if not browser:
            browser = p.chromium.launch(headless=headless)

        if initial_state and os.path.exists(initial_state):
            try:
                context = browser.new_context(storage_state=initial_state)
            except Exception:
                context = browser.new_context()
        else:
            context = browser.new_context()

        page = context.new_page()

        # Clear CDP browser cache
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Network.clearBrowserCache")
        except Exception:
            pass
            pass

        failed_requests = []
        def _on_req_failed(req):
            failed_requests.append(f"{req.method} {req.url} -> {req.failure}")
        page.on("requestfailed", _on_req_failed)

        try:
            nav_success = False
            nav_err = None
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                nav_success = True
                print(f"[Session Capture] Browser open at {target_url}.")
            except Exception as e:
                nav_err = e
                print(f"[Session Capture] Direct navigation to {target_url} encountered error: {e}")
                parsed_t = urlparse(target_url)
                root_url = f"{parsed_t.scheme}://{parsed_t.netloc}/"
                login_url = f"{parsed_t.scheme}://{parsed_t.netloc}/login"

                # If deep route failed (e.g. 404 / ERR_HTTP_RESPONSE_CODE_FAILURE on /users), try root origin or /login
                for fb_url in [root_url, login_url]:
                    if fb_url.rstrip("/") != target_url.rstrip("/"):
                        try:
                            print(f"[Session Capture] Attempting fallback navigation to base route: {fb_url}...")
                            page.goto(fb_url, wait_until="domcontentloaded", timeout=12000)
                            nav_success = True
                            nav_err = None
                            print(f"[Session Capture] Successfully navigated to fallback route: {fb_url}")
                            break
                        except Exception:
                            pass

            # Auto-bypass Microsoft Dev Tunnels "Click Continue" anti-phishing interstitial screen
            try:
                if "devtunnels.ms" in (page.url or "").lower():
                    cont_btn = page.locator('button:has-text("Continue"), input[value="Continue"], #continue-button, [role="button"]:has-text("Continue")').first
                    if cont_btn.count() > 0 and cont_btn.is_visible():
                        print("[Session Capture] Detected Dev Tunnels anti-phishing warning screen; auto-clicking Continue...")
                        cont_btn.click()
                        page.wait_for_timeout(1000)
            except Exception:
                pass

            if not nav_success and nav_err is not None:
                err_str = str(nav_err)
                parsed_t = urlparse(target_url)
                if "devtunnels.ms" in target_url.lower():
                    diag = (
                        f"Dev Tunnel Offline (404/502 from Microsoft Dev Tunnels): The dev tunnel '{parsed_t.netloc}' "
                        f"is not active or port {parsed_t.port or '8080'} is not currently forwarded by the host machine. "
                        f"Please verify that your VS Code Dev Tunnel or 'devtunnel host' is running."
                    )
                elif "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_str:
                    diag = (
                        f"Target Route Unavailable ({err_str}): The server at '{target_url}' returned an HTTP error status code. "
                        f"Please ensure your web server is online and the URL route exists."
                    )
                else:
                    diag = err_str

                # Try importing from local browser profile as fallback
                try:
                    imp = import_session_from_browser(target_url, output_auth_path=output_auth_path)
                    if imp.get("status") == "success" and os.path.exists(output_auth_path) and os.path.getsize(output_auth_path) > 50:
                        saved = True
                        save_auth_target_url(target_url, auth_path=output_auth_path)
                        summary_info = get_auth_session_summary(output_auth_path)
                        return {
                            "status": "success",
                            "message": f"✅ Session recovered from {imp.get('browser', 'local browser')}! ({diag})",
                            "file": output_auth_path,
                            "summary": summary_info,
                        }
                except Exception:
                    pass

                raise RuntimeError(diag) from nav_err

            # If pre-loaded state was provided and user entered a /login URL, redirect to dashboard if authenticated
            if initial_state:
                try:
                    has_tok = page.evaluate("() => localStorage.getItem('accessToken') || localStorage.getItem('token')")
                    if has_tok and any(k in (page.url or "").lower() for k in ["/login", "/signin"]):
                        parsed_t = urlparse(target_url)
                        dash_url = f"{parsed_t.scheme}://{parsed_t.netloc}/dashboard"
                        page.goto(dash_url, wait_until="domcontentloaded", timeout=10000)
                except Exception:
                    pass

            # Track whether we started on a login screen
            was_on_login = False
            initial_url = (page.url or "").lower()
            try:
                if any(k in initial_url for k in ["/login", "/signin", "/sign-in", "/auth"]):
                    was_on_login = True
                elif page.locator('input[type="password"]').count() > 0:
                    was_on_login = True
            except Exception:
                pass

            # Auto-fill credentials from variables so user never has to type manually
            try:
                page.wait_for_timeout(1000)
                try:
                    page.wait_for_selector('input:not([type="hidden"])', timeout=10000)
                except Exception:
                    pass

                id_loc = page.locator(
                    'input[type="email"], input[name*="email" i], input[placeholder*="email" i], '
                    'input[name*="user" i], input[placeholder*="user" i], input[name*="login" i], '
                    'input[type="tel"], input[name*="phone" i], input[placeholder*="phone" i], '
                    'input[placeholder*="mobile" i], input[name*="mobile" i], '
                    'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'
                ).first
                if id_loc.count() > 0 and identifier:
                    id_loc.click()
                    id_loc.fill(identifier)
                    id_loc.dispatch_event("input")
                    id_loc.dispatch_event("change")
                    print(f"[Session Capture] Auto-filled username/email: {identifier}")
                    page.wait_for_timeout(200)

                pass_loc = page.locator('input[type="password"], input[name*="password" i], input[placeholder*="password" i]').first
                if pass_loc.count() > 0 and password:
                    pass_loc.click()
                    pass_loc.fill(password)
                    pass_loc.dispatch_event("input")
                    pass_loc.dispatch_event("change")
                    print("[Session Capture] Auto-filled password.")
                    page.wait_for_timeout(300)

                if auto_submit and identifier and password:
                    submit_loc = page.locator(
                        'button[type="submit"], input[type="submit"], button:has-text("Sign in"), '
                        'button:has-text("Sign In"), button:has-text("Log in"), button:has-text("Login"), '
                        'button:has-text("Submit"), .btn-primary, [role="button"]:has-text("Sign"), '
                        '[role="button"]:has-text("Log")'
                    ).first
                    if submit_loc.count() > 0:
                        page.wait_for_timeout(400)
                        submit_loc.click(force=True)
                        print("[Session Capture] Auto-submitted login credentials.")
            except Exception as fill_err:
                print(f"[Session Capture] Auto-fill note: {fill_err}")

            start_time = time.time()
            while time.time() - start_time < timeout_seconds:
                try:
                    if page.is_closed():
                        break

                    current_url = (page.url or "").lower()

                    has_password_field = False
                    try:
                        has_password_field = page.locator('input[type="password"]').count() > 0
                    except Exception:
                        pass

                    # Detect if tokens are stored in localStorage
                    has_storage_token = False
                    try:
                        has_storage_token = page.evaluate("""() => {
                            for (let i = 0; i < localStorage.length; i++) {
                                const k = (localStorage.key(i) || '').toLowerCase();
                                const v = (localStorage.getItem(k) || '').toLowerCase();
                                if (k.includes('token') || k.includes('auth') || k.includes('user') || k.includes('session') || v.includes('token') || v.includes('jwt')) {
                                    return true;
                                }
                            }
                            return false;
                        }""")
                    except Exception:
                        pass

                    # Check cookies
                    has_auth_cookie = False
                    try:
                        cookies = context.cookies()
                        has_auth_cookie = any(
                            any(k in c.get('name', '').lower() for k in ['token', 'session', 'auth', 'jwt', 'user'])
                            for c in cookies
                        )
                    except Exception:
                        pass

                    # Successful login criteria:
                    # 1. Navigated away from login URL and password field is no longer present
                    navigated_away = was_on_login and not any(k in current_url for k in ["/login", "/signin", "/sign-in"]) and not has_password_field
                    
                    # 2. Or tokens/cookies are stored and password field is gone
                    tokens_present = (has_storage_token or has_auth_cookie) and not has_password_field

                    if (navigated_away or tokens_present) and (time.time() - start_time >= 1.0):
                        print(f"[Session Capture] Login detected! (navigated_away={navigated_away}, tokens_present={tokens_present})")
                        print("[Session Capture] Finalizing session tokens and auto-closing browser...")
                        page.wait_for_timeout(1500)
                        context.storage_state(path=output_auth_path)
                        saved = True
                        print(f"[Session Capture] Active session saved to {output_auth_path}. Auto-closing browser now!")
                        break

                    page.wait_for_timeout(500)
                except Exception as loop_e:
                    print(f"[Session Capture] Loop note: {loop_e}")
                    break

            # If loop exited (manual window close or timeout) and not yet saved
            if not saved:
                page_error = ""
                try:
                    for sel in [".alert-danger", "[role='alert']", ".error-message", ".login-error", ":has-text('Login failed')", ":has-text('credentials')"]:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            txt = el.inner_text().strip()
                            if txt and len(txt) < 200:
                                page_error = txt
                                break
                except Exception:
                    pass

                conn_refused = [fr for fr in failed_requests if any(t in fr.lower() for t in ["err_connection_refused", "localhost", "127.0.0.1", "3002", "3000"])]

                if conn_refused:
                    error_msg = f"Backend Server Offline: The application sent the login request to an offline backend ({conn_refused[0]}). Please ensure your local backend server is running."
                    if page_error:
                        error_msg = f"{page_error} — ({error_msg})"
                    # Fallback recovery: check if active authenticated session exists in user's browser
                    try:
                        imp = import_session_from_browser(target_url, output_auth_path=output_auth_path)
                        if imp.get("status") == "success" and os.path.exists(output_auth_path) and os.path.getsize(output_auth_path) > 50:
                            saved = True
                            error_msg = ""
                            print(f"[Session Capture] Recovered active session from {imp.get('browser')} profile.")
                    except Exception:
                        pass
                elif page_error:
                    error_msg = f"Login rejected by application: {page_error}"
                else:
                    # Check if tokens were actually stored before saving
                    has_tokens = False
                    try:
                        cookies = context.cookies()
                        has_tokens = any(any(k in c.get('name', '').lower() for k in ['token', 'session', 'auth', 'jwt', 'user']) for c in cookies)
                    except Exception:
                        pass
                    if not has_tokens:
                        try:
                            has_tokens = page.evaluate("""() => {
                                for (let i = 0; i < localStorage.length; i++) {
                                    const k = (localStorage.key(i) || '').toLowerCase();
                                    const v = (localStorage.getItem(k) || '').toLowerCase();
                                    if (k.includes('token') || k.includes('auth') || k.includes('user') || k.includes('session')) return true;
                                }
                                return false;
                            }""")
                        except Exception:
                            pass

                    if has_tokens:
                        try:
                            context.storage_state(path=output_auth_path)
                            saved = True
                            print(f"[Session Capture] State saved to {output_auth_path}")
                        except Exception as se:
                            error_msg = str(se)
                    else:
                        error_msg = error_msg or "Authentication incomplete: No session tokens or cookies were detected."

        except Exception as e:
            error_msg = str(e)
            print(f"[Session Capture] Error during session capture: {e}")
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            print("[Session Capture] Browser closed cleanly.")

    if saved and os.path.exists(output_auth_path) and os.path.getsize(output_auth_path) > 10:
        save_auth_target_url(target_url, auth_path=output_auth_path)
        summary_info = get_auth_session_summary(output_auth_path)
        detail = summary_info.get("summary", "Session captured")
        return {
            "status": "success",
            "message": f"✅ Session captured successfully! {detail}",
            "file": output_auth_path,
            "summary": summary_info,
        }
    else:
        return {
            "status": "error",
            "message": f"Session capture incomplete or failed: {error_msg or 'No auth state written'}",
        }
