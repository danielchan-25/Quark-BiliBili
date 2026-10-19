# Quark-BiliBili

面向夸克网盘推广场景的哔哩哔哩运营自动化工具。它连接由用户自行管理、已完成登录的浏览器配置，通过候选池、频率控制和本地留痕完成推广与养号辅助任务，并在每日结束时向飞书发送汇总。

> 本项目不提供账号、Cookie、密钥、推广链接、运行数据库或历史留言数据。使用者须自行遵守哔哩哔哩、夸克网盘及其他相关平台的规则与适用法律，并对发布内容负责。

## 为什么开发

手工维护多个浏览器账号时，候选视频、评论频率、核验记录和每日统计容易脱节。Quark-BiliBili 将这些重复性工作收敛为可审计的本地流程：

- 使用 CDP 连接已有浏览器，不创建、不关闭，也不接管登录状态；
- 每日刷新候选池，避免推广执行时才临时检索；
- 按账号限制每日次数与最小间隔，避免重复发布；
- 记录发布、待核验和失败结果，支持人工复核；
- 每日通过飞书输出统一汇总，而非逐次打扰；
- 提供养号观看与点赞辅助，但不会自动投币或关注。

## 功能概览

| 模块 | 功能 |
| --- | --- |
| 推广任务 | 从候选池选取视频、生成或复用话术、提交评论并核验结果 |
| 候选预热 | 每日搜索已配置关键词，刷新可用视频候选池 |
| 频率控制 | 单账号每日最多 5 次成功/提交记录，最小间隔 15 分钟 |
| 任务汇总 | 按北京时间统计当日结果，推送飞书文本消息 |
| 养号辅助 | 观看首页视频，按配置尝试点赞并保存本地记录 |
| 今日头条归档 | 为已人工确认的留言提供本地归档命令 |

## 项目结构

```text
.
├── data/
│   └── account.example.csv    # 脱敏账号配置示例
├── mediaflow/                 # 核心模块：浏览器、配置、数据库、通知
├── tools/                     # 可选的本地报表工具
├── main.py                    # 命令行入口
├── requirements.txt           # Python 依赖
├── .env.example               # 环境变量模板
└── LICENSE                    # MIT 许可证
```

## 环境要求

- Python 3.11 或更高版本
- 已安装的 Chromium/Chrome，以及能够暴露 CDP 地址的浏览器配置管理工具
- 已完成登录、且由你本人授权使用的平台账号
- 可选：飞书机器人 Webhook（用于每日汇总）

## 手动部署

### 1. 安装依赖

```powershell
git clone https://github.com/danielchan-25/Quark-BiliBili.git
cd Quark-BiliBili
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
Copy-Item data\account.example.csv data\account.csv
```

在 `.env` 中按需填写 AI 服务和飞书机器人配置；不要将该文件提交到 Git。

### 2. 准备浏览器与账号

在你的浏览器配置工具中创建独立配置，手动完成登录后，将 CDP 地址填入 `data/account.csv`。随后登记账号：

```powershell
python main.py account add --role promotion --cdp-url http://127.0.0.1:9222 --profile-label promo-a
python main.py account-check --name bilibili-你的UID
```

添加推广资源：

```powershell
python main.py resource add --category "资料" --url "https://example.com/resource" --keywords "示例关键词"
```

### 3. 运行任务

```powershell
# 每日候选预热（建议在 00:00 执行）
python main.py promotion-candidates

# 预览，不提交评论或写入推广记录
python main.py promotion --account bilibili-你的UID --dry-run

# 执行一次推广
python main.py promotion --account bilibili-你的UID --slot 10:07

# 生成每日汇总（建议在 23:50 执行）
python main.py promotion-summary
```

可以通过 Windows 任务计划程序或其他调度器执行候选预热和每日汇总。需要平台登录或验证码时，应由用户手动完成。

## AI 辅助部署

可将以下说明交给可信的本地编码代理执行；在代理操作前仍应自行检查账号、推广内容与密钥：

```text
在 Quark-BiliBili 项目中创建 Python 虚拟环境，安装 requirements.txt 和 Playwright Chromium；
从 .env.example 与 data/account.example.csv 创建本地配置文件；不要读取、打印或提交 .env、
data/account.csv、数据库、浏览器用户数据或历史报表。完成后仅运行 --dry-run 验证连接。
```

AI 只能协助安装与生成配置；浏览器登录、验证码、推广内容审核和生产执行必须由你负责。

## 配置说明

| 文件 | 是否提交 | 用途 |
| --- | --- | --- |
| `.env.example` | 是 | 配置字段模板，不含任何密钥 |
| `.env` | 否 | AI 与飞书等实际密钥 |
| `data/account.example.csv` | 是 | 脱敏配置示例 |
| `data/account.csv` | 否 | 实际账号、CDP 地址、关键词和推广链接 |
| `data/mediaflow.db` | 否 | 本地运行与核验历史 |

## 常用命令

```powershell
python main.py account list
python main.py resource list
python main.py promotion-candidates
python main.py promotion-summary --date 2026-01-01
python main.py nurture --account bilibili-你的UID
python main.py toutiao list --account toutiao-你的UID
```

## 安全与隐私

### 飞书告警与每日 23:55 日报

推广定时任务使用 `tools/scheduled_promotion.py`，按北京时间时段传入完整的 `--slot`：先 `--phase prepare`，只在返回 `ready` 后 `--phase publish`；新增文案通过 `--phrase-file` 传入本地 JSON。浏览器、账号、数据库、候选、审核或文案准备失败统一落盘并告警。工具外准备异常可用 `--phase stop --stop-reason candidate_preparation_failed` 收尾。恢复停止轮次须用户授权并使用 `prepare --resume`，每次尝试有独立 `attempt_id`，再次失败仍会告警。同一尝试的重复触发不会重复发消息或评论。

| 定时任务名称 | 北京时间每日时段 |
| --- | --- |
| `quark-bilibili-35199508` | 00:40、05:40、10:40、15:40、20:40 |
| `quark-bilibili-1085803742` | 01:40、06:40、11:40、16:40、21:40 |

新账号每次调用都显式传入 `--account bilibili-1085803742`，包括 prepare、publish 和 stop；省略账号仍兼容原有 35199508 任务。入口按账号校验时段、登记 UID 和浏览器实际 UID，并使用独立轮次文件。时段定义集中在 `mediaflow/promotion_schedule.py`；任务创建时的启用时间保存在 `data/promotion-schedules/`，日报不会把新任务创建前的时段算作漏跑。全局视频和文案去重、每账号每天最多 5 条和至少间隔 15 分钟规则仍生效。

每日 **23:55（北京时间）** 的独立 Codex 定时任务 `quark-bilibili-daily-report-35199508` 执行：

```powershell
.\.venv\Scripts\python.exe tools/daily_report.py --accounts bilibili-35199508 bilibili-1085803742 --date YYYY-MM-DD
```

日报将两个账号合并成一条飞书消息，按下方格式先给出任务成功、失败总数，再按账号和计划时段列出结果。成功项附公开视频链接，失败项写明停止原因或缺失执行记录；恢复成功只按该轮最终结果统计一次。已提交但未确认的结果另列“待确认”，尚未完成的轮次另列“未完成”，不计为成功。总数与逐时段条目使用相同口径，不把发布记录、任务记录及重试历史重复相加。

```text
【Quark-BiliBili】
YYYY-MM-DD
任务状态：成功n,失败n

账户: 35199508
- 00:40,任务成功：https://www.bilibili.com/video/BV...
- 05:40,任务失败：（原因）
...

账户: 1085803742
- 01:40,任务成功：https://www.bilibili.com/video/BV...
...
```

日报通过只读数据库汇总当天发布、待确认、失败、停止、恢复及已有核验状态，不连接浏览器；浏览器关闭也能发送。日期固定为调度触发日，跨午夜延迟不会汇总错天。合并日报及其发送回执保存在 `data/daily-reports/combined-账号列表-日期.json` 和同名 `-receipt.json`，各账号仍保留独立快照和回执，一天只发送一次。旧的 `--account` 调用继续有效，并与合并发送共享账号回执，已发账号不会重发；`sending/unknown/failed` 不自动重发。某账号统计异常或发送状态不明不会阻止另一账号检查，会在结果中标明并告警。它与原有 `promotion-summary` 的实时浏览器复核用途不同。

```powershell
# 本地预览：不发飞书、不改变历史日报和回执，也不连接浏览器
.\.venv\Scripts\python.exe tools/daily_report.py --accounts bilibili-35199508 bilibili-1085803742 --date YYYY-MM-DD --preview --output logs/daily-report-preview.txt
```

告警回执保存在 `data/notification-receipts/` 或 `data/scheduled-runs/notification-receipts/`，日志见 `delivery.jsonl`、`daily-report.jsonl`、`scheduled-alerts.jsonl`。只有飞书成功回执才记为 `sent`；拒绝记为 `failed`，超时、断连、非法回执或发送中断记为 `unknown`，不自动重试不明结果。日报失败也发送独立失败告警。项目不能在机器关机、网络中断或飞书服务拒绝时保证送达；本地回执和调度结果用于发现这些情况。

### 流程验证与日志

候选池由两个账号共享。库存只计算通过 TypeSafe 审核、状态可用、无历史发布或预留的视频；总记录数包含已用和拒绝项，不能视为可发布数量。每轮 `prepare` 检查库存，低于 **10 条**时提前补充，目标 **30 条**；没有可评论候选时也尝试补充。只使用当前账号配置的关键词，每个关键词最多搜索 4 页、每页 20 条，跳过已使用、已预留及信息未变的审核通过视频；同一关键词下信息未变的拒绝结果缓存 24 小时。每次补充最多审核 120 条，预算 10 分钟，自动尝试间隔至少 15 分钟；两个账号的补充通过共享锁协调。

候选补充逐条保存审核结果，后续接口异常不会撤销此前已入池的视频。有已验证可评论的合法候选时，局部补充异常不会阻断本轮发布；没有合法候选时仍停止并发送原有飞书告警。审核、访问、账号评论权限及去重检查不降低。补充不执行发布、点赞或文案操作；提交之后也不会再补充候选。

```powershell
# 只读库存检查
.\.venv\Scripts\python.exe tools/candidate_pool.py --account bilibili-35199508
# 用户主动补池：以30条为目标，可在自动冷却期内主动检查补充
.\.venv\Scripts\python.exe tools/candidate_pool.py --account bilibili-35199508 --refill
```

库存、补充原因、检查页数、审核数量和结果写入 `logs/candidate-pool.jsonl`；共享补充状态保存于 `data/candidate-pool/maintenance.json`。命令行补池会拦截评论、点赞和飞书发送。平台暂时没有合格新视频或接口不可用时，补充可能未达到目标，不能用未审核或已使用的视频凑数。

评论提交接口已接受并提供有效评论 ID 时，先将该 ID 与已接受状态一起保存，再用它进行精确核验。核验暂未找到或发生异常不会清空已保存的提交回执 ID，也不会触发重发；接口缺少评论 ID 时仍保留已接受结果并做有限查找。

```powershell
# 模拟测试：不连接平台，不发布、不点赞、不发送真实通知
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 真实只读检查：登录、搜索、视频访问、TypeSafe、已有评论核验、预览
.\.venv\Scripts\python.exe tools/validate_workflow.py --account bilibili-你的UID
```

只读验证工具使用 SQLite 只读连接，并拦截发布、点赞和通知发送。它会调用一次 TypeSafe 审核，结果和独立日志保存在 `logs/validation-时间/`。预览因频率限制、无候选或文案耗尽而停止时，`ready=false`，不代表已具备发布条件。`--dry-run` 不修改任务记录、候选状态、文案使用次数或预留记录。

验证报告分开记录 `functional_checks_passed`（功能检查）、`publication_ready`（当前预览条件）和 `historical_comment_verified`（本次历史评论核验）。预览未就绪为 `blocked`，原因使用 `phrase_exhausted`、`frequency_limit` 等固定标识。历史评论未确认为 `warning`，保留接口原因、码和检查范围；接口或浏览器异常仍为 `failed`。因此不能只凭某个步骤函数返回完成，判断已经具备发布条件。

评论复核使用官方视频信息接口取得视频 ID，已知评论 ID 时优先查询评论详情，再做有限分页检查。必须精确匹配作者 UID、实际评论 ID（如已知）和完整文案 SHA256；不再仅凭 DOM 文本或作者链接片段匹配。`verified` 表示官方接口可读取并匹配，不证明所有访客的页面都可见；`unconfirmed` 表示本次未确认，详情接口 12006 单独标记 `remote_comment_unavailable`，不能据此判断已删除或自动重发。每次接口请求有独立步骤和耗时日志，单请求最多 10 秒、总预算 45 秒、每种排序最多 5 页。`tools/recheck_comments.py` 仅保存新的核验观察，不修改历史发布记录。

文案导入使用 `tools/import_phrases.py`，整批验证类别、长度、唯一 `{url}` 占位符和禁止硬编码链接后，再通过事务保存；任何非法条目或写入异常都不会留下部分导入。继续保留跨账号的已用正文及预留正文去重。定时准备返回 `phrase_ready=false` 时，应按当轮候选主题生成文案并通过 `--phrase-file` 交给既有入口，不能绕过文案检查。

候选筛选除检查视频可访问外，还检查主评论输入框是否可用；页面明确禁用评论时，跳过该候选并记录 `candidate_comments_disabled`。评论权限可能因账号而异，因此不会据此把视频对所有账号永久禁用。检查只聚焦输入框，不输入文案、不点击发布，也不消耗文案或创建发布预留。

发布时只定位可见的主评论输入框，不使用隐藏的 `#reply-commentbox`。输入文案前发生 Playwright 超时，最多重新导航两次（含首次共3次）；每个页面三次准备共用90秒预算，每次最多使用30秒。每次候选选择共用330秒预算，最多检查3个实际准备失败视频，另允许快速跳过最多9个明确禁评视频；加载超时或主输入框失效时，在消费文案和创建预留之前切换候选。明确禁评对当前账号冷却360分钟，瞬时准备失败冷却30分钟，记录于 `comment_candidate_cooldowns`；不会对其他账号永久禁用视频，冷却到期自动恢复选择资格。预览只做本轮避让，不写冷却记录。预算限制后续步骤与重试；浏览器或 CDP 卡顿仍可能导致单次调用实际耗时超过预算。

正式发布流程会在同一浏览器连接内保留刚检查成功的任务页面，30秒内且URL、浏览器上下文一致时复用，提交前再次检查权限和焦点，避免再次导航。过期、关闭、跳转或上下文改变时先关闭任务持有的页面再重新准备；连接结束只清理任务页面并断开，不关闭用户浏览器。`prepare` 和 `publish` 是独立进程，页面不会跨进程复用。发布预检目标改变但资源未变、没有新传入视频专用文案时，可在同一次尝试内更新候选并保留变更记录；传入专用文案时返回 ready 与 phrase_ready=false，按新候选重新生成文案后继续同一轮，避免把文案用于另一视频。已经创建发布意图后不切换视频，输入后及点击提交后不做恢复重发，已接受或结果不明沿用原有核验及去重保护。

`logs/browser-flow.jsonl` 分步骤记录导航、权限、输入框定位、点击、焦点、文案输入和发布按钮检查；准备失败保存脱敏异常和结构状态。`comment_preparation_network` 记录准备编号、预算、实际耗时、评论接口HTTP状态计数及最多12条失败元数据（脚本/页面/评论接口类别、HTTP状态、网络错误码、页面异常计数），不保存请求URL、查询参数、Cookie、响应正文、文案或截图。`logs/workflow.jsonl` 中 `candidate_preparation_skipped`、`candidate_selection_exhausted` 说明候选切换与预算耗尽原因。候选耗尽后的失败仍进入原有任务记录和飞书告警；外部页面或网络故障无法保证完全消除。

`tools/validate_comment_preparation.py --accounts bilibili-35199508 bilibili-1085803742` 可以只读验证两个账号的身份、候选输入框准备与页面复用。它使用只读数据库，并拦截评论输入、发布、点赞和飞书发送；比较验证前后业务数据，报告及步骤日志保存到 `logs/comment-preparation-validation-*/`。

`tools/observe_promotion.py --account bilibili-你的UID` 默认仅预览；只有显式添加 `--publish` 才会提交。每轮推广最多提交一条评论，提交前保存意图。接口已接受后，核验异常不会改记为发布失败；提交状态不明则保留待核验记录并计入频率限制，不自动重发。

日志使用 JSONL，包含 UTC 时间、`trace_id`、`span_id`、父步骤 ID、开始/完成/失败事件及耗时。业务结果以 `promotion_result.status` 和原因判断；步骤完成仅表示函数正常返回。支持通过 `MEDIAFLOW_LOG_DIR` 将日志写到独立目录。

| 日志文件 | 内容 |
| --- | --- |
| `workflow.jsonl` | 配置、账号、资源、文案、候选筛选、养号、汇总及命令步骤 |
| `database.jsonl` | 数据库打开、迁移和关闭 |
| `browser-flow.jsonl` | 浏览器连接、登录、搜索、访问、提交与核验 |
| `promotion-flow.jsonl` | 频率、身份、预留、提交意图、结果落盘、跳过原因 |
| `promotion-candidates.jsonl` / `typesafe-usage.jsonl` | 候选刷新、审核、缓存、耗时和用量 |
| `notifications.jsonl` / `promotion-alerts.jsonl` | 飞书调用结果、告警成功或失败 |
| `daily-review.jsonl` / `scheduled-promotion.jsonl` | 每日复核与定时轮次 |

新增审计事件统一脱敏密钥、Cookie、请求体及 URL。历史日志不被重写；数据库仍保留发布核验所需的原始内容。CLI 预览会显示完整文案，只应在可信的本地终端查看。

- 不要提交 `.env`、浏览器用户数据、CDP 配置、数据库、导出报表或账号配置。
- 推广链接、话术、关键词与账号均应由部署者自行审核。
- 建议先使用 `--dry-run` 验证账号身份和候选池。
- 发布结果以平台实际页面和人工核验为准。

## 许可证

本项目以 [MIT License](LICENSE) 发布。

定时发布预检与 main.promotion 复用同一浏览器连接和已就绪页面，保留 UID、频率和去重复查。prepare 与 publish 均可补充空候选池；publish 返回 ready、phrase_ready=false 时，应按返回候选重新补齐文案并再次调用同一账号、同一时段的 publish，尚未提交时不会将文案耗尽直接记为失败。浏览器导航、评论区加载、输入框权限、定位或焦点异常分别记录具体原因、步骤与底层异常类型，不再统一报库存准备失败；真实浏览器故障仍停止并告警。

若3个视频各3次均在 comment_section 阶段发生 TimeoutError，记录 comment_section_repeated_timeout、total_attempts=9、diagnosis_required=true，以及三个视频逐次失败阶段；飞书明确提示按日志排查持续性故障。混合导航、权限等错误不算作9次评论区超时，不自动重新执行停止轮次。浏览器调用异常超出预算时可能提前结束，实际次数如实记录。

评论区等待前先滚动任务页面触发懒加载，避免等待15秒后才触发组件挂载。准备失败后 comment_runtime_diagnosis 使用独立、限时的本地CDP只读请求比较原生页面与Playwright的DOM响应；只保存结构布尔值和状态，不保存DOM正文、Cookie或完整URL。native_main_responsive不代表评论区已经就绪；只有权限、主输入框、焦点完整检查通过才可发布。


### 评论准备恢复与并发保护

每个浏览器端点使用操作系统文件锁，同一浏览器的项目任务互斥；定时轮次还有独立进程锁。正式 CLI 的 prepare 在受监督子进程内运行，总上限1200秒；超时确认子进程退出并校验执行身份后才保存停止结果和告警。该监督不用于 publish，不能因此重复提交。

导航后先进行最多5秒的自动化 DOM 读取检查，失败单独记录为 `dom_probe` / `browser_dom_access_failed`；主输入框就绪使用250毫秒定时轮询。普通超时重载同一视频；确认原生页面可读但 Playwright 读取失败，或评论组件、头部、主框未生成时，优先换用新的任务页面。每视频仍最多三次尝试、共用90秒预算。诊断失败不会覆盖原始错误；禁评提示晚到时直接跳过，不再无效重载。所有恢复均在输入之前，已接受或状态不明的提交不重发。

账号级可评论候选缓存保留6小时，只调整选择优先级，不跳过身份、审核、全局去重、访问性或权限复查。`tools/comment_stock.py --account bilibili-你的UID --target 3` 可检查库存并写入缓存与冷却，不发布、点赞、输入或通知。

`comment_failure_analysis` 记录每次失败的结构分类和接口状态码、业务码增量；`comment_preparation_ready` 记录成功尝试及是否经过恢复。日志不保留接口响应正文。主机高负载或站点渲染异常仍可能造成超时，单次验证通过不保证长期每轮成功。
