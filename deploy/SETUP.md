# VPS 上线（与 3X-UI/Xray 完全隔离）

前置：VPS 已运行 Xray + Caddy；域名 seventlink.top 在 Cloudflare。

## 1. Cloudflare DNS
加 A 记录 `jifen` → VPS IP，开启代理（橙云）。SSL/TLS → Full。

## 2. 系统用户与目录（不触碰代理）
sudo useradd -r -m -d /opt/chores chores
sudo mkdir -p /opt/chores/app && sudo chown -R chores:chores /opt/chores

## 3. 部署代码
把 app/ 传到 /opt/chores/app（scp 或 git clone）。
sudo -u chores python3 -m venv /opt/chores/app/.venv
sudo -u chores /opt/chores/app/.venv/bin/pip install -r /opt/chores/app/requirements.txt
sudo -u chores cp /opt/chores/app/.env.example /opt/chores/app/.env
# 编辑 .env：CHORES_SECRET_KEY 用 `openssl rand -hex 32`；改两个口令；
# CHORES_DB_PATH=/opt/chores/app/data/chores.db；CHORES_PHOTO_DIR=/opt/chores/app/data/photos

## 4. systemd
sudo cp deploy/chores.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chores
systemctl status chores      # active 即成功；确认监听 127.0.0.1:8080
sudo ss -ltnp | grep 8080

## 5. Caddy（仅追加，不改 Xray 段）
把 deploy/Caddyfile.snippet 内容追加到 /etc/caddy/Caddyfile
sudo systemctl reload caddy

## 6. 验证隔离
- 访问 https://jifen.seventlink.top 正常登录。
- `systemctl status xray`（或 x-ui）仍 active，代理不受影响。
- `free -m` 确认空闲内存仍 > 350MB（chores 受 MemoryMax=180M 限制）。

## 7. 备份
sudo cp deploy/backup.sh /opt/chores/backups.sh
sudo chmod +x /opt/chores/backups.sh
sudo crontab -e   # 加：30 3 * * * /opt/chores/backups.sh >> /opt/chores/backup.log 2>&1

## 回滚
systemctl stop chores；用 /opt/chores/backups 里的 chores-*.db 与 photos-*.tar.gz 还原 data/；systemctl start chores。
