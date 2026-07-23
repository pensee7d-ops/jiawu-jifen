# 家务积分站 2.1

FastAPI + Jinja2 + SQLite 的家庭行为激励工具，包含：

- 不清零的积分账本、每日/周末结算窗口和达标时长奖励；
- 基础/奖励/人工调整时长，以及可审计的电脑开关机记录；
- 管理者发布、领取、提交、驳回重做和审核计分的积分任务；
- 相机/相册照片上传（含 HEIC）与手机优先的像素游戏界面；
- 按凌晨 4 点逻辑日翻阅动态、手动阶段切换与 7 天管理回收站；
- 旧打卡周期的只读历史和无损增量迁移。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 配置 CHORES_DB_PATH、CHORES_PHOTO_DIR、两个角色口令和 SECRET_KEY
bash run.sh
```

首次升级后请用监管者账号进入「阶段规则」，创建阶段后手动点击“切换到此阶段”。没有有效阶段时
积分仍会记账，但不会自动生成基础时长或达标奖励。完整部署流程见
[`../deploy/SETUP.md`](../deploy/SETUP.md)。

## 测试

```bash
.venv/bin/python -m pytest -q
```

弟弟使用 `CHORES_CHECKIN_PASSWORD`；家人使用 `CHORES_SUPERVISOR_PASSWORD`，
登录后选择名字，所有人工调整和记录更正都会署名。

学期中的短假期不需要修改原学期日期：提前保存一个未启用的每日阶段，假期开始时
切换过去，结束后再切回原学期阶段。切换会立即截断旧窗口、替换当天基础电脑额度，
并保留完整切换审计。
