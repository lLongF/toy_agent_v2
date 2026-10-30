# 2026-10-28 会话交接与继续工作说明

> 用途：在新会话中读取本文件，恢复本次项目上下文并从“最后工作任务”继续。  
> 项目目录：`D:\Project\toy_agent-main\toy_agent-main`  
> 当前工作区没有 `.git` 元数据，不能提供 Git diff 或提交回滚。

## 1. 新会话启动指令

请先阅读：

1. `AGENTS.md`
2. `Agent.md`
3. `README.md`
4. 本交接文档
5. `doc/multi_agent_evaluation_roadmap.md`
6. `doc/xiaozhi_integration_implementation_plan.md`
7. `doc/management_update.md`
8. `doc/scoring_configuration_update.md`

然后检查当前源码和测试状态。除非用户明确要求，不要调用真实小智服务、真实裁判、企业微信或定时任务；不要修改正式用例、工具清单、历史报告或凭据。

## 2. 用户已确认并已落地的功能范围

### 2.1 玩具管理

当前页面菜单为“玩具管理”，已改为卡片式列表：

- 侧栏“当前玩具”显示玩具名称，内部 ID 仍用于配置和报告关联。
- 卡片显示名称、启用状态、运行环境、Token 配置状态、最近评测、已声明能力。
- “＋ 接入玩具”弹窗只填写：玩具名称、玩具 Token。
- “配置详情 →”打开同样的简化“编辑玩具”弹窗，只编辑名称和 Token；已有环境、能力、启用状态保持不变。
- 编辑时 Token 不回显，留空表示保留原 Token。
- 玩具 Token 在 Windows 当前用户 DPAPI 下本机加密保存，普通 `toy.json` 仅保存 `local:<id>` 不透明引用；OTA 请求时解密到内存后发送真实 Token。
- 新建玩具默认复用 `data/测试样例.xlsx` 和 `data/工具描述.txt`。
- 历史环境变量引用兼容；但当前产品需求已明确优先支持直接填写 Token。

主要文件：

- `toy_profiles.py`
- `toy_management.py`
- `local_credentials.py`
- `ui_catalogs.py`
- `streamlit_app.py`

### 2.2 公共／专属测试集

已实现：

- `settings/datasets.json` 可选注册表，不存在时不迁移。
- 公共测试集和当前玩具专属测试集。
- 创建空测试集、复制已有测试集、启用/禁用、维护原 Excel 用例。
- 多测试集执行时制作不可变快照。
- 快照记录测试集 ID、名称、文件 hash、工作表、源行号。
- 多集相同类别不会串会话，按“测试集 + 类别”隔离。
- 单条阈值支持 `dataset_id`，多集同编号无 dataset_id 时拒绝歧义。

主要文件：

- `dataset_store.py`
- `evaluation_selection.py`
- `asset_store.py`
- `ui_catalogs.py`
- `streamlit_app.py`

### 2.3 嵌入式工具规则

当前定义：实体玩偶的嵌入式工具只做调用规则验证；天气等服务端工具仍按最终文本模型评分。

已实现：

- 工具定义支持 JSON/MCP、Python literal、OpenAI function 等旧格式。
- 工具名称唯一、description 为字符串、inputSchema 必须为 object。
- 强制调用通过 `tools/call.params.name` 与预期工具名精确匹配。
- 不再使用参数子串或空预期自动通过。
- 强制嵌入式工具规则分为 0/1，通过标准固定 1。
- 规则任务不初始化裁判、不要求裁判 Key。
- 回包/协议异常与工具命中分开；命中但回包异常不能伪造成“未命中”，执行异常单独记录。
- 规则分不混入语言模型平均分。
- 当前程序只模拟设备占位回包，不执行真实实体工具，不验证真实电量或物理动作。

主要文件：

- `tools_source.py`
- `mcp.py`
- `testers.py`
- `judge.py`
- `ui_management.py`
- `report.py`
- `ui_results.py`

### 2.4 自定义用例类型

已实现轻量类型注册：

- 页面“测试集与工具管理 → 用例类型管理”。
- 新增/编辑自定义类型名称、基础执行方式、rubric、默认文本阈值。
- 基础执行方式仅能映射已有流程，不执行配置中的任意代码。
- 原七类保留，无删除/重命名。
- 自定义类型保留原显示名，内部通过 `category_handlers` / `category_bases` 映射已有 tester 和 judge。
- 自定义类型映射 MCP 时，强制工具仍走 0/1 规则；文本阈值仅用于非强制工具语言评分。

主要文件：

- `category_store.py`
- `evaluation_support.py`
- `testers.py`
- `asset_store.py`
- `ui_scoring.py`
- `ui_management.py`

### 2.5 动态裁判模型

已实现：

- 页面“评测配置与执行 → 裁判模型管理”。
- 模型名称、模型 ID、OpenAI Chat Completions 兼容 Base URL、启用状态、受控 extra_body。
- API Key 支持环境变量引用或 Windows DPAPI 本机加密保存。
- 本次运行可多选裁判：单裁判使用自身分数；多裁判全部成功后等权平均；任一选中裁判失败为评分失败。
- 纯嵌入式规则任务可不选裁判。
- 默认不传新参数时保持旧 Qwen + DeepSeek 行为。
- CLI 支持 `--judges id1,id2`。
- 事件可选 `judges` 列表，每日入口可用 `TOY_JUDGES`。
- worker 使用 stdin 传递运行配置，Key 不进入命令行、job.json 或日志。
- JSON 保存无明文 Key；Excel 保留旧 Qwen/DeepSeek 列并追加动态裁判评分/原因列。

主要文件：

- `judge_store.py`
- `judge.py`
- `ui_scoring.py`
- `ui_jobs.py`
- `ui_worker.py`
- `config.py`
- `cli.py`
- `trigger_run.py`
- `daily_run.py`
- `report.py`

## 3. 当前本地验证结果

环境：

- Windows
- Python 3.11.9
- 解释器：`.runtime\venv-win\Scripts\python.exe`
- 依赖锁定检查通过
- `pip check` 通过

最近完整回归：

```powershell
$env:PYTHONUTF8='1'
.runtime\venv-win\Scripts\python.exe -m unittest discover -s tests -v
```

实际结果：**90 项，89 通过、1 跳过**。跳过的是原有 Windows `TerminateProcess` 触发器停止路径测试，待目标 Runner 验收。日志：

- `.runtime/types-judges-final.log`

专项测试：

- 自定义类型：`tests/test_custom_categories.py`，7 项通过。
- 动态裁判：`tests/test_dynamic_judges.py`，9 项通过。
- 管理集成：`tests/test_management_integration.py`，8 项通过。
- 评分/UI 集成：`tests/test_scoring_ui.py`，5 项通过。
- 直接 Token/DPAPI：`tests/test_local_credentials.py`，Windows 本地通过。

当前服务：

- URL：`http://127.0.0.1:8501`
- 健康检查曾返回 HTTP 200。
- 当前应先检查端口和服务进程；模块新增后需要重启旧 Streamlit 进程，不能只看健康接口。

## 4. 小智官方服务接入方案

完整方案文件：

- `doc/xiaozhi_integration_implementation_plan.md`

结论：当前不能把小智官方服务当成现有玩具链路直接接入。必须先完成官方接入调查和授权：

1. 官方账号、目标角色、专用设备和绑定关系。
2. 官方允许的 API 或授权客户端协议。
3. 是否支持文本直入。
4. 频率、并发、额度和测试数据范围。

推荐路线：

1. 官方文本 API（若官方提供并授权）。
2. 获授权设备/客户端协议。
3. 文本不可用时另行设计音频路线。
4. 自动化不可用时，后续另做人工导入。

接入前不得：

- 猜测或抓取私有 API。
- 伪造设备身份或随机 MAC 绕过授权。
- 把社区自建服务成绩标成官方成绩。
- 自动发送官方请求。

接入后推荐架构：

```text
父对比任务
  ├─ 自家 LegacyToyAdapter 子任务
  └─ XiaozhiOfficialAdapter 子任务
       ↓
统一结果
       ↓
现有裁判/规则评分
       ↓
独立报告 + 对比报告
```

首期只做少量公共文本集，排除设备专属工具、长期记忆、知识库共同排名和 Work 任务。父任务必须一次冻结公共用例、会话计划、统一阈值和裁判快照，两个子任务不能各自读取最新文件。

## 5. 最后工作任务／当前暂停点

用户最后提出的是：

> 暂停当前会话，去另一个会话继续；读取本交接文档后衔接最后任务。

此前最近一次运行失败排查已经完成，失败不是裁判问题，而是小智/设备服务端在 WebSocket 阶段主动关闭连接：

```text
received 1013 (try again later) runtime configuration unavailable;
then sent 1013 (try again later) runtime configuration unavailable
```

失败任务：

- `reports/toy-ff0e79f82509/ui_runs/20261028_174817_1826b2bc/job.json`
- 对应日志：`reports/toy-ff0e79f82509/ui_runs/20261028_174817_1826b2bc/run.log`

已确认：

- OTA HTTP 200 成功。
- WebSocket 连接成功。
- hello 握手成功。
- 收到 MCP initialize，但没有 tools/list。
- 第一条发送“你好”后连接关闭。
- judge 任务数为0，尚未进入评分。
- 服务端返回的 WebSocket 路径为 `wss://voiceAgent.lycheeai.com.cn/voiceAgent/stream/v5/`。
- 上午成功报告使用的是 `/voiceAgent/stream/v1/`。
- 两个运行使用相同默认设备 MAC `18:c0:4d:4f:31:3c`。

最可能原因：小智/玩具 Token 对应的服务端运行配置不可用、角色未发布/禁用、绑定配置不完整、v5路由配置异常，或账号/设备绑定不匹配。这个错误发生在 Agent 运行时配置加载阶段，不是本地裁判或 Excel 问题。

下一步应先做：

1. 查看服务端在失败时间 `2026-10-28 17:47:46–17:47:48`、`17:48:18–17:48:19` 的日志。
2. 核对“仪宝”Token 对应的角色、模型、运行配置和设备绑定。
3. 确认 v5 服务是否支持该配置。
4. 确认是否允许两个玩具复用同一个设备 MAC。
5. 不要强行把 v5 改成 v1，也不要重复盲重试。

## 6. 新会话建议立即执行

新会话中可直接发送：

> 请读取 `doc/session_handoff_20261028.md`，恢复项目上下文。继续处理“最后工作任务”：先排查 toy-ff0e79f82509 的 WebSocket 1013 `runtime configuration unavailable`。只读取现有失败报告、run.log、toy.json（不要输出/泄露 Token），对比成功 default 报告的协议阶段和非敏感配置；不要自动重试真实服务。若需要修改代码，先说明最小影响方案。

如果要继续开发小智官方接入，应先确认第4节的官方账号/角色/授权/文本入口，而不是直接写适配器。

## 7. 安全与边界

- 不在聊天、日志、报告、命令行输出真实 Token/API Key/Webhook。
- 真实评测、真实小智请求、企业微信、定时任务都需要明确授权。
- 本地 mock/回归通过不等于官方线上接入通过。
- 当前没有 Git 提交；不要声称存在回滚点或提交记录。
- 旧入口、旧报告、旧七类、自家设备链路必须保持兼容。
