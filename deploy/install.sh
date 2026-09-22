#!/usr/bin/env bash
#
# Установка счётчика бульков на Raspberry Pi OS (Pi 3 / Zero 2 / Pi 4).
#
#   ./deploy/install.sh                 # в /opt/airlock + systemd-сервис
#   ./deploy/install.sh --dir ~/airlock # в домашний каталог
#   ./deploy/install.sh --no-service    # только зависимости, без systemd
#
set -euo pipefail

INSTALL_DIR="/opt/airlock"
SERVICE_NAME="airlock"
WITH_SERVICE=1
SUDO=""

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)        INSTALL_DIR="$2"; shift 2 ;;
    --dir=*)      INSTALL_DIR="${1#*=}"; shift ;;
    --name)       SERVICE_NAME="$2"; shift 2 ;;
    --no-service) WITH_SERVICE=0; shift ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *)            die "Неизвестный аргумент: $1" ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null || die "Нужен root или sudo"
  SUDO="sudo"
fi

RUN_USER="${SUDO_USER:-$(id -un)}"
if [[ $RUN_USER == "root" && -n ${HOME:-} && ${HOME} != "/root" ]]; then
  RUN_USER="$(basename "$HOME")"
fi
RUN_GROUP="$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")"

# Выполнить команду от имени целевого пользователя независимо от того,
# запущен скрипт через sudo (SUDO непустой) или сразу под root (SUDO пустой).
as_user() {
  if [[ -n $SUDO ]]; then
    sudo -u "$RUN_USER" "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$RUN_USER" -- "$@"
  else
    su -s /bin/bash "$RUN_USER" -c "$(printf '%q ' "$@")"
  fi
}

command -v python3 >/dev/null || die "python3 не найден: sudo apt install python3"
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "Python: $(python3 -V 2>&1) (нужно 3.9+)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
  || die "Нужен Python 3.9 или новее"

log "Ставлю системные пакеты (opencv собран под armv7l/arm64, компиляция не потребуется)"
$SUDO apt-get update -qq
$SUDO apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev \
  python3-opencv python3-numpy python3-yaml python3-flask \
  v4l-utils

log "Каталог установки: $INSTALL_DIR"
$SUDO mkdir -p "$INSTALL_DIR/data"
$SUDO cp -r "$SRC_DIR/airlock" "$SRC_DIR/config.yaml" "$SRC_DIR/README.md" "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
$SUDO chown -R "$RUN_USER:$RUN_GROUP" "$INSTALL_DIR"

log "Создаю virtualenv с доступом к системному OpenCV"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
  as_user python3 -m venv --system-site-packages "$INSTALL_DIR/venv" \
    || die "Не удалось создать virtualenv в $INSTALL_DIR/venv"
fi
PYTHON_BIN="$INSTALL_DIR/venv/bin/python"
as_user "$PYTHON_BIN" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true

log "Проверяю импорты"
$PYTHON_BIN - <<'PY'
import cv2, numpy, flask, yaml
print("  cv2 %s | numpy %s | flask %s" % (cv2.__version__, numpy.__version__, flask.__version__))
PY

$SUDO chown -R "$RUN_USER:$RUN_GROUP" "$INSTALL_DIR"

log "Проверяю камеру"
if ls /dev/video* >/dev/null 2>&1; then
  for dev in /dev/video*; do
    name="$(cat "/sys/class/video4linux/$(basename "$dev")/name" 2>/dev/null || echo '?')"
    printf '  %s  %s\n' "$dev" "$name"
  done
else
  warn "/dev/video* не найдено — подключите USB-камеру и перезапустите сервис"
fi

if [[ $WITH_SERVICE -eq 1 ]]; then
  log "Настраиваю systemd-сервис $SERVICE_NAME"
  UNIT="$(mktemp)"
  sed -e "s|__WORKDIR__|$INSTALL_DIR|g" \
      -e "s|__PYTHON__|$PYTHON_BIN|g" \
      -e "s|__USER__|$RUN_USER|g" \
      -e "s|__GROUP__|$RUN_GROUP|g" \
      "$SRC_DIR/deploy/airlock.service" > "$UNIT"
  $SUDO cp "$UNIT" "/etc/systemd/system/$SERVICE_NAME.service"
  rm -f "$UNIT"
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 3
  $SUDO systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true

  PORT="$(grep -E '^\s*port:' "$INSTALL_DIR/config.yaml" | head -1 | grep -oE '[0-9]+' || echo 8080)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  log "Готово. Веб-интерфейс: http://${IP:-<ip-pi>}:$PORT"
  echo
  echo "Полезные команды:"
  echo "  journalctl -u $SERVICE_NAME -f          # живой лог"
  echo "  sudo systemctl restart $SERVICE_NAME    # перезапуск"
  echo "  $PYTHON_BIN -m airlock doctor           # диагностика камеры"
  echo "  $PYTHON_BIN -m airlock calibrate --seconds 60 --apply"
else
  log "Готово (без systemd). Запуск:"
  echo "  cd $INSTALL_DIR && $PYTHON_BIN -m airlock run"
fi
