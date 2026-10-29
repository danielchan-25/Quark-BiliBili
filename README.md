# Quark-BiliBili

面向 Ubuntu 桌面和固定显示环境的哔哩哔哩推广辅助工具。连接由用户自行登录、授权使用的 Chromium CDP 实例，完成候选审核、文案库存、单条评论提交、精确核验与飞书汇总。

本项目由原 Windows 版本迁移而来，保留 MIT 许可证。默认目标是 **Ubuntu 24.04 LTS amd64 / Python 3.12 / ChromeManager-Linux**。仓库不提供账号、Cookie、密钥、推广链接、运行数据库或历史留言。所有示例账号都是虚构值，账号示例默认禁用。

## Linux 版变化

- 核心保留全局视频/正文去重、持久预留、UID 检查、TypeSafe 审核、最少 15 分钟间隔和单轮最多一条。
- 业务日期显式使用北京时间，不依赖主机默认时区；任务和审计日志继续使用原有 UTC 语义。
- Linux 准备进程使用独立进程组，超时有界回收准备程序及其驱动，不关闭外部浏览器。
- 私有 `data/schedules.toml` 配置账号和计划时段，公开源码不包含实际运营账号。
- 提供普通用户安装、离线 doctor、状态备份/校验/恢复和端点更新工具。
- 提供 Ubuntu 回归与隔离真实 Chromium CDP 测试；真实 B 站登录与目标主机验收单独记录。

完整范围、切换顺序和回滚流程见 [迁移计划](docs/MIGRATION_PLAN.md)，实际验证结果见 [验证记录](docs/VALIDATION.md)，智能任务编排见 [定时说明](docs/AUTOMATION.md)。

## 安装

先安装 Ubuntu 的 Python 3.12、venv 和可信 Chrome。浏览器实例建议由 [ChromeManager-Linux](https://github.com/danielchan-25/ChromeManager-Linux) 独立托管；同机使用各账号固定公开端口，正式部署必须启用管理器 relay。浏览器登录、扫码和验证码由用户手动完成。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ca-certificates git
git clone https://github.com/danielchan-25/Quark-BiliBili.git
cd Quark-BiliBili
bash scripts/install-user.sh
```

安装脚本以最终运行项目的普通用户执行，只创建缺失的配置，不覆盖已有 `.env`、账号 CSV 或计划，也不启动浏览器、不发送消息、不启用定时器。Windows `.venv` 不能复制到 Linux。

编辑本地文件：

| 文件 | 内容 |
| --- | --- |
| `.env` | TypeSafe 和飞书配置；没有审核密钥就不能补充未经审核的候选 |
| `data/account.csv` | 自己的账号、独立 CDP 地址、资源链接、已审核关键词；核对后手动启用 |
| `data/schedules.toml` | 同一批实际账号及北京时间小时列表，分钟固定为 40 |

也可使用普通 Chrome 的独立目录，不依赖某个管理器。例如在 Ubuntu 桌面以普通用户启动第一个账号浏览器，完成手动登录；第二个账号使用另一个目录和端口：

```bash
google-chrome --user-data-dir="$HOME/.local/share/quark-bilibili/promo-a" --remote-debugging-address=127.0.0.1 --remote-debugging-port=9600
```

Chrome 调试需要独立的用户数据目录，不能复用默认日常浏览器目录；参见 [Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)。普通桌面启动方式的开机恢复需自行配置，管理器的固定显示/systemd 方案已有独立部署文档。

可导出 `MEDIAFLOW_SCHEDULE_FILE=/absolute/private/schedules.toml` 选择其他计划文件；相对路径基于项目目录。该覆盖变量须放在进程环境中。

```bash
# 只读配置检查，不连接浏览器、不打开数据库、不发布或发送通知
bash scripts/doctor.sh
```

doctor 仅说明离线配置是否齐全，不证明已经具备发布条件。

## 新环境登记

已有生产数据应使用下一节迁移，不要重新建立空库来代替原历史。全新部署才按下面步骤登记：

```bash
bash scripts/run.sh account add --role promotion --cdp-url http://127.0.0.1:9600 --profile-label promo-a
bash scripts/run.sh account-check --name bilibili-YOUR_UID
bash scripts/run.sh resource add --category '资料' --url 'YOUR_REVIEWED_RESOURCE_URL' --keywords 'YOUR_KEYWORDS'
```

将返回的实际账号同步填入本地 CSV 和计划。CDP、登记 UID 和实际登录必须一致；资源链接也必须与配置一致。数据库不能替代 CSV。

```bash
# 候选预热：访问搜索和审核服务，不发布评论
bash scripts/run.sh promotion-candidates --account bilibili-YOUR_UID
# 实际浏览器只读检查；会调用一次 TypeSafe 审核，不提交或通知
.venv/bin/python tools/validate_workflow.py --account bilibili-YOUR_UID
# 只读预览，不消费文案或创建发布预留
bash scripts/run.sh promotion --account bilibili-YOUR_UID --dry-run
```

预览未就绪不能当作可发布；核验未找到也不能断言评论已删除。官方接口精确匹配 UID、评论 ID（已知时）及完整正文哈希，`verified` 不代表所有访客页面均可见。

## 从 Windows 迁移生产状态

先暂停旧机全部发布和日报计划，等待在途操作结束或完成只读核对。备份必须写到仓库外的私有目录：

```bash
python3 tools/migrate_state.py backup --source-root /path/to/stopped-project --destination /private/backup-bundle --confirm-stopped
python3 tools/migrate_state.py verify --bundle /private/backup-bundle
python3 tools/migrate_state.py restore --bundle /private/backup-bundle --target-root /path/to/Quark-BiliBili --confirm-stopped
```

在 Windows 创建备份时使用旧环境可用的 Python，并执行新版本的 `tools/migrate_state.py`；`--source-root` 指向原项目。目标已存在的业务文件不会被覆盖：恢复应在安装脚本创建本地配置之前完成，或者先使用未初始化的目标目录。恢复后再运行幂等安装。

必须保留数据库、全局预留、历史视频、任务轮次、日报、通知回执和计划启用时间。SQLite 通过 backup API 创建一致快照；文件清单带校验值。迁移 bundle 含私有数据，禁止上传 GitHub。本地计划 `data/schedules.toml` 可由原配置补齐；部署者仍需把实际账号写入新计划。

浏览器 Profile/Cookie、旧 PID、Windows venv、锁文件和临时文件不迁移。Linux 管理器重新建立浏览器目录并重新登录。端点需要改变时，用同一工具同步 CSV 和数据库登记：

```bash
.venv/bin/python tools/migrate_state.py account-endpoint --root /path/to/project --account bilibili-YOUR_UID --cdp-url http://127.0.0.1:9600 --confirm-stopped
```

只启用一台机器的发布计划。未知提交、`publishing` 轮次或未知通知不能因迁移而自动重跑。回滚也必须带回迁移后新增的完整状态。

端点更新若返回 `endpoint_recovery_required`，保持计划关闭，保留私有 `data/.migration-endpoint-*` 恢复目录并人工核对；不要直接重试或删除唯一恢复副本。外置 `MEDIAFLOW_SCHEDULE_FILE` 不由固定白名单自动追踪，需另行受控迁移。

## 定时发布和文案

每轮使用完整带 `+08:00` 的原触发时段：先 `prepare`，只有 `ready` 才继续。文案不足时由外部智能任务生成一条匹配当轮候选的 JSON，再经同一 `publish --phrase-file` 导入。不要用 cron 只运行一次 prepare 就认为完成发布。详见 [编排模板](docs/AUTOMATION.md)。

```bash
.venv/bin/python tools/scheduled_promotion.py --account bilibili-YOUR_UID --slot 'YYYY-MM-DDTHH:40:00+08:00' --phase prepare
# 仅在对应轮次ready且到达时段后执行；文案文件需经过当轮主题检查
.venv/bin/python tools/scheduled_promotion.py --account bilibili-YOUR_UID --slot 'YYYY-MM-DDTHH:40:00+08:00' --phase publish --phrase-file logs/phrase.json
```

项目内 `AI_API_*` 字段只是预留配置，并不自动调用模型生成文案。停止轮次须用户明确授权后 `prepare --resume`；已接受或结果不明的提交不重发，核验异常不撤销已有接受回执。

合并日报不连接浏览器，可先预览：

```bash
.venv/bin/python tools/daily_report.py --accounts bilibili-YOUR_UID bilibili-OTHER_UID --date YYYY-MM-DD --preview
bash scripts/daily-report.sh --preview --date YYYY-MM-DD
```

可选安装纯 Python 日报 systemd 单元：`bash scripts/install-user.sh --with-report-units`。安装后仍默认关闭；完成主机验收后才选择启用。不要与智能任务日报同时调度。只有 `sent` 回执可以报告送达；`sending/unknown/failed` 不自动重发。

## 测试

```bash
.venv/bin/python -B scripts/test.py
for script in scripts/*.sh; do bash -n "$script"; done
# 显式开启的隔离CDP测试；仅临时Profile和localhost页面，不访问B站
.venv/bin/python -m playwright install --with-deps chromium
MEDIAFLOW_LIVE_TEST=1 .venv/bin/python -B scripts/test.py test_linux_live_cdp.py
```

`Dockerfile.test` 与 GitHub Actions 用于 Ubuntu 隔离验证。CI 中的浏览器 sandbox 例外只用于测试 fixture，生产运行使用普通用户与正常 Chrome sandbox。

可选的历史 Excel 导出脚本依赖 Node.js 和 `@oai/artifact-tool`；它不属于 Python 发布运行时，不由安装脚本安装。需要该外部库的环境可将 `PYTHON` 指向 `.venv/bin/python` 后运行，不能把本地运营报表提交到 Git。

## 上线验收

至少核对两账号的实际登录、搜索、视频访问、主评论框、页面复用、UID、跨午夜日期、备份恢复、进程组超时和通知去重。再安排各一次单条发布验收，连续观察 48 小时，覆盖所有计划与 23:55 日报。Ubuntu 通用 CDP 测试通过不等于真实 B 站业务验证通过。

日志在 `logs/`，运行状态在 `data/`；本地 `.env`、CSV、TOML、数据库和备份不进 Git。新增审计记录脱敏密钥、Cookie、正文和 URL。请遵守相关平台规则并审核发布内容。

## 许可证

[MIT License](LICENSE)。
