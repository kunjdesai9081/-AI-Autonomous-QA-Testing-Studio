import unittest
from unittest.mock import MagicMock, patch

from runners.native_runner import (
    classify_and_validate_test_channel,
    NativeRunner,
    NativeBatchRunner,
)


class TestNativeRunnerAndChannelValidation(unittest.TestCase):

    @patch("runners.native_runner.list_connected_adb_devices")
    def test_classify_native_mobile_with_device(self, mock_devices):
        mock_devices.return_value = [{"serial": "emulator-5554", "status": "device", "is_ready": True}]
        row = {
            "Type": "Mobile (Native)",
            "Target": "android://com.HeylinkAudio.boult",
            "Playwright_Code": "# Android Native Tap\nadb shell input tap 500 1200",
        }
        channel, err = classify_and_validate_test_channel(row)
        self.assertEqual(channel, "native_mobile")
        self.assertIsNone(err)

    @patch("runners.native_runner.list_connected_adb_devices")
    def test_classify_native_mobile_no_device_returns_validation_error(self, mock_devices):
        mock_devices.return_value = []
        row = {
            "Type": "Mobile (Native)",
            "Target": "android://com.cuvora.carinfo",
        }
        channel, err = classify_and_validate_test_channel(row)
        self.assertEqual(channel, "native_mobile")
        self.assertIsNotNone(err)
        self.assertIn("No Android device or emulator detected", err)

    def test_classify_api_channel(self):
        row = {"Type": "API", "Target": "https://api.example.com/users"}
        channel, err = classify_and_validate_test_channel(row)
        self.assertEqual(channel, "api")
        self.assertIsNone(err)

    def test_classify_web_channel(self):
        row = {"Type": "Web", "Target": "https://example.com/login"}
        channel, err = classify_and_validate_test_channel(row)
        self.assertEqual(channel, "web")
        self.assertIsNone(err)

    @patch("runners.native_runner.list_connected_adb_devices")
    def test_target_with_android_scheme_always_routes_to_native_mobile(self, mock_devices):
        # Even if Type says 'Web', an android:// target package must route to native_mobile, NOT a browser!
        mock_devices.return_value = [{"serial": "emulator-5554", "status": "device", "is_ready": True}]
        row = {"Type": "Web", "Target": "android://com.zeptoconsumerapp"}
        channel, err = classify_and_validate_test_channel(row)
        self.assertEqual(channel, "native_mobile")
        self.assertIsNone(err)

    @patch("runners.native_runner.capture_device_screenshot", return_value=True)
    @patch("runners.native_runner.tap_device_element", return_value=True)
    @patch("runners.native_runner.get_foreground_app_info", return_value={"package": "com.boult", "activity": ".MainActivity"})
    @patch("runners.native_runner.list_connected_adb_devices")
    def test_native_runner_tap_execution(self, mock_devices, mock_fg, mock_tap, mock_ss):
        mock_devices.return_value = [{"serial": "device-123", "status": "device", "is_ready": True}]
        runner = NativeRunner(
            target="android://com.boult",
            test_step="Tap 'Pair Earbuds' native component",
            expected_result="Component responds",
            custom_script="# Android Native Tap\nadb shell input tap 540 1800",
            serial="device-123",
        )
        script = runner.execute()
        mock_tap.assert_called_once_with(540, 1800, serial="device-123")
        self.assertIn("adb shell input tap 540 1800", script)
        self.assertIsNotNone(runner.last_screenshot)

    @patch("runners.native_runner.capture_device_screenshot", return_value=True)
    @patch("runners.native_runner.dump_native_ui_elements")
    @patch("runners.native_runner.get_foreground_app_info", return_value={"package": "com.cuvora.carinfo", "activity": ".MainActivity"})
    @patch("runners.native_runner.list_connected_adb_devices")
    def test_native_runner_assertion_execution(self, mock_devices, mock_fg, mock_dump, mock_ss):
        mock_devices.return_value = [{"serial": "device-123", "status": "device", "is_ready": True}]
        mock_dump.return_value = [
            {"label": "Search Vehicle", "class": "android.widget.TextView", "bounds": "[100,200][500,300]"}
        ]
        runner = NativeRunner(
            target="android://com.cuvora.carinfo",
            test_step="Verify screen title and primary header 'Search Vehicle' is visible",
            expected_result="Search Vehicle is visible",
            serial="device-123",
        )
        script = runner.execute()
        self.assertIn("Android Native Execution", script)
        self.assertIsNotNone(runner.last_screenshot)

    @patch("runners.native_runner.capture_device_screenshot", return_value=True)
    @patch("runners.native_runner.tap_device_element", return_value=True)
    @patch("runners.native_runner.get_foreground_app_info", return_value={"package": "com.boult", "activity": ".MainActivity"})
    @patch("runners.native_runner.list_connected_adb_devices")
    def test_native_batch_runner(self, mock_devices, mock_fg, mock_tap, mock_ss):
        mock_devices.return_value = [{"serial": "device-123", "status": "device", "is_ready": True}]
        batch_runner = NativeBatchRunner(serial="device-123")
        items = [
            {
                "index": 1,
                "url": "android://com.boult",
                "test_step": "Tap 'Connect'",
                "expected_result": "Connected",
                "custom_script": "adb shell input tap 200 400",
            }
        ]
        results = []

        def callback(idx, status, script, ss, notes, duration="-", actual_result=None):
            results.append((idx, status, script, actual_result))

        batch_runner.run_batch(items, on_step_callback=callback)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 1)
        self.assertEqual(results[0][1], "PASSED")


if __name__ == "__main__":
    unittest.main()
