# 模型评分不可用修复记录

## 问题

评测采集完成，但Qwen和DeepSeek均显示评分失败。报告原因：

```text
judge client init failed: Invalid port: ':1]'
```

## 根因

Windows运行环境的代理排除变量包含：

```text
NO_PROXY=127.0.0.1,localhost,::1,[::1]
```

当前OpenAI SDK/httpx代理解析将方括号形式的`[::1]`误解析成非法端口，导致客户端初始化失败。请求还没有发送到Qwen或DeepSeek。

## 修复

在`judge.py`的评分客户端初始化前，清理`NO_PROXY`/`no_proxy`中的`[::1]`、`[::1]:80`、`[::1]:443`，保留其它代理地址和HTTP代理设置。该修复只处理本地代理环境兼容，不改变评分URL、模型、阈值或业务判定。

## 验证

Windows项目Python3.11环境中：

- 动态裁判测试9项通过。
- integration测试10项通过，包括OpenAI SDK双裁判请求序列化。
- 真实最近报告仍是修复前的评分失败记录；未自动重试真实付费评分请求。
- 服务已重启到`http://127.0.0.1:8501`。

再次真实评测前请确保Qwen/DeepSeek Key有效并确认预算；修复后若仍失败，应查看新的裁判具体返回原因，不再出现当前`Invalid port ':1]'`初始化错误。
