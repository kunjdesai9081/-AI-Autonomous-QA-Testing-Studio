import json
import os
import time
from playwright.sync_api import sync_playwright


def get_auth_session_summary(auth_path: str = "auth.json") -> dict:
    """
    Inspects auth.json to verify validity and extract user details
    (name, role, email, cookies) to display rich live status in the UI.
    """
    if not auth_path or not os.path.exists(auth_path) or os.path.getsize(auth_path) < 10:
        return {"active": False, "summary": "No active session saved", "user": "", "role": ""}

    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        user_name = ""
        user_role = ""
        user_email = ""
        origin_url = ""

        # Check origins and localStorage
        origins = data.get("origins", [])
        if origins:
            origin_url = origins[0].get("origin", "")
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
            return {"active": False, "summary": "No active session saved", "user": "", "role": ""}

        return {
            "active": True,
            "summary": summary,
            "user": user_name or user_email,
            "role": user_role,
            "cookies_count": len(cookies),
            "origin": origin_url,
        }
    except Exception:
        return {"active": False, "summary": "No active session saved", "user": "", "role": ""}


def capture_interactive_session(
    target_url: str,
    output_auth_path: str = "auth.json",
    timeout_seconds: int = 60,
    test_data: dict = None,
    auto_submit: bool = True,
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

    with sync_playwright() as p:
        exec_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
        browser = None
        if exec_path and os.path.exists(exec_path):
            try:
                browser = p.chromium.launch(headless=False, executable_path=exec_path)
            except Exception as e:
                print(f"Warning: Failed custom executable at {exec_path}: {e}")

        if not browser:
            try:
                browser = p.chromium.launch(headless=False, channel="msedge")
            except Exception:
                browser = p.chromium.launch(headless=False)

        # Start with fresh context so expired tokens do not block clean login
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            print(f"[Session Capture] Browser open at {target_url}.")

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
                page.wait_for_timeout(800)
                id_loc = page.locator(
                    'input[type="tel"], input[name*="phone" i], input[placeholder*="phone" i], '
                    'input[placeholder*="mobile" i], input[name*="mobile" i], input[type="email"], '
                    'input[name*="email" i], input[placeholder*="email" i], input[name*="user" i], '
                    'input[placeholder*="user" i], input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"])'
                ).first
                if id_loc.count() > 0 and identifier:
                    id_loc.fill(identifier)
                    print(f"[Session Capture] Auto-filled username/phone: {identifier}")

                pass_loc = page.locator('input[type="password"], input[name*="password" i], input[placeholder*="password" i]').first
                if pass_loc.count() > 0 and password:
                    pass_loc.fill(password)
                    print("[Session Capture] Auto-filled password.")

                if auto_submit and identifier and password:
                    submit_loc = page.locator(
                        'button[type="submit"], input[type="submit"], button:has-text("Sign in"), '
                        'button:has-text("Sign In"), button:has-text("Log in"), button:has-text("Login"), '
                        'button:has-text("Submit"), .btn-primary, [role="button"]:has-text("Sign"), '
                        '[role="button"]:has-text("Log")'
                    ).first
                    if submit_loc.count() > 0:
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
                try:
                    context.storage_state(path=output_auth_path)
                    saved = True
                    print(f"[Session Capture] State saved to {output_auth_path}")
                except Exception as se:
                    error_msg = str(se)

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
