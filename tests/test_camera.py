from __future__ import annotations

import unittest
from unittest import mock

import cv2

from airlock import camera
from airlock.camera import CONTROLS, CameraConfig, apply_controls, resolve_backend

LIST_CTRLS = """User Controls
                     brightness 0x00980900 (int)    : min=30 max=255 step=1 default=-8193 value=133 flags=has-min-max
                       contrast 0x00980901 (int)    : min=0 max=10 step=1 default=57343 value=5 flags=has-min-max
                     saturation 0x00980902 (int)    : min=0 max=200 step=1 default=57343 value=103 flags=has-min-max
        white_balance_automatic 0x0098090c (bool)   : default=1 value=0
                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=0 value=1 (Manual Mode)
         exposure_time_absolute 0x009a0902 (int)    : min=1 max=10000 step=1 default=156 value=2 flags=has-min-max
                  focus_absolute 0x009a090a (int)   : min=0 max=16 step=1 default=57343 value=2 flags=has-min-max
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=0
                  zoom_absolute 0x009a090d (int)    : min=0 max=317 step=1 default=57343 value=0 flags=has-min-max
"""


class ProbeTestCase(unittest.TestCase):
    def _probe(self):
        with mock.patch.object(camera.shutil, "which", return_value="/usr/bin/v4l2-ctl"), \
             mock.patch.object(camera.subprocess, "run",
                               return_value=mock.Mock(stdout=LIST_CTRLS)):
            return camera.probe_v4l2_controls(0)

    def test_parses_ranges(self):
        controls = self._probe()
        self.assertEqual(controls["exposure_time_absolute"]["max"], 10000)
        self.assertEqual(controls["exposure_time_absolute"]["min"], 1)
        self.assertEqual(controls["brightness"]["min"], 30)
        self.assertEqual(controls["brightness"]["max"], 255)
        self.assertEqual(controls["focus_absolute"]["max"], 16)
        self.assertEqual(controls["auto_exposure"]["type"], "menu")

    def test_skips_garbage_default(self):
        controls = self._probe()
        self.assertEqual(controls["brightness"]["default"], -8193)  # сырое значение, чистит UI

    def test_no_v4l2_returns_empty(self):
        with mock.patch.object(camera.shutil, "which", return_value=None):
            self.assertEqual(camera.probe_v4l2_controls(0), {})

    def test_device_path(self):
        self.assertEqual(camera.device_path(0), "/dev/video0")
        self.assertEqual(camera.device_path("3"), "/dev/video3")
        self.assertEqual(camera.device_path("/dev/video2"), "/dev/video2")


class ApplyControlsTestCase(unittest.TestCase):
    def test_orders_auto_before_values(self):
        calls = []
        cap = mock.Mock()
        cap.set = lambda prop, value: (calls.append((prop, value)), True)[1]
        apply_controls(cap, {"exposure": 10, "auto_exposure": 1, "brightness": 150})
        order = [p for p, _ in calls]
        self.assertLess(order.index(CONTROLS["auto_exposure"]), order.index(CONTROLS["exposure"]))

    def test_unknown_control_reported_false(self):
        cap = mock.Mock()
        cap.set = lambda prop, value: True
        result = apply_controls(cap, {"teleport": 1, "brightness": 100})
        self.assertFalse(result["teleport"])
        self.assertTrue(result["brightness"])

    def test_empty_controls(self):
        self.assertEqual(apply_controls(mock.Mock(), {}), {})


class ControlsMapTestCase(unittest.TestCase):
    def test_focus_controls_present(self):
        self.assertEqual(CONTROLS["autofocus"], cv2.CAP_PROP_AUTOFOCUS)
        self.assertEqual(CONTROLS["focus"], cv2.CAP_PROP_FOCUS)
        self.assertEqual(CONTROLS["zoom"], cv2.CAP_PROP_ZOOM)

    def test_common_controls_present(self):
        for name in ("exposure", "auto_exposure", "gain", "brightness", "auto_white_balance"):
            self.assertIn(name, CONTROLS)

    def test_v4l2_name_aliases(self):
        self.assertEqual(CONTROLS["exposure_time_absolute"], cv2.CAP_PROP_EXPOSURE)
        self.assertEqual(CONTROLS["focus_absolute"], cv2.CAP_PROP_FOCUS)
        self.assertEqual(CONTROLS["focus_automatic_continuous"], cv2.CAP_PROP_AUTOFOCUS)
        self.assertEqual(CONTROLS["white_balance_automatic"], cv2.CAP_PROP_AUTO_WB)
        self.assertEqual(CONTROLS["white_balance_temperature"], cv2.CAP_PROP_WB_TEMPERATURE)
        self.assertEqual(CONTROLS["backlight_compensation"], cv2.CAP_PROP_BACKLIGHT)
        self.assertEqual(CONTROLS["zoom_absolute"], cv2.CAP_PROP_ZOOM)

    def test_unknown_control_not_mapped(self):
        self.assertIsNone(CONTROLS.get("teleport"))


class CameraConfigTestCase(unittest.TestCase):
    def test_controls_default_empty(self):
        self.assertEqual(CameraConfig().controls, {})

    def test_focus_values_roundtrip(self):
        cfg = CameraConfig(controls={"autofocus": 0, "focus": 100, "exposure": -6})
        data = cfg.as_dict()
        self.assertEqual(data["controls"]["autofocus"], 0)
        self.assertEqual(data["controls"]["focus"], 100)

    def test_backend_resolution_is_int(self):
        for name in ("auto", "v4l2", "any", "dshow", "nonsense"):
            self.assertIsInstance(resolve_backend(name), int)


if __name__ == "__main__":
    unittest.main()
