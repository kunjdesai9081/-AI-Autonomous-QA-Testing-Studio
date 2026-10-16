import os
import re
import subprocess
import requests
import collections
from typing import List, Dict, Any, Optional


_CACHED_ADB_PATH = None


def get_adb_path() -> Optional[str]:
    """Finds adb executable on system PATH or default Android SDK locations."""
    global _CACHED_ADB_PATH
    if _CACHED_ADB_PATH is not None:
        return _CACHED_ADB_PATH

    found = None
    try:
        res = subprocess.run(["where", "adb"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            found = res.stdout.strip().splitlines()[0]
    except Exception:
        pass

    if not found:
        user_profile = os.environ.get("USERPROFILE", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local_app_data, "Android", "Sdk", "platform-tools", "adb.exe"),
            os.path.join(user_profile, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                found = c
                break

    _CACHED_ADB_PATH = found or "adb"
    return _CACHED_ADB_PATH


def list_connected_adb_devices() -> List[Dict[str, str]]:
    """
    Returns list of connected Android devices and emulators.
    Format: [{"serial": "emulator-5554", "model": "Pixel_7", "status": "device"}]
    """
    adb = get_adb_path()
    try:
        res = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        if res.returncode != 0 or not res.stdout:
            return []

        devices = []
        for line in (res.stdout or "").strip().splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0]
                status = parts[1]
                model = serial
                for p in parts[2:]:
                    if p.startswith("model:"):
                        model = p.split(":", 1)[1].replace("_", " ")
                    elif p.startswith("device:"):
                        if model == serial:
                            model = p.split(":", 1)[1]
                devices.append({
                    "serial": serial,
                    "model": model,
                    "status": status,
                    "is_ready": status == "device",
                })

        # Auto-healing: If no devices currently attached, attempt to reconnect via mDNS services
        if not devices:
            try:
                res_mdns = subprocess.run([adb, "mdns", "services"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
                if res_mdns.returncode == 0 and res_mdns.stdout:
                    for mline in res_mdns.stdout.splitlines():
                        if "_adb-tls-connect._tcp" in mline:
                            parts = mline.split()
                            if len(parts) >= 3 and ":" in parts[2]:
                                endpoint = parts[2]
                                subprocess.run([adb, "connect", endpoint], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
                    # Re-query devices
                    res2 = subprocess.run([adb, "devices", "-l"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
                    if res2.returncode == 0 and res2.stdout:
                        for line in (res2.stdout or "").strip().splitlines()[1:]:
                            line = line.strip()
                            if not line:
                                continue
                            parts = line.split()
                            if len(parts) >= 2:
                                serial = parts[0]
                                status = parts[1]
                                model = serial
                                for p in parts[2:]:
                                    if p.startswith("model:"):
                                        model = p.split(":", 1)[1].replace("_", " ")
                                    elif p.startswith("device:"):
                                        if model == serial:
                                            model = p.split(":", 1)[1]
                                devices.append({
                                    "serial": serial,
                                    "model": model,
                                    "status": status,
                                    "is_ready": status == "device",
                                })
            except Exception:
                pass

        return devices
    except Exception as e:
        print(f"[DeviceBridge] Error listing adb devices: {e}")
        return []



def list_device_inspectable_sockets(serial: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Scans the Android device for open devtools_remote unix sockets (Chrome & WebViews).
    Returns: [{"socket": "chrome_devtools_remote", "type": "Chrome Browser", "pid": None, "package": "com.android.chrome"}]
    """
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "cat", "/proc/net/unix"])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)
        if res.returncode != 0 or not res.stdout:
            return []

        sockets = []
        seen = set()
        for line in res.stdout.splitlines():
            line = line.strip()
            if "devtools_remote" in line:
                # Exclude stetho sockets which are not browser/webview endpoints
                if "stetho" in line:
                    continue
                match = re.search(r"@(chrome_devtools_remote|webview_devtools_remote_(\d+))", line)
                if match:
                    raw_socket = match.group(1).lstrip("@")
                    if raw_socket in seen:
                        continue
                    seen.add(raw_socket)

                    pid = match.group(2) if len(match.groups()) >= 2 and match.group(2) else None
                    pkg_name = ""
                    if pid:
                        # Attempt to resolve package name from /proc/<pid>/cmdline
                        try:
                            cmd_pkg = [adb]
                            if serial:
                                cmd_pkg.extend(["-s", serial])
                            cmd_pkg.extend(["shell", f"cat /proc/{pid}/cmdline"])
                            res_pkg = subprocess.run(cmd_pkg, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
                            if res_pkg.returncode == 0 and res_pkg.stdout:
                                pkg_name = res_pkg.stdout.strip().replace("\x00", "")
                        except Exception:
                            pass

                    app_label = "Chrome Browser" if "chrome" in raw_socket else (f"App WebView ({pkg_name or f'PID {pid}'})" if pid else "Android WebView")
                    sockets.append({
                        "socket": raw_socket,
                        "type": "chrome" if "chrome" in raw_socket else "webview",
                        "label": app_label,
                        "pid": pid,
                        "package": pkg_name,
                    })

        # If no explicit socket found yet, provide default Chrome socket as standard option
        if not sockets:
            sockets.append({
                "socket": "chrome_devtools_remote",
                "type": "chrome",
                "label": "Chrome Browser (Standard @chrome_devtools_remote)",
                "pid": None,
                "package": "com.android.chrome",
            })
        return sockets
    except Exception as e:
        print(f"[DeviceBridge] Error scanning unix sockets: {e}")
        return [{
            "socket": "chrome_devtools_remote",
            "type": "chrome",
            "label": "Chrome Browser (Standard @chrome_devtools_remote)",
            "pid": None,
            "package": "com.android.chrome",
        }]


def list_installed_packages(serial: Optional[str] = None, third_party_only: bool = True) -> List[str]:
    """Returns list of installed Android application package names."""
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    flag = "-3" if third_party_only else "-e"
    cmd.extend(["shell", "pm", "list", "packages", flag])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)
        if res.returncode != 0 or not res.stdout:
            return []
        packages = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkg = line.replace("package:", "").strip()
                if pkg:
                    packages.append(pkg)
        return sorted(packages)
    except Exception as e:
        print(f"[DeviceBridge] Error listing packages: {e}")
        return []


def launch_app_on_device(package_name: str, serial: Optional[str] = None) -> bool:
    """Launches an application on the connected Android device using monkey intent."""
    if not package_name:
        return False
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "monkey", "-p", package_name.strip(), "-c", "android.intent.category.LAUNCHER", "1"])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)
        return res.returncode == 0
    except Exception as e:
        print(f"[DeviceBridge] Error launching app {package_name}: {e}")
        return False


def forward_device_port(socket_name: str = "chrome_devtools_remote", local_port: int = 9222, serial: Optional[str] = None) -> bool:
    """Sets up adb forward tcp:<local_port> localabstract:<socket_name>."""
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    clean_socket = socket_name.lstrip("@")
    cmd.extend(["forward", f"tcp:{local_port}", f"localabstract:{clean_socket}"])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        return res.returncode == 0
    except Exception as e:
        print(f"[DeviceBridge] Error forwarding port: {e}")
        return False


def fetch_cdp_targets(endpoint: str = "http://localhost:9222") -> List[Dict[str, Any]]:
    """
    Queries /json/list from the CDP endpoint to discover active pages, tabs, or webview screens.
    Returns: [{"id": "...", "title": "Dashboard", "url": "http://...", "type": "page"}]
    """
    clean_endpoint = endpoint.rstrip("/")
    if not clean_endpoint.startswith("http://") and not clean_endpoint.startswith("https://"):
        clean_endpoint = f"http://{clean_endpoint}"

    try:
        url = f"{clean_endpoint}/json/list"
        resp = requests.get(url, timeout=1.5)
        if resp.status_code == 200:
            targets = resp.json()
            if isinstance(targets, list):
                # Filter for inspectable pages / webviews
                pages = [t for t in targets if isinstance(t, dict) and t.get("type") in ["page", "webview", "app", "other"]]
                return pages if pages else targets
    except Exception:
        pass
    return []


def get_foreground_app_info(serial: Optional[str] = None) -> Dict[str, str]:
    """
    Returns the currently active foreground package and activity name.
    Example: {"package": "com.cuvora.carinfo", "activity": "com.vassuperapp.MainActivity"}
    """
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "dumpsys", "activity", "activities"])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                if "ResumedActivity" in line:
                    match = re.search(r"ActivityRecord\{[0-9a-fA-F]+\s+[^\s]+\s+([^\s/]+)/([^\s\}]+)", line)
                    if match:
                        return {"package": match.group(1), "activity": match.group(2)}
    except Exception as e:
        print(f"[DeviceBridge] Error detecting foreground app: {e}")

    return {"package": "", "activity": ""}


def capture_device_screenshot(output_path: str, serial: Optional[str] = None) -> bool:
    """Takes a full device screenshot via adb exec-out screencap and saves to output_path."""
    adb = get_adb_path()
    if not serial:
        devs = list_connected_adb_devices()
        if devs:
            serial = devs[0].get("serial")

    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["exec-out", "screencap", "-p"])

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=8)
        if res.returncode == 0 and len(res.stdout) > 100:
            import io
            from PIL import Image
            # Validate bytes with PIL before saving to avoid corrupt or 0-byte image files on disk
            try:
                with Image.open(io.BytesIO(res.stdout)) as img:
                    img.verify()
                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(res.stdout)
                return True
            except Exception:
                # On Windows, CRLF conversion can corrupt raw PNG stream; try replacing \r\n
                cleaned = res.stdout.replace(b"\r\n", b"\n")
                try:
                    with Image.open(io.BytesIO(cleaned)) as img:
                        img.verify()
                    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                    with open(output_path, "wb") as f:
                        f.write(cleaned)
                    return True
                except Exception as ve:
                    print(f"[DeviceBridge] Screencap validation failed: {ve}")
    except Exception as e:
        print(f"[DeviceBridge] Error capturing screenshot: {e}")
    return False


DUMMY_LABELS = {
    "textview", "viewgroup", "framelayout", "linearlayout", "imageview",
    "relativelayout", "view", "button", "android.widget.textview",
    "android.view.viewgroup", "android.widget.imageview", "null", "undefined",
    "image", "icon", "widget"
}


def _clean_resource_id(res_id: str) -> str:
    """Extracts human-readable name from Android resource ID, e.g. 'com.boult:id/tab_device' -> 'Device'."""
    if not res_id:
        return ""
    part = res_id.split("/")[-1].split(":id/")[-1]
    # Strip common technical prefixes
    part = re.sub(r"^(btn_|tv_|iv_|tab_|ic_|action_|layout_|view_|img_|item_)", "", part, flags=re.IGNORECASE)
    # Convert camelCase or snake_case to words
    part = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
    part = part.replace("_", " ").strip()
    return part.title() if part else ""


def _derive_element_label(text: str, content_desc: str, res_id: str, cls_name: str) -> str:
    """Synthesizes a clean, meaningful label for an Android UI element without dummy names."""
    # 1. Clean primary text
    if text and text.strip().lower() not in DUMMY_LABELS:
        return text.strip()
    # 2. Content description
    if content_desc and content_desc.strip().lower() not in DUMMY_LABELS:
        return content_desc.strip()
    # 3. Clean Resource ID
    clean_id = _clean_resource_id(res_id)
    if clean_id and clean_id.lower() not in DUMMY_LABELS:
        return clean_id
    # 4. Fallback based on widget class if interactive
    cls_short = cls_name.split(".")[-1]
    cls_short_lower = cls_short.lower()
    if "edit" in cls_short_lower or "search" in cls_short_lower or "autocomplete" in cls_short_lower:
        return "Input Field"
    if "button" in cls_short_lower:
        return "Action Button"
    if "tab" in cls_short_lower:
        return "Navigation Tab"
    if "switch" in cls_short_lower or "toggle" in cls_short_lower:
        return "Toggle Switch"
    if "checkbox" in cls_short_lower or "check" in cls_short_lower:
        return "Checkbox Option"
    if "radiobutton" in cls_short_lower or "radio" in cls_short_lower:
        return "Radio Option"
    if "spinner" in cls_short_lower:
        return "Dropdown Selector"
    if any(k in cls_short_lower for k in ["seekbar", "slider", "rangeslider", "progressbar", "ratingbar"]):
        return "Slider Bar"
    if any(k in cls_short_lower for k in ["drawer", "sidebar"]):
        return "Navigation Drawer"
    if "imageview" in cls_short_lower:
        return "Icon / Action"
    return ""


def _parse_bounds(bounds_str: str) -> Optional[tuple[int, int, int, int]]:
    """Calculates (x1, y1, x2, y2) from an Android bounds string '[x1,y1][x2,y2]'."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(bounds_str).strip())
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None


def _parse_bounds_center(bounds_str: str) -> Optional[tuple[int, int]]:
    """Calculates (center_x, center_y) from an Android bounds string '[x1,y1][x2,y2]'."""
    b = _parse_bounds(bounds_str)
    if b:
        cx = (b[0] + b[2]) // 2
        cy = (b[1] + b[3]) // 2
        return cx, cy
    return None


def compute_jaccard_similarity(set_a: set, set_b: set) -> float:
    """Calculates Jaccard similarity coefficient between two sets of element labels."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return float(intersection) / float(union) if union > 0 else 0.0


def calculate_navigation_desirability(
    elem: Dict[str, Any],
    screen_w: int = 1080,
    screen_h: int = 2400,
) -> float:
    """
    Evaluates how desirable an element is for autonomous flow discovery.
    High score -> explore first (bottom tabs, categories, action CTAs).
    Negative score -> NEVER tap (Android system nav, back/exit buttons, status bar).
    """
    bounds = elem.get("bounds", "")
    center = elem.get("center") or _parse_bounds_center(bounds)
    if not center:
        return -999.0
    cx, cy = center
    lbl = elem.get("label", "").strip().lower()
    res_id = elem.get("resource_id", "").strip().lower()
    cls_name = elem.get("class", "").strip().lower()

    # Rule 1: System Navigation Bar Protection (Bottom ||| O < keys)
    # Tapping the bottom system keys exits or switches the app!
    if cy >= screen_h - 130 or cy >= (screen_h * 0.952):
        return -999.0

    # Rule 2: Top-Left Back Arrow protection, BUT ALLOW Hamburger Menu / Slide Bar Drawer!
    is_drawer_trigger = any(k in lbl or k in res_id for k in ["menu", "drawer", "hamburger", "nav", "sidebar", "slide", "panel"])
    is_back_or_exit = any(k in lbl or k in res_id for k in ["back", "up", "arrow", "prev", "close", "cancel", "dismiss", "exit"])
    if cx <= (screen_w * 0.18) and cy <= (screen_h * 0.14):
        if is_drawer_trigger and not is_back_or_exit:
            return 95.0
        return -999.0

    # Rule 3: Top Status Bar / Clock / Battery area protection
    if cy <= 120 or cy <= (screen_h * 0.045):
        return -999.0

    # Rule 4: App Exit / Close / Cancel / Dismiss / Logout triggers
    exit_keywords = [
        "back", "close", "cancel", "exit", "logout", "log out", "sign out",
        "dismiss", "navigate up", "nav_up", "btn_close", "iv_back", "action_bar_up",
        "delete", "discard", "clear", "remove", "cross"
    ]
    if any(k in lbl for k in exit_keywords) or any(k in res_id for k in exit_keywords):
        return -999.0

    score = 40.0

    # Priority 1: Bottom Navigation Bar / App Tabs (e.g. Device, Tools, Settings, Home, Profile)
    # Usually in the band height * 0.82 <= cy < height - 130
    if (screen_h * 0.80) <= cy < (screen_h - 130):
        score = 100.0
        # Bonus for core app tab keywords
        if any(k in lbl or k in res_id for k in ["tab", "device", "home", "tool", "setting", "profile", "shop", "cart", "explore", "discover", "account"]):
            score += 25.0

    # Priority 2: Header Category Chips / Sub-tabs (height * 0.10 <= cy <= height * 0.35)
    elif (screen_h * 0.10) <= cy <= (screen_h * 0.35) and any(k in lbl or k in res_id or k in cls_name for k in ["tab", "chip", "category", "filter", "all", "model"]):
        score = 85.0

    # Priority 3: Slide Bars / SeekBars / Range Sliders
    elif elem.get("is_slider") or any(k in cls_name for k in ["seekbar", "slider", "rangeslider", "progressbar", "ratingbar"]) or any(k in lbl or k in res_id for k in ["slider", "seekbar", "volume", "equalizer", "seek"]):
        score = 82.0

    # Priority 4: Primary Action CTAs / Buttons
    elif any(k in lbl or k in res_id for k in ["add", "new", "connect", "pair", "scan", "explore", "select", "start", "view", "detail", "more", "help", "support"]):
        score = 75.0

    # Priority 5: Content Cards / Product Items / List Items
    elif (screen_h * 0.25) <= cy <= (screen_h * 0.82):
        score = 60.0
        if len(lbl) > 2:
            score += 5.0

    # If element is explicitly clickable
    if elem.get("clickable", False):
        score += 10.0

    # If element has a clear human label
    if lbl and lbl not in DUMMY_LABELS:
        score += 5.0

    return score


def detect_screen_role(headings: List[str], tabs: List[str], inputs: List[str], action_buttons: List[str]) -> str:
    """Classifies the primary UX role of the discovered Android screen."""
    all_text = " ".join(headings + action_buttons).lower()
    if tabs and len(tabs) >= 2:
        return "Tab Navigation Hub"
    if inputs and len(inputs) >= 1:
        return "Search / Input Form"
    if any(k in all_text for k in ["setting", "preference", "account", "profile", "configuration"]):
        return "Settings & Preferences"
    if any(k in all_text for k in ["device", "connected", "battery", "equalizer", "firmware", "status"]):
        return "Device Dashboard"
    if len(action_buttons) >= 6:
        return "Action Grid / Catalog"
    return "Content Overview"


def _extract_descendant_text(node) -> str:
    """Collects text and content descriptions from nested child nodes."""
    collected = []
    for sub in node.iter("node"):
        if sub is node:
            continue
        t = sub.attrib.get("text", "").strip()
        cd = sub.attrib.get("content-desc", "").strip()
        if t and t.lower() not in DUMMY_LABELS and t not in collected:
            collected.append(t)
        elif cd and cd.lower() not in DUMMY_LABELS and cd not in collected:
            collected.append(cd)
    return " ".join(collected[:3]).strip()


def dump_native_ui_elements(serial: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Dumps the device's native UI hierarchy via UIAutomator, derives human-readable labels,
    extracts child texts for clickable containers, and captures all interactive elements.
    """
    adb = get_adb_path()
    cmd_dump = [adb]
    if serial:
        cmd_dump.extend(["-s", serial])
    cmd_dump.extend(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"])

    try:
        subprocess.run(cmd_dump, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)

        cmd_cat = [adb]
        if serial:
            cmd_cat.extend(["-s", serial])
        cmd_cat.extend(["shell", "cat", "/sdcard/window_dump.xml"])
        res_cat = subprocess.run(cmd_cat, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)

        raw_stdout = (res_cat.stdout or "").strip()
        if res_cat.returncode != 0 or not raw_stdout:
            # Fallback to exec-out if shell cat failed or returned empty
            cmd_exec = [adb]
            if serial:
                cmd_exec.extend(["-s", serial])
            cmd_exec.extend(["exec-out", "cat", "/sdcard/window_dump.xml"])
            res_exec = subprocess.run(cmd_exec, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)
            raw_stdout = (res_exec.stdout or "").strip()
            if res_exec.returncode != 0 or not raw_stdout:
                return []

        # Find XML boundary if ADB printed any prefix text
        xml_start = raw_stdout.find("<?xml")
        if xml_start == -1:
            xml_start = raw_stdout.find("<hierarchy")
        if xml_start != -1:
            raw_stdout = raw_stdout[xml_start:]

        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw_stdout)
        elements = []

        # Detect screen resolution from root bounds if present
        root_bounds = root.attrib.get("bounds", "")
        parsed_root = _parse_bounds(root_bounds) if root_bounds else None
        screen_w = parsed_root[2] if parsed_root else 1080
        screen_h = parsed_root[3] if parsed_root else 2400

        for node in root.iter("node"):
            text = node.attrib.get("text", "").strip()
            content_desc = node.attrib.get("content-desc", "").strip()
            res_id = node.attrib.get("resource-id", "").strip()
            cls_name = node.attrib.get("class", "").strip()
            bounds = node.attrib.get("bounds", "").strip()
            clickable = node.attrib.get("clickable", "false").lower() == "true"
            enabled = node.attrib.get("enabled", "true").lower() == "true"
            checkable = node.attrib.get("checkable", "false").lower() == "true"
            cls_lower = cls_name.lower()
            res_lower = res_id.lower()
            is_slider = any(k in cls_lower for k in ["seekbar", "slider", "rangeslider", "progressbar", "ratingbar"]) or any(k in res_lower for k in ["seek", "slider", "volume", "progress", "equalizer"])
            is_drawer = any(k in cls_lower for k in ["drawer", "nav_view"]) or any(k in res_lower for k in ["drawer", "nav_menu", "hamburger", "sidebar"])

            # If node is clickable/checkable/slider container but lacks direct text, inherit from descendants
            if not text and not content_desc and (clickable or checkable or is_slider or is_drawer or "Button" in cls_name or "Layout" in cls_name or "ViewGroup" in cls_name or "Card" in cls_name):
                desc_text = _extract_descendant_text(node)
                if desc_text:
                    text = desc_text

            label = _derive_element_label(text, content_desc, res_id, cls_name)
            center = _parse_bounds_center(bounds)

            # Contextual fallback for clickable elements without labels
            cls_short = cls_name.split(".")[-1]
            if not label and (clickable or checkable or is_slider or is_drawer or "Edit" in cls_name or "Button" in cls_name):
                clean_id = _clean_resource_id(res_id)
                if clean_id and clean_id.lower() not in DUMMY_LABELS:
                    label = clean_id
                elif is_slider:
                    label = "Slider Bar"
                elif is_drawer:
                    label = "Navigation Drawer"
                elif center:
                    label = f"{cls_short or 'Interactive Element'} ({center[0]}, {center[1]})"
                else:
                    label = cls_short or "Interactive Element"

            # Skip empty, unclickable container views
            if not label and not clickable and not checkable and not is_slider and not is_drawer and "Edit" not in cls_name:
                continue

            if bounds:
                elem_dict = {
                    "label": label,
                    "text": text,
                    "content_desc": content_desc,
                    "resource_id": res_id,
                    "class": cls_name,
                    "bounds": bounds,
                    "center": center,
                    "clickable": clickable,
                    "checkable": checkable,
                    "is_slider": is_slider,
                    "is_drawer": is_drawer,
                    "enabled": enabled,
                    "screen_w": screen_w,
                    "screen_h": screen_h,
                }
                elements.append(elem_dict)
        return elements
    except Exception as e:
        print(f"[DeviceBridge] Error dumping UIAutomator: {e}")
        return []


def tap_device_element(x: int, y: int, serial: Optional[str] = None) -> bool:
    """Sends an adb shell input tap command to the device."""
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "input", "tap", str(x), str(y)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        return res.returncode == 0
    except Exception as e:
        print(f"[DeviceBridge] Error tapping coordinates ({x}, {y}): {e}")
        return False


def press_device_key(keycode: int = 4, serial: Optional[str] = None) -> bool:
    """Sends an adb shell input keyevent command (default 4 = KEYCODE_BACK)."""
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "input", "keyevent", str(keycode)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        return res.returncode == 0
    except Exception as e:
        print(f"[DeviceBridge] Error pressing keycode {keycode}: {e}")
        return False


def swipe_device_screen(start_x: int = 500, start_y: int = 1400, end_x: int = 500, end_y: int = 700, duration_ms: int = 350, serial: Optional[str] = None) -> bool:
    """Sends an adb shell input swipe command to scroll content."""
    adb = get_adb_path()
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["shell", "input", "swipe", str(start_x), str(start_y), str(end_x), str(end_y), str(duration_ms)])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
        return res.returncode == 0
    except Exception as e:
        print(f"[DeviceBridge] Error swiping screen: {e}")
        return False


def explore_android_app_flow(
    serial: Optional[str] = None,
    package_name: Optional[str] = None,
    max_screens: int = 5,
    on_progress_callback: Optional[Any] = None,
    reports_dir: str = "reports",
) -> Dict[str, Any]:
    """
    Autonomous Mobile Flow Discovery Engine with Navigation Desirability Scoring & Jaccard Deduplication:
    1. Keeps the target app locked in the foreground without exiting.
    2. Takes high-res screenshots and dumps native UI hierarchies.
    3. Scores interactive elements with NDS: prioritizes bottom tabs, top categories, and action CTAs.
    4. Protects Android system navigation keys (bottom) and back/exit buttons to avoid closing the app.
    5. Deduplicates screen states using Jaccard label similarity (>0.80) to capture truly distinct screens.
    """
    import time
    os.makedirs(reports_dir, exist_ok=True)

    # 1. Target resolution
    fg = get_foreground_app_info(serial=serial)
    target_pkg = (package_name.replace("android://", "").strip() if package_name and package_name.strip() else fg.get("package"))
    if not target_pkg or target_pkg in ["com.android.launcher3", "com.bbk.launcher2", "com.android.systemui"]:
        target_pkg = fg.get("package") or "com.zeptoconsumerapp"

    # Only launch if app is not already active
    if fg.get("package") != target_pkg:
        launch_app_on_device(target_pkg, serial=serial)
        time.sleep(2.0)

    discovered_screens = []
    clicked_targets = set()
    consecutive_duplicates = 0
    consecutive_empty = 0
    target_cap = int(max_screens) if max_screens and int(max_screens) > 0 else 999999
    max_attempts = max(60, target_cap * 4) if target_cap < 999999 else 250
    total_attempts = 0

    while len(discovered_screens) < target_cap and total_attempts < max_attempts:
        total_attempts += 1
        screen_idx = len(discovered_screens) + 1

        # Foreground enforcement: ensure app didn't lose focus
        curr_fg = get_foreground_app_info(serial=serial)
        if target_pkg and curr_fg.get("package") != target_pkg:
            launch_app_on_device(target_pkg, serial=serial)
            time.sleep(2.0)
            curr_fg = get_foreground_app_info(serial=serial)

        active_pkg = curr_fg.get("package") or target_pkg
        active_act = curr_fg.get("activity") or "MainActivity"

        ts = time.strftime("%Y%m%d_%H%M%S")
        clean_pkg = re.sub(r"[^a-zA-Z0-9]", "_", active_pkg)[:20].strip("_")
        ss_filename = f"mobile_flow_{screen_idx}_{clean_pkg}_{ts}.png"
        ss_path = os.path.join(reports_dir, ss_filename)

        # Capture live screen
        capture_device_screenshot(ss_path, serial=serial)

        # Dump native elements
        elements = dump_native_ui_elements(serial=serial)
        if not elements:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            time.sleep(1.0)
            continue
        consecutive_empty = 0

        # Determine screen resolution
        screen_w = 1080
        screen_h = 2400
        if elements and elements[0].get("screen_w"):
            screen_w = elements[0]["screen_w"]
            screen_h = elements[0]["screen_h"]

        # Extract features & candidates across this entire screen without artificial caps
        headings = []
        action_buttons = []
        inputs = []
        tabs = []
        slide_bars = []
        interactive_candidates = []
        current_labels = set()

        for elem in elements:
            lbl = elem.get("label", "").strip()
            cls = elem.get("class", "")
            clickable = elem.get("clickable", False)
            checkable = elem.get("checkable", False)
            is_elem_slider = elem.get("is_slider", False) or any(k in cls.lower() for k in ["seekbar", "slider", "rangeslider", "progressbar", "ratingbar"]) or "slider" in lbl.lower() or "seekbar" in lbl.lower()
            bounds = elem.get("bounds", "")
            center = elem.get("center") or _parse_bounds_center(bounds)

            if lbl and lbl.lower() not in DUMMY_LABELS:
                current_labels.add(lbl.lower())

            # Inputs
            if any(k in cls.lower() for k in ["edit", "search", "autocomplete"]):
                inputs.append(lbl or "Input Field")

            # Slide Bars / SeekBars
            if is_elem_slider:
                slide_bars.append(lbl or "Slider Bar")

            # Clickables / Actions / Tabs / Checkboxes / Sliders
            if clickable or checkable or is_elem_slider or any(k in cls.lower() for k in ["button", "imageview", "tab", "card", "switch", "checkbox", "seekbar", "slider"]):
                if lbl and lbl.lower() not in DUMMY_LABELS:
                    if any(k in lbl.lower() or k in cls.lower() for k in ["tab", "navigation"]):
                        tabs.append(lbl)
                    elif not is_elem_slider:
                        action_buttons.append(lbl)

                if center:
                    nds_score = calculate_navigation_desirability(elem, screen_w=screen_w, screen_h=screen_h)
                    if nds_score > 0:
                        interactive_candidates.append({
                            "label": lbl,
                            "center": center,
                            "bounds": bounds,
                            "class": cls,
                            "score": nds_score,
                            "is_slider": is_elem_slider,
                            "key": f"{lbl}_{center[0]}_{center[1]}",
                        })
            elif lbl and len(lbl) > 1 and lbl.lower() not in DUMMY_LABELS and lbl not in headings:
                headings.append(lbl)

        # Deduplication check via Jaccard Similarity against previously captured screens
        is_duplicate = False
        if discovered_screens and current_labels:
            for prev_scr in discovered_screens:
                prev_sig = prev_scr.get("_sig_set", set())
                sim = compute_jaccard_similarity(current_labels, prev_sig)
                if sim >= 0.80:
                    is_duplicate = True
                    break

        if is_duplicate and len(discovered_screens) > 0:
            consecutive_duplicates += 1
            unclicked = [c for c in interactive_candidates if c["key"] not in clicked_targets]
            unclicked.sort(key=lambda x: x["score"], reverse=True)
            if unclicked and consecutive_duplicates < 3:
                cand = unclicked[0]
                clicked_targets.add(cand["key"])
                if cand.get("is_slider"):
                    b_c = _parse_bounds(cand.get("bounds", ""))
                    if b_c:
                        x1, y1, x2, y2 = b_c
                        w = max(40, x2 - x1)
                        swipe_device_screen(start_x=int(x1 + w * 0.20), start_y=cand["center"][1], end_x=int(x1 + w * 0.80), end_y=cand["center"][1], duration_ms=350, serial=serial)
                    else:
                        tap_device_element(cand["center"][0], cand["center"][1], serial=serial)
                else:
                    tap_device_element(cand["center"][0], cand["center"][1], serial=serial)
                time.sleep(2.0)
                continue
            elif consecutive_duplicates == 2:
                # Try edge-swiping from left to open any slide-in drawer / sidebar menu
                swipe_device_screen(start_x=10, start_y=screen_h // 2, end_x=int(screen_w * 0.65), end_y=screen_h // 2, duration_ms=350, serial=serial)
                time.sleep(2.0)
                continue
            elif consecutive_duplicates >= 3 and len(discovered_screens) > 1:
                # Stuck on leaf / modal state: backtrack to hub screen
                press_device_key(4, serial=serial)
                consecutive_duplicates = 0
                time.sleep(2.0)
                continue
            else:
                swipe_device_screen(start_x=screen_w // 2, start_y=int(screen_h * 0.70), end_x=screen_w // 2, end_y=int(screen_h * 0.30), duration_ms=400, serial=serial)
                time.sleep(2.0)
                continue

        consecutive_duplicates = 0

        # Screen title derivation
        screen_title = ""
        for h in headings:
            if 3 <= len(h) <= 40:
                screen_title = h
                break
        if not screen_title and tabs:
            screen_title = f"{tabs[0]} Tab"
        if not screen_title:
            screen_title = f"{active_pkg.split('.')[-1].capitalize()} Screen #{len(discovered_screens) + 1}"

        # Sub-features and Screen Role detection (treat screen as a unified multi-element entity without artificial caps)
        detected_role = detect_screen_role(headings, tabs, inputs, action_buttons)
        sub_features = []
        for t in tabs:
            if not any(sf.get("name") == t for sf in sub_features):
                sub_features.append({
                    "type": "tab",
                    "name": t,
                    "trigger": t,
                    "tab": t,
                    "url": f"android://{active_pkg}/{active_act}",
                    "screenshot": ss_path,
                })
        for a in action_buttons:
            if not any(sf.get("name") == a for sf in sub_features):
                sub_features.append({
                    "type": "action",
                    "name": a,
                    "trigger": a,
                    "tab": "",
                    "url": f"android://{active_pkg}/{active_act}",
                    "screenshot": ss_path,
                })
        for sbar in slide_bars:
            if not any(sf.get("name") == sbar for sf in sub_features):
                sub_features.append({
                    "type": "slider",
                    "name": sbar,
                    "trigger": sbar,
                    "tab": "",
                    "url": f"android://{active_pkg}/{active_act}",
                    "screenshot": ss_path,
                })

        # Complete cohesive screen record without artificial slicing
        screen_rec = {
            "step_num": len(discovered_screens) + 1,
            "name": screen_title,
            "url": f"android://{active_pkg}/{active_act}",
            "screenshot": ss_path,
            "title": screen_title,
            "role": detected_role,
            "headings": headings,
            "action_buttons": action_buttons,
            "inputs": inputs,
            "nav_links": tabs,
            "pill_tabs": tabs,
            "slide_bars": slide_bars,
            "config_cards": [],
            "kpi_cards": [],
            "tables_count": 0,
            "sub_features": sub_features,
            "native_elements": elements,
            "total_elements": len(elements),
            "interactive_count": len(action_buttons) + len(inputs) + len(tabs) + len(slide_bars),
            "_sig_set": current_labels,
        }
        discovered_screens.append(screen_rec)

        if on_progress_callback:
            on_progress_callback(
                len(discovered_screens),
                f"📱 Captured Screen #{len(discovered_screens)}: {screen_title} [{detected_role}] ({len(elements)} elements, {len(action_buttons)} actions, {len(slide_bars)} slide bars)",
                screen_rec,
            )

        if len(discovered_screens) >= target_cap:
            break

        # Whole flow navigation strategy:
        # 1. Unclicked candidates sorted by NDS score descending (tabs -> categories -> slide bars -> action CTAs)
        # 2. Backtrack to previous hub if leaf candidates are exhausted
        # 3. Swipe down to discover below-the-fold content
        unclicked_cands = [c for c in interactive_candidates if c["key"] not in clicked_targets]
        unclicked_cands.sort(key=lambda x: x["score"], reverse=True)

        if unclicked_cands:
            best_cand = unclicked_cands[0]
            clicked_targets.add(best_cand["key"])
            cls_cand = best_cand.get("class", "").lower()
            lbl_cand = best_cand.get("label", "").lower()
            if best_cand.get("is_slider") or any(k in cls_cand for k in ["seekbar", "slider", "rangeslider"]) or "slider" in lbl_cand or "seekbar" in lbl_cand:
                b_cand = _parse_bounds(best_cand.get("bounds", ""))
                if b_cand:
                    x1, y1, x2, y2 = b_cand
                    w = max(40, x2 - x1)
                    swipe_device_screen(start_x=int(x1 + w * 0.20), start_y=best_cand["center"][1], end_x=int(x1 + w * 0.80), end_y=best_cand["center"][1], duration_ms=350, serial=serial)
                else:
                    tap_device_element(best_cand["center"][0], best_cand["center"][1], serial=serial)
            else:
                tap_device_element(best_cand["center"][0], best_cand["center"][1], serial=serial)
            time.sleep(2.0)
        elif len(discovered_screens) > 1:
            # Backtrack to parent flow screen so other branches can be explored
            press_device_key(4, serial=serial)
            time.sleep(2.0)
        else:
            # Scroll down to reveal more categories/items
            swipe_device_screen(start_x=screen_w // 2, start_y=int(screen_h * 0.70), end_x=screen_w // 2, end_y=int(screen_h * 0.30), duration_ms=400, serial=serial)
            time.sleep(2.0)

    # Clean temporary signature sets before returning
    for s in discovered_screens:
        s.pop("_sig_set", None)

    return {
        "start_url": f"android://{target_pkg}",
        "total_screens": len(discovered_screens),
        "screens": discovered_screens,
    }


def synthesize_native_test_cases(
    elements: List[Dict[str, Any]],
    package_name: str,
    screen_title: str = "App Screen",
    screenshot_path: str = "",
) -> List[Dict[str, Any]]:
    """
    Synthesizes executable test cases from extracted native UI elements (buttons, inputs, tabs, switches).
    Ensures that when a screen contains multiple elements, every interactive element is tested:
    - Eliminates naive label-skipping so multiple buttons/items on the same screen (e.g. 'Add', 'Select') are all checked.
    - Disambiguates identical labels with instance indices (e.g. 'Add #1', 'Add #2').
    - Filters out system navigation keys (bottom bar) and app-exit triggers.
    """
    cases = []
    clean_target = f"android://{package_name.strip()}" if package_name else "android://app"

    # 1. Screen presence assertion
    cases.append({
        "Run": True,
        "Type": "Mobile (Native)",
        "Generated_By": "⚙️ Rule Engine (Native UI)",
        "Target": clean_target,
        "Test_Step_Description": f"Step 1: Open target app '{package_name}' on device -> Step 2: Verify screen title and primary header '{screen_title}' is visible.",
        "Expected_Result": f"Screen '{screen_title}' loads successfully with all native components",
        "Status": "PENDING",
        "Duration": "-",
        "Actual_Result": "-",
        "Screenshot": screenshot_path,
        "Execution_Notes": "Native UI Automator assertion",
        "Playwright_Code": f'# Android Native Assertion (Playwright Device Shell)\n# Target: {clean_target}\n# Verify screen: {screen_title}\nassert device.shell("dumpsys window | grep mCurrentFocus")\n',
    })

    # Count occurrences of each label across the screen
    label_total_counts = collections.defaultdict(int)
    for elem in elements:
        lbl = elem.get("label", "").strip()
        if lbl and lbl.lower() not in DUMMY_LABELS:
            label_total_counts[lbl] += 1

    seen_positions = set()
    label_instance_index = collections.defaultdict(int)
    exit_keywords = ["back", "close", "cancel", "exit", "logout", "log out", "sign out", "dismiss", "discard", "delete"]

    for elem in elements:
        lbl = elem.get("label", "").strip()
        cls = elem.get("class", "")
        cls_lower = cls.lower()
        clickable = elem.get("clickable", False)
        checkable = elem.get("checkable", False)
        is_slider_elem = elem.get("is_slider", False) or any(k in cls_lower for k in ["seekbar", "slider", "rangeslider", "progressbar", "ratingbar"]) or any(k in lbl.lower() for k in ["slider", "seekbar", "volume", "equalizer", "progress", "seek"])
        bounds = elem.get("bounds", "")
        center = elem.get("center") or _parse_bounds_center(bounds)

        if not lbl or lbl.lower() in DUMMY_LABELS:
            continue

        # Skip exit/back buttons from action test synthesis
        if any(k in lbl.lower() for k in exit_keywords):
            continue

        cx, cy = center if center else (0, 0)

        # Skip bottom system navigation bar if coordinates available
        screen_h = elem.get("screen_h", 2400)
        if cy >= screen_h - 130 or cy >= (screen_h * 0.952):
            continue

        # Deduplicate strictly identical physical pixel coordinates
        if center:
            pos_key = (cx, cy)
            if pos_key in seen_positions:
                continue
            seen_positions.add(pos_key)

        # Disambiguate duplicate labels on the same screen (e.g. multiple "Add" or "Select" buttons)
        label_instance_index[lbl] += 1
        clean_name = lbl[:80] if len(lbl) > 80 else lbl
        if label_total_counts[lbl] > 1:
            display_lbl = f"{clean_name} #{label_instance_index[lbl]}"
        else:
            display_lbl = clean_name

        # Inputs
        if any(k in cls_lower for k in ["edit", "search", "autocomplete"]):
            cases.append({
                "Run": True,
                "Type": "Mobile (Native)",
                "Generated_By": "⚙️ Rule Engine (Native UI)",
                "Target": clean_target,
                "Test_Step_Description": f"Step 1: Locate native input field '{display_lbl}' at ({cx}, {cy}) -> Step 2: Focus and enter test query into '{display_lbl}' input with 'QA_Test_Value' -> Step 3: Verify input receives focus and accepts user input.",
                "Expected_Result": f"Native input field '{display_lbl}' receives focus and accepts user input",
                "Status": "PENDING",
                "Duration": "-",
                "Actual_Result": "-",
                "Screenshot": screenshot_path,
                "Execution_Notes": f"Bounds: {bounds}",
                "Playwright_Code": f'# Android Native Input (Playwright Device Shell)\n# adb shell input tap {cx} {cy}\n# adb shell input text "QA_Test_Value"\ndevice.shell("input tap {cx} {cy}")\ndevice.shell(\'input text "QA_Test_Value"\')\n',
            })
        # Slide Bars / SeekBars / Range Sliders
        elif is_slider_elem:
            b_coords = _parse_bounds(bounds)
            if b_coords:
                x1, y1, x2, y2 = b_coords
                w = max(50, x2 - x1)
                x_start = int(x1 + w * 0.20)
                x_mid = int(x1 + w * 0.50)
                x_end = int(x1 + w * 0.80)
            else:
                x_start = max(10, cx - 150)
                x_mid = cx
                x_end = cx + 150

            cases.append({
                "Run": True,
                "Type": "Mobile (Native)",
                "Generated_By": "⚙️ Rule Engine (Native UI)",
                "Target": clean_target,
                "Test_Step_Description": f"Step 1: Locate native slide bar '{display_lbl}' at ({cx}, {cy}) -> Step 2: Slide and adjust '{display_lbl}' value bar from {x_start} to {x_end} -> Step 3: Verify adjusted slider position is set.",
                "Expected_Result": f"Slide bar '{display_lbl}' adjusts value smoothly upon touch and drag",
                "Status": "PENDING",
                "Duration": "-",
                "Actual_Result": "-",
                "Screenshot": screenshot_path,
                "Execution_Notes": f"Class: {cls} | Bounds: {bounds}",
                "Playwright_Code": f'# Android Native Slide Bar Drag & Adjust (Playwright Device Shell)\n# adb shell input swipe {x_start} {cy} {x_end} {cy} 350\ndevice.shell("input swipe {x_start} {cy} {x_end} {cy} 350")\ndevice.shell("input tap {x_mid} {cy}")\n',
            })
        # Switches, Checkboxes & Radio toggles
        elif checkable or any(k in cls_lower for k in ["switch", "toggle", "checkbox", "radio"]):
            cases.append({
                "Run": True,
                "Type": "Mobile (Native)",
                "Generated_By": "⚙️ Rule Engine (Native UI)",
                "Target": clean_target,
                "Test_Step_Description": f"Step 1: Locate native toggle control '{display_lbl}' at ({cx}, {cy}) -> Step 2: Toggle state for '{display_lbl}' control -> Step 3: Verify control updates checked/toggle state.",
                "Expected_Result": f"Control '{display_lbl}' updates checked/toggle state successfully",
                "Status": "PENDING",
                "Duration": "-",
                "Actual_Result": "-",
                "Screenshot": screenshot_path,
                "Execution_Notes": f"Class: {cls} | Bounds: {bounds}",
                "Playwright_Code": f'# Android Native Toggle (Playwright Device Shell)\n# adb shell input tap {cx} {cy}\ndevice.shell("input tap {cx} {cy}")\n',
            })
        # Action Buttons, Tabs, Clickable Items, Cards
        elif clickable or any(k in cls_lower for k in ["button", "imageview", "tab", "card", "viewgroup", "layout"]):
            cases.append({
                "Run": True,
                "Type": "Mobile (Native)",
                "Generated_By": "⚙️ Rule Engine (Native UI)",
                "Target": clean_target,
                "Test_Step_Description": f"Step 1: Locate native '{display_lbl}' component at ({cx}, {cy}) -> Step 2: Tap '{display_lbl}' native component -> Step 3: Verify component responds to user touch interaction.",
                "Expected_Result": f"Component '{display_lbl}' responds to user touch interaction",
                "Status": "PENDING",
                "Duration": "-",
                "Actual_Result": "-",
                "Screenshot": screenshot_path,
                "Execution_Notes": f"Class: {cls} | Bounds: {bounds}",
                "Playwright_Code": f'# Android Native Tap (Playwright Device Shell)\n# adb shell input tap {cx} {cy}\ndevice.shell("input tap {cx} {cy}")\n',
            })

    return cases




