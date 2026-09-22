from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import cv2

from . import __version__
from .camera import CONTROLS, Camera
from .config import AppConfig, save_overrides
from .detector import BubbleDetector, DetectorParams, StallWatch
from .detector import fermentation_status
from .detector import suggest_thresholds as suggest_thresholds_from_stats
from .store import Store
from .vision import Roi, RoiProcessor, draw_overlay, encode_jpeg, to_gray

log = logging.getLogger("airlock.pipeline")

RESOLUTIONS = ((640, 480), (800, 600), (1280, 720), (1920, 1080))

_DETECTOR_KEYS = set(DetectorParams().as_dict())


def _only_detector_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if k in _DETECTOR_KEYS}


class Pipeline(threading.Thread):
    """Камера → ROI → детектор пиков → журнал. Всё в одном потоке обработки."""

    daemon = True

    def __init__(self, config: AppConfig) -> None:
        super().__init__(name="pipeline")
        self.config = config
        self.camera = Camera(config.camera)
        self.store = Store(config.storage)
        self.stall_watch = StallWatch(config.alerts.stall_minutes)

        self._lock = threading.RLock()
        self._processors: Dict[str, RoiProcessor] = {}
        self._detectors: Dict[str, BubbleDetector] = {}
        self._rois: List[Roi] = []
        self._last_frame = None
        self._last_frame_ts = 0.0
        self._last_index = -1
        self._last_process_wall = 0.0
        self._last_tick = 0.0
        self._stop = threading.Event()

        self.started_at = time.time()
        self._started_mono = time.monotonic()
        self.process_fps = 0.0
        self.frames_processed = 0
        self.last_event_ts: Optional[float] = None
        self._fps_window: Deque[float] = deque(maxlen=256)

        cv2.setNumThreads(1)
        self.rebuild_rois(list(config.rois), reset_counts=False)
        self.store.set_live_provider(self._live_counts)

    def _live_counts(self) -> Dict[str, int]:
        with self._lock:
            return {name: int(detector.count) for name, detector in self._detectors.items()}

    def _params_for(self, roi: Roi) -> DetectorParams:
        return self.config.detector.updated(roi.detector)

    def effective_params(self, roi: Optional[str] = None) -> DetectorParams:
        """Действующие параметры зоны с учётом её override (или базовые для roi=None/'*')."""
        with self._lock:
            base = self.config.detector
            target = self._roi_override(roi) if roi and roi != "*" else None
        if target is None:
            return base
        return base.updated(target.detector)

    def rebuild_rois(self, rois: List[Roi], reset_counts: bool = True) -> None:
        processors: Dict[str, RoiProcessor] = {}
        detectors: Dict[str, BubbleDetector] = {}
        with self._lock:
            old_detectors = dict(self._detectors)
            old_rois = {roi.name: roi.rect() for roi in self._rois}
            for roi in rois:
                processors[roi.name] = RoiProcessor(roi, self.config.vision)
                previous = old_detectors.get(roi.name)
                detector = BubbleDetector(roi.name, self._params_for(roi))
                detector.set_warmup(self.config.vision.warmup_frames)
                if previous is not None and not reset_counts and old_rois.get(roi.name) == roi.rect():
                    detector.count = previous.count
                    detector.timestamps = previous.timestamps
                    detector.stats = previous.stats
                    detector.last_event = previous.last_event
                    detector.disturbances = previous.disturbances
                    detector.set_warmup(0)
                    detector.state = previous.state
                detectors[roi.name] = detector
                if roi.name not in self.store.signals:
                    self.store.buffer_for(roi.name)
            self._rois = list(rois)
            self._processors = processors
            self._detectors = detectors
            self.config.rois = list(rois)
        log.info("Зоны контроля: %s", [roi.name for roi in rois] or "не заданы")

    def _roi_override(self, name: str) -> Optional[Roi]:
        for roi in self._rois:
            if roi.name == name:
                return roi
        return None

    def update_params(
        self,
        detector_patch: Optional[Dict[str, Any]] = None,
        vision_patch: Optional[Dict[str, Any]] = None,
        stall_minutes: Optional[float] = None,
        roi: Optional[str] = None,
        persist: bool = False,
    ) -> Dict[str, Any]:
        """Меняет параметры на лету. roi=None/'*' — базовые для всех зон,
        иначе — override только для указанной зоны."""
        changed_rois = False
        with self._lock:
            if detector_patch:
                if roi in (None, "", "*"):
                    self.config.detector = self.config.detector.updated(detector_patch)
                    self._recompute_all_detector_params()
                else:
                    target = self._roi_override(roi)
                    if target is None:
                        raise KeyError(roi)
                    merged = dict(target.detector)
                    merged.update(detector_patch)
                    target.detector = _only_detector_keys(merged)
                    detector = self._detectors.get(roi)
                    if detector is not None:
                        detector.params = self._params_for(target)
                    changed_rois = True
            if vision_patch:
                new_vision, rebuild_bg = self.config.vision.updated(vision_patch)
                self.config.vision = new_vision
                for processor in self._processors.values():
                    if rebuild_bg:
                        processor.rebuild_background(new_vision)
                    else:
                        processor.params = new_vision
            if stall_minutes is not None:
                self.config.alerts.stall_minutes = float(stall_minutes)
                self.stall_watch = StallWatch(self.config.alerts.stall_minutes)

        patch: Dict[str, Any] = {}
        if detector_patch and roi in (None, "", "*"):
            patch["detector"] = self.config.detector.as_dict()
        if changed_rois:
            patch["rois"] = [r.as_dict() for r in self._rois]
        if vision_patch:
            patch["vision"] = self.config.vision.as_dict()
        if stall_minutes is not None:
            patch["alerts"] = {"stall_minutes": float(stall_minutes)}
        if persist and patch and self.config.overrides_path:
            save_overrides(self.config.overrides_path, patch)
        return patch

    def _recompute_all_detector_params(self) -> None:
        for roi in self._rois:
            detector = self._detectors.get(roi.name)
            if detector is not None:
                detector.params = self._params_for(roi)

    def reset_roi_params(self, name: str, persist: bool = False) -> bool:
        """Снять override зоны — вернуть её к базовым параметрам."""
        with self._lock:
            target = self._roi_override(name)
            if target is None or not target.detector:
                return False
            target.detector = {}
            detector = self._detectors.get(name)
            if detector is not None:
                detector.params = self.config.detector
        if persist and self.config.overrides_path:
            save_overrides(self.config.overrides_path, {"rois": [r.as_dict() for r in self._rois]})
        return True

    def set_rois(self, rois: List[Roi], persist: bool = True) -> None:
        self.rebuild_rois(rois, reset_counts=True)
        if persist and self.config.overrides_path:
            save_overrides(self.config.overrides_path, {"rois": [roi.as_dict() for roi in rois]})

    def reset_counters(self) -> None:
        with self._lock:
            for detector in self._detectors.values():
                detector.reset_counters()
            self.last_event_ts = None
        self.store.reset_totals()

    def camera_config(self) -> Dict[str, Any]:
        cam = self.config.camera
        return {
            "width": int(cam.width), "height": int(cam.height), "fps": float(cam.fps),
            "fourcc": cam.fourcc, "controls": dict(cam.controls),
        }

    def _exposure_modes(self, caps: Dict[str, Any]) -> List[Dict[str, Any]]:
        menu = (caps.get("menus") or {}).get("auto_exposure") or {}
        def label(value, fallback):
            return menu.get(str(value)) or menu.get(value) or fallback
        return [
            {"value": 1, "id": "manual", "label": label(1, "Ручная")},
            {"value": 3, "id": "auto", "label": label(3, "Автоматическая (постоянная)")},
            {"value": 2, "id": "auto_once", "label": label(2, "Автоматическая (однократная)")},
        ]

    def camera_capabilities(self, refresh: bool = False) -> Dict[str, Any]:
        caps = self.camera.probe() if (refresh or not self.camera.caps) else self.camera.capabilities()
        ctrl = caps.get("controls", {})

        def rng(names, dmin, dmax, ddef):
            for n in names:
                if n in ctrl:
                    c = ctrl[n]
                    default = c.get("default")
                    if default is None or default < 0 or default > dmax:
                        default = ddef
                    return {"min": int(c.get("min", dmin)), "max": int(c.get("max", dmax)),
                            "step": int(c.get("step", 1) or 1), "default": int(default), "detected": True}
            return {"min": dmin, "max": dmax, "step": 1, "default": ddef, "detected": False}

        meta = {
            "resolutions": [{"w": w, "h": h} for (w, h) in RESOLUTIONS],
            "exposure": rng(["exposure_time_absolute", "exposure"], 0, 10000, 10),
            "brightness": rng(["brightness"], 30, 255, 150),
            "focus": rng(["focus_absolute", "focus"], 0, 255, 0),
            "autofocus": {"detected": ("focus_automatic_continuous" in ctrl or "autofocus" in ctrl)},
            "exposure_modes": self._exposure_modes(caps),
            "has_v4l2": bool(caps.get("available")),
        }
        return {"config": self.camera_config(), "capabilities": meta, "raw": caps}

    def apply_camera_settings(self, *, width: Optional[int] = None, height: Optional[int] = None,
                              fps: Optional[float] = None, controls: Optional[Dict[str, Any]] = None,
                              save: bool = False) -> Dict[str, Any]:
        controls = {k: v for k, v in (controls or {}).items() if k in CONTROLS}
        cam = self.config.camera
        reopen = ((width and int(width) != int(cam.width)) or
                  (height and int(height) != int(cam.height)) or
                  (fps and abs(float(fps) - float(cam.fps)) > 0.01))
        if controls:
            self.camera.set_controls_live(controls)
        if reopen:
            self.camera.reconfigure(width=width, height=height, fps=fps)
            self.camera.wait_reconfigured(timeout=10.0)
        if save and self.config.overrides_path:
            cfg = self.camera_config()
            save_overrides(self.config.overrides_path, {"camera": {
                "width": cfg["width"], "height": cfg["height"], "fps": cfg["fps"],
                "fourcc": cfg["fourcc"], "controls": cfg["controls"],
            }})
        return self.camera_config()

    def start(self) -> None:
        self.camera.ensure_started()
        super().start()

    def stop(self) -> None:
        self._stop.set()
        self.camera.stop()

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop()
        try:
            self.join(timeout=timeout)
        except RuntimeError:
            pass
        try:
            self.camera.join(timeout=timeout)
        except RuntimeError:
            pass
        self.store.close()

    def latest_frame(self) -> Optional[Tuple[Any, float]]:
        with self._lock:
            if self._last_frame is None:
                return None
            return self._last_frame, self._last_frame_ts

    def snapshot_jpeg(self, quality: int = 85, scale: float = 1.0) -> bytes:
        item = self.latest_frame()
        if item is None:
            return b""
        frame = item[0]
        if 0 < scale < 0.999:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        return encode_jpeg(frame, quality)
    def overlay_states(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            live = {name: int(d.count) for name, d in self._detectors.items()}
            states = {
                name: {"state": d.state, "motion": d.motion, "count": 0}
                for name, d in self._detectors.items()
            }
        totals = self.store.totals(live)
        for name, st in states.items():
            st["count"] = int(totals.get(name, 0))
        return states

    def suggest_per_roi(self) -> Dict[str, Dict[str, float]]:
        """Автоподбор порогов отдельно для каждой зоны (без усреднения по всем)."""
        with self._lock:
            detectors = dict(self._detectors)
        return {name: suggest_thresholds_from_stats([det.stats]) for name, det in detectors.items()}

    def apply_suggestions(self, roi: Optional[str] = None) -> List[str]:
        """Применить подобранные пороги к зоне (или ко всем зонам). Возвращает список зон."""
        suggestions = self.suggest_per_roi()
        applied: List[str] = []
        for name, suggestion in suggestions.items():
            if roi not in (None, "", "*") and name != roi:
                continue
            if not suggestion.get("noise_samples") and not suggestion.get("peak_samples"):
                continue
            self.update_params(
                detector_patch={
                    "on_threshold": suggestion["on_threshold"],
                    "off_threshold": suggestion["off_threshold"],
                },
                roi=name,
                persist=True,
            )
            applied.append(name)
        return applied

    def suggest_thresholds(self) -> Dict[str, float]:
        """Совместимость: сводный (консервативный) подбор по всем зонам сразу."""
        with self._lock:
            detectors = list(self._detectors.values())
        return suggest_thresholds_from_stats([d.stats for d in detectors])

    def render_overlay(self, scale: float = 1.0, show_labels: bool = True):
        item = self.latest_frame()
        if item is None:
            return None
        frame = item[0]
        with self._lock:
            rois = list(self._rois)
        if 0 < scale < 0.999:
            frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            rois = [r.scaled(scale) for r in rois]
        return draw_overlay(frame, rois, self.overlay_states(), show_labels=show_labels)

    def process_frame(self, frame, ts: float) -> List[Any]:
        """Один кадр через весь тракт. Возвращает список зарегистрированных событий.

        ts — метка в шкале time.monotonic(): она не зависит от перевода системных часов.
        """
        wall_ts = time.time()
        with self._lock:
            self._last_frame = frame
            self._last_frame_ts = wall_ts
            rois = list(self._rois)
            processors = self._processors
            detectors = self._detectors

        gray = to_gray(frame)
        events: List[Any] = []
        for roi in rois:
            processor = processors.get(roi.name)
            detector = detectors.get(roi.name)
            if processor is None or detector is None:
                continue
            motion = processor.motion(gray)
            result = detector.update(motion, ts)
            self.store.add_signal(roi.name, ts, motion)
            if result.event is None:
                continue
            result.event.wall_ts = wall_ts
            events.append(result.event)
            self.last_event_ts = result.event.end_ts
            self.store.record_event(result.event, frame_provider=lambda r=roi: self._crop(r))
            if self.config.pipeline.log_events:
                log.info(
                    "Бульк #%d [%s] peak=%.4f dur=%.2fs",
                    detector.count, roi.name, result.event.peak, result.event.duration_s,
                )

        self.frames_processed += 1
        self._fps_window.append(ts)
        cutoff = ts - 1.0
        while self._fps_window and self._fps_window[0] < cutoff:
            self._fps_window.popleft()
        self.process_fps = float(len(self._fps_window))

        if ts - self._last_tick >= 1.0:
            self._last_tick = ts
            self.store.tick(wall_ts)
        return events

    def run(self) -> None:
        interval = 1.0 / max(1.0, float(self.config.pipeline.process_fps))
        log.info("Обработка запущена, целевая частота %.1f к/с", self.config.pipeline.process_fps)
        while not self._stop.is_set():
            item = self.camera.latest()
            if item is None:
                if not self.camera.is_alive() and self.camera.error:
                    log.error("Камера недоступна: %s", self.camera.error)
                self._stop.wait(0.1)
                continue

            frame, _frame_ts, index = item
            now_mono = time.monotonic()
            pause = interval - (now_mono - self._last_process_wall)
            if index == self._last_index or pause > 0:
                self._stop.wait(min(0.02, max(0.001, pause)))
                continue

            self._last_process_wall = now_mono
            self._last_index = index
            self.process_frame(frame, now_mono)

        self.store.tick()
        log.info("Обработка остановлена, всего кадров %d", self.frames_processed)

    def _crop(self, roi: Roi):
        item = self.latest_frame()
        if item is None:
            return None
        frame = item[0]
        height, width = frame.shape[:2]
        x0 = max(0, min(roi.x, width - 1))
        y0 = max(0, min(roi.y, height - 1))
        x1 = max(x0 + 1, min(roi.x + roi.w, width))
        y1 = max(y0 + 1, min(roi.y + roi.h, height))
        return frame[y0:y1, x0:x1]

    def status(self) -> Dict[str, Any]:
        now = time.monotonic()
        now_wall = time.time()
        with self._lock:
            rois = list(self._rois)
            detectors = {name: d.as_dict(now) for name, d in self._detectors.items()}
            warmup_left = max((d.warmup_left for d in self._detectors.values()), default=0)
            config = self.config
        totals_live = {name: int(info["count"]) for name, info in detectors.items()}
        totals = self.store.totals(totals_live)
        grand_total = sum(totals.values())
        hour_totals = self.store.window_totals(now_wall, 3600.0)
        day_totals = self.store.window_totals(now_wall, 86400.0)
        last_age = (now - self.last_event_ts) if self.last_event_ts else None

        roi_list: List[Dict[str, Any]] = []
        for roi in rois:
            info = dict(detectors.get(roi.name, {}))
            info["rect"] = roi.rect()
            info["has_override"] = bool(roi.detector)
            info["detector_override"] = dict(roi.detector)
            info["session_count"] = int(info.get("count", 0))
            info["count"] = int(totals.get(roi.name, 0))          # за всё время (с историей)
            hour = int(hour_totals.get(roi.name, 0))
            day24 = int(day_totals.get(roi.name, 0))
            rates = dict(info.get("rates", {}))
            rates["bph"] = hour                                   # за последний час (с историей)
            rates["day24"] = day24                                # за последние сутки
            info["rates"] = rates
            info["fermentation"] = fermentation_status(day24, hour)
            roi_list.append(info)

        alerts = self.stall_watch.check(now, self.last_event_ts, grand_total)
        if not self.camera.is_open:
            alerts.insert(0, {
                "code": "camera",
                "level": "error",
                "message": self.camera.error or "Камера не отвечает",
            })
        if not rois:
            alerts.insert(0, {
                "code": "no_roi",
                "level": "warning",
                "message": "Не выделена зона гидрозатвора. Откройте «Настройка зон» и обведите гидрозатвор.",
            })

        rates = {
            "bpm_1m": round(sum(d["rates"]["bpm_1m"] for d in detectors.values()), 2),
            "bpm_5m": round(sum(d["rates"]["bpm_5m"] for d in detectors.values()), 2),
            "bpm_30m": round(sum(d["rates"]["bpm_30m"] for d in detectors.values()), 2),
            "bph": int(sum(hour_totals.values())),
        }

        return {
            "now": now_wall,
            "app": {
                "uptime_s": round(now - self._started_mono, 1),
                "since": self.store.started_at,
                "sessions": self.store.sessions,
                "process_fps": round(self.process_fps, 1),
                "frames_processed": self.frames_processed,
                "warmup": warmup_left > 0,
                "warmup_frames_left": warmup_left,
            },
            "camera": self.camera.status(),
            "rois": roi_list,
            "totals": totals,
            "total_count": grand_total,
            "rates": rates,
            "last_event_age_s": round(last_age, 1) if last_age is not None else None,
            "alerts": alerts,
            "params": {
                "detector": config.detector.as_dict(),
                "vision": config.vision.as_dict(),
                "alerts": {"stall_minutes": config.alerts.stall_minutes},
                "stream": {
                    "fps": config.web.stream_fps,
                    "quality": config.web.stream_quality,
                    "scale": config.web.stream_scale,
                    "show_labels": config.web.show_labels,
                },
            },
        }

    def public_status(self) -> Dict[str, Any]:
        """Обрезанный статус для публичной read-only страницы: без порогов и настроек."""
        st = self.status()
        zones = []
        for r in st["rois"]:
            rates = r.get("rates", {})
            zones.append({
                "name": r["name"],
                "state": r["state"],
                "motion": r["motion"],
                "last_event_age_s": r["last_event_age_s"],
                "rates": {"bph": int(rates.get("bph", 0)), "day24": int(rates.get("day24", 0))},
                "fermentation": r.get("fermentation", {"level": "none", "label": "—"}),
            })
        cam = st["camera"]
        return {
            "now": st["now"],
            "version": __version__,
            "camera": {"ok": cam.get("ok"), "read_fps": cam.get("read_fps"),
                       "width": cam.get("width"), "height": cam.get("height")},
            "app": {"warmup": st["app"]["warmup"], "uptime_s": st["app"]["uptime_s"]},
            "rois": zones,
        }
