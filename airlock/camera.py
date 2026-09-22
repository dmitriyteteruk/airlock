from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np

log = logging.getLogger("airlock.camera")

BACKENDS = {
    "auto": None,
    "v4l2": getattr(cv2, "CAP_V4L2", None),
    "any": cv2.CAP_ANY,
    "dshow": getattr(cv2, "CAP_DSHOW", None),
    "msmf": getattr(cv2, "CAP_MSMF", None),
}


def _control_props() -> Dict[str, Any]:
    mapping = {
        "brightness": "CAP_PROP_BRIGHTNESS",
        "contrast": "CAP_PROP_CONTRAST",
        "saturation": "CAP_PROP_SATURATION",
        "sharpness": "CAP_PROP_SHARPNESS",
        "gain": "CAP_PROP_GAIN",
        "exposure": "CAP_PROP_EXPOSURE",
        "auto_exposure": "CAP_PROP_AUTO_EXPOSURE",
        "autofocus": "CAP_PROP_AUTOFOCUS",
        "focus": "CAP_PROP_FOCUS",
        "zoom": "CAP_PROP_ZOOM",
        "white_balance": "CAP_PROP_WB_TEMPERATURE",
        "auto_white_balance": "CAP_PROP_AUTO_WB",
        "backlight": "CAP_PROP_BACKLIGHT",
        "hue": "CAP_PROP_HUE",
        # Алиасы под имена v4l2-ctl, чтобы ключи в конфиге совпадали с --list-ctrls
        "exposure_time_absolute": "CAP_PROP_EXPOSURE",
        "focus_absolute": "CAP_PROP_FOCUS",
        "focus_automatic_continuous": "CAP_PROP_AUTOFOCUS",
        "zoom_absolute": "CAP_PROP_ZOOM",
        "white_balance_temperature": "CAP_PROP_WB_TEMPERATURE",
        "white_balance_automatic": "CAP_PROP_AUTO_WB",
        "backlight_compensation": "CAP_PROP_BACKLIGHT",
    }
    out: Dict[str, Any] = {}
    for key, attr in mapping.items():
        value = getattr(cv2, attr, None)
        if value is not None:
            out[key] = value
    return out


CONTROLS = _control_props()

# Порядок применения важен: автоматику переключаем в ручную до значений, которые она перезаписывает.
CONTROL_ORDER = (
    "auto_exposure", "exposure", "exposure_time_absolute",
    "autofocus", "focus_automatic_continuous", "focus", "focus_absolute",
    "brightness", "contrast", "saturation", "sharpness", "hue",
    "auto_white_balance", "white_balance_automatic", "white_balance", "white_balance_temperature",
    "backlight", "backlight_compensation", "gain", "zoom", "zoom_absolute",
)


def apply_controls(cap: cv2.VideoCapture, controls: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """Задаёт свойства камеры в безопасном порядке. Возвращает имя -> удалось ли."""
    result: Dict[str, bool] = {}
    if not controls:
        return result
    ordered = [k for k in CONTROL_ORDER if k in controls]
    ordered += [k for k in controls if k not in CONTROL_ORDER]
    for name in ordered:
        if name in result:
            continue
        prop = CONTROLS.get(name)
        if prop is None:
            log.warning("Неизвестное свойство камеры '%s' — пропущено", name)
            result[name] = False
            continue
        ok = _set(cap, prop, controls[name])
        result[name] = ok
        if not ok:
            log.debug("Не удалось задать %s=%s (камера не поддерживает)", name, controls[name])
    return result


def device_path(device: Union[int, str]) -> Optional[str]:
    if isinstance(device, str) and device.startswith("/dev/"):
        return device
    try:
        return "/dev/video%d" % int(device)
    except (TypeError, ValueError):
        return None


_CONTROL_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\((\w+)\)\s*:\s*(.*)$")
_ATTR = re.compile(r"(min|max|step|default|value)=(-?\d+)")


def v4l2_available() -> bool:
    return platform.system() == "Linux" and bool(shutil.which("v4l2-ctl"))


def probe_v4l2_controls(device: Union[int, str]) -> Dict[str, Dict[str, Any]]:
    """Разбирает `v4l2-ctl -d <dev> --list-ctrls` в {имя: {type,min,max,step,default,value}}."""
    path = device_path(device)
    if not path or not shutil.which("v4l2-ctl"):
        return {}
    try:
        proc = subprocess.run(["v4l2-ctl", "-d", path, "--list-ctrls"],
                              capture_output=True, text=True, timeout=6)
        out = proc.stdout or ""
    except Exception as exc:  # noqa: BLE001
        log.debug("v4l2-ctl --list-ctrls не удался: %s", exc)
        return {}
    controls: Dict[str, Dict[str, Any]] = {}
    for line in out.splitlines():
        m = _CONTROL_LINE.match(line)
        if not m:
            continue
        name, ctype, rest = m.group(1), m.group(2), m.group(3)
        info: Dict[str, Any] = {"type": ctype}
        for key, val in _ATTR.findall(rest):
            info[key] = int(val)
        if ctype == "menu":
            lm = re.search(r"\(([^)]*)\)\s*$", rest)
            if lm:
                info["current_label"] = lm.group(1)
        controls[name] = info
    return controls


def probe_v4l2_menu(device: Union[int, str], control: str) -> Dict[int, str]:
    """Меню-значения контрола (например auto_exposure) через `v4l2-ctl -l <control>`."""
    path = device_path(device)
    if not path or not shutil.which("v4l2-ctl"):
        return {}
    try:
        proc = subprocess.run(["v4l2-ctl", "-d", path, "-l", control],
                              capture_output=True, text=True, timeout=6)
        out = proc.stdout or ""
    except Exception:  # noqa: BLE001
        return {}
    items: Dict[int, str] = {}
    for m in re.finditer(r"(\d+)=([^\n]+)", out):
        items[int(m.group(1))] = m.group(2).strip()
    return items


def describe_capabilities(controls: Dict[str, Dict[str, Any]]) -> List[str]:
    lines = []
    for name, info in controls.items():
        rng = ""
        if "min" in info and "max" in info:
            rng = " [%s..%s]" % (info["min"], info["max"])
        lines.append("  %-28s %-5s%s default=%s value=%s"
                     % (name, info.get("type", "?"), rng, info.get("default", "-"), info.get("value", "-")))
    return lines


@dataclass
class CameraConfig:
    device: Union[int, str] = 0
    width: int = 640
    height: int = 480
    fps: float = 30.0
    fourcc: str = "MJPG"
    buffer_size: int = 1
    backend: str = "auto"
    warmup_frames: int = 20
    reconnect_delay_s: float = 2.0
    max_reconnect_tries: int = 0
    controls: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data


def resolve_backend(name: str) -> int:
    if name and name != "auto":
        backend = BACKENDS.get(name.lower())
        if backend is not None:
            return backend
        log.warning("Неизвестный backend '%s', использую auto", name)
    if platform.system() == "Linux":
        return getattr(cv2, "CAP_V4L2", cv2.CAP_ANY)
    if platform.system() == "Windows":
        return getattr(cv2, "CAP_DSHOW", cv2.CAP_ANY)
    return cv2.CAP_ANY


class Camera(threading.Thread):
    """Поток захвата: постоянно вычитывает кадры, наружу отдаёт только самый свежий."""

    daemon = True

    def __init__(self, cfg: CameraConfig) -> None:
        super().__init__(name="camera")
        self.cfg = cfg
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._ts = 0.0
        self._index = 0
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._start_lock = threading.Lock()
        self._launch_requested = False
        self._ctrl_lock = threading.Lock()
        self._pending_controls: Dict[str, Any] = {}
        self._apply_pending = threading.Event()
        self._reconfigure = threading.Event()
        self._reconfigured = threading.Event()
        self.error: str = ""
        self.actual: Dict[str, Any] = {}
        self.read_fps = 0.0
        self.reconnects = 0
        self.caps: Dict[str, Dict[str, Any]] = {}
        self.menus: Dict[str, Dict[int, str]] = {}

    def stop(self) -> None:
        self._stop.set()

    def ensure_started(self) -> None:
        """Идемпотентный запуск потока: повторный вызов ничего не делает."""
        with self._start_lock:
            if self._launch_requested:
                return
            self._launch_requested = True
        self.start()

    def wait_ready(self, timeout: float = 15.0) -> bool:
        return self._opened.wait(timeout)

    def start_and_wait(self, timeout: float = 15.0) -> bool:
        self.ensure_started()
        return self.wait_ready(timeout)

    def latest(self) -> Optional[Tuple[np.ndarray, float, int]]:
        with self._lock:
            if self._frame is None:
                return None
            return self._frame, self._ts, self._index

    @property
    def is_open(self) -> bool:
        return self._opened.is_set() and not self.error

    def _open(self) -> Optional[cv2.VideoCapture]:
        cfg = self.cfg
        device: Union[int, str] = cfg.device
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        backend = resolve_backend(cfg.backend)
        cap = cv2.VideoCapture(device, backend) if backend is not None else cv2.VideoCapture(device)
        if not cap.isOpened():
            cap.release()
            self.error = "Не удалось открыть камеру %s (backend=%s)" % (cfg.device, cfg.backend)
            log.error(self.error)
            return None

        self.error = ""
        _set(cap, cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        _set(cap, cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        _set(cap, cv2.CAP_PROP_FPS, cfg.fps)
        _set(cap, cv2.CAP_PROP_BUFFERSIZE, cfg.buffer_size)

        if cfg.fourcc and len(cfg.fourcc) == 4:
            fourcc = cv2.VideoWriter_fourcc(*cfg.fourcc)
            _set(cap, cv2.CAP_PROP_FOURCC, fourcc)

        apply_controls(cap, cfg.controls)

        if not self.caps and v4l2_available():
            self.caps = probe_v4l2_controls(cfg.device)
            for menu_ctrl in ("auto_exposure",):
                if menu_ctrl in self.caps:
                    self.menus[menu_ctrl] = probe_v4l2_menu(cfg.device, menu_ctrl)
            if self.caps:
                log.info("Возможности камеры (%s): %d контролов",
                         device_path(cfg.device), len(self.caps))
                for line in describe_capabilities(self.caps):
                    log.info("  %s", line.strip())

        self.actual = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(float(cap.get(cv2.CAP_PROP_FPS) or 0.0), 2),
            "fourcc": _fourcc_name(cap),
            "backend": cfg.backend,
            "device": str(cfg.device),
        }
        log.info("Камера открыта: %s", self.actual)

        for _ in range(max(0, int(cfg.warmup_frames))):
            cap.grab()
        return cap

    def run(self) -> None:
        tries = 0
        limit = int(self.cfg.max_reconnect_tries)
        while not self._stop.is_set():
            self._reconfigured.clear()
            cap = self._open()
            if cap is None:
                tries += 1
                self._opened.set()
                if limit and tries > limit:
                    log.error("Превышено число попыток подключения к камере")
                    return
                self._stop.wait(self.cfg.reconnect_delay_s)
                continue

            tries = 0
            failures = 0
            first_frame = True
            fps_window: list = []
            reconfigure = False
            while not self._stop.is_set():
                if self._reconfigure.is_set():
                    self._reconfigure.clear()
                    reconfigure = True
                    break
                if self._apply_pending.is_set():
                    self._apply_pending.clear()
                    with self._ctrl_lock:
                        pending = self._pending_controls
                        self._pending_controls = {}
                    apply_controls(cap, pending)

                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= 15:
                        self.error = "Камера перестала отдавать кадры"
                        log.error(self.error)
                        break
                    time.sleep(0.02)
                    continue
                failures = 0
                now = time.time()
                with self._lock:
                    self._index += 1
                    self._frame = frame
                    self._ts = now
                self._opened.set()
                if first_frame:
                    first_frame = False
                    self._reconfigured.set()

                fps_window.append(now)
                cutoff = now - 1.0
                while fps_window and fps_window[0] < cutoff:
                    fps_window.pop(0)
                self.read_fps = round(float(len(fps_window)), 1)

            cap.release()
            if self._stop.is_set():
                break
            if reconfigure:
                log.info("Камера перенастроена, переоткрываю с новыми параметрами")
                continue
            self.reconnects += 1
            log.warning("Переподключаюсь к камере через %.1f с", self.cfg.reconnect_delay_s)
            self._stop.wait(self.cfg.reconnect_delay_s)

        self._opened.set()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            age = (time.time() - self._ts) if self._ts else None
            frames = self._index
        return {
            "ok": self.is_open and self._frame is not None,
            "error": self.error,
            "read_fps": self.read_fps,
            "frames": frames,
            "reconnects": self.reconnects,
            "last_frame_age_s": round(age, 3) if age is not None else None,
            **self.actual,
        }

    def set_controls_live(self, controls: Dict[str, Any]) -> None:
        """Меняет свойства камеры на лету (без переоткрытия) и обновляет cfg для будущих реоткрытий."""
        if not controls:
            return
        with self._ctrl_lock:
            self._pending_controls.update(controls)
            self.cfg.controls.update(controls)
        self._apply_pending.set()

    def reconfigure(self, width: Optional[int] = None, height: Optional[int] = None,
                    fps: Optional[float] = None, fourcc: Optional[str] = None) -> None:
        changed = False
        if width:
            self.cfg.width = int(width); changed = True
        if height:
            self.cfg.height = int(height); changed = True
        if fps:
            self.cfg.fps = float(fps); changed = True
        if fourcc:
            self.cfg.fourcc = str(fourcc); changed = True
        if changed:
            self._reconfigured.clear()
            self._reconfigure.set()

    def wait_reconfigured(self, timeout: float = 8.0) -> bool:
        return self._reconfigured.wait(timeout)

    def capabilities(self) -> Dict[str, Any]:
        return {
            "device": str(self.cfg.device),
            "path": device_path(self.cfg.device),
            "available": bool(self.caps),
            "controls": self.caps,
            "menus": {k: {str(v): lbl for v, lbl in m.items()} for k, m in self.menus.items()},
        }

    def probe(self) -> Dict[str, Any]:
        """Свежий проброс возможностей через v4l2-ctl (для веб-страницы настроек)."""
        if v4l2_available():
            self.caps = probe_v4l2_controls(self.cfg.device)
            for menu_ctrl in ("auto_exposure",):
                if menu_ctrl in self.caps:
                    self.menus[menu_ctrl] = probe_v4l2_menu(self.cfg.device, menu_ctrl)
        return self.capabilities()


def _set(cap: cv2.VideoCapture, prop: Any, value: Any) -> bool:
    try:
        return bool(cap.set(prop, value))
    except Exception as exc:  # noqa: BLE001 - драйверы V4L2 любят кидаться
        log.debug("cap.set(%s) failed: %s", prop, exc)
        return False


def _fourcc_name(cap: cv2.VideoCapture) -> str:
    try:
        raw = int(cap.get(cv2.CAP_PROP_FOURCC))
    except Exception:  # noqa: BLE001
        return ""
    if raw <= 0:
        return ""
    chars = [(raw >> (8 * i)) & 0xFF for i in range(4)]
    return "".join(chr(c) for c in chars if 32 <= c < 127)


def list_v4l2_devices() -> list:
    import glob
    import os

    devices = sorted(glob.glob("/dev/video*"))
    out = []
    for path in devices:
        info: Dict[str, Any] = {"path": path, "name": "", "caps": ""}
        name_path = os.path.join("/sys/class/video4linux", os.path.basename(path), "name")
        if os.path.exists(name_path):
            try:
                with open(name_path, "r", encoding="utf-8", errors="replace") as fh:
                    info["name"] = fh.read().strip()
            except OSError:
                pass
        out.append(info)
    return out
