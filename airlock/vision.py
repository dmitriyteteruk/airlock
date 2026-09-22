from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

import cv2
import numpy as np

BG_MOG2 = "mog2"
BG_MEAN = "mean"

COLOR_IDLE = (90, 200, 90)
COLOR_RISING = (60, 170, 255)
COLOR_FALLING = (60, 220, 255)
COLOR_DISTURBED = (70, 70, 230)
COLOR_WARMUP = (170, 170, 170)

STATE_COLORS = {
    "idle": COLOR_IDLE,
    "rising": COLOR_RISING,
    "falling": COLOR_FALLING,
    "disturbed": COLOR_DISTURBED,
    "warmup": COLOR_WARMUP,
}


@dataclass
class VisionParams:
    background: str = BG_MOG2
    mog2_history: int = 400
    mog2_threshold: float = 30.0
    mog2_learning_rate: float = 0.004
    mean_alpha: float = 0.02
    mean_threshold: float = 18.0
    despeckle: int = 3
    process_scale: float = 1.0
    warmup_frames: int = 60
    equalize: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def updated(self, patch: Optional[Dict[str, Any]]) -> Tuple["VisionParams", bool]:
        """Возвращает новые параметры и флаг, что модель фона нужно пересоздать."""
        data = self.as_dict()
        rebuild = False
        bg_keys = ("background", "mog2_history", "mog2_threshold", "mog2_learning_rate",
                   "mean_alpha", "mean_threshold")
        for key, value in (patch or {}).items():
            if key not in data:
                continue
            if key in bg_keys and data[key] != value:
                rebuild = True
            data[key] = value
        return VisionParams(**data), rebuild


def _odd(value: int) -> int:
    value = int(value)
    if value < 3:
        return 0
    return value if value % 2 == 1 else value + 1


class Mog2Background:
    name = BG_MOG2

    def __init__(self, history: int, threshold: float, learning_rate: float) -> None:
        self.sub = cv2.createBackgroundSubtractorMOG2(
            history=max(10, int(history)),
            varThreshold=max(4.0, float(threshold)),
            detectShadows=False,
        )
        self.learning_rate = float(learning_rate)

    def apply(self, gray: np.ndarray) -> np.ndarray:
        return self.sub.apply(gray, learningRate=self.learning_rate)


class MeanBackground:
    """Скользящее среднее как модель фона. Дешевле MOG2, но хуже при дрейфе освещения."""

    name = BG_MEAN

    def __init__(self, alpha: float, threshold: float) -> None:
        self.alpha = float(alpha)
        self.threshold = float(threshold)
        self.model: Optional[np.ndarray] = None

    def apply(self, gray: np.ndarray) -> np.ndarray:
        frame = gray.astype(np.float32)
        if self.model is None or self.model.shape != frame.shape:
            self.model = frame.copy()
            return np.zeros(gray.shape, dtype=np.uint8)
        diff = cv2.absdiff(self.model, frame)
        cv2.accumulateWeighted(frame, self.model, self.alpha)
        _, mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)
        return mask


def make_background(params: VisionParams):
    if params.background == BG_MEAN:
        return MeanBackground(params.mean_alpha, params.mean_threshold)
    return Mog2Background(params.mog2_history, params.mog2_threshold, params.mog2_learning_rate)


@dataclass
class Roi:
    name: str
    x: int
    y: int
    w: int
    h: int
    detector: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "x": int(self.x),
            "y": int(self.y),
            "w": int(self.w),
            "h": int(self.h),
        }
        if self.detector:
            data["detector"] = dict(self.detector)
        return data

    def rect(self) -> Tuple[int, int, int, int]:
        return int(self.x), int(self.y), int(self.w), int(self.h)

    def scaled(self, factor: float) -> "Roi":
        return Roi(
            self.name,
            int(self.x * factor),
            int(self.y * factor),
            max(1, int(self.w * factor)),
            max(1, int(self.h * factor)),
            dict(self.detector),
        )


class RoiProcessor:
    """Превращает серый кадр в число motion — долю изменившихся пикселей внутри ROI."""

    MIN_SIDE = 5

    def __init__(self, roi: Roi, params: VisionParams) -> None:
        self.roi = roi
        self.params = params
        self.background = make_background(params)
        self.effective: Optional[Tuple[int, int, int, int]] = None
        self.pixels = 0
        self._frames_seen = 0

    def rebuild_background(self, params: VisionParams) -> None:
        self.params = params
        self.background = make_background(params)
        self._frames_seen = 0

    def motion(self, gray: np.ndarray) -> float:
        height, width = gray.shape[:2]
        x, y, w, h = self.roi.rect()
        x0 = max(0, min(x, width - 1))
        y0 = max(0, min(y, height - 1))
        x1 = max(x0 + 1, min(x + w, width))
        y1 = max(y0 + 1, min(y + h, height))
        self.effective = (x0, y0, x1 - x0, y1 - y0)

        if (x1 - x0) < self.MIN_SIDE or (y1 - y0) < self.MIN_SIDE:
            self._frames_seen += 1
            self.pixels = 0
            return 0.0

        sub = gray[y0:y1, x0:x1]
        if sub.size == 0:
            self._frames_seen += 1
            return 0.0

        scale = float(self.params.process_scale)
        if 0 < scale < 0.999:
            sub = cv2.resize(sub, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        elif scale > 1.001:
            sub = cv2.resize(sub, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)

        if self.params.equalize:
            sub = cv2.equalizeHist(sub)

        mask = self.background.apply(sub)
        self._frames_seen += 1
        if self._frames_seen <= 1:
            return 0.0

        kernel = _odd(self.params.despeckle)
        if kernel:
            mask = cv2.medianBlur(mask, kernel)

        self.pixels = int(mask.size)
        if not self.pixels:
            return 0.0
        return float(cv2.countNonZero(mask)) / float(self.pixels)


def to_gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    channels = frame.shape[2]
    if channels == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    if channels == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame[:, :, 0]


def draw_overlay(
    frame: np.ndarray,
    rois: Sequence[Roi],
    states: Dict[str, Dict[str, Any]],
    show_labels: bool = True,
) -> np.ndarray:
    out = frame
    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    else:
        out = out.copy()

    height, width = out.shape[:2]
    for roi in rois:
        info = states.get(roi.name, {})
        color = STATE_COLORS.get(info.get("state", "idle"), COLOR_IDLE)
        x, y, w, h = roi.rect()
        x1 = max(0, min(x, width - 1))
        y1 = max(0, min(y, height - 1))
        x2 = max(x1 + 1, min(x + w, width))
        y2 = max(y1 + 1, min(y + h, height))
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

        if not show_labels:
            continue

        count = info.get("count", 0)
        motion = info.get("motion", 0.0)
        label = "%s  %d" % (roi.name, count)
        bar_w = max(40, (x2 - x1))
        bx = x1
        by = max(12, y1 - 8)
        cv2.rectangle(out, (bx, by - 11), (bx + bar_w, by + 3), (30, 30, 30), -1)
        cv2.putText(out, label, (bx + 3, by), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (245, 245, 245), 1, cv2.LINE_AA)

        fill = int(min(1.0, motion / 0.06) * (bar_w - 4))
        cv2.rectangle(out, (bx + 2, by + 6), (bx + 2 + fill, by + 12), color, -1)
        cv2.rectangle(out, (bx + 2, by + 6), (bx + bar_w - 2, by + 12), (120, 120, 120), 1)

    return out


def encode_jpeg(frame: np.ndarray, quality: int = 78) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        return b""
    return buf.tobytes()
