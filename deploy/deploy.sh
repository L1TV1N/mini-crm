#!/usr/bin/env bash
# выкладка по ssh: deploy/deploy.sh <ip-сервера> [домен] [порт]
# ключ по умолчанию берётся из ssh-агента, другой можно задать через CRM_SSH_KEY=~/.ssh/ключ
# код заливается целиком заново, а база лежит отдельно в /var/lib/mini-crm и повторным деплоем не трогается
set -euo pipefail
HOST="${1:?укажи ip сервера}"
DOMAIN="${2:-}"
PORT="${3:-8101}"
SSH=(ssh -o BatchMode=yes)
[ -n "${CRM_SSH_KEY:-}" ] && SSH+=(-i "$CRM_SSH_KEY" -o IdentitiesOnly=yes)
SSH+=(root@"$HOST")

cd "$(dirname "$0")/.."
tar czf - --exclude=./data --exclude=./docs/screens --exclude=./.git --exclude='__pycache__' --exclude=./.env . \
  | "${SSH[@]}" 'rm -rf /opt/mini-crm.new && mkdir -p /opt/mini-crm.new && tar xzf - -C /opt/mini-crm.new \
                 && rm -rf /opt/mini-crm && mv /opt/mini-crm.new /opt/mini-crm'
"${SSH[@]}" "bash /opt/mini-crm/deploy/setup.sh '$DOMAIN' '$PORT'"
