#!/usr/bin/env bash
set -u

APP_HEALTH_URL=http://127.0.0.1:8080/health
ENV_FILE=/opt/shakeonit/.env
TAG=shakeonit-healthcheck

check_url() {
  curl --fail --silent --show-error --max-time 5 "$@" >/dev/null
}

if ! check_url "$APP_HEALTH_URL"; then
  sleep 2
  if ! check_url "$APP_HEALTH_URL"; then
    logger -t "$TAG" "Local application health check failed; restarting shakeonit"
    systemctl restart shakeonit.service
    sleep 5
    if ! check_url "$APP_HEALTH_URL"; then
      logger -t "$TAG" "Application is still unavailable after restart"
      exit 1
    fi
  fi
fi

if ! systemctl is-active --quiet nginx.service; then
  logger -t "$TAG" "nginx is inactive; restarting it"
  systemctl restart nginx.service
fi

webapp_url=""
if [[ -r "$ENV_FILE" ]]; then
  webapp_url=$(sed -n 's/^WEBAPP_URL=//p' "$ENV_FILE" | tail -n 1 | tr -d '\r')
fi

if [[ "$webapp_url" == https://* ]]; then
  webapp_host=${webapp_url#https://}
  webapp_host=${webapp_host%%/*}
  webapp_host=${webapp_host%%:*}
  proxy_url="https://${webapp_host}/health"
  if ! check_url --resolve "${webapp_host}:443:127.0.0.1" "$proxy_url"; then
    sleep 2
    if ! check_url --resolve "${webapp_host}:443:127.0.0.1" "$proxy_url"; then
      logger -t "$TAG" "HTTPS proxy health check failed; restarting nginx"
      systemctl restart nginx.service
      sleep 3
      check_url --resolve "${webapp_host}:443:127.0.0.1" "$proxy_url" || exit 1
    fi
  fi
fi
