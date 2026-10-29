# 小智实时对话：本地与服务器部署

从空服务器安装、上传到上线验收的完整步骤见[服务器部署与实时对话验收手册](server_deployment_guide.md)。本文件保留桥接原理和关键配置说明。

部署改的是浏览器到本项目的连接地址。服务器仍使用现有 `XiaozhiTransportClient` 请求官方 OTA，再按 OTA 响应的 URL、Token 与请求头连接官方 WebSocket。公司玩具的四个页面链接和评测链路不变。

## 本地运行

不设置下面两个环境变量，继续使用现有启动命令。本机桥接绑定 `127.0.0.1` 的随机端口，页面访问 `http://127.0.0.1:8501/chat`。打开页面不会请求官方 OTA，点击唤醒才连接。

## 服务器配置

在启动 Streamlit 的**同一进程环境**中设置（域名替换为实际 HTTPS 来源）：

```sh
export XIAOZHI_CHAT_PUBLIC_ORIGIN=https://demo.example.com
export XIAOZHI_CHAT_PORT=8766
python -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Windows PowerShell 使用 `$env:XIAOZHI_CHAT_PUBLIC_ORIGIN='https://demo.example.com'` 和 `$env:XIAOZHI_CHAT_PORT='8766'`。

| 变量 | 约定 |
|---|---|
| `XIAOZHI_CHAT_PUBLIC_ORIGIN` | 浏览器实际访问的 HTTP(S) 来源，含非默认端口时须带端口；不含路径、账号、查询参数。生产使用 HTTPS，浏览器才能正常申请麦克风。 |
| `XIAOZHI_CHAT_PORT` | 桥接内部端口。有公共来源配置时默认 8766；纯本地未设置时为随机端口。必须与代理目标端口一致。 |

修改变量后重启 Streamlit。首次打开“小智”页面时启动内部监听，不会提前请求 OTA。设置公共来源后不再向页面注入回环 WS 地址。

## HTTPS 反向代理

使用 [Nginx 配置示例](../deploy/nginx-chat.conf.example)，把两个 `location` 放入实际域名的 HTTPS `server {}` 内，证书由部署环境配置。指令依据 [Nginx 官方 WebSocket 代理说明](https://nginx.org/en/docs/http/websocket.html)。

浏览器访问 `https://demo.example.com/chat?toy=小智`；文本、录音和播放共用这条链路：

```text
浏览器 --wss://demo.example.com/xiaozhi-ws/<ticket>--> Nginx
Nginx --ws://127.0.0.1:8766/<ticket>--> 本项目桥接
本项目桥接 --官方 OTA / OTA 返回的 WS 地址--> 小智官方服务
```

示例 `proxy_pass` 尾部的 `/` 会移除 `/xiaozhi-ws/` 前缀，保留连接票据。代理必须转发 Upgrade、Connection 和原始 Origin，不能把 Origin 改写为本机来源来绕过校验。桥接只接受配置的完整公共来源及本机来源，继续验证随机连接票据；票据不进入代理访问日志。

8501 和 8766 均保留回环监听，通过 HTTPS 代理暴露入口，无需把原始桥接端口直接开放公网。该配置针对同机部署的一个 Streamlit 实例；多进程/多副本需把页面及桥接路由到同一实例，不能直接使用轮询负载均衡。当前只有一个已绑定的小智设备，设备互斥规则继续生效。

演示页面隐藏评测菜单不是访问鉴权；如部署侧限制演示对象，应在 HTTPS 入口统一配置已有访问控制。

## 验证范围

本地自动化覆盖：无公共配置的原链路、公共 WSS 地址生成、来源与端口校验、错误来源/票据拒绝、本机模拟反向代理的路径改写和 Origin 保留、文本与音频二进制往返，以及原页面切换。上游使用测试替身，核对桥接保留上游地址和令牌；不将本地代理测试当作真实 HTTPS/Nginx 或官方端到端验收。

目标服务器部署后需验收：证书、域名、代理升级、浏览器麦克风授权、实际语音收发、服务端对官方 OTA/WS 的网络可达性。官方 Token 仍只保留在服务器内存，不进入页面。
