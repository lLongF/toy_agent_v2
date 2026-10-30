# 方案与当前实现对照（2026-10-27）

后续补全状态见 `completion_status.md`：已按最新授权增加在线用例维护、远程 MCP 来源、单条基准及每日/事件触发，下面保留初次审查时的差距记录。

以 `agent_eval_plan.md` 为需求参考，沿用当前用户授权增量实施。文档中引用的“新会话提示词”不作为本次用户请求；任务状态记录为待验收，不代替用户验收。

| 目标 | 当前源码依据 | 差距 / 实施方式 |
|---|---|---|
| 四页导航与现代视觉 | streamlit_app.py 的侧边栏 radio | 改为总览、用例与工具、配置与执行、结果分析；主题与展示组件独立维护 |
| 真实结果与指标 | report.py / ui_jobs.report_rows | 报告字段适配和统计下沉，不在各页面重算；无报告展示空态 |
| 工具来源 | config.load_mcp_tools_from_txt / toy_profiles | 属于本地文件加载，客户端回复 tools/list，不是服务端实时发现 |
| 分类选择实际执行 | config.load_dialogue_groups_from_xlsx / run_framework(cfg) | 在同一执行入口过滤；保留 sheet_row 与分组顺序，不改原 Excel |
| Prompt 模板 | judge.LLMJudge 五个评分方法 | 抽取模板和分类标准，CLI/UI/定时共用快照 |
| 分类阈值 | Config.judge_pass_threshold / merge_judge_results | 保留默认 0.7，分类覆盖默认空；不启用文档中的示例高阈值 |
| 双裁判异常 | merge_judge_results 的 all_available | 任一不可用仍为评分失败，不降级使用另一裁判 |
| MCP 硬规则 | testers.MCPCallTester / ShortTermMemoryTester | 保留现有工具模糊匹配和 mcp_pass；不顺带重设计参数校验 |
| 任务与日志 | ui_jobs.JobManager / ui_worker | 独立子进程和 job.json 已有；补校验、脱敏、完成导航 |
| 报告归属 | ReportWriter.write 文件名和 ui_runs 目录 | 根据当前报告 / job 元数据匹配附件；防止同目录其他轮报告混入 |
| 定时链路 | daily_run.py → CLI → run_framework → notify_wecom.py | 保持触发规则与通知目标，补 trigger_type；不在页面调通知 |

## 需要明确的兼容口径

- MCP 纯规则用例没有综合数值分数：计入可判定业务结果和通过率，但不计入平均综合分。
- 连接失败、未知类型、采集中断的未执行用例不冒充评分成功，UI 单列“未执行”；原 Excel 标签保留兼容行为。
- 旧报告优先使用当次 pass / judge_available / threshold，不以当前规则重新判定历史结果。
- 缺少阈值的旧报告显示“无记录”，不补造 0.7；无有效分数显示“—”。
- 选择记忆类别的部分行会改变上下文覆盖范围：显示提醒，不自动扩大用户选定范围。
- 当前工作区没有 .git 目录，无法提供 Git diff；以文件清单、测试结果与原始数据哈希验证改动。
- 真实端到端验证需要可用设备 Token 和两个裁判 Key；没有凭据时明确记为未执行。

## 视觉补充

采用浅灰画布、深色侧栏、白色信息卡、靛蓝主操作、语义化状态色、统一圆角与字号层次。优先使用 Streamlit 1.66 原生主题和布局，CSS 仅做公共间距与少量细节。表格、筛选、详情、空态和错误态统一；不加入虚假趋势或无行为装饰按钮。

参考：[Streamlit 主题](https://docs.streamlit.io/develop/concepts/configuration/theming)、[Material 的视觉层级研究](https://design.google/library/expressive-material-design-google-research)。
