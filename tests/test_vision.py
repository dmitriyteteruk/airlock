from __future__ import annotations

import unittest

import cv2
import numpy as np

from airlock.detector import BubbleDetector, DetectorParams
from airlock.vision import (
    MeanBackground,
    Mog2Background,
    Roi,
    RoiProcessor,
    VisionParams,
    draw_overlay,
    encode_jpeg,
    make_background,
    to_gray,
)

WIDTH, HEIGHT = 320, 240
ROI = Roi("airlock-1", 110, 50, 100, 140)


def base_scene(seed=7):
    rng = np.random.default_rng(seed)
    scene = np.full((HEIGHT, WIDTH, 3), 46, dtype=np.uint8)
    for _ in range(40):
        x = int(rng.integers(0, WIDTH))
        y = int(rng.integers(0, HEIGHT))
        color = tuple(int(c) for c in rng.integers(20, 110, size=3))
        cv2.circle(scene, (x, y), int(rng.integers(4, 22)), color, -1)
    cv2.rectangle(scene, (ROI.x, ROI.y), (ROI.x + ROI.w, ROI.y + ROI.h), (150, 160, 170), 2)
    return scene


def render(scene, rng, bubble=None, noise=1.5, bright=0):
    frame = scene.astype(np.int16)
    if bright:
        frame = frame + bright
    if bubble is not None:
        overlay = frame.copy()
        cv2.circle(overlay, bubble, 7, (225, 230, 235), -1)
        frame = overlay
    frame = frame + rng.normal(0.0, noise, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def bubble_at(t, cycle=1.0, duration=0.15):
    """Пузырёк виден `duration` секунд из каждого `cycle`, поднимается снизу вверх."""
    phase = t % cycle
    if phase >= duration:
        return None
    progress = phase / duration
    cx = ROI.x + ROI.w // 2
    cy = int(ROI.y + ROI.h - 20 - progress * (ROI.h - 45))
    return cx, cy


class TestVisionPrimitives(unittest.TestCase):
    def test_to_gray_shapes(self):
        bgr = np.zeros((10, 12, 3), dtype=np.uint8)
        self.assertEqual(to_gray(bgr).shape, (10, 12))
        bgra = np.zeros((10, 12, 4), dtype=np.uint8)
        self.assertEqual(to_gray(bgra).shape, (10, 12))
        gray = np.zeros((10, 12), dtype=np.uint8)
        self.assertIs(to_gray(gray), gray)

    def test_encode_jpeg(self):
        payload = encode_jpeg(np.zeros((30, 40, 3), dtype=np.uint8), 70)
        self.assertTrue(payload.startswith(b"\xff\xd8"))

    def test_draw_overlay_returns_bgr(self):
        frame = np.zeros((60, 80, 3), dtype=np.uint8)
        out = draw_overlay(frame, [Roi("z", 5, 5, 30, 20)], {"z": {"state": "idle", "count": 3, "motion": 0.01}})
        self.assertEqual(out.shape, frame.shape)
        self.assertFalse(np.array_equal(out, frame))

    def test_draw_overlay_on_gray(self):
        frame = np.zeros((60, 80), dtype=np.uint8)
        out = draw_overlay(frame, [Roi("z", 5, 5, 30, 20)], {})
        self.assertEqual(out.shape[2], 3)

    def test_roi_outside_frame_is_clipped(self):
        processor = RoiProcessor(Roi("far", 1000, 1000, 50, 50), VisionParams(warmup_frames=0))
        gray = to_gray(base_scene())
        self.assertEqual(processor.motion(gray), 0.0)

    def test_background_factory(self):
        self.assertIsInstance(make_background(VisionParams(background="mog2")), Mog2Background)
        self.assertIsInstance(make_background(VisionParams(background="mean")), MeanBackground)


class TestMotionSignal(unittest.TestCase):
    def setUp(self):
        self.scene = base_scene()
        self.rng = np.random.default_rng(11)
        self.params = VisionParams(warmup_frames=0)

    def feed(self, processor, frames):
        values = []
        for frame in frames:
            values.append(processor.motion(to_gray(frame)))
        return values

    def test_static_scene_settles_to_noise_floor(self):
        processor = RoiProcessor(ROI, self.params)
        values = self.feed(processor, [render(self.scene, self.rng) for _ in range(120)])
        tail = values[-60:]
        self.assertLess(max(tail), 0.004)
        self.assertLess(sum(tail) / len(tail), 0.002)

    def test_bubble_raises_signal_above_threshold(self):
        processor = RoiProcessor(ROI, self.params)
        self.feed(processor, [render(self.scene, self.rng) for _ in range(120)])
        peak = 0.0
        for i in range(30):
            t = i / 20.0
            frame = render(self.scene, self.rng, bubble=bubble_at(t, cycle=1.0, duration=0.15))
            peak = max(peak, processor.motion(to_gray(frame)))
        self.assertGreater(peak, 0.004)

    def test_process_scale_preserves_relative_response(self):
        full = RoiProcessor(ROI, VisionParams(warmup_frames=0))
        half = RoiProcessor(ROI, VisionParams(warmup_frames=0, process_scale=0.5))
        frames = [render(self.scene, self.rng) for _ in range(80)]
        frames += [render(self.scene, self.rng, bubble=bubble_at(i / 20.0)) for i in range(20)]
        a = self.feed(full, frames)
        b = self.feed(half, frames)
        self.assertGreater(max(a), 0.004)
        self.assertGreater(max(b), 0.004)

    def test_global_light_change_saturates(self):
        processor = RoiProcessor(ROI, self.params)
        self.feed(processor, [render(self.scene, self.rng) for _ in range(100)])
        values = self.feed(processor, [render(self.scene, self.rng, bright=90) for _ in range(5)])
        self.assertGreater(max(values), 0.5)

    def test_mean_background_reacts_to_bubble(self):
        processor = RoiProcessor(ROI, VisionParams(background="mean", warmup_frames=0, mean_threshold=12))
        self.feed(processor, [render(self.scene, self.rng) for _ in range(150)])
        values = self.feed(processor, [
            render(self.scene, self.rng, bubble=bubble_at(i / 20.0, duration=0.2)) for i in range(20)
        ])
        self.assertGreater(max(values), 0.002)


class TestEndToEndCounting(unittest.TestCase):
    """Прогон синтетического видео через весь тракт: кадры → ROI → motion → счётчик."""

    def _run_video(self, params, vision, seconds=16.0, fps=20.0, cycle=1.0, scene=None, bubbles=True):
        scene = scene if scene is not None else base_scene()
        rng = np.random.default_rng(3)
        processor = RoiProcessor(ROI, vision)
        detector = BubbleDetector(ROI.name, params)
        detector.set_warmup(vision.warmup_frames)
        peaks = []
        total = int(seconds * fps)
        for i in range(total):
            t = i / fps
            bubble = bubble_at(t, cycle=cycle, duration=0.15) if bubbles else None
            frame = render(scene, rng, bubble=bubble)
            motion = processor.motion(to_gray(frame))
            peaks.append(motion)
            detector.update(motion, 1_700_000_000.0 + t)
        return detector, peaks

    def test_counts_one_bubble_per_second(self):
        detector, peaks = self._run_video(
            DetectorParams(on_threshold=0.004, off_threshold=0.001, min_off_frames=2, refractory_s=0.2),
            VisionParams(warmup_frames=60),
            seconds=18.0,
            cycle=1.0,
        )
        self.assertGreaterEqual(detector.count, 13)
        self.assertLessEqual(detector.count, 15)
        self.assertGreater(max(peaks), 0.004)

    def test_counts_two_bubbles_per_second(self):
        detector, _ = self._run_video(
            DetectorParams(on_threshold=0.004, off_threshold=0.001, refractory_s=0.12),
            VisionParams(warmup_frames=60),
            seconds=16.0,
            cycle=0.5,
        )
        self.assertGreaterEqual(detector.count, 24)
        self.assertLessEqual(detector.count, 26)

    def test_static_scene_counts_nothing(self):
        detector, peaks = self._run_video(
            DetectorParams(),
            VisionParams(warmup_frames=60),
            seconds=12.0,
            bubbles=False,
        )
        self.assertEqual(detector.count, 0)
        self.assertEqual(detector.disturbances, 0)
        self.assertEqual(len(peaks), 240)
        self.assertLess(max(peaks), 0.004)

    def test_suggested_thresholds_are_usable(self):
        detector, _ = self._run_video(
            DetectorParams(on_threshold=0.004, off_threshold=0.001),
            VisionParams(warmup_frames=60),
            seconds=18.0,
            cycle=1.0,
        )
        suggestion = detector.stats.suggest_thresholds()
        self.assertGreater(suggestion["peak_median"], suggestion["noise_p99"])
        self.assertGreater(suggestion["on_threshold"], suggestion["noise_p99"])
        self.assertLess(suggestion["on_threshold"], suggestion["peak_median"])


if __name__ == "__main__":
    unittest.main()
