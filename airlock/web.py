from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__
from .config import AppConfig
from .pipeline import Pipeline
from .vision import Roi, encode_jpeg

log = logging.getLogger("airlock.web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_ROIS = 12


def create_app(pipeline: Pipeline) -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
    cfg: AppConfig = pipeline.config

    def authorized() -> bool:
        token = cfg.web.token
        if not token:
            return True
        supplied = request.headers.get("X-Airlock-Token") or request.args.get("token") or ""
        return supplied == token

    def guard(view):
        def wrapper(*args, **kwargs):
            if not authorized():
                return jsonify({"ok": False, "error": "Требуется токен доступа"}), 401
            return view(*args, **kwargs)

        wrapper.__name__ = view.__name__
        return wrapper

    def body() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        if data is None and request.form:
            data = request.form.to_dict()
        return data if isinstance(data, dict) else {}

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/health")
    def health():
        frame = pipeline.latest_frame()
        return jsonify({
            "ok": bool(frame) and pipeline.camera.is_open,
            "version": __version__,
            "total": pipeline.status()["total_count"],
            "camera_fps": pipeline.camera.read_fps,
        })

    @app.route("/api/status")
    @guard
    def api_status():
        return jsonify({"ok": True, "version": __version__, "max_rois": MAX_ROIS, **pipeline.status()})

    @app.route("/api/config")
    @guard
    def api_config():
        return jsonify({"ok": True, "config": cfg.as_dict(), "source": cfg.source_path})

    @app.route("/api/events")
    @guard
    def api_events():
        limit = _int_arg("limit", 100, 1, 2000)
        offset = _int_arg("offset", 0, 0, 100000)
        return jsonify({"ok": True, "events": pipeline.store.events(limit, offset)})

    @app.route("/api/events.csv")
    @guard
    def api_events_csv():
        limit = _int_arg("limit", 0, 0, 1000000)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return Response(
            pipeline.store.csv_stream(limit),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="airlock-events-%s.csv"' % stamp},
        )

    @app.route("/api/timeline")
    @guard
    def api_timeline():
        minutes = _int_arg("minutes", 180, 5, 86400)
        data = pipeline.store.timeline_series(time.time(), minutes)
        return jsonify({
            "ok": True,
            "requested_minutes": minutes,
            "minutes": data.get("minutes", minutes),
            "step_minutes": data.get("step_minutes", 1),
            "series": data.get("points", []),
        })

    @app.route("/api/signal")
    @guard
    def api_signal():
        roi = request.args.get("roi") or (cfg.rois[0].name if cfg.rois else "")
        limit = _int_arg("limit", 600, 10, 5000)
        return jsonify({
            "ok": True,
            "roi": roi,
            "available": cfg.roi_names(),
            "series": pipeline.store.signal_series(roi, limit),
            "thresholds": {
                "on": cfg.detector.on_threshold,
                "off": cfg.detector.off_threshold,
                "saturate": cfg.detector.saturate_ratio,
            },
        })

    @app.route("/api/snapshot.jpg")
    @guard
    def api_snapshot():
        scale = _float_arg("scale", 1.0, 0.1, 1.0)
        payload = pipeline.snapshot_jpeg(quality=88, scale=scale)
        if not payload:
            return jsonify({"ok": False, "error": "Кадр ещё не получен"}), 503
        return Response(payload, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.route("/stream")
    @guard
    def stream():
        return make_stream_response(pipeline, cfg)

    @app.route("/api/rois", methods=["POST"])
    @guard
    def api_set_rois():
        data = body()
        raw = data.get("rois")
        if not isinstance(raw, list):
            return jsonify({"ok": False, "error": "Ожидается список rois"}), 400
        if len(raw) > MAX_ROIS:
            return jsonify({"ok": False, "error": "Максимум %d зон" % MAX_ROIS}), 400

        bounds = _frame_bounds(pipeline)
        rois: List[Roi] = []
        seen = set()
        for idx, item in enumerate(raw):
            parsed, error = _parse_roi(item, idx, bounds, seen)
            if error:
                return jsonify({"ok": False, "error": error}), 400
            eff = cfg.detector.updated(parsed.detector)
            if eff.off_threshold >= eff.on_threshold:
                return jsonify({"ok": False, "error": "Зона '%s': off_threshold >= on_threshold" % parsed.name}), 400
            if eff.saturate_ratio <= eff.on_threshold:
                return jsonify({"ok": False, "error": "Зона '%s': saturate_ratio <= on_threshold" % parsed.name}), 400
            rois.append(parsed)
            seen.add(parsed.name)

        pipeline.set_rois(rois, persist=bool(data.get("persist", True)))
        return jsonify({"ok": True, "rois": [roi.as_dict() for roi in rois]})

    @app.route("/api/settings", methods=["POST"])
    @guard
    def api_settings():
        data = body()
        roi = data.get("roi")
        if roi is not None and str(roi) != "*":
            roi = str(roi)
            if roi not in cfg.roi_names():
                return jsonify({"ok": False, "error": "Неизвестная зона '%s'" % roi}), 404
        else:
            roi = None

        detector_patch = _num_patch(data.get("detector"), _DETECTOR_KEYS)
        vision_patch = _num_patch(data.get("vision"), _VISION_KEYS, _VISION_ENUM_KEYS, dict.fromkeys(_VISION_BOOL_KEYS))
        stall = None
        alerts = data.get("alerts")
        if isinstance(alerts, dict) and "stall_minutes" in alerts:
            stall = _to_float(alerts.get("stall_minutes"))
        if not detector_patch and not vision_patch and stall is None:
            return jsonify({"ok": False, "error": "Нечего применять"}), 400

        if detector_patch:
            base = pipeline.effective_params(roi)
            on = float(detector_patch.get("on_threshold", base.on_threshold))
            off = float(detector_patch.get("off_threshold", base.off_threshold))
            sat = float(detector_patch.get("saturate_ratio", base.saturate_ratio))
            if off >= on:
                return jsonify({"ok": False, "error": "off_threshold должен быть меньше on_threshold"}), 400
            if sat <= on:
                return jsonify({"ok": False, "error": "saturate_ratio должен быть больше on_threshold"}), 400

        try:
            applied = pipeline.update_params(
                detector_patch=detector_patch or None,
                vision_patch=vision_patch or None,
                stall_minutes=stall,
                roi=roi,
                persist=bool(data.get("persist", True)),
            )
        except KeyError:
            return jsonify({"ok": False, "error": "Неизвестная зона '%s'" % roi}), 404
        status = pipeline.status()
        zone_params = {r["name"]: r.get("params") for r in status["rois"]}
        return jsonify({
            "ok": True,
            "applied": applied,
            "roi": roi or "*",
            "params": status["params"],
            "zone_params": zone_params,
        })

    @app.route("/api/roi/params/reset", methods=["POST"])
    @guard
    def api_roi_params_reset():
        data = body()
        roi = str(data.get("roi") or "")
        if not roi:
            return jsonify({"ok": False, "error": "Не указана зона"}), 400
        done = pipeline.reset_roi_params(roi, persist=bool(data.get("persist", True)))
        return jsonify({"ok": True, "reset": done, "roi": roi})

    @app.route("/api/suggest", methods=["POST"])
    @guard
    def api_suggest():
        data = body()
        apply_now = bool(data.get("apply", False))
        roi = data.get("roi")
        by_roi = pipeline.suggest_per_roi()
        if not by_roi:
            return jsonify({"ok": False, "error": "Пока нет данных для подбора порогов"}), 409
        has_data = any(
            s.get("noise_samples") or s.get("peak_samples") for s in by_roi.values()
        )
        if not has_data:
            return jsonify({"ok": False, "error": "Пока нет данных для подбора порогов"}), 409
        applied: List[str] = []
        if apply_now:
            applied = pipeline.apply_suggestions(roi=None if roi in (None, "", "*") else str(roi))
        return jsonify({"ok": True, "by_roi": by_roi, "applied": applied})

    @app.route("/api/reset", methods=["POST"])
    @guard
    def api_reset():
        pipeline.reset_counters()
        return jsonify({"ok": True, "total_count": pipeline.status()["total_count"]})

    @app.route("/api/camera")
    @guard
    def api_camera():
        return jsonify({"ok": True, **pipeline.camera_capabilities(refresh=False)})

    @app.route("/api/camera/capabilities")
    @guard
    def api_camera_capabilities():
        return jsonify({"ok": True, **pipeline.camera_capabilities(refresh=True)})

    @app.route("/api/camera/apply", methods=["POST"])
    @guard
    def api_camera_apply():
        return _camera_update(save=False)

    @app.route("/api/camera/save", methods=["POST"])
    @guard
    def api_camera_save():
        return _camera_update(save=True)

    def _camera_update(save: bool):
        data = body()
        caps = pipeline.camera_capabilities(refresh=False)["capabilities"]
        controls = _camera_controls_from_body(data, caps)
        width = _to_int(data.get("width"))
        height = _to_int(data.get("height"))
        fps = _to_float(data.get("fps"))
        result = pipeline.apply_camera_settings(
            width=width, height=height, fps=fps, controls=controls, save=save)
        return jsonify({"ok": True, "saved": save, "config": result})

    def _int_arg(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(request.args.get(name, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def _float_arg(name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(request.args.get(name, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    register_public_routes(app, pipeline, cfg)
    return app


def make_stream_response(pipeline: Pipeline, cfg: AppConfig) -> Response:
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"

    def generate():
        interval = 1.0 / max(0.5, float(cfg.web.stream_fps))
        idle = 0
        try:
            while True:
                started = time.monotonic()
                image = pipeline.render_overlay(
                    scale=float(cfg.web.stream_scale),
                    show_labels=bool(cfg.web.show_labels),
                )
                if image is None:
                    idle += 1
                    if idle > 200:
                        break
                    time.sleep(0.1)
                    continue
                idle = 0
                payload = encode_jpeg(image, int(cfg.web.stream_quality))
                if payload:
                    yield boundary + payload + b"\r\n"
                spent = time.monotonic() - started
                if spent < interval:
                    time.sleep(interval - spent)
        except GeneratorExit:
            return

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def register_public_routes(app: Flask, pipeline: Pipeline, cfg: AppConfig) -> None:
    """Открытые read-only маршруты для внешних наблюдателей — без токена и без настроек."""

    def _minutes():
        try:
            value = int(request.args.get("minutes", 1440))
        except (TypeError, ValueError):
            value = 1440
        return max(5, min(86400, value))

    @app.route("/public")
    @app.route("/public/")
    def public_index():
        return send_from_directory(STATIC_DIR, "public.html")

    @app.route("/public/api/status")
    def public_api_status():
        return jsonify({"ok": True, **pipeline.public_status()})

    @app.route("/public/api/timeline")
    def public_api_timeline():
        minutes = _minutes()
        data = pipeline.store.timeline_series(time.time(), minutes)
        return jsonify({
            "ok": True,
            "minutes": data.get("minutes", minutes),
            "step_minutes": data.get("step_minutes", 1),
            "series": data.get("points", []),
        })

    @app.route("/public/stream")
    def public_stream():
        return make_stream_response(pipeline, cfg)


def create_public_app(pipeline: Pipeline) -> Flask:
    """Отдельное приложение только для публичного просмотра (для своего порта)."""
    app = Flask("airlock_public", static_folder=STATIC_DIR, static_url_path="/static")
    app.config["JSON_AS_ASCII"] = False
    cfg = pipeline.config
    register_public_routes(app, pipeline, cfg)

    @app.route("/")
    def root():
        return send_from_directory(STATIC_DIR, "public.html")

    @app.route("/health")
    def health():
        return jsonify({"ok": pipeline.camera.is_open, "public": True})

    return app


_DETECTOR_KEYS = {
    "on_threshold": (1e-6, 0.5),
    "off_threshold": (0.0, 0.5),
    "drop_ratio": (0.0, 1.0),
    "min_off_frames": (1, 60),
    "min_peak_frames": (1, 60),
    "refractory_s": (0.0, 10.0),
    "max_hump_s": (0.2, 120.0),
    "saturate_ratio": (0.01, 1.0),
    "saturate_cooldown_s": (0.0, 60.0),
}

_VISION_KEYS = {
    "mog2_history": (10, 5000),
    "mog2_threshold": (4.0, 400.0),
    "mog2_learning_rate": (-1.0, 1.0),
    "mean_alpha": (0.0005, 1.0),
    "mean_threshold": (1.0, 200.0),
    "despeckle": (0, 15),
    "process_scale": (0.05, 4.0),
    "warmup_frames": (0, 2000),
}

_VISION_ENUM_KEYS = {"background": ("mog2", "mean")}
_VISION_BOOL_KEYS = {"equalize"}


def _frame_bounds(pipeline: Pipeline) -> Tuple[int, int]:
    item = pipeline.latest_frame()
    if item is not None:
        height, width = item[0].shape[:2]
        return int(width), int(height)
    return int(pipeline.config.camera.width), int(pipeline.config.camera.height)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


EXPOSURE_MODE_VALUES = {"manual": 1, "auto": 3, "auto_once": 2}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "включен")
    return bool(value)


def _clamped_int(value: Any, rng: Dict[str, Any]) -> Optional[int]:
    number = _to_float(value)
    if number is None:
        return None
    return int(max(rng["min"], min(rng["max"], number)))


def _camera_controls_from_body(data: Dict[str, Any], caps: Dict[str, Any]) -> Dict[str, Any]:
    controls: Dict[str, Any] = {}
    mode = data.get("exposure_mode")
    if isinstance(mode, str) and mode in EXPOSURE_MODE_VALUES:
        controls["auto_exposure"] = EXPOSURE_MODE_VALUES[mode]
    for field, key in (("exposure", "exposure"), ("brightness", "brightness"), ("focus", "focus")):
        if field in data:
            value = _clamped_int(data.get(field), caps[key])
            if value is not None:
                controls[key] = value
    if "autofocus" in data:
        controls["autofocus"] = 1 if _truthy(data.get("autofocus")) else 0
    return controls


def _num_patch(
    raw: Any,
    allowed: Dict[str, Tuple[float, float]],
    enums: Optional[Dict[str, Tuple[str, ...]]] = None,
    bools: Optional[Dict[str, None]] = None,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, (low, high) in allowed.items():
        if key not in raw:
            continue
        value = _to_float(raw[key])
        if value is None:
            continue
        out[key] = max(low, min(high, value))
    for key, choices in (enums or {}).items():
        if key in raw and str(raw[key]) in choices:
            out[key] = str(raw[key])
    for key in (bools or {}):
        if key in raw:
            out[key] = bool(raw[key])
    return out


def _parse_roi(item: Any, idx: int, bounds: Tuple[int, int], seen: set) -> Tuple[Optional[Roi], Optional[str]]:
    if not isinstance(item, dict):
        return None, "rois[%d]: ожидается объект" % idx
    width, height = bounds
    name = str(item.get("name") or "zone-%d" % (idx + 1)).strip()[:40]
    if not name:
        name = "zone-%d" % (idx + 1)
    if name in seen:
        return None, "Имя зоны '%s' уже используется" % name
    coords = {}
    for key in ("x", "y", "w", "h"):
        value = _to_int(item.get(key))
        if value is None:
            return None, "rois[%d]: поле '%s' должно быть числом" % (idx, key)
        coords[key] = value
    if coords["w"] < 5 or coords["h"] < 5:
        return None, "Зона '%s' слишком маленькая (минимум 5x5)" % name
    if width > 0 and height > 0:
        coords["x"] = max(0, min(coords["x"], width - 5))
        coords["y"] = max(0, min(coords["y"], height - 5))
        coords["w"] = min(coords["w"], width - coords["x"])
        coords["h"] = min(coords["h"], height - coords["y"])
    detector = _num_patch(item.get("detector"), _DETECTOR_KEYS) if isinstance(item.get("detector"), dict) else {}
    return Roi(name=name, detector=detector, **coords), None


def make_server(app: Flask, host: str, port: int):
    """WSGI-сервер, который можно корректно остановить server.shutdown()."""
    from werkzeug.serving import make_server as _make_server

    return _make_server(host, port, app, threaded=True)


def serve(app: Flask, host: str, port: int, stop_event) -> None:
    """Крутит сервер в фоновом потоке, пока не взведут stop_event, затем чисто гасит его.

    Главный поток не блокируется в serve_forever(), поэтому SIGINT/SIGTERM
    могут разбудить его и завершить процесс без kill -9.
    """
    import threading

    server = make_server(app, host, port)
    worker = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        name="web",
        daemon=True,
    )
    worker.start()
    try:
        while not stop_event.is_set():
            stop_event.wait(0.25)
    finally:
        server.shutdown()
        worker.join(timeout=5.0)
