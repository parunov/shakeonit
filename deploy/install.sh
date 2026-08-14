#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/shakeonit
SERVICE_USER=shakeonit
SERVICE_FILE=/etc/systemd/system/shakeonit.service
HEALTHCHECK_SERVICE_FILE=/etc/systemd/system/shakeonit-healthcheck.service
HEALTHCHECK_TIMER_FILE=/etc/systemd/system/shakeonit-healthcheck.timer

if [[ $EUID -ne 0 ]]; then
  echo "Run this script as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "Project not found in $APP_DIR" >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --editable "$APP_DIR"

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$APP_DIR/data"
if [[ ! -f "$APP_DIR/.env" ]]; then
  install -o root -g "$SERVICE_USER" -m 0640 "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Created $APP_DIR/.env. Replace BOT_TOKEN before starting the service."
else
  chown root:"$SERVICE_USER" "$APP_DIR/.env"
  chmod 0640 "$APP_DIR/.env"
fi

install -o root -g root -m 0644 "$APP_DIR/deploy/shakeonit.service" "$SERVICE_FILE"
install -o root -g root -m 0755 "$APP_DIR/deploy/healthcheck.sh" /usr/local/sbin/shakeonit-healthcheck
install -o root -g root -m 0644 \
  "$APP_DIR/deploy/shakeonit-healthcheck.service" "$HEALTHCHECK_SERVICE_FILE"
install -o root -g root -m 0644 \
  "$APP_DIR/deploy/shakeonit-healthcheck.timer" "$HEALTHCHECK_TIMER_FILE"
systemctl daemon-reload
systemctl enable shakeonit.service
systemctl enable shakeonit-healthcheck.timer

echo "Installation complete. Set BOT_TOKEN in $APP_DIR/.env, then run:"
echo "  sudo systemctl restart shakeonit"
echo "  sudo systemctl start shakeonit-healthcheck.timer"
echo "  sudo systemctl status shakeonit --no-pager"
