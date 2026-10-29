# Linux 定时编排

本项目有两个独立层次：Python 负责身份、候选、频控、发布、核验和持久化；外部智能任务负责按时启动、需要时生成文案、继续同一轮 publish。直接定时运行一次 prepare 不会自动完成发布。

## 配置

1. 先在目标机完成安装和浏览器重新登录；保持新任务关闭。
2. 编辑私有 `data/schedules.toml`，将虚构示例账号替换为自己的登记账号。时区必须 `Asia/Shanghai`，分钟固定为 40，hours 为每账号自己的小时列表。
3. 如需不同位置，通过进程环境导出 `MEDIAFLOW_SCHEDULE_FILE=/absolute/private/schedules.toml`。相对路径按项目目录解释，不能只把此变量放进 `.env` 期待导入顺序改变。
4. 修改所有新任务中的项目绝对目录和 `.venv/bin/python`；不要复制 Windows 盘符或 `.venv/Scripts/python.exe`。

两个发布任务和合并日报任务分别创建。只从新环境启用后的下一应到时段执行，保持旧机任务关闭，不补发停机时段。

## 每账号发布任务提示词模板

将 PROJECT 和 ACCOUNT 替换为目标机绝对目录与实际账号，再按该账号私有 hours 创建北京时间定时计划：

```text
这是独立定时任务。先切换到 PROJECT，使用 PROJECT/.venv/bin/python。
如果目录或入口不存在，停止并报告，不在其他目录创建替代项目。
仅操作 ACCOUNT。本次最多提交一条评论。
按本次原始触发时间计算应执行的北京时间时段，传入完整带+08:00的--slot。
如果提前触发，可以先prepare，但publish必须等待SLOT到达后才执行。
不要补跑历史时段，不要自动恢复停止轮次。
先调用 tools/scheduled_promotion.py --account ACCOUNT --slot SLOT --phase prepare。
只有stage=ready继续；already_exists、already_running、stopped均结束本轮。
若phrase_ready=false，按返回候选主题与资源类别生成一条简短真实的推荐文案，
不编造资源内容、个人体验或承诺。JSON格式为[{"category":"类别","text":"正文 {url}"}]，
仅一个{url}，不硬编码链接，写入本地logs下JSON文件。
调用同账号、同时段--phase publish，需要时传--phrase-file。
如果返回ready且phrase_ready=false，按新的返回候选重新准备文案，再继续同轮publish。
工具之外准备失败，用同账号同时段--phase stop --stop-reason candidate_preparation_failed收尾。
正式入口创建FeishuNotifier，不替换为None，不省略原有失败告警。
平台接受或结果未知后只读核验，绝不重新提交；停止轮次仅在用户明确授权后prepare --resume。
检查notification_receipts；failed/unknown/sending不能报告发送成功，也不自动重发。
保留本地数据库、轮次、核验、回执和日志。不打印密钥、Cookie或完整推广链接，不提交私有数据。
实际发布完成、失败或需要用户处理时简短报告账号、本轮结果与公开视频链接。
```

## 每日 23:55 合并日报

```text
切换到PROJECT，使用PROJECT/.venv/bin/python。
按本次原触发时间确定应汇总的北京时间日期；跨午夜延迟仍使用原触发日。
读取本地计划中的账号，一次调用tools/daily_report.py --accounts ACCOUNT1 ACCOUNT2 --date YYYY-MM-DD。
不逐账号分别重复发送。只有对应账号与combined回执均为sent，才报告发送成功。
already_sent/already_running结束；failed/unknown/sending不自动重发，保留回执并报告。
不连接浏览器，不发布、点赞或刷新候选，不代替发布任务补跑。
```

安装脚本提供可选的 systemd 日报单元和 `scripts/daily-report.sh`，默认不启用。若选择 systemd 日报，就不要同时启用智能任务中的日报，以免重复调度。纯 Python 日报并不依赖模型；发布任务的补文案仍需要外部编排。

Linux 桌面本地任务需要机器开机、应用保持运行并具有项目权限；浏览器管理器需保持独立运行。开机启动、锁屏、注销和睡眠后的行为在目标主机验收。
