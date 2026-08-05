#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/shakeonit
SERVICE_USER=shakeonit
CONFIG_DIR=/etc/shakeonit
PUBLIC_HOST=${1:-}
WEBHOOK_PORT=${2:-8443}
WEBHOOK_PATH=/telegram/webhook
CERT_FILE=$CONFIG_DIR/webhook.crt
KEY_FILE=$CONFIG_DIR/webhook.key
ENV_FILE=$APP_DIR/.env

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/configure-webhook.sh <public-ip-or-domain>" >&2
  exit 1
fi

if [[ -z "$PUBLIC_HOST" ]]; then
  echo "Usage: sudo bash deploy/configure-webhook.sh <public-ip-or-domain> [port]" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]] || ! grep -q '^BOT_TOKEN=' "$ENV_FILE"; then
  echo "BOT_TOKEN is missing in $ENV_FILE" >&2
  exit 1
fi

if ! [[ "$WEBHOOK_PORT" =~ ^(443|8443|80|88)$ ]]; then
  echo "Telegram supports webhook ports 443, 80, 88 and 8443" >&2
  exit 1
fi

install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"
if [[ "$PUBLIC_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  SUBJECT_ALT_NAME="IP:$PUBLIC_HOST"
else
  SUBJECT_ALT_NAME="DNS:$PUBLIC_HOST"
fi

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
  -keyout "$KEY_FILE" \
  -out "$CERT_FILE" \
  -subj "/CN=$PUBLIC_HOST" \
  -addext "subjectAltName=$SUBJECT_ALT_NAME"
chown root:"$SERVICE_USER" "$CERT_FILE" "$KEY_FILE"
chmod 0640 "$CERT_FILE" "$KEY_FILE"

WEBHOOK_SECRET=$(openssl rand -hex 32)
OLD_ENV=$(mktemp)
NEW_ENV=$(mktemp)
trap 'rm -f -- "$OLD_ENV" "$NEW_ENV"' EXIT
grep -Ev '^WEBHOOK_(URL|SECRET|PATH|HOST|PORT|CERT_PATH|KEY_PATH)=' "$ENV_FILE" > "$OLD_ENV"
cp "$OLD_ENV" "$NEW_ENV"
{
  printf 'WEBHOOK_URL=https://%s:%s%s\n' "$PUBLIC_HOST" "$WEBHOOK_PORT" "$WEBHOOK_PATH"
  printf 'WEBHOOK_SECRET=%s\n' "$WEBHOOK_SECRET"
  printf 'WEBHOOK_PATH=%s\n' "$WEBHOOK_PATH"
  printf 'WEBHOOK_HOST=0.0.0.0\n'
  printf 'WEBHOOK_PORT=%s\n' "$WEBHOOK_PORT"
  printf 'WEBHOOK_CERT_PATH=%s\n' "$CERT_FILE"
  printf 'WEBHOOK_KEY_PATH=%s\n' "$KEY_FILE"
} >> "$NEW_ENV"
install -o root -g "$SERVICE_USER" -m 0640 "$NEW_ENV" "$ENV_FILE"

systemctl restart shakeonit
echo "Webhook configuration installed for https://$PUBLIC_HOST:$WEBHOOK_PORT$WEBHOOK_PATH"
