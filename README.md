# 玩具助手文本测试

AI 开发前请阅读 [项目开发约束与 AI Coding 边界](Agent.md)；Codex 指令入口为 [AGENTS.md](AGENTS.md)。

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

- Python **3.11.x**（CLI、Streamlit、本机 Windows Runner 统一使用此系列）
- WSS 连接保留证书与主机名验证，并加载已有 `certifi` CA 包，兼容 macOS Python 默认 CA 文件缺失的环境。
- 能访问待测环境和评分接口（默认 Qwen/DashScope + DeepSeek V4 Pro 双裁判）

```powershell
python -m pip install -r requirements.txt
```

建议用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

或：

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` 是完整依赖锁文件，包含 Streamlit 及其表格、图表、网络依赖。
不要单独执行 `pip install --upgrade streamlit`，以免 CLI 和界面的环境发生漂移。
版本基线与升级流程见 [环境与Streamlit.md](环境与Streamlit.md)。

## Streamlit 评测界面

安装依赖后启动（Windows、macOS、Linux 命令一致）：

```sh
python scripts/check_environment.py
python -m pip check
python -m streamlit run streamlit_app.py
```

打开终端显示的本地地址，默认 `http://127.0.0.1:8501`。七个一级菜单提供评测与实时对话：

- **评测总览**：最近真实报告、通过率、分类指标和失败关注项；没有报告时显示空态。
- **玩具管理**：创建/编辑/禁用玩具，配置 OTA/WS 接口、环境、凭据环境变量引用与支持/不支持/未验证能力；配置校验不联网，连接检查需明确点击。
- **测试集与工具管理**：公共/当前玩具专属测试集，新建空表或复制已有集，维护用例；导入/预览/保存嵌入式工具定义、复用已有工具来源、备份恢复。远程 MCP 来源保留在高级兼容入口。
- **评测配置与执行**：全量/按类别/指定行，分类与单条阈值管理，独立任务、重跑、真实日志，以及每日 / 文件事件触发管理。
- **结果分析**：统一统计、类别与状态筛选、单条诊断、JSON / Excel / Markdown 下载及当次规则快照。
- **发版自测**：面向后端开发人员，填写 WebSocket 地址、选择玩具、输入临时 Token、选择测试集和裁判模型即可运行。直接连接填写的地址，不请求 OTA；原评测入口仍保留 OTA 流程。不再提供运行环境选项，实际目标完全由填写的接口地址决定。高级设置支持设备 MAC 和客户端 ID，需与服务端绑定规则匹配。沿用现有评分与设备互斥，后台任务每 3 秒刷新状态和日志；支持历史记录、失败/异常筛选、单条诊断、Excel 下载与完整结果跳转。裁判模型复用已有注册配置，Key 支持本次临时输入或已有凭据引用；Token 不保存到任务配置、日志或报告。接口 URL 不接受内嵌凭据、查询参数或片段，鉴权 Token 单独填写。
- **玩具实时对话**：二级菜单为曦仔、粤悦、仪宝、闹闹，均已嵌入各自的对话网页，保留各自链接的设备 MAC 与产品名称参数。对话区域随窗口高度调整，底部输入栏保持可见；聊天记录较多时，在聊天记录区滚动查看。点击原网页的“唤醒”后开始对话，语音输入需允许浏览器麦克风权限。对话页不启动自动评测或裁判评分。

环境与版本信息放在侧栏展开项中。每次界面或事件任务最长运行 4 小时。

独立发版自测演示地址为 `http://127.0.0.1:8501/release`（服务器部署后将域名替换为部署地址）。页面隐藏平台侧栏，仅展示自测表单、运行进度、停止按钮、历史任务和结果；完整报告在该路由内查看并可返回。该入口与工作台「发版自测」菜单共用任务服务、设备互斥、测试集、裁判和报告，原工作台 `/` 与实时对话 `/chat` 保持原有入口。

独立 `/release` 采用浅紫白底卡片布局，左侧配置自测，右侧展示当前玩具、所选测试集与用例数量、裁判及凭据读取状态，下方集中展示运行与结果。凭据“已读取”只表示当前服务进程能解析 Key，不代表已通过接口鉴权；页面展示不会调用设备或裁判。窄屏自动改为上下排列，工作台内自测保留原布局。

结果区与配置区统一卡片风格，提供真实任务结论、Excel 下载和完整报告入口；分别展示通过、未通过、评分失败、执行异常与未执行，业务通过率沿用已有统计口径。支持关键词搜索、状态筛选以及点击表格行或选择编号查看右侧诊断，运行日志位于结果下方。失败或停止时明确标记结果不完整，不因已有部分报告而显示为执行成功。

发版自测中，每个已选裁判旁均可临时输入 API Key，手动输入优先，启动成功后清空输入框，不写入模型注册表。留空时使用现有凭据引用；默认 Qwen 读取 `DASHSCOPE_API_KEY`，DeepSeek 读取 `DEEPSEEK_API_KEY`。服务器部署时将变量配置到实际运行 Streamlit 的服务环境并重启服务，即可直接选模型运行。自定义裁判按其 `credential_ref` 指定的环境变量读取。真实 Key 不提交 Git。评测配置与执行仍保留裁判选择和临时凭据输入，不再显示裁判模型管理表单。

发版自测运行时展示启动校验、连接准备、采集会话、裁判评分和生成报告阶段。采集按已处理类型会话计数，评分按已完成评分任务计数，每 3 秒刷新；进度表示当前阶段的处理数量，不代表总耗时或用例通过率。失败任务保留失败状态，不显示完成 100%。同设备已有界面任务时显示其记录并禁用重复启动；旧任务缺少来源标记时从报告快照恢复展示，不改写历史文件。

运行状态区提供“停止运行”，仅终止所选任务的后台进程，退出后释放设备占用，历史记录显示“已停止”。日志和已生成的报告保留，未完成部分不计为通过；停止不会额外调用设备或裁判。
通过率排除评分失败和未执行；MCP 规则得分为 0/1（通过标准固定 1），计入业务通过率但不混入语言平均综合分。调用命中与协议执行错误分别保存，执行异常列为未执行并标记任务失败。

### 公司玩具 / 小智 AI 场景切换

在“评测配置与执行 → 凭据（仅本次会话）”切换接入场景：

- **公司玩具**：保留现有 OTA/WS 链路，输入本次玩具 Token；裁判 Key 沿用当前输入和校验方式。
- **小智 AI**：OTA 固定为 `https://api.tenclass.net/xiaozhi/ota/`，无需公司 Token；复用用户在控制台登录后已验证成功的固定设备 MAC 与客户端 UUID，后续运行不再随机换身份。WS 地址、Token、协议版本使用当次 OTA 响应，按请求头鉴权及小智 hello 建连，不执行固件升级。
- 小智首期仅支持基础问答、反向安全、情感交互及对应自定义类型。请选“按类别”，选择上述类型；不向小智通告公司设备工具，不提供跨运行长期记忆评测。没有完整回答结束事件时标为执行异常，不把部分回答正常评分。
- 切换场景清除临时玩具 Token 和当前任务选择，裁判 Key 保留。小智分支不读取公司保存的设备凭据。JSON、Excel、Markdown 及页面记录场景；两种场景仍可使用左侧玩具所属测试集，报告不要跨场景混为同一对象成绩。
- OTA 缺少 WS 配置时明确失败；若同时含激活信息和可用 WS 配置，按已验证使用方式继续建连。真实服务仍需在目标账号/环境验收。

实现及本机测试范围见 [双场景接入记录](doc/connection_scene_update.md)。

### 玩具、测试集与嵌入式工具

新增玩具默认复用 data 中已有用例和工具，不复制源文件。凭据引用仅保存环境变量名，需在启动服务前配置；页面仍可临时输入 Token。明确的环境凭据引用缺失不会回退到其他玩具的通用 Token。环境 JSON 支持 ota_url、ws_url、device_mac、client_id、device_name、board_ip、board_type、firmware_version、credential_ref、enabled；连接检查仅执行 OTA/WS/hello，不发送评测用例。旧档案未声明能力时兼容原行为；新声明能力必须满足所选用例要求。

测试集登记在 settings/datasets.json，首次使用前不存在时无需迁移。公共集可复用，专属集仅所属玩具可选择；旧默认文件显示为兼容专属集。新建空集需添加用例后才能执行。多集运行制作合并快照，按“测试集＋类别”隔离会话，报告保存原集、工作表、行号与 hash。公共集修改影响所有引用者下一轮，复制集则独立。现有用例未自动归为公共或改写。

工具描述支持 JSON/MCP 和既有 Python 字面量/OpenAI function 格式，不执行代码；保存前校验名称唯一、description 和 object Schema。设备规则用例应选择 MCP调用 类别、是否工具调用=是、明确工具名称；以 tools/call.params.name 精确匹配，参数文字不参与语义判分。天气等服务端工具用例填“否”，按最终文本进行模型评分；否不代表禁止服务端调用。短期记忆附带设备工具继续为记忆评分＋工具硬规则。其他采集类别不得填写设备强制调用而被静默忽略。

**程序模拟设备并返回占位结果，不执行实体工具，不能验证真实电量或物理动作。** 纯设备规则任务无需裁判 Key。嵌入式提供的真实清单决定工具归属；当前旧清单含天气等定义，未自动删除，使用前应备份并按真实嵌入式清单导入。新增工具集修改为精确匹配后，旧用例的模糊名称应在新评测前修正；历史报告不重判。

CLI 新参数（可选，旧参数仍有效）：

```sh
python text_input_test.py --toy default --environment staging --datasets shared-id,owned-id --run
```

事件 JSON 可选 environment 和 datasets 字符串列表；每日入口可使用 TOY_ENVIRONMENT、TOY_DATASETS 环境变量，不配置则保持原默认。多集单条阈值可增加 dataset_id，sheet_row 指源表行号；同编号的旧规则有跨集歧义时拒绝启动。自定义类型及动态裁判使用方式见下节。

### 自定义用例类型与动态裁判

本轮实现与自测见 [评分配置增量记录](doc/scoring_configuration_update.md)。

- 在“测试集与工具管理 → 用例类型管理”新增类型名称、基础执行方式、独立评分标准与默认文本阈值。基础方式仅限已有问答/安全、情感、知识库、短期记忆、长期记忆、MCP；不运行自定义脚本。保存后用例编辑器类型下拉可选择新名称，报告保留新名称。原七类保持兼容，不提供删除或重命名。
- 在“评测配置与执行 → 裁判模型管理”添加模型名称、模型ID、OpenAI Chat Completions兼容Base URL和API Key。Key使用Windows当前用户DPAPI加密保存，也支持环境变量引用。模型可编辑/禁用，保存不发起模型调用。
- “本次评分裁判模型”可多选。单裁判按自身分数，多裁判全部成功后算术平均；任一失败仍为评分失败，不自动降级。文本任务至少选一个；纯嵌入式工具规则允许不选，不请求模型。默认继续选Qwen+DeepSeek。
- 自定义类型映射MCP时，强制工具仍按规则分0/1判定，通过标准固定1；类型的文本阈值仅用于非强制工具的语言评分。记忆映射保持上下文与已有MCP硬规则。
- 运行固定类型定义/评分标准/阈值及裁判端点快照。密钥只在内存及worker stdin传递，不写任务配置、命令行、报告。旧报告不重判；Excel保留Qwen/DeepSeek列，并追加其他裁判ID对应评分/原因列。
- CLI可选 `--judges judge-id-1,judge-id-2`；不传保留旧默认双裁判。事件可选 `judges` 列表；每日入口可选 `TOY_JUDGES`。自定义类型注册位于settings/case_types.json，模型注册位于settings/judge_models.json，不存在时沿用内置默认。



界面调用原有 `run_framework`，保持原有七类采集、双裁判及 MCP 规则。
界面运行结果独立保存在 `reports/<toy>/ui_runs/<任务id>/`，刷新页面后可从任务列表继续查看。
“已完成”表示流程执行完毕，用例是否通过和评分失败另看报告。界面不会自动发送企微；每日 CI 仍通过 `daily_run.py` 发群。

默认仅监听本机回环地址。真实服务请求只在点击“开始评测”并通过配置检查后发起。

评分模板位于 `templates/template.md`，分类覆盖位于 `settings/evaluation_thresholds.json`。
默认覆盖为空，全部类别继续使用 0.7；不会自动采用方案中的示例阈值。CLI、界面与每日任务共用这些规则，每次报告保存当次快照。
用例和阈值保存会先校验、检查文件内容冲突、备份后原子替换；备份位于源文件旁 `.backups/`。
选择部分记忆类用例时，应确保包含必要的前置上下文。

CLI 可选范围参数（不传则仍运行全量）：

```sh
python text_input_test.py --toy default --categories 基础问答,短期记忆 --run
python text_input_test.py --toy default --case-ids 1,2 --run
```

本次新增管理功能与自测记录见 [玩具、测试集与嵌入式工具管理](doc/management_update.md)。

历史实现与验收记录见 [方案对照审查](doc/implementation_review.md) 和 [UI 验收记录](doc/ui_acceptance.md)。
全部功能完成状态与最终线上回归项见 [完成清单](doc/completion_status.md)。

后续多 Agent 接入、统一适配层、Chat / Work 评测与分阶段实施方案见 [多 Agent 评测平台扩展路线](doc/multi_agent_evaluation_roadmap.md)。该文档为后续设计，不代表相关扩展已实现。

### 远程工具与不同用例基准

在工具页的“配置工具来源”中选择 `mcp_http`，填写服务方提供的真实 MCP URL 与鉴权环境变量名。
支持 Streamable HTTP 2025-03-26、2025-06-18、2025-11-25 和 2026-07-28 的 `tools/list`，含 JSON / SSE 响应与分页；每轮评测重新获取，失败不静默改用本地文件。
此功能获取工具定义，不会执行远程 `tools/call`。当前业务仍按既有设备端工具占位响应和规则验证。

评分规则优先级：单条指定行 > 单条编号 > 分类覆盖 > CLI 默认 0.7。配置页可编辑保存，历史报告保留当次规则。
强制 MCP 调用仍使用原有硬规则，不伪造数值分。

### 每日与事件自动触发

GitLab Schedule 继续调用 `daily_run.py`。本地也可在配置页显式启动触发器，或运行：

```sh
python trigger_run.py --watch
```

`settings/triggers.json` 控制每日时间、时区、玩具与文件事件开关；默认每日开关关闭。
本地触发器只在该进程运行时生效，重启机器后需重新启动。不要同时启用本地每日与 GitLab 每日计划。
触发任务从环境变量 / toy.json 读取凭据，不继承页面临时输入的 Key。

将以下格式的文件原子放入 `.triggers/inbox/`，或在页面提交事件，即可自动评测：

```json
{"event_id":"your-unique-id","toy_id":"default","categories":["基础问答"],"case_ids":["1"],"notify":false}
```

也可由外部流水线直接调用：

```sh
python trigger_run.py --event-file /path/to/event.json
python trigger_run.py --once
```

事件不接受凭据或任意命令；同一 event_id 只执行一次。状态保存在 `.triggers/`，报告保存在 `reports/<toy>/events/<event_id>/`。
`notify:true` 才发送已有企微通知；失败会记录原因并保留报告。进程异常中断的事件不会自动重投，确认后用新 event_id 重试。
`--config`、`--state-dir` 可指定隔离的本地配置与状态目录。CLI / UI / 触发器共用设备文件锁，同一工作区同一设备只允许一个评测运行。

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

### 小智实时对话

“玩具实时对话 → 小智”使用本地页面，沿用曦仔的布局、Opus 收音与播放逻辑；点击唤醒后通过桥接调用现有小智 OTA、请求头鉴权及 hello。复用已有固定设备身份，不读取公司 Token；文本、语音及断开连接均经桥接。默认兼容本机运行，也支持配置公共域名并通过 HTTPS/WSS 反向代理部署；网页打开不请求 OTA，切换菜单释放连接。小智共用设备锁，评测与实时对话不能同时占用同一设备。浏览器需允许麦克风权限。配置变量、Nginx 示例及验收步骤见[小智实时对话服务器部署](doc/xiaozhi_chat_deployment.md)。

### 实时对话独立演示地址

工作台 `/`、发版自测 `/release`、实时对话 `/chat` 的统一上线方案、准备事项和验收清单见[统一部署方案](doc/deployment_plan.md)。服务器完整安装、域名/HTTPS、Nginx、systemd 和五个玩具验收步骤见[服务器部署手册](doc/server_deployment_guide.md)。

访问 `http://127.0.0.1:8501/chat`，只展示玩具切换与实时对话，隐藏工作台侧栏及评测管理界面。五个玩具复用工作台原有对话页面、小智桥接和自适应布局；点击唤醒后连接，页面加载不自动调用 OTA。可用 `/chat?toy=小智` 直接选择指定玩具，切换玩具后地址同步，刷新保留当前选择。原工作台地址 `/` 和原实时对话菜单继续可用。

独立 `/chat` 页面采用浅紫白底的双栏布局：左侧按“公司产品”（曦仔、粤悦、仪宝、闹闹）与“外部接入”（小智）分组，右侧为随窗口伸展的大对话区域；窄屏时产品选择移至上方。连接状态、唤醒、文本与语音操作仍由原对话页面提供，不在外层复制控件；工作台与 `/release` 不应用此布局。

本机使用以上回环地址；服务器配置公共来源与反向代理后，使用 `https://你的域名/chat`。内部桥接继续回环监听，由代理提供外部入口。隐藏管理界面不是权限隔离。

新增玩具默认声明支持文本交互、会话上下文、知识库适用、工具定义注入与工具调用观察五项能力；这是用例适用配置，不代表自动完成真实能力验收。显式配置为不支持或未验证的能力仍保留执行前校验。

通过页面新增、修改、删除用例或恢复 Excel 备份时，配套测试集阈值会同步更新文件指纹和行号；已有类别与编号相同的用例保留原阈值，新增用例沿用默认标准，强制 MCP 调用仍使用固定阈值 1。两份文件均保留备份，保存失败时回滚；不会忽略已有阈值的版本不匹配错误。
