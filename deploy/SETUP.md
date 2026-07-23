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

## 从 1.x 升级到 2.0

升级会增加积分、阶段、时长和悬赏任务表，并扩展电脑记录表。迁移由应用启动时
自动、幂等执行，但上线前仍必须备份数据库和照片：

```bash
/opt/chores/backup.sh
systemctl stop chores
cd /opt/chores/jiawu-jifen
sudo -u chores git pull --ff-only
sudo -u chores app/.venv/bin/pip install -q -r app/requirements.txt
systemctl start chores
journalctl -u chores -n 60 --no-pager
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/login
```

`pillow-heif` 用于读取 iPhone 相册中的 HEIC/HEIF 照片，随 requirements 一起安装，
不需要额外启动常驻服务。首次登录后按以下顺序验收：

1. 监管者进入「阶段规则」，创建当前阶段并确认基础分钟、目标分和奖励分钟。
2. 打开「积分账本」，核对期初余额只包含升级前的当前开放周期。
3. 用手机分别测试相册选择和现场拍照。
4. 补一段测试电脑记录，确认时长和审计记录正确后再正式使用。

如果启动迁移失败：停止服务，恢复刚才生成的 `chores-*.db`，切回上一代码版本后
重新启动。照片迁移不会改写原文件。

## 从 2.0 升级到 2.1

2.1 会为打卡、任务、积分流水、电脑会话和时长流水增加软删除字段，并新增
`record_deletions` 与 `stage_switches`。迁移仍由应用启动自动执行，重复启动安全。
上线前除了日常数据库与照片备份，还应保存当前代码：

```bash
/opt/chores/backup.sh
tar -czf /opt/chores/backups/code-pre-v2.1-$(date +%F-%H%M).tar.gz \
  -C /opt/chores/jiawu-jifen app/chores app/requirements.txt deploy
```

启动后依次检查：管理者底栏不再出现打卡、阶段新建区默认收起、每日模式不显示星期
字段、动态日期可以前后翻阅、回收站可删除并恢复一条测试记录。回收站记录保留 7 天，
应用会在启动和请求时清理过期照片与正文；最小删除审计不会被清除。

假期流程固定为人工切换：提前建立未启用的每日假期阶段，放假时切换过去，结束后
切回原学期星期阶段。不要缩短或拆分原学期日期，也不要同时启用多个阶段。
