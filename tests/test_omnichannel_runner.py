import unittest
from unittest.mock import MagicMock, patch
import json
import os
import tempfile

from runners.web_runner import (
    OMNICHANNEL_PRESETS,
    _resolve_platform_config,
    _create_platform_context,
    _apply_network_throttling,
    WebRunner,
    WebBatchRunner,
)


class TestOmnichannelRunner(unittest.TestCase):
    def test_omnichannel_presets_registry(self):
        """Verifies all required platform presets are defined in OMNICHANNEL_PRESETS."""
        expected_keys = [
            "desktop_chrome",
            "iphone_15_pro",
            "pixel_7",
            "galaxy_s23",
            "ipad_air",
            "desktop_firefox",
            "desktop_safari",
            "android_cdp",
            "electron_cdp",
        ]
        for key in expected_keys:
            self.assertIn(key, OMNICHANNEL_PRESETS, f"Preset '{key}' missing from OMNICHANNEL_PRESETS")
            preset = OMNICHANNEL_PRESETS[key]
            self.assertIn("name", preset)
            self.assertIn("engine", preset)
            self.assertIn("is_mobile", preset)
            self.assertIn("has_touch", preset)

    def test_resolve_platform_config_defaults(self):
        """Resolving None or empty config must cleanly default to desktop_chrome."""
        cfg_none = _resolve_platform_config(None)
        self.assertEqual(cfg_none["platform"], "desktop_chrome")
        self.assertFalse(cfg_none["is_mobile"])
        self.assertFalse(cfg_none["has_touch"])
        self.assertEqual(cfg_none["engine"], "chromium")

        cfg_empty = _resolve_platform_config({})
        self.assertEqual(cfg_empty["platform"], "desktop_chrome")

    def test_resolve_mobile_and_tablet_presets(self):
        """Mobile and tablet presets must configure is_mobile, has_touch, and device_name."""
        iphone_cfg = _resolve_platform_config({"platform": "iphone_15_pro"})
        self.assertTrue(iphone_cfg["is_mobile"])
        self.assertTrue(iphone_cfg["has_touch"])
        self.assertEqual(iphone_cfg["device_name"], "iPhone 15 Pro")

        pixel_cfg = _resolve_platform_config({"platform": "pixel_7"})
        self.assertTrue(pixel_cfg["is_mobile"])
        self.assertTrue(pixel_cfg["has_touch"])
        self.assertEqual(pixel_cfg["device_name"], "Pixel 7")

        ipad_cfg = _resolve_platform_config({"platform": "ipad_air"})
        self.assertFalse(ipad_cfg["is_mobile"])  # iPads use tablet desktop-class browsing
        self.assertTrue(ipad_cfg["has_touch"])
        self.assertEqual(ipad_cfg["device_name"], "iPad Air")

    def test_resolve_cross_browser_engines(self):
        """Cross-browser presets must map to firefox and webkit engines."""
        firefox_cfg = _resolve_platform_config({"platform": "desktop_firefox"})
        self.assertEqual(firefox_cfg["engine"], "firefox")

        safari_cfg = _resolve_platform_config({"platform": "desktop_safari"})
        self.assertEqual(safari_cfg["engine"], "webkit")

    def test_resolve_cdp_endpoints(self):
        """Android and Electron CDP configurations must resolve custom or default endpoints."""
        android_cfg = _resolve_platform_config({"platform": "android_cdp"})
        self.assertTrue(android_cfg["is_cdp"])
        self.assertEqual(android_cfg["cdp_url"], "http://localhost:9222")

        electron_cfg = _resolve_platform_config({
            "platform": "electron_cdp",
            "cdp_url": "http://127.0.0.1:9333",
        })
        self.assertTrue(electron_cfg["is_cdp"])
        self.assertEqual(electron_cfg["cdp_url"], "http://127.0.0.1:9333")

    def test_create_platform_context_with_device_emulation(self):
        """Verifies _create_platform_context requests correct device parameters from p.devices."""
        mock_p = MagicMock()
        mock_p.devices = {
            "iPhone 15 Pro": {
                "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)...",
                "viewport": {"width": 393, "height": 852},
                "device_scale_factor": 3,
                "is_mobile": True,
                "has_touch": True,
            }
        }
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        cfg = _resolve_platform_config({"platform": "iphone_15_pro", "orientation": "portrait"})
        ctx, storage = _create_platform_context(mock_p, mock_browser, cfg)

        self.assertEqual(ctx, mock_context)
        call_kwargs = mock_browser.new_context.call_args[1]
        self.assertEqual(call_kwargs["viewport"], {"width": 393, "height": 852})
        self.assertTrue(call_kwargs["is_mobile"])
        self.assertTrue(call_kwargs["has_touch"])

    def test_create_platform_context_landscape_orientation(self):
        """Landscape orientation must invert width and height of the device viewport."""
        mock_p = MagicMock()
        mock_p.devices = {
            "Pixel 7": {
                "user_agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7)...",
                "viewport": {"width": 412, "height": 915},
                "device_scale_factor": 2.625,
                "is_mobile": True,
                "has_touch": True,
            }
        }
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        cfg = _resolve_platform_config({"platform": "pixel_7", "orientation": "landscape"})
        ctx, _ = _create_platform_context(mock_p, mock_browser, cfg)

        call_kwargs = mock_browser.new_context.call_args[1]
        self.assertEqual(call_kwargs["viewport"], {"width": 915, "height": 412})

    def test_network_throttling_offline_route_abort(self):
        """Offline network preset must register route abort handler."""
        mock_context = MagicMock()
        _apply_network_throttling(mock_context, "offline")
        mock_context.route.assert_called_once()
        pattern, handler = mock_context.route.call_args[0]
        self.assertEqual(pattern, "**/*")

        # Test route abort execution
        mock_route = MagicMock()
        handler(mock_route)
        mock_route.abort.assert_called_once_with("internetdisconnected")

    def test_runner_initialization_with_platform_config(self):
        """WebRunner and WebBatchRunner must retain platform_config upon initialization."""
        custom_cfg = {"platform": "pixel_7", "orientation": "landscape", "network_preset": "fast_4g"}

        runner = WebRunner(
            url="http://example.com",
            platform_config=custom_cfg,
        )
        self.assertEqual(runner.platform_config["platform"], "pixel_7")
        self.assertEqual(runner.platform_config["orientation"], "landscape")
        self.assertEqual(runner.platform_config["network_preset"], "fast_4g")

        batch_runner = WebBatchRunner(
            platform_config=custom_cfg,
        )
        self.assertEqual(batch_runner.platform_config["platform"], "pixel_7")
        self.assertEqual(batch_runner.platform_config["network_preset"], "fast_4g")


if __name__ == "__main__":
    unittest.main()
