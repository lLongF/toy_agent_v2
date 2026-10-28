# 统一环境与 Streamlit

## 基线

本项目统一使用 Python 3.11.x。`.python-version`、`pyproject.toml`、环境检查脚本与 GitLab CI 保持一致。
当前开发机是 Python 3.11.4，因此保留同一次版本来做可执行验证；新装环境建议使用 Python 3.11 当前可获得的最新安全补丁版本。
Python 3.12 及以上没有纳入当前锁文件的支持矩阵，升级时重新解析依赖并跑回归。

直接依赖范围在 `requirements.in`；部署使用 `requirements.txt` 的精确版本。
Streamlit 固定为 1.66.0；配套 pandas、NumPy、PyArrow、Altair、Plotly、protobuf、Watchdog、ASGI 服务依赖等一并锁定。
只安装项目实际使用的支持库，不引入 Snowflake、SQL、认证等未使用 extras。
Windows 专用依赖通过环境标记声明，不把 macOS 专属 wheel 路径写进锁文件。

## 安装

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/check_environment.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

macOS / Linux：

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/check_environment.py
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m streamlit run streamlit_app.py
```

原 CLI 使用同一个虚拟环境：`python text_input_test.py --toy default --run`。
Windows Runner 需让 PATH 中的 `python` 指向 3.11；CI 在项目 `.venv` 中安装和执行，不修改系统包。

## 集成边界

- `streamlit_app.py`：页面、用例预览、结果统计、图表和下载。
- `ui_jobs.py`：独立任务目录、进程启动、同一应用实例内设备互斥。
- `ui_worker.py`：从标准输入接收配置，运行现有引擎，将任务状态写入 `job.json`。
- `tools_source.py`：本地 / 远程 MCP 工具发现；`asset_store.py`：带备份和冲突校验的资产维护。
- `ui_management.py`：用例、工具来源、分类/单条阈值与触发器的页面入口。
- `trigger_run.py` / `trigger_control.py`：每日与文件事件自动触发、CLI / UI 启停。
- 页面重跑不重新创建运行中的 WebSocket；任务进程拥有自己的事件循环和 RuntimeState。
- Fast 参数从 CLI 抽成共享函数，保持原参数值。
- 界面配置凭据经 stdin 传递，不写任务请求文件或命令行。新 JSON trace 和结果追加列会脱敏凭据；旧报告在界面下载 JSON 时再次脱敏。对话仍属于评测数据，应按原项目要求保管。
- 任务列表可在页面刷新后恢复查看。应用进程重启后，旧任务可继续写报告，但新应用无法恢复原进程句柄；重启前应等待任务完成。
- CLI、UI、每日与事件共用同工作区设备 OS 文件锁；跨机器仍需部署侧协调。
- 不在页面全局缓存中存放 WebSocket、事件循环或用户凭据；只缓存线程安全的任务管理器。

## 兼容性验证

离线回归覆盖：原 Excel 98 条用例读取、CLI 帮助和列玩具、Fast 参数一致性、七类会话隔离与关闭、MCP 握手与调用、双裁判、JSON / Markdown / Excel 导出、评分失败分类、任务失败状态、Streamlit 页面导航与表单校验。
离线测试通过模拟 OTA、WebSocket 与裁判响应运行真实评测编排；不会连接线上玩具或发送企微。
`pip check` 检查依赖声明冲突；`scripts/check_environment.py` 检查实际安装与锁文件是否一致。
真实 OTA、裁判账号、企业微信，以及 Windows Runner 仍需在目标网络和账号下验收。

### 本次验证记录（2026-10-27）

- macOS ARM64 / Python 3.11.4：安装完成，环境一致性检查与 `pip check` 通过。
- 首轮 10 项、方案迭代后 20 项离线回归全部通过，包含锁定版本的真实本机 WebSocket 握手和 OpenAI SDK HTTP 请求序列化。
- 功能补全后 **40 项测试全部通过**，新增 MCP HTTP / SSE、本地工作进程端到端、真实 UI 保存、备份恢复、单条基准、事件和定时联动验证；见 `doc/completion_status.md`。
- Streamlit 首页与 `/_stcore/health` 均返回 HTTP 200；页面导航、表单校验和混合 MCP / LLM 报告渲染通过 AppTest。
- Windows x64 / Linux x64 的 Python 3.11 二进制依赖解析通过；尚未在这两个系统执行运行测试。
- 修复原 `ReportWriter._turn_export_fields` 静态方法引用未定义 `self` 的导出错误，改成类方法；字段和判分规则保持原值。

## 升级方式

1. 修改 `requirements.in`，使用 uv 解析跨平台依赖：`uv pip compile requirements.in --python-version 3.11 --universal --output-file requirements.txt`。
2. 在新的 Python 3.11 虚拟环境中执行 `python -m pip install -r requirements.txt`；不要用本机 `pip freeze` 覆盖跨平台标记。
3. 执行环境检查、`pip check`、离线回归和目标环境的真实样例验证。
4. 一起提交范围文件、锁文件和验证记录；开发机与 Runner 统一升级。

websockets 14 起顶层 connect 切换到新 asyncio 实现，15 起自动读取代理。当前使用锁定的新实现，保留项目原有 `connect(url)` 的默认代理行为，不擅自改变网络路由。
如果部署网络要求直连或代理，请单独确认网络策略，再调整连接参数，并增加连接回归。

官方参考：

- [Streamlit 1.66.0 包信息](https://pypi.org/project/streamlit/1.66.0/)
- [Streamlit 发布说明](https://docs.streamlit.io/develop/quick-reference/release-notes)
- [websockets 变更记录](https://websockets.readthedocs.io/en/stable/project/changelog.html)
