# 自定义类型与动态裁判增量实现

## 已实现

- 测试集与工具管理增加轻量类型管理，创建/编辑自定义名称、固定基础执行方式、rubric和默认文本阈值；原七类不变，无删除/重命名。登记类型进入用例下拉、资产校验、能力校验、执行分发及评分。未知名称拒绝。不同测试集和类别保留原隔离策略。
- 评分配置增加裁判管理与本次多选。支持OpenAI Chat Completions兼容模型，名称、模型ID、URL、启用状态、受控兼容参数及API Key。Key可引用环境变量或Windows DPAPI本机加密保存，不回显。
- 单模型自身分、多模型全部成功等权平均；任何选中模型失败记评分失败。文本空选择阻断，纯嵌入式规则无模型调用。MCP硬规则仍0/1、标准1；自定义MCP类型文本阈值仅管非强制工具回答。
- 默认未传新CLI参数仍使用原Qwen+DeepSeek，旧开关保留。新CLI --judges ID1,ID2；事件judges列表，每日TOY_JUDGES。
- 类型和裁判无密钥配置快照，worker凭据仅内存/stdin；动态模型Excel新增ID评分/原因列，同时保留旧Qwen/DeepSeek列；历史报告不重判。
- 注册表可选：settings/case_types.json，settings/judge_models.json。保存带CAS/备份，无文件时保留内置默认。本次未改正式用例/工具/历史报告、未建立虚假的正式裁判配置。

## 页面使用

1. 测试集与工具管理 → 用例类型管理，选新增，填名称/基础执行方式/分类标准/文本阈值，保存。重新进入维护区可在类型列选择。
2. 评测配置与执行 → 裁判模型管理，添加真实模型ID、兼容Base URL、Key或引用并保存。管理保存不会调用模型。
3. 在本次评分裁判模型多选一到多个模型；纯工具规则可不选。旧Qwen/DeepSeek临时Key仍在凭据展开区，新增模型Key通过管理加密保存。
4. 开始评测时固定快照。保存配置影响后续运行，不改历史结论。

## 验证

Windows Python3.11.9，PYTHONUTF8=1，项目.runtime/venv-win环境。

```powershell
$env:PYTHONUTF8='1'
.runtime\venv-win\Scripts\python.exe -m unittest discover -s tests -v
```

实际完整结果：90项，89通过、1跳过，耗时25.440秒。跳过原有Windows触发器停止路径测试。日志 .runtime/types-judges-final.log。

新增：自定义类型7项；动态裁判后端9项；评分UI/集成5项。包括真实AppTest创建编辑、实际临时配置与备份、临时DPAPI无明文、单/双/零裁判选择、worker密钥仅stdin、自定义emotion类别mock完整主流程、动态Excel列与JSON快照、原列兼容。

环境锁定检查与pip check通过。刷新服务后本机健康HTTP200；实际项目AppTest三菜单无异常，类型管理及裁判多选控件渲染通过。内置浏览器首页加载正常，但工具点击菜单未可靠触发，故页面交互证据以AppTest为准。未发真实付费请求、未发送企微，新的真实端到端仍需用户验收。
