# VPS 上线手册（实际部署记录 · 与 3X-UI/Xray 完全隔离）

> 本文档按**实际上线过程**重写。原计划假设"VPS 已装 Caddy 抢 443"——
> 实地探查发现 **443 被 Xray 独占且不可动、且没装 Caddy/Nginx**，
> 故最终方案改为 **Cloudflare Tunnel**：cloudflared 主动外连 Cloudflare，
> 不开任何入站端口，对 Xray 零风险，访客到服务全程加密。

环境实测：Ubuntu 24.04 / root / Python 3.12 / 内存 961M（可用 ~600M + swap 1G）/
443 = `xray-linux-amd64`（x-ui 管理，勿动）/ 80 空闲 / 无 Caddy 无 Nginx。

目录约定：仓库 clone 到 `/opt/chores/jiawu-jifen`，应用在其 `app/` 子目录，
运行数据 `app/data/`（不入版本控制），专用系统用户 `chores`。
域名：`seventylink.top`（注意是 sevent**y**link，DNS 在 Cloudflare），子域名 `jifen.seventylink.top`。

## 1. 部署应用（不触碰代理）

```
useradd -r -m -d /opt/chores chores
mkdir -p /opt/chores && chown chores:chores /opt/chores
sudo -u chores git clone https://github.com/pensee7d-ops/jiawu-jifen.git /opt/chores/jiawu-jifen
apt-get install -y python3.12-venv          # Ubuntu 把 venv 拆成独立包
sudo -u chores python3 -m venv /opt/chores/jiawu-jifen/app/.venv
sudo -u chores /opt/chores/jiawu-jifen/app/.venv/bin/pip install -q --upgrade pip
sudo -u chores /opt/chores/jiawu-jifen/app/.venv/bin/pip install -q -r /opt/chores/jiawu-jifen/app/requirements.txt
```

`.env`（`umask 077`，仅 chores 可读）：
```
CHORES_DB_PATH=/opt/chores/jiawu-jifen/app/data/chores.db
CHORES_PHOTO_DIR=/opt/chores/jiawu-jifen/app/data/photos
CHORES_SECRET_KEY=<openssl rand -hex 32 生成>
CHORES_CHECKIN_PASSWORD=<弟弟口令，必须与监管者口令不同>
CHORES_SUPERVISOR_PASSWORD=<家人口令，必须与打卡口令不同>
CHORES_WEEKLY_GOAL=300
```
> ⚠️ 两个口令**必须不同**：登录先比监管者口令再比打卡口令，相同则打卡角色永远进不去。
建数据目录：`sudo -u chores mkdir -p /opt/chores/jiawu-jifen/app/data`

## 2. systemd（监听 127.0.0.1:8080，内存限额隔离）

`deploy/chores.service` → `/etc/systemd/system/chores.service`，然后：
```
systemctl daemon-reload
systemctl enable --now chores
systemctl is-active chores                  # active
ss -ltnp | grep 8080                        # uvicorn 监听 127.0.0.1:8080
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login   # 200
```
注意：Type=simple 一 fork 即报 active；首启 uvicorn 约需 3s 才绑端口，
验证前 `sleep 3` 再 curl，否则会误报 000。

## 3. Cloudflare Tunnel（不开入站端口，不碰 443/Xray）

```
curl -L -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
apt-get install -y /tmp/cloudflared.deb
cloudflared tunnel login          # 打印网址→浏览器登录 Cloudflare→选 seventylink.top 授权
cloudflared tunnel create chores  # 生成 tunnel UUID + /root/.cloudflared/<UUID>.json
cloudflared tunnel route dns chores jifen.seventylink.top   # 自动建 proxied CNAME
# 写 /etc/cloudflared/config.yml（见 deploy/cloudflared-config.yml，填入实际 UUID）
cloudflared service install       # 装成 systemd 服务并启动
journalctl -u cloudflared -n 15   # 应见 4 条 "Registered tunnel connection"
```
> `cloudflared tunnel route dns` 的主机名必须含完整域名 `jifen.seventylink.top`。
> 若误传 `jifen.seventlink.top`（漏 y），cloudflared 会当成 zone 下记录名生成
> 畸形的 `jifen.seventlink.top.seventylink.top`，需在 CF 面板 DNS 删掉重建。

## 4. 验证隔离 + 备份

```
curl -s -o /dev/null -w '%{http_code}' https://jifen.seventylink.top/login   # 200
systemctl is-active x-ui chores cloudflared    # 三个 active
pgrep -a xray                                  # xray 进程 pid 与部署前一致 = 代理未受影响
free -m                                        # 仍有富余（实测可用 ~570M + swap）
```
备份：`deploy/backup.sh` 路径已对应实际目录，装到 `/opt/chores/backup.sh`，
`chmod +x`，需 `apt-get install -y sqlite3`。cron（root，每日 3:30，留最近 14 份）：
```
30 3 * * * /opt/chores/backup.sh >> /opt/chores/backup.log 2>&1
```

## 回滚 / 排查

- 应用挂：`journalctl -u chores -n 40`；改 `.env` 后必须 `systemctl restart chores`。
- 访问不通：先 `curl 127.0.0.1:8080`（本机层），再 `journalctl -u cloudflared`（隧道层）。
- 数据还原：`systemctl stop chores`，用 `/opt/chores/backups/` 里的
  `chores-*.db` 与 `photos-*.tar.gz` 还原到 `app/data/`，`systemctl start chores`。
- 内存吃紧：`chores.service` 的 `MemoryMax=180M` 是兜底；若上传大图 OOM 频繁重启，
  调高到 220–256M（实测总内存仍留 >350M 给 Xray/系统）或加 swap。
