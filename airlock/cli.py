from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .config import AppConfig, ConfigError, save_overrides

log = logging.getLogger("airlock")

SPARK = "▁▂▃▄▅▆▇█"


def _common_flags() -> argparse.ArgumentParser:
    """Одни и те же флаги и до, и после имени подкоманды."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="путь к config.yaml")
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help="каталог для журнала и состояния")
    common.add_argument("--device", default=argparse.SUPPRESS, help="индекс камеры или /dev/videoN")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="подробный лог")
    common.add_argument("--log-level", default=argparse.SUPPRESS, help="DEBUG|INFO|WARNING|ERROR")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airlock",
        description="Счётчик бульков гидрозатвора по USB-веб-камере (Raspberry Pi 3).",
    )
    parser.add_argument("--version", action="version", version="airlock %s" % __version__)
    parser.add_argument("-c", "--config", default=None, help="путь к config.yaml")
    parser.add_argument("--data-dir", default=None, help="каталог для журнала и состояния")
    parser.add_argument("--device", default=None, help="индекс камеры или /dev/videoN")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")

    common = _common_flags()
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="запустить подсчёт и веб-интерфейс")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--public-port", type=int, default=None,
                     help="отдельный порт только для публичного просмотра (напр. 8081)")
    run.add_argument("--no-web", action="store_true", help="только подсчёт в лог")
    run.add_argument("--token", default=None, help="токен доступа к веб-интерфейсу")

    doctor = sub.add_parser("doctor", parents=[common], help="проверить камеру и зоны")
    doctor.add_argument("--seconds", type=float, default=5.0)
    doctor.add_argument("--out", default=None, help="куда сохранить контрольный кадр")

    snap = sub.add_parser("snapshot", parents=[common], help="сохранить один кадр с камеры")
    snap.add_argument("--out", default="snapshot.jpg")
    snap.add_argument("--delay", type=float, default=1.0)

    calib = sub.add_parser("calibrate", parents=[common], help="записать сигнал и предложить пороги")
    calib.add_argument("--seconds", type=float, default=60.0)
    calib.add_argument("--roi", default=None, help="x,y,w,h — временная зона вместо конфигурации")
    calib.add_argument("--apply", action="store_true", help="сохранить предложенные пороги")

    events = sub.add_parser("events", parents=[common], help="показать последние события из журнала")
    events.add_argument("--limit", type=int, default=30)

    return parser


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.INFO
    if args.log_level:
        level = getattr(logging, args.log_level.upper(), logging.INFO)
    elif args.verbose:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def default_config_path() -> Optional[str]:
    for candidate in ("config.yaml", os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")):
        if os.path.exists(candidate):
            return candidate
    return None


def load_config(args: argparse.Namespace, device: Optional[str] = None, data_dir: Optional[str] = None) -> AppConfig:
    extra: Dict[str, Any] = {}
    target_dir = data_dir or getattr(args, "data_dir", None)
    if target_dir:
        extra["storage"] = {"data_dir": target_dir}
    dev = device or getattr(args, "device", None)
    if dev:
        extra.setdefault("camera", {})["device"] = int(dev) if str(dev).isdigit() else dev
    path = args.config or default_config_path()
    return AppConfig.load(path, extra=extra or None)


def _override_from_args(args: argparse.Namespace, config: AppConfig) -> AppConfig:
    if getattr(args, "host", None):
        config.web.host = args.host
    if getattr(args, "port", None):
        config.web.port = int(args.port)
    if getattr(args, "public_port", None):
        config.web.public_port = int(args.public_port)
    if getattr(args, "token", None):
        config.web.token = args.token
    if getattr(args, "data_dir", None):
        config.storage.data_dir = args.data_dir
    return config


def _log_camera_capabilities(pipeline) -> None:
    """Проверка функций камеры через v4l2-ctl при запуске сервиса."""
    try:
        caps = pipeline.camera_capabilities(refresh=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось опросить камеру: %s", exc)
        return
    meta = caps["capabilities"]
    raw = caps["raw"].get("controls", {})
    if not meta.get("has_v4l2"):
        log.warning("v4l2-ctl недоступен (нужен пакет v4l-utils) — использую диапазоны по умолчанию")
        return
    log.info("v4l2-ctl: камера поддерживает %d контрол(ов): %s", len(raw), ", ".join(sorted(raw)) or "—")
    for key, title in (("exposure", "экспозиция"), ("brightness", "яркость"), ("focus", "фокус")):
        r = meta[key]
        src = "камера" if r.get("detected") else "по умолчанию"
        log.info("  %-11s %s..%s (шаг %s, по умолчанию %s) [%s]",
                 title, r["min"], r["max"], r["step"], r["default"], src)
    modes = ", ".join(m["label"] for m in meta["exposure_modes"])
    log.info("  экспозиция режимы: %s", modes)


def cmd_run(args: argparse.Namespace) -> int:
    import threading

    from .pipeline import Pipeline

    config = _override_from_args(args, load_config(args))
    pipeline = Pipeline(config)

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        if stop_event.is_set():
            return
        log.info("Получен сигнал %s, останавливаюсь", signum)
        stop_event.set()
        pipeline.stop()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass

    pipeline.start()
    if not pipeline.camera.wait_ready(timeout=20.0):
        log.error("Камера не ответила за 20 секунд")

    _log_camera_capabilities(pipeline)

    if not config.rois:
        log.warning(
            "Зоны не заданы — откройте веб-интерфейс и выделите гидрозатвор. "
            "Подсчёт начнётся автоматически после сохранения зоны."
        )

    if args.no_web or not config.web.enabled:
        log.info("Работа без веб-интерфейса. Ctrl+C для остановки.")
        try:
            while not stop_event.is_set():
                stop_event.wait(5.0)
                if stop_event.is_set():
                    break
                status = pipeline.status()
                log.info(
                    "всего=%d за минуту=%.1f последний бульк %s с назад fps=%.1f",
                    status["total_count"],
                    status["rates"]["bpm_1m"],
                    status["last_event_age_s"],
                    status["app"]["process_fps"],
                )
        finally:
            pipeline.shutdown()
        return 0

    from .web import create_app, create_public_app, serve

    app = create_app(pipeline)
    host = config.web.host
    port = config.web.port
    public_port = int(getattr(config.web, "public_port", 0) or 0)
    shown = host if host not in ("0.0.0.0", "::") else _local_ip()
    if config.web.token:
        log.info("Доступ по токену — он потребуется при первом открытии страницы")

    if public_port and public_port != port:
        try:
            public_app = create_public_app(pipeline)
            threading.Thread(
                target=serve, args=(public_app, host, public_port, stop_event),
                name="public-web", daemon=True).start()
            log.info("Публичная страница (только просмотр): http://%s:%d/public  и  http://%s:%d",
                     shown, port, shown, public_port)
        except OSError as exc:
            log.error("Не удалось поднять публичный сервер на %d — %s", public_port, exc)
    else:
        log.info("Публичная страница (только просмотр): http://%s:%d/public", shown, port)

    try:
        log.info("Веб-интерфейс: http://%s:%d  (Ctrl+C для остановки)", shown, port)
        serve(app, host, port, stop_event)
    except OSError as exc:
        log.error("Не удалось поднять веб-сервер на %s:%d — %s", host, port, exc)
        pipeline.shutdown()
        return 1
    finally:
        log.info("Останавливаю обработку...")
        pipeline.shutdown()
        log.info("Остановлено")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .camera import Camera, list_v4l2_devices

    config = load_config(args, device=args.device)
    if sys.platform.startswith("linux"):
        devices = list_v4l2_devices()
        print("Устройства V4L2:")
        if not devices:
            print("  /dev/video* не найдены")
        for item in devices:
            print("  %(path)s  %(name)s" % item)
        print()

    camera = Camera(config.camera)
    print("Открываю камеру %s ..." % config.camera.device)
    if not camera.start_and_wait(timeout=15.0):
        print("ОШИБКА: камера не ответила за 15 секунд")
        camera.stop()
        camera.join(timeout=3)
        return 1

    time.sleep(1.0)
    status = camera.status()
    print("Камера: %(ok)s  %(width)sx%(height)s  %(fps)s к/с  fourcc=%(fourcc)s" % status)
    print("Чтение: %s к/с, кадров получено: %s" % (status["read_fps"], status["frames"]))
    if status.get("error"):
        print("Ошибка: %s" % status["error"])

    item = camera.latest()
    if item is None:
        print("ОШИБКА: кадр не получен")
        camera.stop()
        return 1

    frame = item[0]
    out = args.out or os.path.join(config.storage.data_dir, "doctor.jpg")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    from .vision import encode_jpeg

    with open(out, "wb") as fh:
        fh.write(encode_jpeg(frame, 92))
    print("Контрольный кадр сохранён: %s (%dx%d)" % (out, frame.shape[1], frame.shape[0]))

    height, width = frame.shape[:2]
    if not config.rois:
        print("Зоны контроля не заданы. Откройте веб-интерфейс и обведите гидрозатвор.")
    for roi in config.rois:
        inside = 0 <= roi.x < width and 0 <= roi.y < height and roi.x + roi.w <= width and roi.y + roi.h <= height
        print(
            "  зона '%s': %dx%d в (%d,%d) — %s"
            % (roi.name, roi.w, roi.h, roi.x, roi.y, "в кадре" if inside else "ВНЕ КАДРА, поправьте координаты")
        )

    if args.seconds > 0:
        print("Наблюдаю %.0f с за движением (порог on_threshold=%.5f)..." % (args.seconds, config.detector.on_threshold))
        from .detector import BubbleDetector
        from .vision import Roi, RoiProcessor, to_gray

        rois = config.rois or [Roi("full-frame", 0, 0, width, height)]
        processors = {r.name: RoiProcessor(r, config.vision) for r in rois}
        detectors = {r.name: BubbleDetector(r.name, config.detector) for r in rois}
        for det in detectors.values():
            det.set_warmup(config.vision.warmup_frames)
        deadline = time.time() + args.seconds
        last_index = -1
        peak_motion = {name: 0.0 for name in detectors}
        while time.time() < deadline:
            grabbed = camera.latest()
            if grabbed is None or grabbed[2] == last_index:
                time.sleep(0.01)
                continue
            last_index = grabbed[2]
            gray = to_gray(grabbed[0])
            for roi in rois:
                motion = processors[roi.name].motion(gray)
                peak_motion[roi.name] = max(peak_motion[roi.name], motion)
                detectors[roi.name].update(motion, time.time())
        for name, det in detectors.items():
            print("  %-16s бульков: %-5d пик сигнала: %.5f" % (name, det.count, peak_motion[name]))

    camera.stop()
    camera.join(timeout=3)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from .camera import Camera
    from .vision import encode_jpeg

    config = load_config(args, device=args.device)
    camera = Camera(config.camera)
    if not camera.start_and_wait(timeout=15.0):
        print("ОШИБКА: камера не ответила")
        camera.stop()
        return 1
    time.sleep(max(0.0, args.delay))
    item = camera.latest()
    camera.stop()
    camera.join(timeout=3)
    if item is None:
        print("ОШИБКА: кадр не получен")
        return 1
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(encode_jpeg(item[0], 92))
    print("Сохранено: %s (%dx%d)" % (out, item[0].shape[1], item[0].shape[0]))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from .detector import BubbleDetector
    from .pipeline import Pipeline
    from .vision import Roi

    config = load_config(args, device=args.device)
    rois: List[Roi] = list(config.rois)
    if args.roi:
        try:
            x, y, w, h = [int(float(v)) for v in args.roi.split(",")]
        except ValueError:
            print("Неверный формат --roi, ожидается x,y,w,h")
            return 2
        rois = [Roi("calib", x, y, w, h)]
    if not rois:
        print("Зоны не заданы — калибрую по всему кадру. Потом уточните зону и повторите.")
        rois = [Roi("full-frame", 0, 0, int(config.camera.width), int(config.camera.height))]
    config.rois = rois

    pipeline = Pipeline(config)
    pipeline.start()
    if not pipeline.camera.wait_ready(timeout=15.0):
        print("ОШИБКА: камера не ответила")
        pipeline.shutdown()
        return 1

    print("Запись %.0f с. Не трогайте камеру и не булькайте принудительно — пусть идёт естественный процесс." % args.seconds)
    samples: List[float] = []
    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            for name in config.roi_names():
                samples.extend(v for _, v in pipeline.store.signal_series(name, 4))
            left = int(deadline - time.time())
            if left % 10 == 0 and left > 0:
                sys.stderr.write("\rосталось %d с   " % left)
                sys.stderr.flush()
    except KeyboardInterrupt:
        print("\nПрервано")
    finally:
        sys.stderr.write("\r" + " " * 24 + "\r")
        pipeline.shutdown()

    suggestion = pipeline.suggest_thresholds()
    for info in pipeline.status()["rois"]:
        print("  зона '%s': насчитано бульков %d, помех %d"
              % (info["name"], info["count"], info["disturbances"]))

    print()
    print("Шум фона   p99=%.6f  max=%.6f  (%d отсчётов)"
          % (suggestion["noise_p99"], suggestion["noise_max"], suggestion["noise_samples"]))
    print("Пики       p05=%.6f  медиана=%.6f  max=%.6f  (%d событий)"
          % (suggestion["peak_p05"], suggestion["peak_median"], suggestion["peak_max"], suggestion["peak_samples"]))
    print("Рекомендуется: on_threshold=%.6f  off_threshold=%.6f"
          % (suggestion["on_threshold"], suggestion["off_threshold"]))

    if samples:
        print()
        print("Сигнал движения (последние %d отсчётов):" % min(len(samples), 240))
        print(_sparkline(samples[-240:]))

    if args.apply:
        if not config.overrides_path:
            print("Не определён каталог данных — сохранить некуда")
            return 1
        save_overrides(config.overrides_path, {
            "detector": {
                "on_threshold": suggestion["on_threshold"],
                "off_threshold": suggestion["off_threshold"],
            }
        })
        print("Пороги сохранены в %s" % config.overrides_path)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    config = load_config(args)
    path = os.path.join(config.storage.data_dir, config.storage.events_file)
    if not os.path.exists(path):
        print("Журнал пуст: %s" % path)
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    print("Всего записей: %d (показаны последние %d)" % (len(lines), args.limit))
    for line in lines[-args.limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        print("%s  %-14s #%d  peak=%.5f dur=%.2fs"
              % (row.get("wall_time", "?"), row.get("roi", "?"), row.get("id", 0),
                 row.get("peak", 0.0), row.get("duration_s", 0.0)))
    return 0


def _sparkline(values: List[float]) -> str:
    if not values:
        return ""
    top = max(values) or 1.0
    return "".join(SPARK[min(len(SPARK) - 1, int(v / top * (len(SPARK) - 1)))] for v in values)


def _local_ip() -> str:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


COMMANDS = {
    "run": cmd_run,
    "doctor": cmd_doctor,
    "snapshot": cmd_snapshot,
    "calibrate": cmd_calibrate,
    "events": cmd_events,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args)
    command = args.command or "run"
    handler = COMMANDS.get(command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 2
    except ImportError as exc:
        log.error("Не хватает зависимости: %s", exc)
        log.error("Установите: sudo apt install python3-opencv python3-numpy python3-yaml python3-flask")
        return 3
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
        return 0


if __name__ == "__main__":
    sys.exit(main())
