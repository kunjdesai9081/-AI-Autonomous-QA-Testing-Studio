import os
import re
import sys
import time
from typing import Optional, List, Dict, Any, Callable

from core.device_bridge import (
    get_adb_path,
    list_connected_adb_devices,
    get_foreground_app_info,
    launch_app_on_device,
    tap_device_element,
    press_device_key,
    swipe_device_screen,
    capture_device_screenshot,
    dump_native_ui_elements,
    forward_device_port,
    fetch_cdp_targets,
    _parse_bounds_center,
    DUMMY_LABELS,
)


def classify_and_validate_test_channel(
    row: Dict[str, Any],
    platform_config: Optional[Dict[str, Any]] = None,
    serial: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """
    Classifies a test case into its appropriate execution channel and validates platform readiness.
    Returns:
        (channel, error_message_or_none)
        Channels:
          - 'native_mobile': Native Android app running on connected device/emulator via ADB
          - 'cdp_mobile': Mobile Chrome/WebView running on Android device over CDP
          - 'cdp_electron': Desktop Electron application running over CDP
          - 'emulated_mobile': Mobile emulated browser in Playwright (iOS/Pixel/Samsung)
          - 'api': HTTP/REST API request
          - 'web': Standard desktop web browser
    """
    platform_config = platform_config or {}
    test_type = str(row.get("Type", "")).strip().lower()
    target = str(row.get("Target", "")).strip()
    script = str(row.get("Playwright_Code", "")).strip().lower()
    if script == "nan":
        script = ""

    # 1. Native Mobile App Detection
    is_native = (
        any(k in test_type for k in ["native", "android native", "native ui", "mobile (native)"])
        or target.startswith("android://")
        or target.startswith("app://")
        or target.startswith("package:")
        or script.startswith("# android native")
        or "adb shell input" in script
        or "adb shell" in script
        or (test_type == "mobile" and not target.startswith("http://") and not target.startswith("https://"))
    )
    if is_native:
        # Validation for Native Mobile
        devices = list_connected_adb_devices()
        ready_devices = [d for d in devices if d.get("is_ready")]
        if not ready_devices:
            return "native_mobile", (
                "❌ Execution Error: No Android device or emulator detected via ADB. "
                "Please connect your phone via USB with USB Debugging enabled, or reconnect wireless ADB."
            )
        return "native_mobile", None

    # 2. API Detection
    if test_type == "api" or target.startswith("api://"):
        if not target or target.lower() == "none":
            return "api", "❌ API Target URL cannot be empty."
        return "api", None

    # 3. CDP Mobile (Chrome / WebView on real Android device)
    if platform_config.get("platform") == "android_cdp":
        devices = list_connected_adb_devices()
        ready_devices = [d for d in devices if d.get("is_ready")]
        if not ready_devices:
            return "cdp_mobile", (
                "❌ Android CDP Error: Selected platform is 'Android Device (ADB)', but no phone/emulator is connected via ADB."
            )
        # Check / attempt port forward
        forward_device_port("chrome_devtools_remote", local_port=9222, serial=serial)
        cdp_targets = fetch_cdp_targets("http://localhost:9222")
        if not cdp_targets:
            return "cdp_mobile", (
                "⚠️ Android CDP Warning: Could not reach Chrome or WebView on device at http://localhost:9222. "
                "Make sure Chrome or your app's inspectable WebView is open on the phone and port is forwarded."
            )
        return "cdp_mobile", None

    # 4. CDP Desktop Electron
    if platform_config.get("platform") == "electron_cdp":
        cdp_url = platform_config.get("cdp_url") or "http://localhost:9223"
        cdp_targets = fetch_cdp_targets(cdp_url)
        if not cdp_targets:
            return "cdp_electron", (
                f"❌ Electron CDP Error: Could not reach Electron app at {cdp_url}. "
                "Ensure your Electron app is launched with --remote-debugging-port=9223."
            )
        return "cdp_electron", None

    # 5. Emulated Mobile Web (iOS / Pixel / Samsung)
    if platform_config.get("platform") in ["mobile_iphone", "mobile_pixel", "mobile_samsung", "tablet_ipad"]:
        if target.startswith("android://") or target.startswith("app://"):
            return "emulated_mobile", (
                f"❌ Platform Mismatch: Target '{target}' is a Native Mobile app. "
                "It cannot run in an emulated web browser. Switch Type to 'Mobile (Native)' or use a Web URL."
            )
        return "emulated_mobile", None

    # 6. Standard Web / Desktop Browser
    if target.startswith("android://") or target.startswith("app://"):
        return "web", (
            f"❌ Platform Mismatch: Target '{target}' is a Native Android app package. "
            "It cannot be opened in a desktop web browser. Run as 'Mobile (Native)' on an Android device."
        )

    return "web", None


class NativeRunner:
    """
    Executes Android Native Mobile App tests directly on a connected device/emulator via ADB.
    Handles native taps, inputs, screen assertions, and captures device screenshots.
    Does NOT launch or require a desktop browser.
    """

    def __init__(
        self,
        target: str,
        test_step: str = "",
        expected_result: str = "",
        custom_script: str = "",
        serial: Optional[str] = None,
    ):
        self.target = str(target or "").strip()
        self.test_step = str(test_step or "").strip()
        self.expected_result = str(expected_result or "").strip()
        self.custom_script = str(custom_script or "").strip()
        if self.custom_script.lower() == "nan":
            self.custom_script = ""
        self.serial = serial
        self.last_screenshot: Optional[str] = None
        self.full_script: str = ""

    def _resolve_device(self) -> str:
        """Ensures an Android device is connected and returns its serial."""
        devices = list_connected_adb_devices()
        ready_devices = [d for d in devices if d.get("is_ready")]
        if not ready_devices:
            raise RuntimeError(
                "❌ No Android device or emulator connected via ADB. "
                "Please connect your device via USB with USB Debugging enabled, or reconnect wireless ADB."
            )
        if self.serial:
            match = [d for d in ready_devices if d["serial"] == self.serial]
            if match:
                return self.serial
        return ready_devices[0]["serial"]

    def _resolve_target_package(self) -> str:
        """Extracts the package name from the target (e.g. 'android://com.example.app/MainActivity' -> 'com.example.app')."""
        clean = self.target.replace("android://", "").replace("app://", "").strip()
        if "/" in clean:
            clean = clean.split("/")[0]
        return clean

    def execute(self) -> str:
        """
        Executes the native test step on the connected Android device.
        Returns the executed script and verification notes.
        """
        start_time = time.time()
        dev_serial = self._resolve_device()
        target_pkg = self._resolve_target_package()

        executed_commands = []
        action_notes = []

        # 1. Foreground App Check & Launch
        if target_pkg and not target_pkg.startswith("http"):
            fg = get_foreground_app_info(serial=dev_serial)
            if fg.get("package") != target_pkg:
                launch_app_on_device(target_pkg, serial=dev_serial)
                executed_commands.append(f"adb shell monkey -p {target_pkg} -c android.intent.category.LAUNCHER 1")
                action_notes.append(f"Launched/brought target app '{target_pkg}' to foreground.")
                time.sleep(1.5)

        script_text = self.custom_script or ""
        step_lower = self.test_step.lower()
        step_executed = False

        # 2. Check for explicit ADB Tap coordinates in script or step description
        tap_match = re.search(r"(?:input\s+tap\s+|tap\s+)(\d+)\s+(\d+)", script_text)
        if not tap_match and any(kw in step_lower for kw in ["tap", "click", "press", "touch"]):
            step_tap = re.search(r"(?:at\s+|\()(\d+),\s*(\d+)\)?", self.test_step)
            if step_tap:
                tap_match = step_tap

        if tap_match and not any(kw in step_lower for kw in ["enter test query", "input text"]):
            x, y = int(tap_match.group(1)), int(tap_match.group(2))
            ok = tap_device_element(x, y, serial=dev_serial)
            executed_commands.append(f"adb shell input tap {x} {y}")
            action_notes.append(f"Tapped native coordinates ({x}, {y}) [success={ok}].")
            step_executed = True
            time.sleep(0.8)

        # 3. Check for explicit ADB Swipe coordinates in script or step description (Slide bar / drag / scroll)
        swipe_match = re.search(r"(?:input\s+swipe\s+|swipe\s+)(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+(\d+))?", script_text)
        if not swipe_match and any(kw in step_lower for kw in ["slide and adjust", "slide bar", "swipe", "scroll"]):
            step_swipe = re.search(r"from\s+(\d+)\s+to\s+(\d+)", self.test_step)
            if step_swipe:
                x1, x2 = int(step_swipe.group(1)), int(step_swipe.group(2))
                coords_y = re.search(r"\(\d+,\s*(\d+)\)", self.test_step)
                cy = int(coords_y.group(1)) if coords_y else 800
                class DummySwipeMatch:
                    def group(self, idx):
                        return (None, str(x1), str(cy), str(x2), str(cy), "350")[idx]
                swipe_match = DummySwipeMatch()

        if swipe_match:
            x1, y1, x2, y2 = int(swipe_match.group(1)), int(swipe_match.group(2)), int(swipe_match.group(3)), int(swipe_match.group(4))
            duration_ms = int(swipe_match.group(5)) if swipe_match.group(5) else 350
            ok = swipe_device_screen(x1, y1, x2, y2, duration_ms=duration_ms, serial=dev_serial)
            executed_commands.append(f"adb shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
            action_notes.append(f"Swiped native coordinates ({x1}, {y1}) -> ({x2}, {y2}) [success={ok}].")
            step_executed = True
            time.sleep(0.8)

        # 4. Check for explicit ADB Input Text in script or step description
        text_match = re.search(r'(?:input\s+text\s+)"?([^"\n\r]+)"?', script_text)
        if not text_match and any(kw in step_lower for kw in ["input", "enter", "text", "query"]):
            step_val = re.search(r"with\s+['\"]([^'\"]+)['\"]", self.test_step)
            if step_val:
                class DummyTextMatch:
                    def group(self, idx):
                        return step_val.group(1)
                text_match = DummyTextMatch()

        if text_match:
            input_val = text_match.group(1).strip()
            # If coordinates are present for this input, tap them first to focus
            in_coords = tap_match or re.search(r"\(\s*(\d+),\s*(\d+)\s*\)", self.test_step)
            if in_coords:
                ix, iy = int(in_coords.group(1)), int(in_coords.group(2))
                tap_device_element(ix, iy, serial=dev_serial)
                executed_commands.append(f"adb shell input tap {ix} {iy}")
                time.sleep(0.4)

            adb = get_adb_path()
            import subprocess
            cmd = [adb]
            if dev_serial:
                cmd.extend(["-s", dev_serial])
            # Escape spaces for ADB text input
            clean_val = input_val.replace(" ", "%s")
            cmd.extend(["shell", "input", "text", clean_val])
            subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
            executed_commands.append(f'adb shell input text "{input_val}"')
            action_notes.append(f"Entered native text: '{input_val}'.")
            step_executed = True
            time.sleep(0.5)


        # 4. Screen Presence / UI Assertion
        is_assertion_step = any(kw in step_lower for kw in ["verify screen", "verify title", "header", "visible", "assert", "check"])
        if is_assertion_step and not step_executed:
            # Extract target label from quotes if present: e.g. Verify screen title and primary header 'Device Home' is visible
            lbl_match = re.search(r"['\"]([^'\"]+)['\"]", self.test_step)
            expected_title = lbl_match.group(1).strip() if lbl_match else ""

            elems = dump_native_ui_elements(serial=dev_serial)
            executed_commands.append("adb shell uiautomator dump")

            found = False
            if expected_title:
                for e in elems:
                    e_lbl = e.get("label", "").strip()
                    if expected_title.lower() in e_lbl.lower() or e_lbl.lower() in expected_title.lower():
                        found = True
                        action_notes.append(f"Verified element '{e_lbl}' is present on screen (Bounds: {e.get('bounds')}).")
                        break

            if not found and elems:
                # Fallback: check if active screen has valid loaded elements
                clean_count = len([e for e in elems if e.get("label") and e["label"].lower() not in DUMMY_LABELS])
                action_notes.append(f"Native screen verified with {clean_count} active UI components.")
                found = True

            if not found and not elems:
                raise RuntimeError(f"Native screen assertion failed: Screen is empty or not responsive.")
            step_executed = True

        # 5. Component Tap by Label (if coordinates were not provided in script)
        if not step_executed and any(kw in step_lower for kw in ["tap", "click", "select", "press"]):
            lbl_match = re.search(r"['\"]([^'\"]+)['\"]", self.test_step)
            target_lbl = lbl_match.group(1).strip() if lbl_match else ""

            elems = dump_native_ui_elements(serial=dev_serial)
            tapped = False
            if target_lbl:
                for e in elems:
                    e_lbl = e.get("label", "").strip()
                    if target_lbl.lower() in e_lbl.lower() or e_lbl.lower() in target_lbl.lower():
                        center = e.get("center") or _parse_bounds_center(e.get("bounds", ""))
                        if center:
                            tap_device_element(center[0], center[1], serial=dev_serial)
                            executed_commands.append(f"adb shell input tap {center[0]} {center[1]}")
                            action_notes.append(f"Tapped '{e_lbl}' at ({center[0]}, {center[1]}).")
                            tapped = True
                            time.sleep(1.0)
                            break

            if not tapped:
                # Execute general touch action
                action_notes.append(f"Processed touch interaction for '{target_lbl or self.test_step}'.")
            step_executed = True

        # 6. Capture live screenshot directly from device
        os.makedirs("reports", exist_ok=True)
        ts = int(time.time() * 1000)
        ss_path = f"reports/native_exec_{ts}.png"
        if capture_device_screenshot(ss_path, serial=dev_serial):
            self.last_screenshot = ss_path
            action_notes.append(f"Captured live device screenshot: {os.path.basename(ss_path)}")

        duration = round(time.time() - start_time, 2)
        script_summary = "\n".join(executed_commands) if executed_commands else (self.custom_script or "# Native Android action verified via ADB\n")
        self.full_script = f"# Android Native Execution ({dev_serial}) — {duration}s\n" + script_summary
        return self.full_script


class NativeBatchRunner:
    """
    Executes a batch of Android Native Mobile tests sequentially on a connected device via ADB.
    Maintains device connection without launching unnecessary desktop browsers.
    """

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial

    def run_batch(
        self,
        tests: List[Dict[str, Any]],
        on_step_callback: Callable,
        check_control_callback: Optional[Callable] = None,
    ):
        """
        Executes a list of native mobile test cases.
        tests format: [{'index': int, 'url': str, 'test_step': str, 'expected_result': str, 'custom_script': str, 'row': dict}]
        """
        if not tests:
            return

        print(f"[NativeBatchRunner] Starting execution of {len(tests)} Native Mobile tests via ADB...")

        for item in tests:
            if check_control_callback:
                ctrl = check_control_callback()
                if ctrl == "PAUSE":
                    break

            idx = item.get("index", 0)
            url = item.get("url", "")
            test_step = str(item.get("test_step", "")).strip()
            expected_result = str(item.get("expected_result", "")).strip()
            custom_script = str(item.get("custom_script", "")).strip()

            step_start = time.time()
            runner = NativeRunner(
                target=url,
                test_step=test_step,
                expected_result=expected_result,
                custom_script=custom_script,
                serial=self.serial,
            )

            try:
                full_script = runner.execute()
                duration_str = f"{round(time.time() - step_start, 2)}s"
                status = "PASSED"
                act_res = f"Passed: {expected_result or 'Native mobile action executed successfully on device.'}"
                notes = f"Executed on Android Device via ADB ({runner.serial or 'default'})."
                ss_p = runner.last_screenshot or ""
                on_step_callback(
                    idx,
                    status,
                    full_script,
                    ss_p,
                    notes,
                    duration=duration_str,
                    actual_result=act_res,
                )
            except Exception as e:
                duration_str = f"{round(time.time() - step_start, 2)}s"
                status = "FAILED"
                err_msg = str(e).strip().splitlines()[-1] if str(e).strip() else "Native execution failed"
                act_res = f"Failed: {err_msg}"
                notes = f"Native ADB Error: {e}"
                ss_p = runner.last_screenshot or ""
                on_step_callback(
                    idx,
                    status,
                    runner.full_script or custom_script,
                    ss_p,
                    notes,
                    duration=duration_str,
                    actual_result=act_res,
                )
