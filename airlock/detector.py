from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

STATE_IDLE = "idle"
STATE_RISING = "rising"
STATE_FALLING = "falling"
STATE_DISTURBED = "disturbed"
STATE_WARMUP = "warmup"

KIND_BUBBLE = "bubble"
KIND_LONG = "long"


@dataclass
class DetectorParams:
    on_threshold: float = 0.004
    off_threshold: float = 0.0012
    drop_ratio: float = 0.45
    min_off_frames: int = 2
    min_peak_frames: int = 1
    refractory_s: float = 0.12
    max_hump_s: float = 4.0
    saturate_ratio: float = 0.5
    saturate_cooldown_s: float = 1.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def updated(self, patch: Optional[Dict[str, Any]]) -> "DetectorParams":
        data = self.as_dict()
        data.update({k: v for k, v in (patch or {}).items() if k in data})
        return DetectorParams(**data)


@dataclass
class Event:
    """start_ts/end_ts — в monotonic-шкале (устойчиво к прыжкам системных часов).

    wall_ts проставляет пайплайн перед записью в журнал — это метка реального времени.
    """

    id: int = 0
    roi: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    peak: float = 0.0
    frames: int = 0
    kind: str = KIND_BUBBLE
    wall_ts: float = 0.0

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def stamp(self) -> float:
        return self.wall_ts or self.end_ts

    def to_dict(self) -> Dict[str, Any]:
        stamp = self.stamp
        return {
            "id": self.id,
            "roi": self.roi,
            "wall_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
            "ts": round(stamp, 3),
            "duration_s": round(self.duration_s, 3),
            "peak": round(self.peak, 6),
            "frames": self.frames,
            "kind": self.kind,
        }


@dataclass
class FrameResult:
    ts: float
    motion: float
    state: str
    event: Optional[Event] = None


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * pct))
    return float(ordered[max(0, min(len(ordered) - 1, idx))])


def suggest_thresholds(stats_list) -> Dict[str, float]:
    """Консервативный подбор порогов: шум берём по максимуму, пики — по минимуму."""
    noise_hi = 0.0
    noise_max = 0.0
    peak_lo = 0.0
    peak_med = 0.0
    peak_max = 0.0
    noise_samples = 0
    peak_samples = 0
    has_peaks = False
    for stats in stats_list:
        noise = list(stats.noise)
        peaks = list(stats.peaks)
        noise_samples += len(noise)
        peak_samples += len(peaks)
        if noise:
            noise_hi = max(noise_hi, _percentile(noise, 0.99))
            noise_max = max(noise_max, max(noise))
        if peaks:
            has_peaks = True
            low = _percentile(peaks, 0.05)
            peak_lo = low if not peak_lo else min(peak_lo, low)
            peak_med = max(peak_med, _percentile(peaks, 0.5))
            peak_max = max(peak_max, max(peaks))

    base = max(noise_hi, noise_max * 1.15)
    if has_peaks and peak_lo > base * 1.5:
        on = round(base + (peak_lo - base) * 0.35, 6)
    else:
        on = round(max(base * 1.6, 1e-4), 6)
    off = round(min(on * 0.3, max(on * 0.9 - 1e-6, 0.0)), 6)
    return {
        "on_threshold": on,
        "off_threshold": off,
        "noise_p99": round(noise_hi, 6),
        "noise_max": round(noise_max, 6),
        "peak_p05": round(peak_lo, 6),
        "peak_median": round(peak_med, 6),
        "peak_max": round(peak_max, 6),
        "noise_samples": noise_samples,
        "peak_samples": peak_samples,
    }


FERMENTATION_LEVELS = {
    "none": "Отсутствует",
    "veryslow": "Очень медленное",
    "slow": "Медленное",
    "medium": "Среднее",
    "fast": "Быстрое",
    "veryfast": "Очень быстрое",
}


def fermentation_status(day24: int, hour: int) -> Dict[str, str]:
    """Интенсивность брожения по зоне.

    Нижние две ступени смотрим по суткам (мало ли шло вообще), верхние — по темпу в час.
    Пороги из ТЗ: <1/сут = Отсутствует; 1..24/сут = Очень медленное;
    далее по часам: 1..30 Медленное, 31..60 Среднее, 61..360 Быстрое, >360 Очень быстрое.
    """
    day24 = max(0, int(day24))
    hour = max(0, int(hour))
    if day24 < 1:
        level = "none"
    elif day24 <= 24:
        level = "veryslow"
    elif hour <= 30:
        level = "slow"
    elif hour <= 60:
        level = "medium"
    elif hour <= 360:
        level = "fast"
    else:
        level = "veryfast"
    return {"level": level, "label": FERMENTATION_LEVELS[level]}


class MotionStats:
    """Статистика сигнала: уровень шума и амплитуда пиков. Нужно для подбора порогов."""

    def __init__(self, maxlen: int = 400) -> None:
        self.noise: Deque[float] = deque(maxlen=maxlen)
        self.peaks: Deque[float] = deque(maxlen=maxlen)

    def add_noise(self, motion: float) -> None:
        self.noise.append(motion)

    def add_peak(self, peak: float) -> None:
        self.peaks.append(peak)

    def suggest_thresholds(self) -> Dict[str, float]:
        return suggest_thresholds([self])

    def as_dict(self) -> Dict[str, Any]:
        return {
            "noise_p50": round(_percentile(list(self.noise), 0.5), 6),
            "noise_p99": round(_percentile(list(self.noise), 0.99), 6),
            "peak_median": round(_percentile(list(self.peaks), 0.5), 6),
            "peak_p95": round(_percentile(list(self.peaks), 0.95), 6),
            "peak_max": round(max(self.peaks) if self.peaks else 0.0, 6),
            "samples": {"noise": len(self.noise), "peaks": len(self.peaks)},
        }


class BubbleDetector:
    """Считает горбы motion-сигнала. Один горб = один бульк.

    Гистерезис (on_threshold/off_threshold) защищает от дрожи на пороге,
    refractory_s — от двойного счёта одного булька, max_hump_s — от залипания
    на непрерывном возмущении, saturate_ratio — от засветки всего кадра.
    """

    def __init__(self, name: str, params: Optional[DetectorParams] = None) -> None:
        self.name = name
        self.params = params or DetectorParams()
        self.count = 0
        self.state = STATE_WARMUP
        self.motion = 0.0
        self.peak = 0.0
        self.last_event: Optional[Event] = None
        self.disturbances = 0
        self.stats = MotionStats()

        self.timestamps: Deque[float] = deque(maxlen=20000)
        self._hump_start: Optional[float] = None
        self._hump_peak = 0.0
        self._hump_frames = 0
        self._off_run = 0
        self._cooldown_until = 0.0
        self._last_event_ts = -1e9
        self._warmup_left = 0

    @property
    def warmup_left(self) -> int:
        return self._warmup_left

    def set_warmup(self, frames: int) -> None:
        self._warmup_left = max(0, int(frames))
        if self._warmup_left:
            self.state = STATE_WARMUP

    def reset_counters(self) -> None:
        self.count = 0
        self.timestamps.clear()
        self.last_event = None
        self.disturbances = 0
        self._reset_hump()

    def _reset_hump(self) -> None:
        self._hump_start = None
        self._hump_peak = 0.0
        self._hump_frames = 0
        self._off_run = 0

    def rate(self, now: float, window_s: float) -> float:
        if window_s <= 0:
            return 0.0
        cutoff = now - window_s
        hits = 0
        for ts in reversed(self.timestamps):
            if ts < cutoff:
                break
            hits += 1
        return round(hits * 60.0 / window_s, 2)

    def bpm(self, now: float) -> Dict[str, float]:
        return {
            "bpm_1m": self.rate(now, 60.0),
            "bpm_5m": self.rate(now, 300.0),
            "bpm_30m": self.rate(now, 1800.0),
            "bph": int(round(self.rate(now, 3600.0) * 60.0)),
        }

    def update(self, motion: float, ts: float) -> FrameResult:
        p = self.params
        self.motion = motion

        if self._warmup_left > 0:
            self._warmup_left -= 1
            self.state = STATE_WARMUP
            return FrameResult(ts=ts, motion=motion, state=STATE_WARMUP)

        if motion >= p.saturate_ratio:
            self._reset_hump()
            self.disturbances += 1
            self._cooldown_until = ts + p.saturate_cooldown_s
            self.state = STATE_DISTURBED
            return FrameResult(ts=ts, motion=motion, state=STATE_DISTURBED)

        if ts < self._cooldown_until:
            self._reset_hump()
            self.state = STATE_DISTURBED
            return FrameResult(ts=ts, motion=motion, state=STATE_DISTURBED)

        if self._hump_start is not None and (ts - self._hump_start) > p.max_hump_s:
            event = self._emit(ts, KIND_LONG)
            self._reset_hump()
            self.state = STATE_IDLE
            return FrameResult(ts=ts, motion=motion, state=STATE_IDLE, event=event)

        if motion >= p.on_threshold:
            if self._hump_start is None:
                self._hump_start = ts
            self._hump_frames += 1
            if motion > self._hump_peak:
                self._hump_peak = motion
            self._off_run = 0
            self.state = STATE_RISING
            return FrameResult(ts=ts, motion=motion, state=STATE_RISING)

        if self._hump_start is None:
            self.stats.add_noise(motion)
            self.state = STATE_IDLE
            self._off_run = 0
            return FrameResult(ts=ts, motion=motion, state=STATE_IDLE)

        if motion <= p.off_threshold or motion <= self._hump_peak * p.drop_ratio:
            self._off_run += 1
        else:
            self._off_run = 0
        self.state = STATE_FALLING

        if self._off_run < max(1, p.min_off_frames):
            return FrameResult(ts=ts, motion=motion, state=STATE_FALLING)

        if self._hump_frames < max(1, p.min_peak_frames):
            self._reset_hump()
            self.state = STATE_IDLE
            return FrameResult(ts=ts, motion=motion, state=STATE_IDLE)

        if (ts - self._last_event_ts) < p.refractory_s:
            self._reset_hump()
            self.state = STATE_IDLE
            return FrameResult(ts=ts, motion=motion, state=STATE_IDLE)

        event = self._emit(ts, KIND_BUBBLE)
        self._reset_hump()
        self.state = STATE_IDLE
        return FrameResult(ts=ts, motion=motion, state=STATE_IDLE, event=event)

    def _emit(self, ts: float, kind: str) -> Event:
        self.count += 1
        self._last_event_ts = ts
        event = Event(
            roi=self.name,
            start_ts=self._hump_start if self._hump_start is not None else ts,
            end_ts=ts,
            peak=self._hump_peak,
            frames=self._hump_frames,
            kind=kind,
        )
        self.last_event = event
        self.peak = event.peak
        self.timestamps.append(ts)
        self.stats.add_peak(event.peak)
        return event

    def as_dict(self, now: float) -> Dict[str, Any]:
        rates = self.bpm(now)
        last_age: Optional[float] = None
        if self.last_event is not None:
            last_age = round(max(0.0, now - self.last_event.end_ts), 1)
        return {
            "name": self.name,
            "count": self.count,
            "motion": round(self.motion, 6),
            "state": self.state,
            "peak": round(self.peak, 6),
            "last_event_age_s": last_age,
            "disturbances": self.disturbances,
            "rates": rates,
            "stats": self.stats.as_dict(),
            "params": self.params.as_dict(),
            "has_override": False,
        }


class MinuteTimeline:
    """Поминутная гистограмма бульков для графика.

    Хранит только непустые минуты (разреженно), поэтому 60 суток весят мало.
    Для длинных окон series() прореживает до «красивого» шага, чтобы число точек
    оставалось bounded (~200), а не 86400.
    """

    STEPS = (1, 2, 3, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)

    def __init__(self, keep_minutes: int = 86400, max_points: int = 240) -> None:
        self.keep_minutes = max(10, keep_minutes)
        self.max_points = max(20, max_points)
        self._buckets: Dict[int, Dict[str, int]] = {}
        self._order: Deque[int] = deque()

    @staticmethod
    def _minute(ts: float) -> int:
        return int(ts // 60) * 60

    def add(self, ts: float, roi: str, n: int = 1) -> None:
        minute = self._minute(ts)
        bucket = self._buckets.get(minute)
        if bucket is None:
            bucket = {}
            self._buckets[minute] = bucket
            self._order.append(minute)
        bucket[roi] = bucket.get(roi, 0) + n
        self._prune(self._minute(ts))

    def _prune(self, now_minute: int) -> None:
        limit = now_minute - self.keep_minutes * 60
        while self._order and self._order[0] < limit:
            old = self._order.popleft()
            self._buckets.pop(old, None)

    def _choose_step(self, minutes: int) -> int:
        for step in self.STEPS:
            if minutes / step <= self.max_points:
                return step
        return self.STEPS[-1]

    def series(self, now: float, minutes: int = 180) -> Dict[str, Any]:
        minutes = max(1, min(int(minutes), self.keep_minutes))
        step = self._choose_step(minutes)
        step_sec = step * 60
        end_minute = self._minute(now)
        last_bucket = (end_minute // step_sec) * step_sec
        count = max(1, int(round(minutes / step)))
        first_bucket = last_bucket - (count - 1) * step_sec

        bins: Dict[int, Dict[str, int]] = {}
        for minute, bucket in self._buckets.items():
            key = (minute // step_sec) * step_sec
            if key < first_bucket or key > last_bucket:
                continue
            agg = bins.setdefault(key, {})
            for roi, n in bucket.items():
                agg[roi] = agg.get(roi, 0) + n

        points = []
        for k in range(count):
            key = first_bucket + k * step_sec
            agg = bins.get(key, {})
            points.append({
                "minute": key,
                "total": sum(agg.values()),
                "by_roi": dict(agg),
            })
        return {"step_minutes": step, "minutes": count * step, "points": points}

    def totals(self) -> Dict[str, int]:
        acc: Dict[str, int] = {}
        for bucket in self._buckets.values():
            for roi, n in bucket.items():
                acc[roi] = acc.get(roi, 0) + n
        return acc

    def window_totals(self, now: float, window_s: float) -> Dict[str, int]:
        """Сумма по зонам за последние window_s секунд (из истории, устойчиво к рестарту)."""
        cutoff = int(now) - int(window_s)
        out: Dict[str, int] = {}
        for minute, bucket in self._buckets.items():
            if minute < cutoff:
                continue
            for roi, n in bucket.items():
                out[roi] = out.get(roi, 0) + n
        return out

    def recent_count(self, roi: str, now: float, minutes: int = 60) -> int:
        """Сколько бульков у зоны за последние `minutes` минут (по журналу, с историей)."""
        minutes = max(1, int(minutes))
        end = self._minute(now)
        total = 0
        for k in range(minutes):
            bucket = self._buckets.get(end - k * 60)
            if bucket:
                total += bucket.get(roi, 0)
        return total


class SignalBuffer:
    """Кольцевой буфер сырого motion-сигнала для живого графика и отладки порогов."""

    def __init__(self, maxlen: int = 900) -> None:
        self._data: Deque[Tuple[float, float]] = deque(maxlen=maxlen)

    def add(self, ts: float, value: float) -> None:
        self._data.append((round(ts, 3), round(value, 6)))

    def clear(self) -> None:
        self._data.clear()

    def resize(self, maxlen: int) -> None:
        self._data = deque(self._data, maxlen=max(60, int(maxlen)))

    def series(self, limit: int = 0) -> List[List[float]]:
        items = list(self._data)
        if limit and limit > 0:
            items = items[-limit:]
        return [[ts, v] for ts, v in items]


class StallWatch:
    """Тревога, если брожение замерло: нет бульков дольше stall_minutes."""

    def __init__(self, stall_minutes: float = 0.0) -> None:
        self.stall_minutes = stall_minutes

    def check(self, now: float, last_event_ts: Optional[float], total_count: int) -> List[Dict[str, str]]:
        alerts: List[Dict[str, str]] = []
        if self.stall_minutes <= 0:
            return alerts
        if last_event_ts is None:
            return alerts
        idle_min = (now - last_event_ts) / 60.0
        if idle_min >= self.stall_minutes:
            alerts.append({
                "code": "stall",
                "level": "warning",
                "message": "Нет бульков уже %.0f мин (порог %.0f мин). Возможно, брожение завершилось или нарушена герметичность."
                % (idle_min, self.stall_minutes),
            })
        return alerts
