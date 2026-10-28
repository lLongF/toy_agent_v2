# 四页面迭代验收记录

日期：2026-10-27。首轮记录保留；最新状态：**功能开发与本地回归已完成，待最终线上回归**，详见 `completion_status.md`。未把模拟响应当作线上评测记录。

## 本轮补充与实施范围

审查结论见 `implementation_review.md`。保留现有 Python 3.11 / Streamlit 1.66.0 与完整依赖锁，不新增框架或服务。
四个功能入口采用统一原生主题、深色侧栏、浅色画布、语义色与卡片布局。只读用例与规则，所有按钮对应真实行为。

## 文件与职责

| 文件 | 本轮变化 |
|---|---|
| streamlit_app.py / ui_styles.py / .streamlit/config.toml | 四页面、样式、导航、空态、状态和详情 |
| ui_results.py | 报告契约、总体/分类汇总、旧报告兼容、受限附件匹配 |
| evaluation_support.py / templates/template.md / settings/evaluation_thresholds.json | 安全模板渲染、分类规则、阈值覆盖、范围筛选与快照 |
| ui_request.py / ui_jobs.py / ui_worker.py | 共享启动前校验、任务范围传递、日志脱敏、4 小时上限 |
| config.py / cli.py / text_input_test.py / judge.py | 增加可选范围、规则加载、有限数值校验；保留原采集及聚合逻辑 |
| report.py / safe_output.py | 凭据脱敏、附件映射、纠正 Markdown 将分组数当用例数的问题 |
| daily_run.py | 仅增加 scheduled 触发标识，仍调用同一 CLI、评分和通知链路 |
| tests / README / 环境与Streamlit.md | 回归与操作说明 |

## 数据契约与口径（UI-01）

- 运行：`protocol_info.toy_id / run_id / started_at / finished_at / trigger_type`；旧报告没有字段时不补造。
- 范围：`protocol_info.selection` 保留类别、用例编号、原表行号。
- 规则：`protocol_info.evaluation_rules` 保留模板内容、摘要和实际阈值；不以现行规则重判旧报告。
- 用例：`results[].case_spec.turns[]` 对应原始输入；`turn_results[]` 按 `sheet_row` 关联实际回复、裁判结果和工具调用。
- 状态：优先复用原报告标签；缺少执行结果或待评分标记单列“未执行”。
- 可判定业务结果 = 通过 + 业务不通过（含 MCP 纯规则）；通过率 = 通过 / 可判定业务结果。
- 平均综合分只使用评分有效且在 0–1 范围内的综合分；MCP 无数值分时不补 0 或 1。
- 任一启用裁判异常，仍记评分失败；保留双裁判算术平均与 MCP 硬规则。

## 已执行验证

命令：`.venv/bin/python -m unittest discover -s tests -v`

实际输出：`Ran 20 tests ... OK`。

覆盖原有 98 条用例与 CLI、七类采集、真实本机 WebSocket、SDK 请求、三种报告产物、评分错误分类、任务异常与互斥；新增范围实际执行、分类阈值、模板快照、无效分数、空/损坏报告、下载路径、表单阻断、页面筛选与跨页范围、切换玩具失效、源 Excel 哈希不变。

实际执行范围测试使用原表用例编号 1、基础问答类别和测试阈值 0.95，只采集该条；模拟裁判返回 0.9，结果为业务不通过，报告保存当次阈值。该阈值仅存在于测试临时目录，没有写入正式配置。

浏览器检查：

1. 首页显示真实空态，资产为 98 条 / 7 类 / 5 个本地工具。
2. 用例页筛选短期记忆，显示 7 / 98 条。
3. 点击“使用7条当前范围创建评测”，配置页保持这 7 条并提示记忆上下文风险。
4. 模板和阈值正常显示；凭据缺失时启动按钮禁用。
5. 页面导航与刷新未创建正式报告或任务。

## 未执行及限制

- 当前未配置设备 Token、DASHSCOPE_API_KEY、DEEPSEEK_API_KEY，真实线上端到端评测 **未执行**。
- 企微发送、GitLab 真实 schedule / Runner **未执行**；没有发送消息或更改远程定时计划。
- 结果页的数据分支用临时测试报告验证，未将测试报告放入正式历史列表。线上报告、真实附件下载与总体指标仍待真实评测后验收。
- 4 小时任务上限代码已实现，未实际等待 4 小时验证。
- 同一应用实例按设备互斥；多个 UI 实例与外部 CLI / CI 的设备协调仍由部署侧负责。
- 当前工作区没有 Git 仓库元数据，`git status --short` 返回非 Git 仓库，无法输出 Git diff。

## 下一步验收

用户先确认四页布局与操作体验。配置真实凭据后，使用少量问答、记忆、MCP 用例进行线上验收，再核对报告指标、下载与每日通知。任务状态保持“待验收”，用户确认后才改为“验收通过”。
