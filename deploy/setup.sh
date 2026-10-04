#!/usr/bin/env bash
# Выполняется на сервере от root: /opt/mini-crm/deploy/setup.sh [домен] [порт]
# Идемпотентен. Ничего не трогает за пределами своих файлов: своя папка, свой пользователь, своя база, свой сервис,
# свой порт и свой блок nginx, чужие сайты и их конфиги остаются как были.
set -euo pipefail
DOMAIN="${1:-}"
PORT="${2:-8101}"
APP=/opt/mini-crm
ENVF=/etc/mini-crm.env

python3 -c "import sys; sys.exit(sys.version_info < (3, 11))" || { echo "нужен Python 3.11+"; exit 1; }
if ss -ltn | awk '{print $4}' | grep -qE "[:.]$PORT\$" && ! systemctl is-active --quiet mini-crm; then
  echo "порт $PORT уже занят другим сервисом, укажите другой третьим аргументом"; exit 1
fi

id minicrm &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin minicrm
install -d -o minicrm -g minicrm -m 700 /var/lib/mini-crm

# пароль и токен пишем один раз, повторный запуск их не трогает, порт обновляем всегда
if [ ! -f "$ENVF" ]; then
  printf 'BOT_TOKEN=\nCRM_PASSWORD=%s\nCRM_PORT=%s\n' "$(head -c 12 /dev/urandom | base64 | tr -d '/+=')" "$PORT" > "$ENVF"
  echo "создан $ENVF: пароль сгенерирован, BOT_TOKEN впишите сами"
else
  grep -q '^CRM_PORT=' "$ENVF" && sed -i "s/^CRM_PORT=.*/CRM_PORT=$PORT/" "$ENVF" || echo "CRM_PORT=$PORT" >> "$ENVF"
fi
chmod 600 "$ENVF"

cp "$APP/deploy/crm.service" /etc/systemd/system/mini-crm.service
systemctl daemon-reload
systemctl enable mini-crm >/dev/null 2>&1
systemctl restart mini-crm

if [ -n "$DOMAIN" ]; then
  command -v nginx >/dev/null || { apt-get update -qq && apt-get install -y -qq nginx; }
  install -d /var/www/letsencrypt
  # сертификат уже выпущен (deploy/https.sh) — ставим https-вариант, иначе обычный, так повторный деплой не сбрасывает https
  TPL="$APP/deploy/nginx.conf"
  [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] && TPL="$APP/deploy/nginx-ssl.conf"
  [ -f /etc/nginx/sites-available/mini-crm ] && cp /etc/nginx/sites-available/mini-crm /etc/nginx/sites-available/mini-crm.bak
  sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$PORT/g" "$TPL" > /etc/nginx/sites-available/mini-crm
  ln -sf /etc/nginx/sites-available/mini-crm /etc/nginx/sites-enabled/mini-crm
  # перечитываем конфиг только если он целиком валиден, иначе возвращаем прежний блок и оставляем всё как было
  if nginx -t 2>/dev/null; then systemctl reload nginx; else
    nginx -t || true
    if [ -f /etc/nginx/sites-available/mini-crm.bak ]; then mv /etc/nginx/sites-available/mini-crm.bak /etc/nginx/sites-available/mini-crm
    else rm -f /etc/nginx/sites-enabled/mini-crm; fi
    echo "конфиг nginx не прошёл проверку, свой блок возвращён как был"; exit 1
  fi
  rm -f /etc/nginx/sites-available/mini-crm.bak
fi
sleep 1
systemctl is-active --quiet mini-crm && echo "mini-crm запущен на 127.0.0.1:$PORT" || { journalctl -u mini-crm -n 20 --no-pager; exit 1; }
