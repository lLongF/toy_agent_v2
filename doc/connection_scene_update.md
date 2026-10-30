# 公司玩具与小智 AI 双场景接入

本次实现固定双场景和单对象评测，不是多 Agent 对比平台。此前凭据持久化/优先级优化继续暂停，未修改 DPAPI 或裁判选择语义。

## 实现

- `connection_scenes.py`：固定场景、固定复用已验证 MAC/UUID、首期类别限制。
- `xiaozhi_connection.py`：按提供 OTA 文档构造请求，统一请求头/顶层/board MAC 与 Client-Id；OTA 取得 WS URL/token/version；请求头鉴权、小智 hello、文本 session_id。
- `streamlit_app.py`：凭据区场景切换，公司输入 Token，小智自动身份；切换清理设备凭据，保留裁判输入，实际配置传入 worker。
- `ui_jobs.py` / `execution_lock.py`：任务创建时准备固定身份；worker 与各会话复用，直接核心调用也初始化身份。
- `text_input_test.py`：按场景选择连接实现，禁用小智设备工具，复用评分与报告；OTA 返回的新 Token 加入日志脱敏，不记录无关 MQTT 凭据或激活挑战。
- 报告保持 schema_version=2，增加 connection_scene/target_name/client_id/ota_url，旧报告按 company 兼容读取。Excel/Markdown 显式保存接入场景。

小智固定设备描述来自用户提供的 OTA 示例（xingzhi-cube-1.54tft-wifi/1.0.1），不读取设备硬件、不下载固件。用户随后确认，使用提供的同一组参数在控制台登录后可正常对话，因此修正为跨运行复用该身份。

## 本机验证

- 场景及页面专项8项通过：固定身份、跨运行相同身份、OTA一致性、WS请求头、hello、文本session、超时、类别限制及页面实际提交。
- 独立worker通过本机HTTP OTA、真实WebSocket、OpenAI兼容HTTP模拟服务，两个类别各建独立WS，同轮身份相同；实际生成JSON/Excel/Markdown，核对场景与凭据脱敏。
- 公司协议、评分、类型、工具、范围及场景页面等相关回归55项通过（包含8项场景专项）。旧Windows凭据在Mac不存在，回归使用内存mock凭据，不改正式档案。
- 最初版本未调用真实服务；本次修正后用已验证身份完成一次真实 OTA/WS“你好”诊断，收到正常文本与 tts stop。未调用真实裁判、未发送企业微信、未启动调度；页面完整评分仍待用户执行。

## 使用

执行页选择小智AI，按类别选基础问答/反向安全/情感交互，填写当前裁判Key并开始。日志显示场景与本轮Device-Id/Client-Id，报告保存当次WS来源。切回公司场景后重新填写公司Token。

## 身份复用修正

用户反馈控制台登录后，Apifox 使用同组身份正常回答；最新失败任务仍使用随机新身份。当前固定复用 Device-Id `ba:6a:df:36:31:16`、Client-Id `a9c66f04-362c-4834-a315-727c537e8f82`，WS/token仍每轮重新请求OTA。

小智只拼接 tts/sentence_start 文本，避免 sentence_end 重复以及 llm 情绪符号混入评分；公司原拼接不变。新增OTA激活状态（不保存激活码）和失败会话事件摘要，便于定位后续异常。

真实单条诊断：OTA200，无activation，WS/hello成功，收到正常问候文本、sentence_end及tts stop；无裁判调用。专项8项通过，含真实本机worker的重复事件去重断言。
