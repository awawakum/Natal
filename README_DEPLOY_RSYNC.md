# Деплой Astro Bot через rsync

Инструкция для копирования проекта с Windows на Linux VPS. Команды для Windows
выполняются в PowerShell или WSL, команды для сервера — в SSH-сессии.

## Архитектура

```text
Telegram polling
      |
      v
   bot.py ---------------------- Redis :6379 (только localhost)
      |
      +-- aiohttp :8080 (только localhost)
              ^
              |
        Nginx HTTPS :443
              ^
              |
   ЮKassa / xRocket webhook
```

Наружу публикуются только порты `22`, `80` и `443`. Порты `6379` и `8080`
не открываются в интернет.

## 1. Что потребуется

- Linux VPS, например Ubuntu 22.04/24.04;
- домен или поддомен, например `bot.example.com`;
- SSH-доступ к VPS;
- установленный `rsync` на локальной машине;
- новый Telegram Bot Token и ключи провайдеров.

На Windows варианты установки `rsync`:

- использовать WSL и установить `rsync` внутри Ubuntu;
- использовать Git Bash с доступным `rsync`;
- использовать `rsync` из MSYS2.

Проверка:

```powershell
rsync --version
ssh -V
```

## 2. SSH-ключ для деплоя

На локальном компьютере создай отдельный ключ:

```powershell
ssh-keygen -t ed25519 -f "$HOME/.ssh/astro_bot_deploy" -C "astro-bot-deploy"
```

Не используй ключ без passphrase для обычного личного компьютера.

Добавь публичный ключ на сервер. Если доступ по паролю разрешён:

```powershell
Get-Content "$HOME/.ssh/astro_bot_deploy.pub" | ssh root@SERVER_IP "umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys"
```

Проверь вход:

```powershell
ssh -i "$HOME/.ssh/astro_bot_deploy" root@SERVER_IP
```

После настройки отдельного пользователя отключи вход root по SSH и парольную
авторизацию в `/etc/ssh/sshd_config`, но сначала убедись, что вход ключом
работает в отдельном окне SSH.

## 3. Подготовить сервер

Подключись к VPS:

```bash
ssh -i ~/.ssh/astro_bot_deploy root@SERVER_IP
```

Установи системные пакеты:

```bash
apt update
apt install -y python3 python3-venv python3-pip nginx redis-server ufw fail2ban git rsync certbot python3-certbot-nginx
```

Создай отдельного пользователя:

```bash
useradd --system --create-home --home-dir /opt/astro_bot --shell /usr/sbin/nologin astro
mkdir -p /opt/astro_bot
chown -R astro:astro /opt/astro_bot
```

Запусти Redis:

```bash
systemctl enable --now redis-server
redis-cli ping
```

Ожидаемый результат:

```text
PONG
```

Настрой firewall:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose
```

Не открывай `6379` и `8080`:

```bash
ufw deny 6379/tcp
ufw deny 8080/tcp
```

## 4. Подготовить локальную копию

Перейди в чистую папку, подготовленную для GitHub:

```powershell
cd C:\Users\Asus\Downloads\astro_bot_github
```

Перед отправкой проверь, что секретов нет:

```powershell
Get-ChildItem -Recurse -Force -File |
  Where-Object {
    $_.Name -eq ".env" -or
    $_.Name -match "sqlite|\.pyc$"
  }
```

Команда не должна показывать `.env` или SQLite-базу.

Проверь, что `rsync` исключит секреты и окружения:

```powershell
rsync -avzn --delete `
  --exclude='.env' `
  --exclude='data/' `
  --exclude='ven/' `
  --exclude='venv/' `
  --exclude='.venv/' `
  --exclude='__pycache__/' `
  --exclude='*.pyc' `
  --exclude='.git/' `
  ./ root@SERVER_IP:/opt/astro_bot/
```

Флаг `-n` означает dry-run: файлы ещё не копируются. Проверь список.

Важно: `--delete` удаляет на сервере файлы, которых нет в локальной копии.
Для первого запуска его можно убрать, если на сервере уже есть данные, которые
нужно сохранить.

## 5. Скопировать проект через rsync

После проверки dry-run:

```powershell
rsync -avz --delete `
  --exclude='.env' `
  --exclude='data/' `
  --exclude='ven/' `
  --exclude='venv/' `
  --exclude='.venv/' `
  --exclude='__pycache__/' `
  --exclude='*.pyc' `
  --exclude='.git/' `
  -e "ssh -i $HOME/.ssh/astro_bot_deploy" `
  ./ root@SERVER_IP:/opt/astro_bot/
```

После копирования на VPS верни владельца кода сервисному пользователю:

```bash
chown -R astro:astro /opt/astro_bot
```

Если используешь WSL, путь будет примерно таким:

```bash
cd /mnt/c/Users/Asus/Downloads/astro_bot_github
```

Проверка на сервере:

```bash
find /opt/astro_bot -maxdepth 2 -type f | sort
```

`.env` и `data/` намеренно не должны копироваться через rsync.

## 6. Установить Python-зависимости

На сервере:

```bash
sudo -u astro python3 -m venv /opt/astro_bot/.venv
sudo -u astro /opt/astro_bot/.venv/bin/python -m pip install --upgrade pip
sudo -u astro /opt/astro_bot/.venv/bin/pip install -r /opt/astro_bot/requirements.txt
sudo -u astro /opt/astro_bot/.venv/bin/python -m compileall -q /opt/astro_bot
```

## 7. Создать серверный `.env`

Не передавай production `.env` через GitHub и не добавляй его в rsync.
Создай его непосредственно на сервере:

```bash
sudo -u astro nano /opt/astro_bot/.env
```

Минимальный шаблон:

```env
BOT_TOKEN=новый_telegram_token
LLM_BASE_URL=https://router.cheap/v1
LLM_API_KEY=новый_llm_key
LLM_MODEL=твоя_модель
LLM_VISION_MODEL=твоя_vision_модель

NEW_USER_REQUESTS=2
FREE_REQUESTS_PER_DAY=1
TEMPORARY_BLOCK_HOURS=3
ADMIN_USER_IDS=твой_telegram_id
PAYMENTS_DB_PATH=data/payments.sqlite3

REDIS_URL=redis://127.0.0.1:6379/0
REDIS_KEY_PREFIX=astro_bot:fsm
REDIS_STATE_TTL_SECONDS=86400
REDIS_DATA_TTL_SECONDS=86400

YOOKASSA_SHOP_ID=тестовый_или_боевой_shop_id
YOOKASSA_SECRET_KEY=тестовый_или_боевой_key
YOOKASSA_RETURN_URL=https://bot.example.com/payment-success

XROCKET_API_TOKEN=твой_xrocket_api_token
XROCKET_WEBHOOK_SECRET=твой_xrocket_webhook_secret
XROCKET_API_BASE_URL=https://pay.api.xrocket.exchange

PAYMENT_WEBHOOK_URL=https://bot.example.com
PAYMENT_WEBHOOK_HOST=127.0.0.1
PAYMENT_WEBHOOK_PORT=8080

PRIVACY_POLICY_URL=https://твой-домен/privacy
PUBLIC_OFFER_URL=https://твой-домен/offer
```

Ограничь доступ:

```bash
chown astro:astro /opt/astro_bot/.env
chmod 600 /opt/astro_bot/.env
mkdir -p /opt/astro_bot/data
chown astro:astro /opt/astro_bot/data
chmod 700 /opt/astro_bot/data
```

Для локального Redis оставь:

```env
REDIS_URL=redis://127.0.0.1:6379/0
```

## 8. DNS и HTTPS

Создай DNS-запись:

```text
A  bot.example.com  SERVER_IP
```

Проверь с локального компьютера:

```powershell
nslookup bot.example.com
```

Создай конфигурацию Nginx:

```bash
nano /etc/nginx/sites-available/astro_bot
```

```nginx
limit_req_zone $binary_remote_addr zone=payment_webhooks:10m rate=10r/s;

server {
    listen 80;
    listen [::]:80;
    server_name bot.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name bot.example.com;

    client_max_body_size 2m;

    location = /health {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /payments/yookassa {
        limit_req zone=payment_webhooks burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 20s;
    }

    location = /payments/xrocket {
        limit_req zone=payment_webhooks burst=20 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 20s;
    }

    location = /payment-success {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        return 404;
    }

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    ssl_certificate /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;
}
```

Активируй конфигурацию:

```bash
ln -s /etc/nginx/sites-available/astro_bot /etc/nginx/sites-enabled/astro_bot
nginx -t
systemctl reload nginx
```

Получи сертификат:

```bash
certbot --nginx -d bot.example.com
```

Проверь:

```bash
curl -i https://bot.example.com/health
```

Ожидается HTTP 200 и:

```json
{"ok": true}
```

## 9. Настроить платежные ссылки

В `.env`:

```env
PAYMENT_WEBHOOK_URL=https://bot.example.com
YOOKASSA_RETURN_URL=https://bot.example.com/payment-success
```

Полные адреса:

```text
ЮKassa: https://bot.example.com/payments/yookassa
xRocket: https://bot.example.com/payments/xrocket
```

В кабинете ЮKassa укажи URL уведомлений:

```text
https://bot.example.com/payments/yookassa
```

В приложении xRocket Pay выбери **Current Pay API** и укажи callback:

```text
https://bot.example.com/payments/xrocket
```

`YOOKASSA_RETURN_URL` только возвращает пользователя после оплаты. Начисление
запросов происходит через webhook и проверку платежа.

## 10. Systemd

Создай сервис:

```bash
nano /etc/systemd/system/astro-bot.service
```

```ini
[Unit]
Description=Astro Telegram Bot
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
Type=simple
User=astro
Group=astro
WorkingDirectory=/opt/astro_bot
EnvironmentFile=/opt/astro_bot/.env
ExecStart=/opt/astro_bot/.venv/bin/python /opt/astro_bot/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=30

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/astro_bot/data
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077

[Install]
WantedBy=multi-user.target
```

Запусти:

```bash
systemctl daemon-reload
systemctl enable --now astro-bot
systemctl status astro-bot
```

Логи:

```bash
journalctl -u astro-bot -f
```

В логах ожидается:

```text
FSM storage: Redis
```

## 11. Проверка после запуска

```bash
redis-cli ping
curl -i http://127.0.0.1:8080/health
curl -i https://bot.example.com/health
systemctl is-active astro-bot
```

В Telegram проверь:

```text
/start
/limits
/buy
```

Проверь отдельно:

- Таро с вопросом в одном сообщении;
- короткое `Таро` без вопроса;
- дату рождения;
- фото кофейной гущи;
- фото натальной карты;
- reply на сообщение бота;
- `/new` и `/forget`;
- Stars invoice;
- тестовый платеж ЮKassa;
- xRocket invoice;
- повторную доставку webhook.

## 12. Обновление через rsync

Сначала останови бота:

```bash
systemctl stop astro-bot
```

На локальной машине выполни dry-run:

```powershell
cd C:\Users\Asus\Downloads\astro_bot_github
rsync -avzn --delete `
  --exclude='.env' `
  --exclude='data/' `
  --exclude='ven/' `
  --exclude='venv/' `
  --exclude='.venv/' `
  --exclude='__pycache__/' `
  --exclude='*.pyc' `
  --exclude='.git/' `
  -e "ssh -i $HOME/.ssh/astro_bot_deploy" `
  ./ root@SERVER_IP:/opt/astro_bot/
```

Если список правильный, выполни тот же `rsync` без `-n`.

После копирования:

```bash
sudo -u astro /opt/astro_bot/.venv/bin/pip install -r /opt/astro_bot/requirements.txt
sudo -u astro /opt/astro_bot/.venv/bin/python -m compileall -q /opt/astro_bot
systemctl start astro-bot
systemctl status astro-bot
```

## 13. Backup SQLite перед обновлением

```bash
systemctl stop astro-bot
sudo -u astro cp --preserve=mode,ownership,timestamps \
  /opt/astro_bot/data/payments.sqlite3 \
  /opt/astro_bot/data/payments.sqlite3.backup-$(date +%Y%m%d-%H%M%S)
systemctl start astro-bot
```

Не копируй `data/` через `rsync --delete`: это может удалить баланс, лимиты,
платёжный журнал и память пользователей.

При обновлении от `root` сохрани владельца каталога данных и конфигурации:

```bash
chown -R astro:astro /opt/astro_bot
chmod 600 /opt/astro_bot/.env
chmod 700 /opt/astro_bot/data
```

## 14. Откат

Сохраняй предыдущую версию в отдельный каталог или используй Git tag. Перед
обновлением можно сделать копию исходников:

```bash
cp -a /opt/astro_bot /opt/astro_bot.previous
```

Если новая версия не запускается:

```bash
systemctl stop astro-bot
rm -rf /opt/astro_bot
mv /opt/astro_bot.previous /opt/astro_bot
systemctl start astro-bot
```

Не выполняй эти команды, если в каталоге есть уникальные данные без backup.

## 15. Безопасность

- не отправляй `.env` через GitHub или `rsync`;
- не публикуй `.env` даже в приватном репозитории;
- не открывай Redis `6379` и aiohttp `8080` в firewall;
- храни `.env` с правами `600`;
- используй отдельного пользователя `astro`, не `root`;
- регулярно обновляй Ubuntu и Python-зависимости;
- проверяй `nginx -t` перед reload;
- не логируй токены, webhook secrets и полные платёжные payload;
- перед production перевыпусти ключи, которые появлялись в переписке;
- регулярно копируй SQLite backup на другой сервер или объектное хранилище.

## Переменные ссылок

```env
# Базовый публичный HTTPS URL без завершающего /
PAYMENT_WEBHOOK_URL=https://bot.example.com

# Страница возврата пользователя из ЮKassa
YOOKASSA_RETURN_URL=https://bot.example.com/payment-success

# Публичная политика и оферта
PRIVACY_POLICY_URL=https://bot.example.com/privacy
PUBLIC_OFFER_URL=https://bot.example.com/offer
```

Итоговая схема:

```text
https://bot.example.com/payments/yookassa -> Nginx -> 127.0.0.1:8080
https://bot.example.com/payments/xrocket  -> Nginx -> 127.0.0.1:8080
Redis                                  -> 127.0.0.1:6379
SQLite                                 -> /opt/astro_bot/data/payments.sqlite3
```
