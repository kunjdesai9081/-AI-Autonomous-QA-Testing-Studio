import unittest
from unittest.mock import MagicMock, patch
import subprocess

from core.device_bridge import (
    list_connected_adb_devices,
    list_device_inspectable_sockets,
    list_installed_packages,
    forward_device_port,
    launch_app_on_device,
    fetch_cdp_targets,
)


class TestDeviceBridge(unittest.TestCase):
    @patch("core.device_bridge.subprocess.run")
    def test_list_connected_adb_devices_parsing(self, mock_run):
        """Verifies parsing of adb devices -l output into structured dictionaries."""
        mock_output = (
            "List of devices attached\n"
            "emulator-5554          device product:sdk_gphone64_arm64 model:Pixel_7 device:emu64a transport_id:1\n"
            "R5CN10ABCD            device product:a52sxq model:SM_A528B device:a52s transport_id:2\n"
            "192.168.1.50:5555     offline transport_id:3\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)

        devices = list_connected_adb_devices()
        self.assertEqual(len(devices), 3)

        dev0 = devices[0]
        self.assertEqual(dev0["serial"], "emulator-5554")
        self.assertEqual(dev0["model"], "Pixel 7")
        self.assertTrue(dev0["is_ready"])

        dev1 = devices[1]
        self.assertEqual(dev1["serial"], "R5CN10ABCD")
        self.assertEqual(dev1["model"], "SM A528B")
        self.assertTrue(dev1["is_ready"])

        dev2 = devices[2]
        self.assertEqual(dev2["status"], "offline")
        self.assertFalse(dev2["is_ready"])

    @patch("core.device_bridge.get_adb_path", return_value="adb")
    @patch("core.device_bridge.subprocess.run")
    def test_list_device_inspectable_sockets_chrome_and_webview(self, mock_run, mock_adb_path):
        """Verifies regex extraction of @chrome_devtools_remote and @webview_devtools_remote_<pid>."""
        mock_unix_net = (
            "0000000000000000: 00000002 00000000 00010000 0001 01 12345 @chrome_devtools_remote\n"
            "0000000000000000: 00000002 00000000 00010000 0001 01 67890 @webview_devtools_remote_14205\n"
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=mock_unix_net),
            MagicMock(returncode=0, stdout="com.mycompany.app\x00"),
        ]

        sockets = list_device_inspectable_sockets(serial="emulator-5554")
        self.assertEqual(len(sockets), 2)

        s0 = sockets[0]
        self.assertEqual(s0["socket"], "chrome_devtools_remote")
        self.assertEqual(s0["type"], "chrome")

        s1 = sockets[1]
        self.assertEqual(s1["socket"], "webview_devtools_remote_14205")
        self.assertEqual(s1["type"], "webview")
        self.assertEqual(s1["pid"], "14205")
        self.assertEqual(s1["package"], "com.mycompany.app")

    @patch("core.device_bridge.subprocess.run")
    def test_list_installed_packages(self, mock_run):
        """Verifies parsing of pm list packages output."""
        mock_packages = "package:com.android.chrome\npackage:com.twitter.android\npackage:com.instagram.android\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_packages)

        pkgs = list_installed_packages(serial="emulator-5554")
        self.assertEqual(pkgs, ["com.android.chrome", "com.instagram.android", "com.twitter.android"])

    @patch("core.device_bridge.subprocess.run")
    def test_forward_device_port(self, mock_run):
        """Verifies adb forward arguments."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        ok = forward_device_port("chrome_devtools_remote", local_port=9222, serial="emulator-5554")
        self.assertTrue(ok)

        call_args = mock_run.call_args[0][0]
        self.assertIn("forward", call_args)
        self.assertIn("tcp:9222", call_args)
        self.assertIn("localabstract:chrome_devtools_remote", call_args)

    @patch("core.device_bridge.subprocess.run")
    def test_launch_app_on_device(self, mock_run):
        """Verifies monkey launch command construction."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        ok = launch_app_on_device("com.example.app", serial="emulator-5554")
        self.assertTrue(ok)

        call_args = mock_run.call_args[0][0]
        self.assertIn("monkey", call_args)
        self.assertIn("com.example.app", call_args)

    @patch("core.device_bridge.requests.get")
    def test_fetch_cdp_targets_json(self, mock_get):
        """Verifies fetching and parsing /json/list from CDP endpoint."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"id": "tab1", "title": "App Home", "type": "page", "url": "http://10.0.2.2:3000"},
            {"id": "tab2", "title": "Service Worker", "type": "service_worker", "url": "sw.js"},
        ]
        mock_get.return_value = mock_resp

        targets = fetch_cdp_targets("http://localhost:9222")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["id"], "tab1")
    @patch("core.device_bridge.subprocess.run")
    def test_get_foreground_app_info(self, mock_run):
        mock_output = (
            "  ResumedActivity: ActivityRecord{151898549 u0 com.cuvora.carinfo/com.vassuperapp.MainActivity t77291 d0}\n"
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
        from core.device_bridge import get_foreground_app_info
        info = get_foreground_app_info()
        self.assertEqual(info["package"], "com.cuvora.carinfo")
        self.assertEqual(info["activity"], "com.vassuperapp.MainActivity")

    @patch("core.device_bridge.get_adb_path", return_value="adb")
    @patch("core.device_bridge.subprocess.run")
    def test_dump_native_ui_elements(self, mock_run, mock_adb):
        xml_content = (
            '<hierarchy rotation="0">'
            '<node index="0" text="Search Vehicle" resource-id="com.cuvora.carinfo:id/search" '
            'class="android.widget.EditText" package="com.cuvora.carinfo" content-desc="" '
            'clickable="true" enabled="true" bounds="[50,100][500,200]" />'
            '</hierarchy>'
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=xml_content),
        ]
        from core.device_bridge import dump_native_ui_elements
        elems = dump_native_ui_elements()
        self.assertEqual(len(elems), 1)
        self.assertEqual(elems[0]["label"], "Search Vehicle")
        self.assertEqual(elems[0]["resource_id"], "com.cuvora.carinfo:id/search")
        self.assertTrue(elems[0]["clickable"])

    @patch("core.device_bridge.get_adb_path", return_value="adb")
    @patch("core.device_bridge.subprocess.run")
    def test_tap_device_element(self, mock_run, mock_adb):
        mock_run.return_value = MagicMock(returncode=0)
        from core.device_bridge import tap_device_element
        ok = tap_device_element(123, 456)
        self.assertTrue(ok)
        mock_run.assert_called_once_with(["adb", "shell", "input", "tap", "123", "456"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)

    @patch("core.device_bridge.swipe_device_screen", return_value=True)
    @patch("core.device_bridge.tap_device_element", return_value=True)
    @patch("core.device_bridge.dump_native_ui_elements")
    @patch("core.device_bridge.capture_device_screenshot", return_value=True)
    @patch("core.device_bridge.get_foreground_app_info", return_value={"package": "com.zeptoconsumerapp", "activity": ".MainActivity"})
    def test_explore_android_app_flow(self, mock_fg, mock_ss, mock_dump, mock_tap, mock_swipe):
        mock_dump.side_effect = [
            [
                {"label": "Grocery & Kitchen", "class": "android.widget.TextView", "bounds": "[0,100][500,200]", "clickable": False, "enabled": True},
                {"label": "Top Picks tab", "class": "android.widget.TextView", "bounds": "[10,200][200,400]", "clickable": True, "enabled": True},
            ],
            [
                {"label": "Noodles & Snacks", "class": "android.widget.TextView", "bounds": "[0,100][500,200]", "clickable": False, "enabled": True},
            ]
        ]
        from core.device_bridge import explore_android_app_flow
        res = explore_android_app_flow(package_name="com.zeptoconsumerapp", max_screens=2)
        self.assertEqual(res["start_url"], "android://com.zeptoconsumerapp")
        self.assertGreaterEqual(len(res["screens"]), 1)
        self.assertEqual(res["screens"][0]["name"], "Grocery & Kitchen")
        self.assertIn("role", res["screens"][0])

    def test_clean_resource_id_and_label_derivation(self):
        from core.device_bridge import _clean_resource_id, _derive_element_label
        self.assertEqual(_clean_resource_id("com.boult:id/tab_device"), "Device")
        self.assertEqual(_clean_resource_id("com.boult:id/iv_settings"), "Settings")
        self.assertEqual(_clean_resource_id("com.boult:id/btn_add_device"), "Add Device")

        # Dummy labels ignored, fall back to cleaned resource ID
        lbl = _derive_element_label(text="TextView", content_desc="", res_id="com.boult:id/tab_tools", cls_name="android.widget.TextView")
        self.assertEqual(lbl, "Tools")

        # Fallback to widget category if interactive with no id
        lbl2 = _derive_element_label(text="", content_desc="", res_id="", cls_name="android.widget.EditText")
        self.assertEqual(lbl2, "Input Field")

    def test_jaccard_similarity(self):
        from core.device_bridge import compute_jaccard_similarity
        set1 = {"device", "tools", "settings"}
        set2 = {"device", "tools", "settings"}
        self.assertEqual(compute_jaccard_similarity(set1, set2), 1.0)

        set3 = {"device", "tools", "profile"}
        sim = compute_jaccard_similarity(set1, set3)
        self.assertAlmostEqual(sim, 2.0 / 4.0)

    def test_navigation_desirability_scoring(self):
        from core.device_bridge import calculate_navigation_desirability
        # System navigation key at y=2750 on 2800 screen -> must be rejected (-999)
        sys_elem = {"bounds": "[500,2700][600,2800]", "clickable": True, "label": "Back"}
        self.assertEqual(calculate_navigation_desirability(sys_elem, screen_w=1260, screen_h=2800), -999.0)

        # Top-left back button -> must be rejected (-999)
        back_elem = {"bounds": "[40,120][140,240]", "clickable": True, "label": "Back"}
        self.assertEqual(calculate_navigation_desirability(back_elem, screen_w=1260, screen_h=2800), -999.0)

        # App bottom tab (Device tab at y=2500 on 2800 screen) -> high score (>=100)
        tab_elem = {"bounds": "[100,2450][400,2600]", "clickable": True, "label": "Device", "resource_id": "tab_device"}
        score = calculate_navigation_desirability(tab_elem, screen_w=1260, screen_h=2800)
        self.assertGreaterEqual(score, 100.0)

    def test_detect_screen_role(self):
        from core.device_bridge import detect_screen_role
        role1 = detect_screen_role(headings=["Equalizer"], tabs=["Device", "Tools", "Settings"], inputs=[], action_buttons=["Bass Boost"])
        self.assertEqual(role1, "Tab Navigation Hub")

        role2 = detect_screen_role(headings=["Settings"], tabs=[], inputs=[], action_buttons=["Account", "Notifications"])
        self.assertEqual(role2, "Settings & Preferences")

    def test_synthesize_native_test_cases_filters_dummy_and_exit(self):
        from core.device_bridge import synthesize_native_test_cases
        elements = [
            {"label": "TextView", "class": "android.widget.TextView", "clickable": True, "bounds": "[10,10][100,100]"},
            {"label": "Close", "class": "android.widget.Button", "clickable": True, "bounds": "[10,10][100,100]"},
            {"label": "Pair Earbuds", "class": "android.widget.Button", "clickable": True, "bounds": "[100,500][400,600]", "screen_h": 2800},
            {"label": "Search Model", "class": "android.widget.EditText", "clickable": True, "bounds": "[100,200][800,300]", "screen_h": 2800},
        ]
        cases = synthesize_native_test_cases(elements, package_name="com.boult", screen_title="Device Home")
        self.assertGreaterEqual(len(cases), 3)  # Presence assertion + Pair Earbuds + Search Model
        labels_in_cases = [c["Test_Step_Description"] for c in cases]
        # Ensure TextView and Close were NOT synthesized as action steps
        self.assertFalse(any("TextView" in desc for desc in labels_in_cases))
        self.assertFalse(any("Close" in desc for desc in labels_in_cases))
        self.assertTrue(any("Pair Earbuds" in desc for desc in labels_in_cases))
        self.assertTrue(any("Search Model" in desc for desc in labels_in_cases))


    @patch("core.device_bridge.get_adb_path", return_value="adb")
    @patch("core.device_bridge.subprocess.run")
    def test_dump_native_ui_elements_with_unicode_characters(self, mock_run, mock_adb):
        """Verifies dump_native_ui_elements handles non-ASCII characters without cp1252 UnicodeDecodeError."""
        unicode_xml = (
            '<hierarchy rotation="0">'
            '<node index="0" text="गाड़ी खोजें 🚗" resource-id="com.cuvora.carinfo:id/search" '
            'class="android.widget.TextView" package="com.cuvora.carinfo" content-desc="वाहन विवरण" '
            'clickable="true" enabled="true" bounds="[50,100][500,200]" />'
            '</hierarchy>'
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=unicode_xml),
        ]
        from core.device_bridge import dump_native_ui_elements
        elems = dump_native_ui_elements()
        self.assertEqual(len(elems), 1)
        self.assertIn("गाड़ी खोजें", elems[0]["text"])
        self.assertEqual(elems[0]["content_desc"], "वाहन विवरण")

    def test_multi_element_screen_no_ignored_elements(self):
        """Verifies screens with multiple identical or varied elements test all elements without ignoring any."""
        from core.device_bridge import synthesize_native_test_cases
        elements = [
            # 3 buttons with identical label 'Add' at different coordinates
            {"label": "Add", "class": "android.widget.Button", "clickable": True, "bounds": "[100,500][300,600]", "screen_h": 2800},
            {"label": "Add", "class": "android.widget.Button", "clickable": True, "bounds": "[100,700][300,800]", "screen_h": 2800},
            {"label": "Add", "class": "android.widget.Button", "clickable": True, "bounds": "[100,900][300,1000]", "screen_h": 2800},
            # Switch toggle
            {"label": "Dark Mode", "class": "android.widget.Switch", "clickable": True, "checkable": True, "bounds": "[100,1100][300,1200]", "screen_h": 2800},
            # Input search
            {"label": "Product Search", "class": "android.widget.EditText", "clickable": True, "bounds": "[50,150][950,250]", "screen_h": 2800},
        ]
        cases = synthesize_native_test_cases(elements, package_name="com.test.app", screen_title="Catalog Screen")
        # 1 presence assertion + 3 'Add' buttons + 1 switch toggle + 1 input = 6 test cases
        self.assertEqual(len(cases), 6)
        descs = [c["Test_Step_Description"] for c in cases]
        self.assertTrue(any("Add #1" in d for d in descs))
        self.assertTrue(any("Add #2" in d for d in descs))
        self.assertTrue(any("Add #3" in d for d in descs))
        self.assertTrue(any("Toggle state for 'Dark Mode'" in d for d in descs))
        self.assertTrue(any("Focus and enter test query into 'Product Search'" in d for d in descs))

    @patch("core.device_bridge.get_adb_path", return_value="adb")
    @patch("core.device_bridge.subprocess.run")
    def test_descendant_text_extraction_for_clickable_container(self, mock_run, mock_adb):
        """Verifies clickable container views inherit text from descendant TextViews."""
        xml_with_container = (
            '<hierarchy rotation="0">'
            '<node index="0" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">'
            '  <node index="1" class="android.view.ViewGroup" bounds="[100,400][900,600]" clickable="true" enabled="true">'
            '    <node index="2" class="android.widget.ImageView" bounds="[120,420][200,500]" />'
            '    <node index="3" class="android.widget.TextView" text="Explore Flash Deals" bounds="[220,420][850,500]" />'
            '  </node>'
            '</node>'
            '</hierarchy>'
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout=xml_with_container),
        ]
        from core.device_bridge import dump_native_ui_elements
        elems = dump_native_ui_elements()
        # Ensure the clickable ViewGroup inherited the label from the inner TextView
        clickable_vg = [e for e in elems if "ViewGroup" in e["class"] and e["clickable"]]
        self.assertTrue(len(clickable_vg) > 0)
        self.assertEqual(clickable_vg[0]["label"], "Explore Flash Deals")

    @patch("time.sleep", return_value=None)
    @patch("core.device_bridge.press_device_key", return_value=True)
    @patch("core.device_bridge.swipe_device_screen", return_value=True)
    @patch("core.device_bridge.tap_device_element", return_value=True)
    @patch("core.device_bridge.dump_native_ui_elements")
    @patch("core.device_bridge.capture_device_screenshot", return_value=True)
    @patch("core.device_bridge.get_foreground_app_info", return_value={"package": "com.test.app", "activity": ".MainActivity"})
    def test_whole_flow_exploration_beyond_10_screens(self, mock_fg, mock_ss, mock_dump, mock_tap, mock_swipe, mock_back, mock_sleep):
        """Verifies crawler can autonomously crawl more than 10 screens without truncation."""
        # Generate 12 distinct screen states
        screen_dumps = [
            [{"label": f"Screen Category {i}", "class": "android.widget.TextView", "bounds": f"[0,{100*i}][500,{100*i+50}]", "clickable": False, "enabled": True},
             {"label": f"Tab {i}", "class": "android.widget.TextView", "bounds": f"[10,{200*i}][200,{200*i+50}]", "clickable": True, "enabled": True}]
            for i in range(12)
        ]
        mock_dump.side_effect = screen_dumps
        from core.device_bridge import explore_android_app_flow
        res = explore_android_app_flow(package_name="com.test.app", max_screens=12)
        self.assertEqual(res["total_screens"], 12)
        self.assertEqual(len(res["screens"]), 12)
        self.assertEqual(res["screens"][11]["name"], "Screen Category 11")

    def test_slide_bar_detection_and_test_synthesis(self):
        """Verifies SeekBars/Sliders are detected and synthesized into slide/adjust test cases."""
        from core.device_bridge import synthesize_native_test_cases, _derive_element_label
        # Label derivation
        lbl = _derive_element_label(text="", content_desc="", res_id="com.boult:id/equalizer_seek", cls_name="android.widget.SeekBar")
        self.assertEqual(lbl, "Equalizer Seek")

        elements = [
            {"label": "Volume Slider", "class": "android.widget.SeekBar", "clickable": True, "bounds": "[100,600][900,700]", "screen_h": 2400},
            {"label": "Equalizer Bar", "class": "com.google.android.material.slider.Slider", "clickable": False, "is_slider": True, "bounds": "[100,800][900,900]", "screen_h": 2400},
        ]
        cases = synthesize_native_test_cases(elements, package_name="com.boult", screen_title="Equalizer")
        slide_cases = [c for c in cases if "Slide and adjust" in c["Test_Step_Description"]]
        self.assertEqual(len(slide_cases), 2)
        self.assertIn("adb shell input swipe", slide_cases[0]["Playwright_Code"])

    def test_navigation_drawer_hamburger_top_left_allowed(self):
        """Verifies top-left hamburger/drawer buttons are prioritized rather than rejected as back buttons."""
        from core.device_bridge import calculate_navigation_desirability
        # Top-left back button -> rejected (-999)
        back_elem = {"bounds": "[30,60][120,140]", "clickable": True, "label": "Back", "resource_id": "iv_back"}
        self.assertEqual(calculate_navigation_desirability(back_elem, screen_w=1080, screen_h=2400), -999.0)

        # Top-left hamburger/drawer button -> high priority (95.0)
        drawer_elem = {"bounds": "[30,60][120,140]", "clickable": True, "label": "Open Menu", "resource_id": "iv_drawer_menu"}
        score = calculate_navigation_desirability(drawer_elem, screen_w=1080, screen_h=2400)
        self.assertGreaterEqual(score, 90.0)

    @patch("time.sleep", return_value=None)
    @patch("core.device_bridge.swipe_device_screen", return_value=True)
    @patch("core.device_bridge.tap_device_element", return_value=True)
    @patch("core.device_bridge.dump_native_ui_elements")
    @patch("core.device_bridge.capture_device_screenshot", return_value=True)
    @patch("core.device_bridge.get_foreground_app_info", return_value={"package": "com.test.app", "activity": ".MainActivity"})
    def test_uncapped_flow_crawl_no_limit(self, mock_fg, mock_ss, mock_dump, mock_tap, mock_swipe, mock_sleep):
        """Verifies setting max_screens=0 runs uncapped discovery and screen_rec has no sliced caps."""
        mock_dump.side_effect = [
            [
                {"label": f"Category Item {j}", "class": "android.widget.TextView", "bounds": f"[0,{j*50}][500,{j*50+40}]", "clickable": True}
                for j in range(60)  # Over 50 items to verify no truncation
            ] + [{"label": "Equalizer Seek", "class": "android.widget.SeekBar", "bounds": "[100,500][900,600]", "is_slider": True}],
            [],
            [],
        ]
        from core.device_bridge import explore_android_app_flow
        res = explore_android_app_flow(package_name="com.test.app", max_screens=0)
        self.assertGreaterEqual(res["total_screens"], 1)
        scr = res["screens"][0]
        # Verify action_buttons has all 60 items without a [:50] cap
        self.assertEqual(len(scr["action_buttons"]), 60)
        # Verify slide_bars captured the seekbar
        self.assertEqual(len(scr["slide_bars"]), 1)


if __name__ == "__main__":
    unittest.main()


