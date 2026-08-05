#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/opt/shakeonit
SERVICE_USER=shakeonit
ENV_FILE=$APP_DIR/.env
DOMAIN=${1:-}
NGINX_SITE=/etc/nginx/sites-available/shakeonit

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/configure-miniapp.sh <domain>" >&2
  exit 1
fi

if [[ -z "$DOMAIN" ]]; then
  echo "Usage: sudo bash deploy/configure-miniapp.sh <domain>" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]] || ! grep -q '^BOT_TOKEN=' "$ENV_FILE"; then
  echo "BOT_TOKEN is missing in $ENV_FILE" >&2
  exit 1
fi

if ! getent ahostsv4 "$DOMAIN" | awk '{print $1}' | grep -qx '153.76.201.10'; then
  echo "$DOMAIN does not resolve to 153.76.201.10" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nginx certbot python3-certbot-nginx

cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    server_tokens off;

    client_max_body_size 128k;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 35s;
    }
}
EOF

ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/shakeonit
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email \
  --redirect -d "$DOMAIN"

WEBHOOK_SECRET=$(grep '^WEBHOOK_SECRET=' "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$WEBHOOK_SECRET" ]]; then
  WEBHOOK_SECRET=$(openssl rand -hex 32)
fi
OLD_ENV=$(mktemp)
NEW_ENV=$(mktemp)
trap 'rm -f -- "$OLD_ENV" "$NEW_ENV"' EXIT
grep -Ev '^(WEBHOOK_(URL|SECRET|PATH|HOST|PORT|CERT_PATH|KEY_PATH)|WEBAPP_URL)=' \
  "$ENV_FILE" > "$OLD_ENV"
cp "$OLD_ENV" "$NEW_ENV"
{
  printf 'WEBHOOK_URL=https://%s/telegram/webhook\n' "$DOMAIN"
  printf 'WEBHOOK_SECRET=%s\n' "$WEBHOOK_SECRET"
  printf 'WEBHOOK_PATH=/telegram/webhook\n'
  printf 'WEBHOOK_HOST=127.0.0.1\n'
  printf 'WEBHOOK_PORT=8080\n'
  printf 'WEBAPP_URL=https://%s/app\n' "$DOMAIN"
} >> "$NEW_ENV"
install -o root -g "$SERVICE_USER" -m 0640 "$NEW_ENV" "$ENV_FILE"

systemctl restart shakeonit
echo "Mini App configured at https://$DOMAIN/app"
