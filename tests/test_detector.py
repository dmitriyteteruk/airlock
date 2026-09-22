from __future__ import annotations

import math
import unittest

from airlock.detector import (
    BubbleDetector,
    DetectorParams,
    MinuteTimeline,
    MotionStats,
    SignalBuffer,
    StallWatch,
    fermentation_status,
    suggest_thresholds,
)

DT = 0.05


def hump(center, amp, sigma=0.08):
    def f(t):
        return amp * math.exp(-((t - center) ** 2) / (2 * sigma * sigma))
    return f


def run(detector, signal, dt=DT, t0=1000.0):
    events = []
    for i, value in enumerate(signal):
        result = detector.update(value, t0 + i * dt)
        if result.event is not None:
            events.append(result.event)
    return events


def synth(count, spacing=1.0, amp=0.02, base=0.0005, duration=6.0, dt=DT, sigma=0.08, offset=0.3):
    frames = int(duration / dt)
    bumps = [hump(offset + k * spacing, amp, sigma) for k in range(count)]
    return [base + sum(b(i * dt) for b in bumps) for i in range(frames)]


class TestBubbleDetector(unittest.TestCase):
    def test_pure_noise_gives_no_events(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(0)
        signal = [0.0003 + (i % 3) * 0.0001 for i in range(600)]
        self.assertEqual(run(detector, signal), [])
        self.assertEqual(detector.count, 0)

    def test_counts_each_hump(self):
        for expected in (1, 3, 7):
            with self.subTest(expected=expected):
                detector = BubbleDetector("a", DetectorParams())
                detector.set_warmup(0)
                events = run(detector, synth(expected, spacing=1.0, duration=expected * 1.0 + 2.0))
                self.assertEqual(len(events), expected)
                self.assertEqual(detector.count, expected)

    def test_dense_bubbling(self):
        detector = BubbleDetector("a", DetectorParams(refractory_s=0.08))
        detector.set_warmup(0)
        events = run(detector, synth(20, spacing=0.25, sigma=0.03, duration=6.0))
        self.assertEqual(len(events), 20)

    def test_refractory_blocks_double_count(self):
        detector = BubbleDetector("a", DetectorParams(refractory_s=0.4))
        detector.set_warmup(0)
        double = [0.0005 + hump(0.4, 0.02)(i * DT) + hump(0.5, 0.018)(i * DT) for i in range(200)]
        events = run(detector, double)
        self.assertEqual(len(events), 1)
        self.assertEqual(detector.count, 1)

    def test_saturation_is_not_a_bubble(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(0)
        signal = [0.0005] * 20 + [0.92] * 8 + [0.0005] * 60
        events = run(detector, signal)
        self.assertEqual(events, [])
        self.assertEqual(detector.count, 0)
        self.assertGreaterEqual(detector.disturbances, 1)

    def test_warmup_swallows_events(self):
        signal = synth(1, duration=2.0) + synth(2, duration=4.0)

        warmed = BubbleDetector("a", DetectorParams())
        warmed.set_warmup(20)
        self.assertEqual(len(run(warmed, signal)), 2)

        cold = BubbleDetector("b", DetectorParams())
        cold.set_warmup(0)
        self.assertEqual(len(run(cold, signal)), 3)

    def test_saturation_during_warmup_is_silent(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(10)
        events = run(detector, [0.5] * 10 + synth(3, duration=5.0))
        self.assertEqual(len(events), 3)
        self.assertEqual(detector.disturbances, 0)

    def test_long_hump_force_closed(self):
        detector = BubbleDetector("a", DetectorParams(max_hump_s=1.0))
        detector.set_warmup(0)
        signal = [0.0005] * 5 + [0.012] * 100 + [0.0005] * 20
        events = run(detector, signal)
        self.assertTrue(events)
        self.assertEqual(events[0].kind, "long")

    def test_min_peak_frames_filters_spikes(self):
        params = DetectorParams(min_peak_frames=3)
        detector = BubbleDetector("a", params)
        detector.set_warmup(0)
        signal = [0.0005] * 10
        for _ in range(6):
            signal += [0.03, 0.0005] + [0.0005] * 18
        self.assertEqual(run(detector, signal), [])

        detector2 = BubbleDetector("b", DetectorParams(min_peak_frames=1))
        detector2.set_warmup(0)
        self.assertEqual(len(run(detector2, list(signal))), 6)

    def test_event_fields(self):
        detector = BubbleDetector("airlock-1", DetectorParams())
        detector.set_warmup(0)
        events = run(detector, synth(1, duration=3.0))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.roi, "airlock-1")
        self.assertGreater(event.peak, 0.01)
        self.assertGreater(event.duration_s, 0.0)
        self.assertLessEqual(event.duration_s, 1.0)
        data = event.to_dict()
        self.assertEqual(data["roi"], "airlock-1")
        self.assertEqual(data["kind"], "bubble")

    def test_rate_window(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(0)
        run(detector, synth(10, spacing=1.0, duration=11.0))
        now = 1000.0 + 11.0
        self.assertAlmostEqual(detector.rate(now, 60.0), 10.0 * 60.0 / 60.0, delta=1.0)
        rates = detector.bpm(now)
        self.assertIn("bpm_1m", rates)
        self.assertIn("bph", rates)
        self.assertGreater(rates["bpm_5m"], 0)

    def test_reset_counters(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(0)
        run(detector, synth(4, duration=6.0))
        self.assertEqual(detector.count, 4)
        detector.reset_counters()
        self.assertEqual(detector.count, 0)
        self.assertIsNone(detector.last_event)
        self.assertEqual(len(detector.timestamps), 0)


class TestStatsAndSuggest(unittest.TestCase):
    def test_suggest_between_noise_and_peaks(self):
        detector = BubbleDetector("a", DetectorParams())
        detector.set_warmup(0)
        run(detector, synth(8, spacing=1.0, duration=10.0))
        stats = detector.stats
        self.assertGreater(len(stats.noise), 0)
        self.assertEqual(len(stats.peaks), 8)
        suggestion = suggest_thresholds([stats])
        self.assertGreater(suggestion["on_threshold"], suggestion["noise_p99"])
        self.assertLess(suggestion["on_threshold"], suggestion["peak_median"])
        self.assertLess(suggestion["off_threshold"], suggestion["on_threshold"])

    def test_suggest_without_peaks_falls_back_to_noise(self):
        stats = MotionStats()
        for _ in range(50):
            stats.add_noise(0.001)
        suggestion = suggest_thresholds([stats])
        self.assertGreater(suggestion["on_threshold"], 0.001)
        self.assertLess(suggestion["off_threshold"], suggestion["on_threshold"])

    def test_suggest_empty(self):
        suggestion = suggest_thresholds([MotionStats()])
        self.assertGreater(suggestion["on_threshold"], 0)
        self.assertEqual(suggestion["peak_samples"], 0)

    def test_suggest_merges_conservatively(self):
        quiet = MotionStats()
        for _ in range(30):
            quiet.add_noise(0.0005)
        quiet.add_peak(0.01)
        noisy = MotionStats()
        for _ in range(30):
            noisy.add_noise(0.005)
        noisy.add_peak(0.05)
        merged = suggest_thresholds([quiet, noisy])
        self.assertEqual(merged["noise_p99"], 0.005)
        self.assertEqual(merged["peak_p05"], 0.01)
        self.assertGreater(merged["on_threshold"], 0.005)

    def test_stats_dict_shape(self):
        stats = MotionStats()
        stats.add_noise(0.001)
        stats.add_peak(0.02)
        data = stats.as_dict()
        for key in ("noise_p50", "noise_p99", "peak_median", "peak_p95", "peak_max", "samples"):
            self.assertIn(key, data)


class TestTimelineAndBuffers(unittest.TestCase):
    def test_minute_timeline(self):
        timeline = MinuteTimeline(keep_minutes=10)
        base = 1_700_000_000
        for i in range(5):
            timeline.add(base + i, "a")
        timeline.add(base + 90, "b")
        timeline.add(base + 90, "b")
        data = timeline.series(base + 120, minutes=5)
        self.assertEqual(data["step_minutes"], 1)
        self.assertEqual(sum(point["total"] for point in data["points"]), 7)
        self.assertEqual(timeline.totals(), {"a": 5, "b": 2})

    def test_timeline_downsamples_long_window(self):
        timeline = MinuteTimeline(keep_minutes=90000, max_points=240)
        base = 1_700_000_000
        for day in range(60):
            timeline.add(base + day * 86400, "a")
        data = timeline.series(base + 59 * 86400, minutes=86400)
        self.assertGreaterEqual(data["step_minutes"], 360)
        self.assertLessEqual(len(data["points"]), 240)
        self.assertEqual(sum(p["total"] for p in data["points"]), 60)

    def test_timeline_short_window_keeps_minute_step(self):
        timeline = MinuteTimeline(keep_minutes=86400)
        base = 1_700_000_000
        for i in range(30):
            timeline.add(base + i, "a")
        data = timeline.series(base + 30, minutes=60)
        self.assertEqual(data["step_minutes"], 1)
        self.assertEqual(len(data["points"]), 60)

    def test_timeline_prunes_old(self):
        timeline = MinuteTimeline(keep_minutes=2)
        base = 1_700_000_000
        timeline.add(base, "a")
        timeline.add(base + 3600, "a")
        self.assertEqual(timeline.totals(), {"a": 1})

    def test_window_totals(self):
        timeline = MinuteTimeline(keep_minutes=1440)
        now = 1_700_000_000
        timeline.add(now - 100, "a")
        timeline.add(now - 50, "a")
        timeline.add(now - 4000, "a")   # дальше часа
        timeline.add(now - 10, "b")
        totals = timeline.window_totals(now, 3600)
        self.assertEqual(totals.get("a"), 2)
        self.assertEqual(totals.get("b"), 1)

    def test_signal_buffer(self):
        buf = SignalBuffer(maxlen=5)
        for i in range(10):
            buf.add(float(i), i * 0.001)
        series = buf.series()
        self.assertEqual(len(series), 5)
        self.assertEqual(series[0][0], 5.0)
        buf.clear()
        self.assertEqual(buf.series(), [])
        buf.add(1.0, 0.5)
        buf.resize(3)
        self.assertEqual(len(buf.series()), 1)

    def test_stall_watch(self):
        watch = StallWatch(stall_minutes=10)
        now = 1_700_000_000.0
        self.assertEqual(watch.check(now, now - 60, 5), [])
        alerts = watch.check(now, now - 900, 5)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["code"], "stall")
        self.assertEqual(StallWatch(0).check(now, now - 99999, 5), [])
        self.assertEqual(watch.check(now, None, 0), [])


class TestFermentationStatus(unittest.TestCase):
    def test_absent(self):
        self.assertEqual(fermentation_status(0, 0)["level"], "none")
        self.assertEqual(fermentation_status(0, 0)["label"], "Отсутствует")

    def test_very_slow_by_day(self):
        for day in (1, 2, 12, 24):
            self.assertEqual(fermentation_status(day, 5)["level"], "veryslow")

    def test_hourly_levels(self):
        self.assertEqual(fermentation_status(100, 1)["level"], "slow")
        self.assertEqual(fermentation_status(100, 30)["level"], "slow")
        self.assertEqual(fermentation_status(100, 31)["level"], "medium")
        self.assertEqual(fermentation_status(100, 60)["level"], "medium")
        self.assertEqual(fermentation_status(100, 61)["level"], "fast")
        self.assertEqual(fermentation_status(100, 360)["level"], "fast")
        self.assertEqual(fermentation_status(100, 361)["level"], "veryfast")

    def test_labels_present(self):
        for day, hour in ((0, 0), (10, 0), (100, 20), (100, 45), (100, 200), (100, 500)):
            status = fermentation_status(day, hour)
            self.assertIn("level", status)
            self.assertTrue(status["label"])

    def test_negative_clamped(self):
        self.assertEqual(fermentation_status(-5, -3)["level"], "none")


class TestParams(unittest.TestCase):
    def test_updated_ignores_unknown_keys(self):
        params = DetectorParams()
        patched = params.updated({"on_threshold": 0.01, "nonsense": 5})
        self.assertEqual(patched.on_threshold, 0.01)
        self.assertEqual(patched.refractory_s, params.refractory_s)

    def test_roundtrip(self):
        params = DetectorParams(on_threshold=0.007)
        self.assertEqual(DetectorParams(**params.as_dict()).on_threshold, 0.007)

    def test_warmup_left(self):
        detector = BubbleDetector("a")
        detector.set_warmup(3)
        self.assertEqual(detector.warmup_left, 3)
        detector.update(0.0, 1.0)
        self.assertEqual(detector.warmup_left, 2)


if __name__ == "__main__":
    unittest.main()
