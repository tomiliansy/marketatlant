#!/usr/bin/env bash
# Установка моста Маркет <-> Telegram на Ubuntu 24.04.
# Запуск от root:  bash setup.sh market.ваш-домен.ru
# Можно запускать повторно: обновит app.py и перезапустит сервис.
set -euo pipefail

DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "Укажите домен: bash setup.sh market.example.ru"; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root"; exit 1; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER=bridge
APP_DIR=/opt/market-bridge
[ -f "$SRC_DIR/app.py" ] || { echo "Рядом со скриптом нет файла app.py"; exit 1; }

echo "==> Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip nginx certbot python3-certbot-nginx ufw curl openssl

echo "==> Пользователь и приложение"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home \
  --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -o "$APP_USER" -g "$APP_USER" -m 644 "$SRC_DIR/app.py" "$APP_DIR/app.py"
[ -d "$APP_DIR/venv" ] || runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/venv"
runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/pip" install --quiet flask requests gunicorn

ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "==> Настройки (ввод виден на экране, проверьте себя)"
  read -rp "TG_TOKEN (токен бота от BotFather): " TG_TOKEN
  read -rp "MARKET_TOKEN (Api-Key Маркета): " MARKET_TOKEN
  read -rp "BUSINESS_ID: " BUSINESS_ID
  read -rp "OWNER_ID (ваш chat_id в Telegram, если не знаете - 0): " OWNER_ID
  OWNER_ID="${OWNER_ID:-0}"
  SECRET="$(openssl rand -hex 16)"
  ( umask 077
cat > "$ENV_FILE" <<EOF
TG_TOKEN=$TG_TOKEN
MARKET_TOKEN=$MARKET_TOKEN
BUSINESS_ID=$BUSINESS_ID
OWNER_ID=$OWNER_ID
SECRET=$SECRET
NOTIFY_STATUS=0
EOF
  )
  chown "$APP_USER:$APP_USER" "$ENV_FILE"
fi
SECRET="$(grep '^SECRET=' "$ENV_FILE" | cut -d= -f2-)"

echo "==> Сервис systemd"
cat > /etc/systemd/system/market-bridge.service <<EOF
[Unit]
Description=Market <-> Telegram bridge
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 --threads 4 -b 127.0.0.1:8000 app:app
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo "==> nginx"
cat > /etc/nginx/sites-available/market-bridge <<'EOF'
server {
    listen 80;
    server_name __DOMAIN__;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }
}
EOF
sed -i "s/__DOMAIN__/$DOMAIN/" /etc/nginx/sites-available/market-bridge
ln -sf /etc/nginx/sites-available/market-bridge /etc/nginx/sites-enabled/market-bridge
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

echo "==> Файрвол (22, 80, 443)"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable

echo "==> Сертификат Let's Encrypt"
MY_IP="$(curl -4 -s --max-time 10 https://ifconfig.me || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}' || true)"
if [ -n "$MY_IP" ] && [ "$MY_IP" = "$DNS_IP" ]; then
  read -rp "Email для Let's Encrypt (Enter - без email): " EMAIL
  if [ -n "$EMAIL" ]; then MAIL_ARG="-m $EMAIL"; else MAIL_ARG="--register-unsafely-without-email"; fi
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos $MAIL_ARG --redirect
else
  echo "!! Домен $DOMAIN указывает на '${DNS_IP:-ничего}', а IP сервера '${MY_IP:-неизвестен}'."
  echo "!! Сертификат НЕ выпущен. Поправьте A-запись и выполните:"
  echo "!!   certbot --nginx -d $DOMAIN --redirect"
fi

echo "==> Запуск бота"
systemctl daemon-reload
systemctl enable market-bridge >/dev/null 2>&1
systemctl restart market-bridge
sleep 3

echo "==> Проверки"
echo -n "Сервис: "; systemctl is-active market-bridge || true
echo -n "Локальный ответ (ожидаем ok): "; curl -s --max-time 5 "http://127.0.0.1:8000/$SECRET" || true; echo
curl -s -o /dev/null -w "Telegram API: HTTP %{http_code}\n" --max-time 10 https://api.telegram.org || echo "Telegram API: недоступен"
curl -s -o /dev/null -w "Маркет API:   HTTP %{http_code}\n" --max-time 10 https://api.partner.market.yandex.ru || echo "Маркет API: недоступен"

echo
echo "Готово. Адрес уведомлений для кабинета Маркета:"
echo "  https://$DOMAIN/$SECRET"
echo "Изменить настройки: nano $ENV_FILE && systemctl restart market-bridge"
echo "Логи бота:          journalctl -u market-bridge -f"
