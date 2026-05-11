#!/bin/sh
set -eu

APP_DIR="/oem/smart-gw/m101_scene_change"
SERVICE_FILE="/etc/systemd/system/m101-scene-change.service"
STAMP="$(date +%Y%m%d%H%M%S)"

if [ "$(id -u)" != "0" ]; then
  echo "Please run as root." >&2
  exit 1
fi

cd "$(dirname "$0")"

mkdir -p "$APP_DIR" "$APP_DIR/backups"

if systemctl list-unit-files m101-scene-change.service >/dev/null 2>&1; then
  systemctl stop m101-scene-change.service || true
fi

if [ -f "$APP_DIR/m101_scene_change_service.py" ]; then
  cp -a "$APP_DIR/m101_scene_change_service.py" "$APP_DIR/backups/m101_scene_change_service.py.$STAMP.bak"
fi

if [ -f "$SERVICE_FILE" ]; then
  cp -a "$SERVICE_FILE" "$APP_DIR/backups/m101-scene-change.service.$STAMP.bak"
fi

cp -a m101_scene_change_service.py "$APP_DIR/m101_scene_change_service.py"
cp -a m101-scene-change.service "$SERVICE_FILE"

if [ ! -f "$APP_DIR/config.json" ]; then
  cp -a config.example.json "$APP_DIR/config.json"
fi

chmod 755 "$APP_DIR/m101_scene_change_service.py"
python3 -m py_compile "$APP_DIR/m101_scene_change_service.py"

systemctl daemon-reload
systemctl enable m101-scene-change.service

echo "Installed m101 scene change service."
echo "Run verification before starting or restarting:"
echo "  python3 $APP_DIR/m101_scene_change_service.py --once --dry-run --verbose"
echo "Start service:"
echo "  systemctl restart m101-scene-change.service"
