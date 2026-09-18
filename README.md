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

- 不要提交 `.env`、浏览器用户数据、CDP 配置、数据库、导出报表或账号配置。
- 推广链接、话术、关键词与账号均应由部署者自行审核。
- 建议先使用 `--dry-run` 验证账号身份和候选池。
- 发布结果以平台实际页面和人工核验为准。

## 许可证

本项目以 [MIT License](LICENSE) 发布。
