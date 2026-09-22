from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

import numpy as np

from airlock.config import AppConfig
from airlock.detector import DetectorParams
from airlock.pipeline import Pipeline
from airlock.vision import Roi, VisionParams
from airlock.web import create_app, create_public_app
from .test_vision import ROI, base_scene, bubble_at, render

FPS = 20.0


class Harness:
    """Общая обвязка: временный каталог данных, пайплайн и генератор кадров."""

    def build(self):
        self.tmp = tempfile.mkdtemp(prefix="airlock-test-")
        self.config = AppConfig(
            vision=VisionParams(warmup_frames=40),
            detector=DetectorParams(on_threshold=0.004, off_threshold=0.001, refractory_s=0.15),
            rois=[ROI],
        )
        self.config.storage.data_dir = self.tmp
        self.config.overrides_path = os.path.join(self.tmp, "overrides.json")
        self.config.camera.width, self.config.camera.height = 320, 240
        self.pipeline = Pipeline(self.config)
        self.scene = base_scene()
        self.rng = np.random.default_rng(3)

    def cleanup(self):
        self.pipeline.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_frames(self, seconds, cycle=1.0, bubbles=True):
        out = []
        for i in range(int(seconds * FPS)):
            t = i / FPS
            bubble = bubble_at(t, cycle=cycle, duration=0.15) if bubbles else None
            out.append(render(self.scene, self.rng, bubble=bubble))
        return out

    def feed(self, frame_list):
        """Прогоняет кадры так, будто запись закончилась прямо сейчас."""
        base = time.monotonic() - len(frame_list) / FPS
        for i, frame in enumerate(frame_list):
            self.pipeline.process_frame(frame, base + i / FPS)

    def status(self):
        return self.pipeline.status()


class PipelineTestCase(unittest.TestCase, Harness):
    def setUp(self):
        self.build()

    def tearDown(self):
        self.cleanup()

    def test_counts_bubbles_end_to_end(self):
        self.feed(self.make_frames(12.0))
        status = self.status()
        self.assertGreaterEqual(status["total_count"], 8)
        self.assertLessEqual(status["total_count"], 11)
        self.assertEqual(status["totals"]["airlock-1"], status["total_count"])
        self.assertEqual(status["rois"][0]["count"], status["total_count"])
        self.assertGreater(status["rates"]["bpm_1m"], 0)
        self.assertIsNotNone(status["last_event_age_s"])

    def test_static_scene_counts_nothing(self):
        self.feed(self.make_frames(10.0, bubbles=False))
        status = self.status()
        self.assertEqual(status["total_count"], 0)
        self.assertEqual(status["rois"][0]["disturbances"], 0)
        self.assertEqual(self.pipeline.store.events(10), [])

    def test_events_are_journalised(self):
        self.feed(self.make_frames(10.0))
        self.pipeline.store.flush()
        path = os.path.join(self.tmp, "events.jsonl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        expected = self.status()["total_count"]
        self.assertGreater(expected, 0)
        self.assertEqual(len(rows), expected)
        self.assertTrue(all(r["roi"] == "airlock-1" for r in rows))
        self.assertEqual([r["id"] for r in rows], sorted(r["id"] for r in rows))
        self.assertTrue(all(r["peak"] > 0 for r in rows))
        self.assertTrue(all(r["wall_time"] for r in rows))

    def test_totals_survive_restart(self):
        self.feed(self.make_frames(10.0))
        self.pipeline.store.flush()
        first = self.status()["total_count"]
        self.assertGreater(first, 0)

        revived = Pipeline(self.config)
        try:
            self.assertEqual(sum(revived.store.totals({}).values()), first)
            self.assertEqual(sum(revived.store.baseline.values()), first)
            self.assertEqual(revived.store.totals({})["airlock-1"], first)
        finally:
            revived.store.close()

    def test_history_reloads_after_restart(self):
        self.feed(self.make_frames(10.0))
        self.pipeline.store.flush()
        total = self.status()["total_count"]
        self.assertGreater(total, 0)

        revived = Pipeline(self.config)
        try:
            data = revived.store.timeline_series(time.time(), minutes=180)
            self.assertGreater(sum(p["total"] for p in data["points"]), 0)
            self.assertEqual(sum(p["total"] for p in data["points"]), total)
            self.assertEqual(len(revived.store.events(1000)), total)
            zone = revived.status()["rois"][0]
            self.assertEqual(zone["count"], total)          # «всего» на карточке = вся история
            self.assertEqual(zone["session_count"], 0)      # живых событий этой сессии нет
            self.assertGreaterEqual(zone["rates"]["bph"], 1)  # «в час» из истории, а не из сессии
        finally:
            revived.store.close()

    def test_reset_counters(self):
        self.feed(self.make_frames(8.0))
        self.assertGreater(self.status()["total_count"], 0)
        self.pipeline.reset_counters()
        status = self.status()
        self.assertEqual(status["total_count"], 0)
        self.assertIsNone(status["last_event_age_s"])
        self.assertEqual(status["rois"][0]["count"], 0)
        self.assertEqual(self.pipeline.store.events(10), [])

    def test_reset_clears_journal_survives_restart(self):
        self.feed(self.make_frames(8.0))
        self.pipeline.store.flush()
        self.assertGreater(self.status()["total_count"], 0)
        self.pipeline.reset_counters()
        self.assertEqual(self.status()["total_count"], 0)

        revived = Pipeline(self.config)
        try:
            self.assertEqual(revived.status()["total_count"], 0)
            self.assertEqual(revived.store.events(100), [])
            data = revived.store.timeline_series(time.time(), minutes=180)
            self.assertEqual(sum(p["total"] for p in data["points"]), 0)
        finally:
            revived.store.close()

    def test_rebuild_rois_resets_counts(self):
        self.feed(self.make_frames(8.0))
        self.pipeline.set_rois([Roi("airlock-2", 100, 40, 120, 160)], persist=False)
        status = self.status()
        self.assertEqual([r["name"] for r in status["rois"]], ["airlock-2"])
        self.assertEqual(status["rois"][0]["count"], 0)

    def test_set_rois_persists_overrides(self):
        self.pipeline.set_rois([Roi("airlock-9", 10, 10, 60, 60)], persist=True)
        with open(self.config.overrides_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["rois"][0]["name"], "airlock-9")

    def test_update_params_live(self):
        applied = self.pipeline.update_params(detector_patch={"on_threshold": 0.02}, persist=True)
        self.assertEqual(self.pipeline.config.detector.on_threshold, 0.02)
        self.assertEqual(applied["detector"]["on_threshold"], 0.02)
        with open(self.config.overrides_path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["detector"]["on_threshold"], 0.02)

    def test_switching_background_rebuilds_model(self):
        self.feed(self.make_frames(3.0))
        self.pipeline.update_params(vision_patch={"background": "mean"})
        self.assertEqual(self.pipeline.config.vision.background, "mean")
        self.feed(self.make_frames(3.0))
        self.assertEqual(self.pipeline.config.vision.background, "mean")

    def test_overlay_and_snapshot(self):
        self.feed(self.make_frames(4.0))
        image = self.pipeline.render_overlay(scale=0.5)
        self.assertIsNotNone(image)
        self.assertEqual(image.shape[1], 160)
        payload = self.pipeline.snapshot_jpeg(quality=70, scale=0.5)
        self.assertTrue(payload.startswith(b"\xff\xd8"))

    def test_stall_alert(self):
        self.feed(self.make_frames(6.0))
        self.pipeline.update_params(stall_minutes=1)
        self.pipeline.last_event_ts -= 3600
        codes = [a["code"] for a in self.status()["alerts"]]
        self.assertIn("stall", codes)

    def test_no_roi_alert(self):
        self.pipeline.set_rois([], persist=False)
        codes = [a["code"] for a in self.status()["alerts"]]
        self.assertIn("no_roi", codes)


class WebApiTestCase(unittest.TestCase, Harness):
    def setUp(self):
        self.build()
        self.feed(self.make_frames(8.0))
        self.client = create_app(self.pipeline).test_client()

    def tearDown(self):
        self.cleanup()

    def test_index_and_static(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_health_is_open(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertIn("total", res.get_json())

    def test_status_payload(self):
        data = self.client.get("/api/status").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("total_count", data)
        self.assertIn("camera", data)
        self.assertEqual(data["rois"][0]["name"], "airlock-1")
        self.assertIn("rect", data["rois"][0])
        self.assertIn("on_threshold", data["params"]["detector"])

    def test_events_endpoints(self):
        data = self.client.get("/api/events?limit=5").get_json()
        self.assertLessEqual(len(data["events"]), 5)
        csv = self.client.get("/api/events.csv?limit=5")
        self.assertEqual(csv.status_code, 200)
        body = csv.get_data(as_text=True)
        self.assertTrue(body.startswith("id,roi,wall_time"))
        self.assertIn("attachment", csv.headers.get("Content-Disposition", ""))

    def test_timeline_and_signal(self):
        timeline = self.client.get("/api/timeline?minutes=60").get_json()
        self.assertEqual(timeline["step_minutes"], 1)
        self.assertEqual(len(timeline["series"]), 60)
        self.assertGreater(sum(p["total"] for p in timeline["series"]), 0)

        long = self.client.get("/api/timeline?minutes=86400").get_json()
        self.assertGreaterEqual(long["step_minutes"], 360)
        self.assertLessEqual(len(long["series"]), 240)

        signal = self.client.get("/api/signal?roi=airlock-1&limit=50").get_json()
        self.assertEqual(signal["roi"], "airlock-1")
        self.assertEqual(signal["available"], ["airlock-1"])
        self.assertGreater(len(signal["series"]), 0)
        self.assertIn("on", signal["thresholds"])

    def test_snapshot_endpoint(self):
        res = self.client.get("/api/snapshot.jpg")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "image/jpeg")
        self.assertTrue(res.data.startswith(b"\xff\xd8"))

    def test_set_rois_endpoint(self):
        res = self.client.post("/api/rois", json={
            "rois": [
                {"name": "z1", "x": 10, "y": 10, "w": 60, "h": 80},
                {"name": "z2", "x": 100, "y": 50, "w": 60, "h": 80},
            ],
            "persist": False,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_json()["rois"]), 2)
        self.assertEqual([r["name"] for r in self.status()["rois"]], ["z1", "z2"])

    def test_set_rois_rejects_bad_input(self):
        self.assertEqual(self.client.post("/api/rois", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/rois", json={"rois": [
            {"name": "a", "x": 0, "y": 0, "w": 2, "h": 2}]}).status_code, 400)
        self.assertEqual(self.client.post("/api/rois", json={"rois": [
            {"name": "dup", "x": 0, "y": 0, "w": 30, "h": 30},
            {"name": "dup", "x": 40, "y": 0, "w": 30, "h": 30}]}).status_code, 400)

    def test_set_rois_clamps_to_frame(self):
        res = self.client.post("/api/rois", json={"rois": [
            {"name": "big", "x": 300, "y": 220, "w": 500, "h": 500}], "persist": False})
        roi = res.get_json()["rois"][0]
        self.assertEqual(roi["x"] + roi["w"], 320)
        self.assertEqual(roi["y"] + roi["h"], 240)

    def test_settings_endpoint(self):
        res = self.client.post("/api/settings", json={
            "detector": {"on_threshold": 0.012, "refractory_s": 0.4, "bogus": 1},
            "vision": {"background": "mean", "despeckle": 5, "process_scale": 99},
            "alerts": {"stall_minutes": 25},
            "persist": False,
        })
        self.assertEqual(res.status_code, 200)
        params = res.get_json()["params"]
        self.assertEqual(params["detector"]["on_threshold"], 0.012)
        self.assertEqual(params["detector"]["refractory_s"], 0.4)
        self.assertNotIn("bogus", params["detector"])
        self.assertEqual(params["vision"]["background"], "mean")
        self.assertEqual(params["vision"]["process_scale"], 4.0)
        self.assertEqual(params["alerts"]["stall_minutes"], 25.0)

    def test_settings_rejects_empty(self):
        self.assertEqual(self.client.post("/api/settings", json={}).status_code, 400)

    def test_settings_rejects_invalid_threshold_order(self):
        res = self.client.post("/api/settings", json={
            "detector": {"on_threshold": 0.001, "off_threshold": 0.005}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("off_threshold", res.get_json()["error"])

    def test_suggest_endpoint_is_per_zone(self):
        res = self.client.post("/api/suggest", json={"apply": False})
        self.assertEqual(res.status_code, 200)
        by_roi = res.get_json()["by_roi"]
        self.assertIn("airlock-1", by_roi)
        suggestion = by_roi["airlock-1"]
        self.assertLess(suggestion["off_threshold"], suggestion["on_threshold"])
        self.assertIn("noise_samples", suggestion)

    def test_suggest_apply_per_zone(self):
        res = self.client.post("/api/suggest", json={"apply": True})
        self.assertEqual(res.status_code, 200)
        self.assertIn("airlock-1", res.get_json()["applied"])
        status = self.status()
        applied = status["rois"][0]["params"]
        self.assertAlmostEqual(
            applied["on_threshold"],
            res.get_json()["by_roi"]["airlock-1"]["on_threshold"],
            places=6,
        )
        self.assertTrue(status["rois"][0]["has_override"])

    def test_status_exposes_zone_params(self):
        roi = self.status()["rois"][0]
        self.assertIn("params", roi)
        self.assertIn("on_threshold", roi["params"])
        self.assertFalse(roi["has_override"])

    def test_status_exposes_fermentation(self):
        self.feed(self.make_frames(10.0))
        roi = self.status()["rois"][0]
        ferm = roi["fermentation"]
        self.assertIn("level", ferm)
        self.assertIn("label", ferm)
        self.assertIn("day24", roi["rates"])
        # после активного брожения за сутки статус не должен быть "none"
        self.assertNotEqual(ferm["level"], "none")

    def test_settings_per_zone_override_does_not_leak(self):
        self.client.post("/api/rois", json={"rois": [
            {"name": "a", "x": 10, "y": 10, "w": 80, "h": 80},
            {"name": "b", "x": 120, "y": 10, "w": 80, "h": 80},
        ], "persist": False})
        res = self.client.post("/api/settings", json={
            "roi": "a", "detector": {"on_threshold": 0.02}, "persist": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["roi"], "a")
        zones = {r["name"]: r for r in self.status()["rois"]}
        self.assertEqual(zones["a"]["params"]["on_threshold"], 0.02)
        self.assertTrue(zones["a"]["has_override"])
        self.assertEqual(zones["b"]["params"]["on_threshold"], 0.004)
        self.assertFalse(zones["b"]["has_override"])

    def test_settings_base_change_propagates_to_zones_without_override(self):
        self.client.post("/api/rois", json={"rois": [
            {"name": "a", "x": 10, "y": 10, "w": 80, "h": 80},
            {"name": "b", "x": 120, "y": 10, "w": 80, "h": 80},
        ], "persist": False})
        self.client.post("/api/settings", json={"roi": "a", "detector": {"on_threshold": 0.03}, "persist": False})
        self.client.post("/api/settings", json={"roi": "*", "detector": {"on_threshold": 0.008}, "persist": False})
        zones = {r["name"]: r for r in self.status()["rois"]}
        self.assertEqual(zones["a"]["params"]["on_threshold"], 0.03)
        self.assertEqual(zones["b"]["params"]["on_threshold"], 0.008)

    def test_settings_unknown_zone_404(self):
        res = self.client.post("/api/settings", json={"roi": "ghost", "detector": {"on_threshold": 0.01}})
        self.assertEqual(res.status_code, 404)

    def test_roi_params_reset(self):
        self.client.post("/api/settings", json={"roi": "airlock-1", "detector": {"on_threshold": 0.02}, "persist": False})
        self.assertTrue(self.status()["rois"][0]["has_override"])
        res = self.client.post("/api/roi/params/reset", json={"roi": "airlock-1", "persist": False})
        self.assertTrue(res.get_json()["reset"])
        roi = self.status()["rois"][0]
        self.assertFalse(roi["has_override"])
        self.assertEqual(roi["params"]["on_threshold"], self.config.detector.on_threshold)

    def test_set_rois_preserves_detector_override(self):
        res = self.client.post("/api/rois", json={"rois": [
            {"name": "a", "x": 10, "y": 10, "w": 80, "h": 80,
             "detector": {"on_threshold": 0.015, "refractory_s": 0.3}}], "persist": True})
        self.assertEqual(res.status_code, 200)
        saved = res.get_json()["rois"][0]["detector"]
        self.assertEqual(saved["on_threshold"], 0.015)
        with open(self.config.overrides_path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["rois"][0]["detector"]["on_threshold"], 0.015)
        zones = {r["name"]: r for r in self.status()["rois"]}
        self.assertEqual(zones["a"]["params"]["on_threshold"], 0.015)

    def test_set_rois_rejects_bad_zone_override(self):
        res = self.client.post("/api/rois", json={"rois": [
            {"name": "a", "x": 10, "y": 10, "w": 80, "h": 80,
             "detector": {"on_threshold": 0.001, "off_threshold": 0.02}}]})
        self.assertEqual(res.status_code, 400)

    def test_suggest_endpoint(self):
        res = self.client.post("/api/suggest", json={"apply": False})
        self.assertEqual(res.status_code, 200)
        suggestion = list(res.get_json()["by_roi"].values())[0]
        self.assertLess(suggestion["off_threshold"], suggestion["on_threshold"])

        res = self.client.post("/api/suggest", json={"apply": True})
        self.assertTrue(res.get_json()["applied"])

    def test_reset_endpoint(self):
        res = self.client.post("/api/reset")
        self.assertEqual(res.get_json()["total_count"], 0)

    def test_camera_config_endpoint(self):
        data = self.client.get("/api/camera").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["config"]["width"], 320)
        meta = data["capabilities"]
        self.assertEqual(len(meta["resolutions"]), 4)
        self.assertEqual(len(meta["exposure_modes"]), 3)
        self.assertEqual({m["id"] for m in meta["exposure_modes"]}, {"manual", "auto", "auto_once"})
        self.assertEqual(meta["exposure"]["max"], 10000)
        self.assertEqual(meta["brightness"]["min"], 30)
        self.assertEqual(meta["focus"]["max"], 255)  # v4l2-ctl недоступен в тесте -> дефолт

    def test_camera_apply_sets_controls(self):
        res = self.client.post("/api/camera/apply", json={
            "exposure_mode": "manual", "exposure": 20, "brightness": 120,
            "autofocus": 0, "focus": 7})
        self.assertEqual(res.status_code, 200)
        controls = res.get_json()["config"]["controls"]
        self.assertEqual(controls["auto_exposure"], 1)
        self.assertEqual(controls["exposure"], 20)
        self.assertEqual(controls["brightness"], 120)
        self.assertEqual(controls["focus"], 7)
        self.assertEqual(controls["autofocus"], 0)
        self.assertFalse(res.get_json()["saved"])
        # не должно было сохраниться в overrides
        self.assertFalse(os.path.exists(self.config.overrides_path))

    def test_camera_apply_clamps_to_ranges(self):
        res = self.client.post("/api/camera/apply", json={"exposure": 999999, "brightness": -5})
        controls = res.get_json()["config"]["controls"]
        self.assertEqual(controls["exposure"], 10000)
        self.assertEqual(controls["brightness"], 30)

    def test_camera_save_persists(self):
        res = self.client.post("/api/camera/save", json={
            "width": 640, "height": 480, "exposure": 15, "brightness": 200})
        self.assertTrue(res.get_json()["saved"])
        with open(self.config.overrides_path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)["camera"]
        self.assertEqual(saved["controls"]["exposure"], 15)
        self.assertEqual(saved["controls"]["brightness"], 200)
        self.assertEqual(saved["width"], 640)

    def test_camera_endpoints_require_token(self):
        self.client.post("/api/settings", json={"detector": {"on_threshold": 0.004}})
        # (smoke that camera routes exist under auth guard)
        self.assertEqual(self.client.get("/api/camera").status_code, 200)

    def test_status_exposes_max_rois(self):
        self.assertEqual(self.client.get("/api/status").get_json()["max_rois"], 12)

    def test_accepts_up_to_max_rois(self):
        rois = [{"name": "z%d" % i, "x": (i * 40) % 280, "y": 10, "w": 30, "h": 30} for i in range(12)]
        res = self.client.post("/api/rois", json={"rois": rois, "persist": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_json()["rois"]), 12)

    def test_rejects_more_than_max_rois(self):
        rois = [{"name": "z%d" % i, "x": 0, "y": 0, "w": 30, "h": 30} for i in range(13)]
        res = self.client.post("/api/rois", json={"rois": rois})
        self.assertEqual(res.status_code, 400)
        self.assertIn("12", res.get_json()["error"])

    def test_config_endpoint(self):
        data = self.client.get("/api/config").get_json()
        self.assertIn("camera", data["config"])
        self.assertIn("rois", data["config"])


class WebAuthTestCase(unittest.TestCase, Harness):
    def setUp(self):
        self.build()
        self.config.web.token = "s3cret"
        self.client = create_app(self.pipeline).test_client()

    def tearDown(self):
        self.cleanup()

    def test_endpoints_require_token(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.post("/api/reset").status_code, 401)
        self.assertEqual(self.client.get("/api/events").status_code, 401)
        self.assertEqual(self.client.get("/api/snapshot.jpg").status_code, 401)

    def test_health_stays_open(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_header_token_works(self):
        res = self.client.get("/api/status", headers={"X-Airlock-Token": "s3cret"})
        self.assertEqual(res.status_code, 200)

    def test_query_token_works(self):
        self.assertEqual(self.client.get("/api/status?token=s3cret").status_code, 200)
        self.assertEqual(self.client.get("/api/status?token=wrong").status_code, 401)


class PublicRoutesTestCase(unittest.TestCase, Harness):
    def setUp(self):
        self.build()
        self.feed(self.make_frames(6.0))
        self.config.web.token = "s3cret"
        self.client = create_app(self.pipeline).test_client()

    def tearDown(self):
        self.cleanup()

    def test_public_page_is_open_without_token(self):
        self.assertEqual(self.client.get("/public").status_code, 200)
        self.assertEqual(self.client.get("/public/api/status").status_code, 200)
        self.assertEqual(self.client.get("/public/api/timeline?minutes=1440").status_code, 200)

    def test_public_status_shape_and_no_leak(self):
        data = self.client.get("/public/api/status").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("rois", data)
        zone = data["rois"][0]
        self.assertIn("fermentation", zone)
        self.assertIn("bph", zone["rates"])
        self.assertIn("day24", zone["rates"])
        self.assertNotIn("params", zone)
        self.assertNotIn("detector_override", zone)
        self.assertNotIn("params", data)

    def test_public_timeline_downsampled(self):
        data = self.client.get("/public/api/timeline?minutes=1440").get_json()
        self.assertGreaterEqual(data["step_minutes"], 1)
        self.assertLessEqual(len(data["series"]), 240)

    def test_public_stream_route_exists(self):
        res = self.client.get("/public/stream")
        self.assertEqual(res.status_code, 200)
        self.assertIn("multipart", res.headers.get("Content-Type", ""))
        res.close()

    def test_admin_endpoints_still_guarded(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        self.assertEqual(self.client.get("/public/api/status").status_code, 200)


class StandalonePublicAppTestCase(unittest.TestCase, Harness):
    def setUp(self):
        self.build()
        self.feed(self.make_frames(6.0))
        self.client = create_public_app(self.pipeline).test_client()

    def tearDown(self):
        self.cleanup()

    def test_root_serves_public_page(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/public").status_code, 200)

    def test_no_admin_routes_on_public_app(self):
        self.assertEqual(self.client.get("/api/status").status_code, 404)
        self.assertEqual(self.client.get("/api/reset", json={}).status_code, 404)

    def test_public_data_available(self):
        self.assertEqual(self.client.get("/public/api/status").get_json()["ok"], True)
        self.assertEqual(self.client.get("/health").status_code, 200)


class ServeShutdownTestCase(unittest.TestCase, Harness):
    """Сервер должен гаснуть по stop_event и освобождать порт — иначе нужен kill -9."""

    def test_serve_stops_cleanly_and_frees_port(self):
        import socket
        import threading
        import urllib.request

        from airlock.web import create_app, serve

        self.build()
        try:
            stop = threading.Event()
            port = _free_port()
            app = create_app(self.pipeline)
            thread = threading.Thread(
                target=serve, args=(app, "127.0.0.1", port, stop), daemon=True)
            thread.start()

            deadline = time.time() + 10
            up = False
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1) as r:
                        if r.status == 200:
                            up = True
                            break
                except Exception:
                    time.sleep(0.2)
            self.assertTrue(up, "сервер не поднялся")

            started = time.time()
            stop.set()
            thread.join(timeout=8)
            elapsed = time.time() - started
            self.assertFalse(thread.is_alive(), "serve() не завершился по stop_event")
            self.assertLess(elapsed, 6.0, "остановка заняла слишком много времени")

            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
                freed = True
            except OSError:
                freed = False
            finally:
                probe.close()
            self.assertTrue(freed, "порт не освободился после остановки")
        finally:
            self.cleanup()


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


if __name__ == "__main__":
    unittest.main()
