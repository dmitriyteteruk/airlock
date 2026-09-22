from __future__ import annotations

import csv
import glob
import io
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from .config import StorageConfig
from .detector import Event, MinuteTimeline, SignalBuffer

log = logging.getLogger("airlock.store")

STATE_VERSION = 1


class Store:
    """Журнал событий, поминутная гистограмма, буферы сигнала и персистентность счётчиков."""

    def __init__(self, cfg: StorageConfig) -> None:
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.events_path = os.path.join(cfg.data_dir, cfg.events_file)
        self.state_path = os.path.join(cfg.data_dir, cfg.state_file)
        self.snapshots_path = os.path.join(cfg.data_dir, cfg.snapshots_dir)
        self._lock = threading.RLock()
        self._pending: List[Dict[str, Any]] = []
        self._last_flush = 0.0
        self._last_state_save = 0.0
        self._dirty_state = False
        self._next_id = 1
        self._live: Optional[Callable[[], Dict[str, int]]] = None

        self.recent: Deque[Dict[str, Any]] = deque(maxlen=max(20, int(cfg.recent_events)))
        self.timeline = MinuteTimeline(keep_minutes=int(cfg.timeline_minutes))
        self.signals: Dict[str, SignalBuffer] = {}
        self.baseline: Dict[str, int] = {}
        self.started_at = time.time()
        self.sessions = 0
        self._signal_min_interval = 1.0 / max(0.5, float(cfg.signal_sample_hz))
        self._last_signal_ts: Dict[str, float] = {}

        os.makedirs(self.data_dir, exist_ok=True)
        if cfg.snapshots:
            os.makedirs(self.snapshots_path, exist_ok=True)
            self._snapshot_names: Deque[str] = deque(sorted(_existing_snapshots(self.snapshots_path)))
        else:
            self._snapshot_names = deque()

        self.load_state()
        self.load_history()

    def load_history(self) -> None:
        """Восстанавливает поминутный график и последние события из журнала событий.

        Без этого после перезапуска сервиса график «Бульков в минуту» пустой, хотя
        вся история лежит в events.jsonl.
        """
        if not os.path.exists(self.events_path):
            return
        window_start = time.time() - self.timeline.keep_minutes * 60 - 3600
        loaded = 0
        max_id = self._next_id - 1
        journal_totals: Dict[str, int] = {}
        try:
            with open(self.events_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    loaded += 1
                    rid = rec.get("id")
                    if isinstance(rid, int) and rid > max_id:
                        max_id = rid
                    self.recent.appendleft(rec)
                    ts = rec.get("ts")
                    roi = rec.get("roi")
                    if roi:
                        journal_totals[str(roi)] = journal_totals.get(str(roi), 0) + 1
                    if isinstance(ts, (int, float)) and roi and ts >= window_start:
                        self.timeline.add(ts, str(roi))
        except OSError as exc:
            log.error("Не удалось загрузить историю из %s: %s", self.events_path, exc)
            return
        if max_id >= self._next_id:
            self._next_id = max_id + 1
        # Журнал — авторитет по общему числу (флашится чаще state.json): поднимаем baseline
        # до него, но никогда не понижаем (защита, если events.jsonl урезали вручную).
        for roi, n in journal_totals.items():
            if n > int(self.baseline.get(roi, 0)):
                self.baseline[roi] = n
        if loaded:
            log.info(
                "Загружена история из журнала: %d событий, активных минут на графике: %d",
                loaded, len(self.timeline._buckets),
            )

    def set_signal_buffer_size(self, size: int) -> None:
        with self._lock:
            self.cfg.signal_buffer = max(60, int(size))
            for buf in self.signals.values():
                buf.resize(self.cfg.signal_buffer)

    def buffer_for(self, roi: str) -> SignalBuffer:
        buf = self.signals.get(roi)
        if buf is None:
            buf = SignalBuffer(maxlen=max(60, int(self.cfg.signal_buffer)))
            self.signals[roi] = buf
        return buf

    def load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.error("Не удалось прочитать %s: %s", self.state_path, exc)
            return
        if not isinstance(data, dict):
            return
        totals = data.get("totals") or {}
        if isinstance(totals, dict):
            self.baseline = {str(k): int(v) for k, v in totals.items() if _is_int(v)}
        self._next_id = int(data.get("next_id", 1)) if _is_int(data.get("next_id")) else 1
        self.started_at = float(data.get("since", time.time())) if _is_number(data.get("since")) else time.time()
        self.sessions = int(data.get("sessions", 0)) + 1 if _is_int(data.get("sessions")) else 1
        log.info(
            "Восстановлены счётчики: всего %d бульков с %s",
            sum(self.baseline.values()),
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
        )

    def set_live_provider(self, provider: Optional[Callable[[], Dict[str, int]]]) -> None:
        """Источник «живых» счётчиков за текущую сессию. Baseline хранит то, что было до рестарта."""
        self._live = provider

    def _live_totals(self) -> Dict[str, int]:
        if self._live is None:
            return {}
        try:
            return {str(k): int(v) for k, v in (self._live() or {}).items()}
        except Exception as exc:  # noqa: BLE001 - статус не должен ронять подсчёт
            log.debug("Не удалось получить живые счётчики: %s", exc)
            return {}

    def save_state(self) -> None:
        live = self._live_totals()
        with self._lock:
            totals = dict(self.baseline)
            for name, value in live.items():
                totals[name] = int(totals.get(name, 0)) + int(value)
            data = {
                "version": STATE_VERSION,
                "totals": totals,
                "next_id": self._next_id,
                "since": self.started_at,
                "updated": time.time(),
                "sessions": self.sessions,
            }
        tmp = self.state_path + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
            self._dirty_state = False
        except OSError as exc:
            log.error("Не удалось сохранить %s: %s", self.state_path, exc)

    def set_baseline(self, roi: str, value: int) -> None:
        with self._lock:
            self.baseline[roi] = int(value)
            self._dirty_state = True

    def record_event(self, event: Event, frame_provider: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
        with self._lock:
            event.id = self._next_id
            self._next_id += 1
            record = event.to_dict()
            self.recent.appendleft(record)
            self._pending.append(record)
            self.timeline.add(event.stamp, event.roi)
            self._dirty_state = True
            if self.cfg.snapshots and frame_provider is not None:
                self._save_snapshot(event, frame_provider)
            if len(self._pending) >= 200:
                self._flush_locked()
        return record

    def _save_snapshot(self, event: Event, frame_provider: Callable[[], Any]) -> None:
        try:
            from .vision import encode_jpeg

            image = frame_provider()
            if image is None:
                return
            name = "ev%08d_%s.jpg" % (event.id, event.roi)
            path = os.path.join(self.snapshots_path, name)
            payload = encode_jpeg(image, 82)
            if not payload:
                return
            with open(path, "wb") as fh:
                fh.write(payload)
            self._snapshot_names.append(name)
            while len(self._snapshot_names) > max(10, int(self.cfg.snapshots_keep)):
                old = self._snapshot_names.popleft()
                try:
                    os.remove(os.path.join(self.snapshots_path, old))
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001 - снимки не должны ронять подсчёт
            log.debug("Снимок не сохранён: %s", exc)

    def add_signal(self, roi: str, ts: float, motion: float) -> None:
        last = self._last_signal_ts.get(roi, 0.0)
        if ts - last < self._signal_min_interval:
            return
        self._last_signal_ts[roi] = ts
        self.buffer_for(roi).add(ts, motion)

    def tick(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        with self._lock:
            if self._pending and (now - self._last_flush) >= self.cfg.flush_interval_s:
                self._flush_locked()
            if self._dirty_state and (now - self._last_state_save) >= 10.0:
                self._last_state_save = now
                save = True
            else:
                save = False
        if save:
            self.save_state()

    def _flush_locked(self) -> None:
        if not self._pending:
            self._last_flush = time.time()
            return
        batch = self._pending
        self._pending = []
        self._last_flush = time.time()
        try:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                for record in batch:
                    fh.write(json.dumps(record, ensure_ascii=False))
                    fh.write("\n")
        except OSError as exc:
            log.error("Не удалось записать %s: %s", self.events_path, exc)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()
        if self._dirty_state:
            self.save_state()

    def close(self) -> None:
        self.flush()

    def reset_totals(self) -> None:
        with self._lock:
            self.baseline = {}
            self.recent.clear()
            for buf in self.signals.values():
                buf.clear()
            self.timeline = MinuteTimeline(keep_minutes=int(self.cfg.timeline_minutes))
            self.started_at = time.time()
            self._pending = []
            self._next_id = 1
            self._dirty_state = True
            # Чистим и журнал, иначе load_history при следующем старте вернёт старые
            # счётчики и график, и сброс окажется недействительным.
            try:
                with open(self.events_path, "w", encoding="utf-8"):
                    pass
            except OSError as exc:
                log.error("Не удалось очистить журнал %s: %s", self.events_path, exc)
        self.save_state()
        log.info("Счётчики сброшены")

    def totals(self, live: Optional[Dict[str, int]] = None) -> Dict[str, int]:
        """Итог = счётчики с прошлых сессий (baseline) + насчитанное сейчас (live)."""
        live = self._live_totals() if live is None else (live or {})
        with self._lock:
            base = dict(self.baseline)
        names = set(base) | set(live)
        return {name: int(base.get(name, 0)) + int(live.get(name, 0)) for name in sorted(names)}

    def events(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), len(self.recent)))
        with self._lock:
            items = list(self.recent)
        return items[offset:offset + limit]

    def signal_series(self, roi: str, limit: int = 0) -> List[List[float]]:
        buf = self.signals.get(roi)
        if buf is None:
            return []
        return buf.series(limit)

    def timeline_series(self, now: float, minutes: int = 180) -> Dict[str, Any]:
        return self.timeline.series(now, minutes)

    def window_totals(self, now: float, window_s: float) -> Dict[str, int]:
        return self.timeline.window_totals(now, window_s)

    def csv_stream(self, limit: int = 0) -> Iterable[str]:
        rows = self._read_records(limit)
        handle = io.StringIO()
        writer = csv.writer(handle)
        writer.writerow(["id", "roi", "wall_time", "ts", "duration_s", "peak", "frames", "kind"])
        yield handle.getvalue()
        handle.seek(0)
        handle.truncate(0)
        for row in rows:
            writer.writerow([
                row.get("id"), row.get("roi"), row.get("wall_time"),
                row.get("ts"), row.get("duration_s"),
                row.get("peak"), row.get("frames"), row.get("kind"),
            ])
            yield handle.getvalue()
            handle.seek(0)
            handle.truncate(0)

    def _read_records(self, limit: int = 0) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if os.path.exists(self.events_path):
            try:
                with open(self.events_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError as exc:
                log.error("Не удалось прочитать %s: %s", self.events_path, exc)
        if limit and limit > 0:
            records = records[-limit:]
        records.reverse()
        with self._lock:
            pending = list(self._pending)
        if pending:
            pending.reverse()
            seen = {r.get("id") for r in records}
            records = [r for r in pending if r.get("id") not in seen] + records
        return records


def _existing_snapshots(directory: str) -> List[str]:
    try:
        return [n for n in os.listdir(directory) if n.endswith(".jpg")]
    except OSError:
        return []


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
