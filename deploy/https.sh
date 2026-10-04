#!/usr/bin/env bash
# Выполняется на сервере от root: /opt/mini-crm/deploy/https.sh <домен> [порт]
# Выпускает сертификат Let's Encrypt для домена и www и включает https в блоке nginx этого проекта.
# Запуск ЗНАЧИТ принятие условий Let's Encrypt (--agree-tos) и регистрацию без e-mail, поэтому запускать осознанно.
# Чужие сайты на сервере не затрагиваются: certbot только выпускает сертификат (webroot), конфиги nginx он не правит.
set -euo pipefail
DOMAIN="${1:?укажи домен}"
PORT="${2:-8101}"
command -v certbot >/dev/null || { apt-get update -qq && apt-get install -y -qq certbot; }
install -d /var/www/letsencrypt
# домен должен уже смотреть на этот сервер и отдаваться блоком nginx по http (его ставит setup.sh)
ARGS=(certonly --webroot -w /var/www/letsencrypt -d "$DOMAIN" -d "www.$DOMAIN" --non-interactive --agree-tos
      --register-unsafely-without-email --deploy-hook "systemctl reload nginx")
certbot "${ARGS[@]}" --dry-run
certbot "${ARGS[@]}"
bash /opt/mini-crm/deploy/setup.sh "$DOMAIN" "$PORT"
# Автопродление отключено сознательно: сертификат живёт 90 дней, а проекту хватает недели.
# Для долгой жизни включить: systemctl enable --now certbot.timer
systemctl disable --now certbot.timer >/dev/null 2>&1 || true
echo "сертификат выпущен, срок: $(openssl x509 -in /etc/letsencrypt/live/$DOMAIN/fullchain.pem -noout -enddate), автопродление выключено"
