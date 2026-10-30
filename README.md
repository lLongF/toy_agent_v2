# 玩具助手文本测试

用 Excel 用例，通过 WebSocket 把话发给玩具助手，收集回复，再自动判分。

适合第一次接触本仓库的人：先会跑，再按需改用例或加玩具。完整评测链路（建连、分类型采集、双裁判、出报告）见 [评测流程.md](评测流程.md)。

## 目录

```
toy_agent/
├── text_input_test.py   # 入口：采集完再评分
├── config.py            # 全仓库固定配置（字段都有注释）
├── collection.py        # OTA / WebSocket / 文本采集
├── mcp.py               # MCP 工具调用校验
├── testers.py           # 按类型采集（知识库只采一轮）
├── judge.py             # Qwen + DeepSeek 裁判
├── report.py            # JSON / Markdown / Excel 报告
├── cli.py               # 命令行
├── toy_profiles.py      # 多玩具档案加载
├── daily_run.py         # 每日跑测 + 发企微
├── notify_wecom.py      # 把结果 Excel 发到企微
├── toys/
│   ├── README.md        # 如何加新玩具
│   ├── _template/       # 新玩具模板
│   └── default/         # 当前默认玩具（沿用 data/ 里的用例和工具）
├── data/
│   ├── 测试样例.xlsx     # 默认用例
│   └── 工具描述.txt     # 默认 MCP 工具清单
└── reports/             # 跑完后的报告（按玩具分子目录，不入库）
```

## 环境

- Python 3.10+
- 能访问待测环境和评分接口（默认 Qwen/DashScope + DeepSeek V4 Pro 双裁判）

```powershell
python -m pip install requests websockets openpyxl openai
```

建议用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install requests websockets openpyxl openai
```

或：

```powershell
python -m pip install -r requirements.txt
```

## 怎么跑

先看配置是否正常（不连服务、不跑用例）：

```powershell
python .\text_input_test.py
python .\text_input_test.py --list-toys
```

真正跑测试，加上 `--run`。默认玩具是 `default`（`--toy` 填的是 `toys/` 下的文件夹名）：

```powershell
python .\text_input_test.py --run
python .\text_input_test.py --toy default --run
```

后续加新玩具后，只换 `--toy`。各玩具只换测试样例、工具描述和 token，裁判和其余配置不变：

```powershell
python .\text_input_test.py --toy story-bear --run
```

指定服务令牌和评分 Key（推荐，不要把密钥写进代码）：

```powershell
$env:TOY_AGENT_TOKEN = "服务端令牌"
$env:DASHSCOPE_API_KEY = "Qwen 评分 API Key"
$env:DEEPSEEK_API_KEY = "DeepSeek 评分 API Key"

python .\text_input_test.py --run --token $env:TOY_AGENT_TOKEN --api-key $env:DASHSCOPE_API_KEY --deepseek-api-key $env:DEEPSEEK_API_KEY
```

全部参数：`python .\text_input_test.py --help`

跑得慢可以加 `--fast`。

## 脚本在做什么

1. 按 `--toy` 读取该玩具的 Excel 用例和工具描述
2. 先请求 OTA，拿到 WebSocket 地址
3. **同一「类型」共用一条连接**（记忆类用例靠这个保留上下文；换类型会新建连接）
4. 按行发送「消息列表」，收集回复和工具调用
5. 全部跑完后再用 Qwen、DeepSeek 两个裁判打分
6. 把结果写到 `reports/<玩具id>/`

不加 `--run` 只打印当前配置，不会发请求。

## Excel 怎么填

优先读名为 `Test case` 的工作表。列如下：

| 列 | 必填 | 说明 |
| --- | --- | --- |
| 类型 | 是 | `基础问答` / `反向安全` / `情感交互` / `短期记忆` / `长期记忆` / `知识库` / `MCP调用`。空单元格沿用上一行 |
| 用例编号 | 是 | 行号标识 |
| 消息列表 | 是 | 用户说的话。一格里用 `/` 可连发多句 |
| 预期结果 | 是 | 给裁判看的通过标准 |
| 是否工具调用 | 否 | `是` 或 `否`。**空着默认「否」，不强制调工具** |
| 工具名称 | 否 | 期望调用的工具名，例如 `get_weather` |
| 工具参数文字描述 | MCP 用例建议填 | 用来核对参数，例如 `音量调成70%` |

当前支持的类型和判分方式：

| 类型 | 怎么判 |
| --- | --- |
| 基础问答、反向安全 | 大模型对照「预期结果」打分 |
| 情感交互 | 看是否共情、安抚、情绪方向对 |
| 知识库 | 只采集一轮回复，大模型对照「预期结果」打分 |
| 短期记忆 / 长期记忆 | 看有没有记住前文 |
| MCP调用 | 标了「是」则核对是否调了对应工具；标了「否」或空着则按问答评分 |

## 怎么打分

默认两个裁判一起打分：

- `qwen-plus`（DashScope）
- `deepseek-v4-pro`（DeepSeek 官方 API）

两边都可用时，**平均分 ≥ `0.7` 即通过**（不要求两边各自都过）。只要 Qwen 时加 `--disable-deepseek-judge`。

Excel 结果列：

| 列 | 含义 |
| --- | --- |
| 测试结果是否通过 | `通过` / `不通过` / `评分失败` |
| 实际返回内容 | 助手回复。知识库也只写这一次回答 |
| 是否有mcp调用 | 本轮是否实际调了工具 |
| Qwen评分 / Qwen原因 | qwen-plus 的分数和评语 |
| DeepSeek评分 / DeepSeek原因 | DeepSeek V4 Pro 的分数和评语 |
| 综合评分 | 两边平均分，用来判定是否通过 |

裁判接口失败（Key 无效、超时、解析失败）时，「测试结果是否通过」标为 **评分失败**，不记成业务不通过。

## 报告看哪里

默认在 `reports/<玩具id>/`：

- `<玩具id>_text_result_<时间>_export.xlsx`：在原用例后面加了上表那些列。日常看这个就够
- `<玩具id>_text_summary_<时间>.md`：简要摘要
- `<玩具id>_text_result_<时间>.json`：最全的原始记录，排障时用

指定导出路径：

```powershell
python .\text_input_test.py --run --export-xlsx ".\reports\本次结果.xlsx"
```

不要把 `--export-xlsx` 指回原用例文件，以免把结果写进用例表。

## 常见问题

**找不到用例文件**  
确认 `toys/<id>/测试样例.xlsx` 存在（默认玩具 `default` 用 `data/测试样例.xlsx`）。`--toy` 填的是文件夹名，例如 `default`。

**全部评分不通过 / 全是评分失败**  
检查 `--api-key` / `DASHSCOPE_API_KEY` 以及 `--deepseek-api-key` / `DEEPSEEK_API_KEY` 是否有效，并确认能访问 DashScope 和 `https://api.deepseek.com`。裁判客户端挂了会标「评分失败」，不是业务不通过。

**该调工具却没调**  
1. 当前玩具的工具描述能否被脚本读到（`default` 用 `data/工具描述.txt`，新玩具用 `toys/<id>/工具描述.txt`）  
2. Excel 里「是否工具调用」是不是明确填了 `是`，工具名是否写对  
3. 打开 JSON 报告看 `mcp_calls`

**跑得太久**  
每条消息最多等约 45 秒。可把 `--response-collect-timeout` 调小，或先裁一批用例试跑。

## 每天 10 点自动跑，结果发企微

仓库里已经加了 GitLab CI。**push 代码不会跑测试**，只有定时计划或网页上手动 Run pipeline 才会跑。跑完后把结果 xlsx 发到企微群。

### 1. 配置 CI 变量

打开 GitLab 项目 → **Settings → CI/CD → Variables**，添加（勾选 Masked）：

| 变量名 | 值 |
| --- | --- |
| `WECOM_WEBHOOK_URL` | 企微群机器人 Webhook 完整地址 |
| `TOY_AGENT_TOKEN` | 默认玩具服务令牌 |
| `TOY_ID` / `TOY_IDS` | 要跑的玩具 id；多个用逗号分隔，例如 `default,story-bear` |
| `DASHSCOPE_API_KEY` | Qwen 评分 API Key |
| `DEEPSEEK_API_KEY` | DeepSeek V4 Pro 评分 API Key |

机器人地址不要写进代码，只放在 CI 变量里。

### 2. Runner

当前按 **本机 Windows + shell** 配置（不用 Docker）。电脑要开机，Runner 保持 Online，并且能访问玩具助手、企微、DashScope 和 DeepSeek API。

服务账号要能直接运行 `python`（建议把 Python 加到**系统**环境变量 PATH，不要只加当前用户）。

### 3. 创建每天 10 点的计划

GitLab 项目 → **Build → Pipeline schedules**（有的版本在 **CI/CD → Schedules**）→ **New schedule**：

- Description：`每日10点文本测试`
- Interval Pattern：自定义 cron，填 `0 10 * * *`
- Timezone：**Asia/Shanghai**（北京时间 10:00）
- Target branch：默认分支（一般是 `main` 或 `master`）
- 保存并启用

到点后会跑 `daily_run.py`：按 `TOY_ID`/`TOY_IDS` 依次测各玩具，再把 `reports/<玩具id>/daily_result_*.xlsx` 发到群里，并带上通过 / 不通过 / 评分失败条数。

想立刻验证：在 Pipeline schedules 里点一次 **Play**，或 **Build → Pipelines → Run pipeline**。

本地手动测通知（不跑完整用例）：

```powershell
$env:WECOM_WEBHOOK_URL = "你的Webhook"
python .\notify_wecom.py .\reports\某次结果.xlsx
```

本地一次跑测+发群：

```powershell
$env:WECOM_WEBHOOK_URL = "你的Webhook"
python .\daily_run.py
```

## 加一个新玩具

每个玩具只换三样：测试样例、工具描述、token。OTA、设备、裁判都固定。

1. 复制 `toys/_template/` 为 `toys/<id>/`（id 就是 `--toy` 要填的文件夹名）
2. 在 `toy.json` 填该玩具 token
3. 放入该玩具的 `测试样例.xlsx` 和 `工具描述.txt`
4. `python .\text_input_test.py --toy <id> --run`

详见 [toys/README.md](toys/README.md)。固定配置字段说明见 `config.py`。

## 注意

- 不要把真实 Token、API Key、企微 Webhook 提交进 Git
- 报告里有用户原话和模型回复，外发前检查是否要脱敏
