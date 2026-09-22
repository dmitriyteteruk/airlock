from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from airlock.config import (
    AlertConfig,
    AppConfig,
    CameraConfig,
    ConfigError,
    PipelineConfig,
    StorageConfig,
    WebConfig,
    _deep_merge,
    load_overrides,
    save_overrides,
)
from airlock.detector import DetectorParams
from airlock.vision import Roi, VisionParams

MINIMAL_YAML = """
camera:
  device: 1
  width: 1280
  height: 720
detector:
  on_threshold: 0.01
  off_threshold: 0.002
rois:
  - name: airlock-1
    x: 10
    y: 20
    w: 60
    h: 80
web:
  port: 9000
  token: secret
"""


class ConfigFileTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="airlock-cfg-")
        self.path = os.path.join(self.tmp, "config.yaml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_defaults_are_valid(self):
        config = AppConfig()
        config.validate()
        self.assertEqual(config.camera.device, 0)
        self.assertEqual(config.vision.background, "mog2")
        self.assertEqual(config.rois, [])

    def test_load_yaml_overrides_defaults(self):
        self.write(MINIMAL_YAML)
        config = AppConfig.load(self.path, overrides_path=os.path.join(self.tmp, "none.json"))
        self.assertEqual(config.camera.device, 1)
        self.assertEqual(config.camera.width, 1280)
        self.assertEqual(config.detector.on_threshold, 0.01)
        self.assertEqual(config.web.port, 9000)
        self.assertEqual(config.rois[0].name, "airlock-1")
        self.assertEqual(config.rois[0].rect(), (10, 20, 60, 80))
        self.assertEqual(config.storage.data_dir, "data")

    def test_missing_file_falls_back_to_defaults(self):
        config = AppConfig.load(os.path.join(self.tmp, "nope.yaml"))
        self.assertEqual(config.camera.width, 640)

    def test_empty_yaml(self):
        self.write("# только комментарий\n")
        config = AppConfig.load(self.path)
        self.assertEqual(config.camera.device, 0)

    def test_broken_yaml_raises(self):
        self.write("camera: [1, 2\n")
        with self.assertRaises(ConfigError):
            AppConfig.load(self.path)

    def test_non_mapping_root_raises(self):
        self.write("- 1\n- 2\n")
        with self.assertRaises(ConfigError):
            AppConfig.load(self.path)

    def test_overrides_win_over_yaml(self):
        self.write(MINIMAL_YAML)
        overrides = os.path.join(self.tmp, "overrides.json")
        with open(overrides, "w", encoding="utf-8") as fh:
            json.dump({"detector": {"on_threshold": 0.03}, "rois": [
                {"name": "z", "x": 1, "y": 2, "w": 40, "h": 40}]}, fh)

        config = AppConfig.load(self.path, overrides_path=overrides)
        self.assertEqual(config.detector.on_threshold, 0.03)
        self.assertEqual(config.detector.off_threshold, 0.002)
        self.assertEqual([r.name for r in config.rois], ["z"])
        self.assertEqual(config.overrides_path, overrides)

    def test_broken_overrides_are_ignored(self):
        self.write(MINIMAL_YAML)
        overrides = os.path.join(self.tmp, "overrides.json")
        with open(overrides, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        config = AppConfig.load(self.path, overrides_path=overrides)
        self.assertEqual(config.detector.on_threshold, 0.01)

    def test_extra_patch_wins_over_files(self):
        self.write(MINIMAL_YAML)
        config = AppConfig.load(self.path, extra={"camera": {"device": "/dev/video2"}})
        self.assertEqual(config.camera.device, "/dev/video2")
        self.assertEqual(config.camera.width, 1280)

    def test_roi_detector_override_parsed(self):
        self.write("""
rois:
  - name: a
    x: 10
    y: 10
    w: 60
    h: 60
    detector:
      on_threshold: 0.02
      refractory_s: 0.3
""")
        config = AppConfig.load(self.path)
        self.assertEqual(config.rois[0].detector["on_threshold"], 0.02)
        self.assertEqual(config.rois[0].as_dict()["detector"]["refractory_s"], 0.3)

    def test_roi_override_roundtrip_serialise(self):
        config = AppConfig(rois=[Roi("a", 0, 0, 20, 20, {"on_threshold": 0.01})])
        restored = AppConfig.from_dict(config.as_dict())
        self.assertEqual(restored.rois[0].detector["on_threshold"], 0.01)

    def test_camera_controls_focus_parsed(self):
        self.write("""
camera:
  controls:
    autofocus: 0
    focus: 120
    exposure: -5
""")
        config = AppConfig.load(self.path)
        self.assertEqual(config.camera.controls["autofocus"], 0)
        self.assertEqual(config.camera.controls["focus"], 120)
        self.assertEqual(config.camera.controls["exposure"], -5)

    def test_unknown_keys_do_not_crash(self):
        self.write("camera:\n  nonsense: 5\ndetector:\n  bogus: 1\n")
        config = AppConfig.load(self.path)
        self.assertEqual(config.camera.device, 0)


class ValidationTestCase(unittest.TestCase):
    def test_duplicate_roi_names_rejected(self):
        config = AppConfig(rois=[Roi("a", 0, 0, 20, 20), Roi("a", 30, 0, 20, 20)])
        with self.assertRaises(ConfigError):
            config.validate()

    def test_tiny_roi_rejected(self):
        with self.assertRaises(ConfigError):
            AppConfig(rois=[Roi("a", 0, 0, 3, 3)]).validate()

    def test_threshold_order_rejected(self):
        config = AppConfig(detector=DetectorParams(on_threshold=0.001, off_threshold=0.005))
        with self.assertRaises(ConfigError):
            config.validate()

    def test_saturate_below_on_rejected(self):
        config = AppConfig(detector=DetectorParams(on_threshold=0.05, off_threshold=0.01, saturate_ratio=0.02))
        with self.assertRaises(ConfigError):
            config.validate()

    def test_bad_process_scale_rejected(self):
        with self.assertRaises(ConfigError):
            AppConfig(vision=VisionParams(process_scale=0)).validate()

    def test_per_zone_bad_ordering_rejected(self):
        config = AppConfig(
            detector=DetectorParams(on_threshold=0.004, off_threshold=0.001),
            rois=[Roi("a", 0, 0, 20, 20, {"on_threshold": 0.001, "off_threshold": 0.005})],
        )
        with self.assertRaises(ConfigError):
            config.validate()

    def test_roi_detector_must_be_mapping(self):
        config = AppConfig(rois=[Roi("a", 0, 0, 20, 20)])
        config.rois[0].detector = "not-a-dict"
        with self.assertRaises(ConfigError):
            config.validate()


class OverridesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="airlock-ovr-")
        self.path = os.path.join(self.tmp, "sub", "overrides.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_gives_empty_dict(self):
        self.assertEqual(load_overrides(self.path), {})

    def test_save_creates_dirs_and_merges(self):
        save_overrides(self.path, {"detector": {"on_threshold": 0.01}})
        save_overrides(self.path, {"detector": {"refractory_s": 0.3}, "rois": []})
        saved = load_overrides(self.path)
        self.assertEqual(saved["detector"]["on_threshold"], 0.01)
        self.assertEqual(saved["detector"]["refractory_s"], 0.3)
        self.assertEqual(saved["rois"], [])

    def test_save_does_not_leave_tmp(self):
        save_overrides(self.path, {"web": {"port": 1}})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_unicode_roundtrip(self):
        save_overrides(self.path, {"rois": [{"name": "затвор-1", "x": 0, "y": 0, "w": 10, "h": 10}]})
        self.assertEqual(load_overrides(self.path)["rois"][0]["name"], "затвор-1")


class MergeAndSerialiseTestCase(unittest.TestCase):
    def test_deep_merge_is_recursive(self):
        merged = _deep_merge(
            {"a": {"b": 1, "c": 2}, "d": [1]},
            {"a": {"c": 3}, "d": [2, 3], "e": 4},
        )
        self.assertEqual(merged, {"a": {"b": 1, "c": 3}, "d": [2, 3], "e": 4})

    def test_deep_merge_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": {"c": 2}})
        self.assertEqual(base, {"a": {"b": 1}})

    def test_as_dict_roundtrip(self):
        original = AppConfig(
            camera=CameraConfig(device=2, width=320, height=240),
            rois=[Roi("a", 1, 2, 30, 40)],
            web=WebConfig(port=8123),
        )
        data = original.as_dict()
        restored = AppConfig.from_dict(data)
        self.assertEqual(restored.camera.device, 2)
        self.assertEqual(restored.rois[0].rect(), (1, 2, 30, 40))
        self.assertEqual(restored.web.port, 8123)

    def test_runtime_dict_contains_tunables_only(self):
        data = AppConfig(rois=[Roi("a", 0, 0, 10, 10)]).runtime_dict()
        self.assertEqual(set(data), {"rois", "vision", "detector", "alerts"})

    def test_frame_size(self):
        self.assertEqual(AppConfig(camera=CameraConfig(width=640, height=480)).frame_size(), (640, 480))
        self.assertIsNone(AppConfig(camera=CameraConfig(width=0, height=0)).frame_size())

    def test_section_dataclasses_have_defaults(self):
        for cls in (PipelineConfig, StorageConfig, AlertConfig, WebConfig):
            self.assertIsNotNone(cls())


if __name__ == "__main__":
    unittest.main()
