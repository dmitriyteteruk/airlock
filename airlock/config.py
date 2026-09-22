from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

import yaml

from .camera import CameraConfig
from .detector import DetectorParams
from .vision import Roi, VisionParams

log = logging.getLogger("airlock.config")


class ConfigError(RuntimeError):
    pass


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    public_port: int = 0
    token: str = ""
    stream_fps: float = 5.0
    stream_quality: int = 72
    stream_scale: float = 0.8
    show_labels: bool = True


@dataclass
class StorageConfig:
    data_dir: str = "data"
    events_file: str = "events.jsonl"
    state_file: str = "state.json"
    overrides_file: str = "overrides.json"
    recent_events: int = 500
    timeline_minutes: int = 86400
    signal_buffer: int = 900
    signal_sample_hz: float = 8.0
    flush_interval_s: float = 5.0
    snapshots: bool = False
    snapshots_dir: str = "snapshots"
    snapshots_keep: int = 500


@dataclass
class AlertConfig:
    stall_minutes: float = 0.0


@dataclass
class PipelineConfig:
    process_fps: float = 20.0
    log_events: bool = True


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _fill(cls, data: Optional[Dict[str, Any]], path: str, unknown: List[str]):
    data = data or {}
    known = {f.name for f in fields(cls)}
    kwargs: Dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            unknown.append("%s.%s" % (path, key))
            continue
        kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError("Ошибка в секции '%s': %s" % (path, exc)) from exc


@dataclass
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    vision: VisionParams = field(default_factory=VisionParams)
    detector: DetectorParams = field(default_factory=DetectorParams)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    web: WebConfig = field(default_factory=WebConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    rois: List[Roi] = field(default_factory=list)
    source_path: Optional[str] = None
    overrides_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "camera": self.camera.as_dict(),
            "vision": self.vision.as_dict(),
            "detector": self.detector.as_dict(),
            "pipeline": asdict(self.pipeline),
            "web": asdict(self.web),
            "storage": asdict(self.storage),
            "alerts": asdict(self.alerts),
            "rois": [roi.as_dict() for roi in self.rois],
        }

    def runtime_dict(self) -> Dict[str, Any]:
        """Параметры, которые можно менять из веб-интерфейса и сохранять в overrides."""
        return {
            "rois": [roi.as_dict() for roi in self.rois],
            "vision": self.vision.as_dict(),
            "detector": self.detector.as_dict(),
            "alerts": asdict(self.alerts),
        }

    def roi_names(self) -> List[str]:
        return [roi.name for roi in self.rois]

    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        return AppConfig().as_dict()

    @classmethod
    def load(
        cls,
        path: Optional[str] = None,
        overrides_path: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> "AppConfig":
        data: Dict[str, Any] = cls.defaults()

        source: Optional[str] = None
        if path and os.path.exists(path):
            source = path
            loaded = _read_yaml(path)
            data = _deep_merge(data, loaded)
        elif path:
            log.warning("Конфиг %s не найден, работаем со значениями по умолчанию", path)

        if extra:
            data = _deep_merge(data, extra)

        if overrides_path is None:
            overrides_path = os.path.join(
                str(data.get("storage", {}).get("data_dir", "data")),
                str(data.get("storage", {}).get("overrides_file", "overrides.json")),
            )
        if overrides_path and os.path.exists(overrides_path):
            try:
                with open(overrides_path, "r", encoding="utf-8") as fh:
                    data = _deep_merge(data, json.load(fh) or {})
                log.info("Применены сохранённые настройки из %s", overrides_path)
            except (OSError, ValueError) as exc:
                log.error("Не удалось прочитать %s: %s", overrides_path, exc)

        config = cls.from_dict(data)
        config.source_path = source
        config.overrides_path = overrides_path
        return config

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        unknown: List[str] = []
        rois_raw = data.get("rois") or []
        rois: List[Roi] = []
        for idx, item in enumerate(rois_raw):
            if not isinstance(item, dict):
                unknown.append("rois[%d]" % idx)
                continue
            rois.append(_fill(Roi, item, "rois[%d]" % idx, unknown))

        config = cls(
            camera=_fill(CameraConfig, data.get("camera"), "camera", unknown),
            vision=_fill(VisionParams, data.get("vision"), "vision", unknown),
            detector=_fill(DetectorParams, data.get("detector"), "detector", unknown),
            pipeline=_fill(PipelineConfig, data.get("pipeline"), "pipeline", unknown),
            web=_fill(WebConfig, data.get("web"), "web", unknown),
            storage=_fill(StorageConfig, data.get("storage"), "storage", unknown),
            alerts=_fill(AlertConfig, data.get("alerts"), "alerts", unknown),
            rois=rois,
        )

        for key in data:
            if key not in {f.name for f in fields(cls)}:
                unknown.append(key)
        if unknown:
            log.warning("Неизвестные ключи в конфиге (проигнорированы): %s", ", ".join(sorted(set(unknown))))

        config.validate()
        return config

    def validate(self) -> None:
        if not self.rois:
            log.warning(
                "Не задано ни одной зоны контроля (rois). "
                "Откройте веб-интерфейс и выделите гидрозатвор рамкой."
            )
        names = [roi.name for roi in self.rois]
        if len(names) != len(set(names)):
            raise ConfigError("Имена зон (rois[].name) должны быть уникальными")
        for roi in self.rois:
            if roi.w <= 4 or roi.h <= 4:
                raise ConfigError("Зона '%s' слишком маленькая (минимум 5x5 пикселей)" % roi.name)
            if not isinstance(roi.detector, dict):
                raise ConfigError("Зона '%s': поле 'detector' должно быть набором параметров" % roi.name)
            eff = self.detector.updated(roi.detector)
            if eff.off_threshold >= eff.on_threshold:
                raise ConfigError("Зона '%s': off_threshold >= on_threshold" % roi.name)
            if eff.saturate_ratio <= eff.on_threshold:
                raise ConfigError("Зона '%s': saturate_ratio <= on_threshold" % roi.name)
        if self.detector.off_threshold >= self.detector.on_threshold:
            raise ConfigError("detector.off_threshold должен быть меньше detector.on_threshold")
        if self.detector.saturate_ratio <= self.detector.on_threshold:
            raise ConfigError("detector.saturate_ratio должен быть больше detector.on_threshold")
        if not (0 < self.vision.process_scale <= 4):
            raise ConfigError("vision.process_scale должен быть в диапазоне (0, 4]")

    def frame_size(self) -> Optional[tuple]:
        if self.camera.width and self.camera.height:
            return int(self.camera.width), int(self.camera.height)
        return None


def _read_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError("Не удалось прочитать %s: %s" % (path, exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError("Ошибка разбора YAML в %s: %s" % (path, exc)) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("Корень %s должен быть словарём" % path)
    return loaded


def load_overrides(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log.error("Не удалось прочитать overrides %s: %s", path, exc)
        return {}


def save_overrides(path: str, patch: Dict[str, Any]) -> None:
    """Дописывает изменённые пользовательские настройки, не трогая config.yaml с комментариями."""
    current = load_overrides(path)
    merged = _deep_merge(current, patch)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    log.info("Настройки сохранены в %s", path)
