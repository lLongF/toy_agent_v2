#!/usr/bin/env python3
"""命令行：解析参数并组装 Config。裁判与连接配置全仓库共用。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from config import (
    Config,
    DEFAULT_DEVICE_MAC,
    DEFAULT_FIRMWARE_VERSION,
    DEVICE_MAC_LIST,
)
from toy_profiles import DEFAULT_TOY_ID, TOYS_DIR, format_toy_catalog, load_toy_profile

class _ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "用法: "
        return super().add_usage(usage, actions, groups, prefix)


def parse_args() -> tuple[Config, bool]:
    """解析命令行。玩具只覆盖用例/工具/token，其余默认值与 config.Config 注释一致。"""
    _base = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="文本输入 WebSocket 测试框架（用 --toy 切换用例/工具/token；其余配置固定）",
        formatter_class=_ChineseHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示本帮助信息并退出")
    parser.add_argument(
        "--toy",
        default=(os.getenv("TOY_ID") or DEFAULT_TOY_ID).strip() or DEFAULT_TOY_ID,
        help=f"玩具 id，对应 toys/<id>/；只切换测试样例、工具描述和 token。默认 {DEFAULT_TOY_ID}",
    )
    parser.add_argument(
        "--list-toys",
        action="store_true",
        help="列出 toys/ 下已配置的玩具后退出",
    )
    parser.add_argument("--ota-url", default="https://voiceagent.lycheeai.com.cn/iot-server/ota/", help="OTA 接口地址")
    parser.add_argument(
        "--ws-url",
        default="wss://voiceAgent.lycheeai.com.cn/voiceAgent/stream/v1/",
        help="禁用 OTA 时直接使用的 WebSocket 地址",
    )
    parser.add_argument(
        "--device-mac",
        default=DEFAULT_DEVICE_MAC,
        help="设备 MAC，同时作为 OTA 请求头 Device-Id 和 board.mac；默认 18:c0:4d:4f:31:3c，不再随机生成",
    )
    parser.add_argument(
        "--firmware-version",
        default=DEFAULT_FIRMWARE_VERSION,
        help="OTA 请求体 application.version（固件版本），默认 1.0.0",
    )
    parser.add_argument(
        "--device-mac-index",
        type=int,
        default=-1,
        help="使用 DEVICE_MAC_LIST 中的下标（>=0）。有效时覆盖 --device-mac。默认 -1 表示仍使用 --device-mac。",
    )
    parser.add_argument("--board-ip", default="192.168.1.1", help="OTA 请求体中的 board.ip")
    parser.add_argument("--board-type", default="xiaozhi-web-test", help="OTA 请求体中的 board.type")
    parser.add_argument("--client-id", default="web_test_client", help="协议字段中的客户端 ID")
    parser.add_argument("--device-name", default="text-test-device", help="hello 报文中的设备名称")
    parser.add_argument("--token", default="", help="hello 报文中使用的令牌；留空则用 toys/<id>/toy.json 或 TOY_AGENT_TOKEN")
    parser.add_argument(
        "--cases-xlsx",
        default="",
        help="用例表格（xlsx）路径；留空则用 toys/<id>/测试样例.xlsx。列：类型/用例编号/消息列表/预期结果/是否工具调用/工具参数",
    )
    parser.add_argument(
        "--mcp-tools-file",
        default="",
        help="MCP 工具描述文件；留空则用 toys/<id>/工具描述.txt",
    )
    parser.add_argument("--reports-dir", default="", help="报告输出目录；留空则写入 reports/<玩具id>/")
    parser.add_argument("--recv-timeout", type=float, default=3.0, help="单次接收超时时间（秒）")
    parser.add_argument("--send-interval", type=float, default=0.2, help="对话组之间的间隔时间（秒）")
    parser.add_argument(
        "--response-collect-timeout",
        type=float,
        default=45.0,
        help="每次用户发送后采集服务端文本的最长等待时间（秒，直到 TTS 结束或空闲）",
    )
    parser.add_argument(
        "--judge-concurrency",
        type=int,
        default=6,
        help="第二阶段 LLM 评分并发数",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="缩短握手排空和回复收尾等待，并提高评分并发（采集仍按用例隔离 WebSocket）",
    )
    parser.add_argument(
        "--judge-model",
        default="qwen-plus",
        help="Qwen 评分模型名称",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Qwen 评分 API Key（未填写时回退环境变量 DASHSCOPE_API_KEY）",
    )
    parser.add_argument(
        "--deepseek-judge-model",
        default="deepseek-v4-pro",
        help="DeepSeek 评分模型名称",
    )
    parser.add_argument(
        "--deepseek-api-key",
        default="",
        help="DeepSeek 评分 API Key（未填写时回退环境变量 DEEPSEEK_API_KEY）",
    )
    parser.add_argument(
        "--deepseek-base-url",
        default="https://api.deepseek.com",
        help="DeepSeek OpenAI 兼容接口地址",
    )
    parser.add_argument(
        "--disable-deepseek-judge",
        action="store_true",
        help="关闭 DeepSeek 裁判，仅使用 Qwen 打分",
    )
    parser.add_argument(
        "--judge-pass-threshold",
        type=float,
        default=0.7,
        help="LLM 评分通过阈值（双裁判平均分达到该值即通过）",
    )
    parser.add_argument(
        "--export-xlsx",
        default="",
        help="带测试结果列的导出 xlsx 路径；留空则写入 reports/text_result_<时间>_export.xlsx；可与 --cases-xlsx 相同以写回该文件",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="执行完整测试流水线（连接、握手排空、跑用例、写报告）",
    )
    parser._optionals.title = "可选参数"
    args = parser.parse_args()
    if args.list_toys:
        print(format_toy_catalog(TOYS_DIR))
        raise SystemExit(0)
    try:
        toy = load_toy_profile(str(args.toy).strip() or DEFAULT_TOY_ID)
    except FileNotFoundError as exc:
        print(str(exc))
        print(format_toy_catalog(TOYS_DIR))
        raise SystemExit(2) from exc
    mac_is_random = False
    if 0 <= args.device_mac_index < len(DEVICE_MAC_LIST):
        selected_mac = DEVICE_MAC_LIST[args.device_mac_index]
    elif str(args.device_mac).strip():
        selected_mac = str(args.device_mac).strip()
    else:
        selected_mac = DEFAULT_DEVICE_MAC
    export_xlsx: Optional[Path] = None
    if str(args.export_xlsx).strip():
        export_xlsx = Path(args.export_xlsx).expanduser().resolve()
    reports_dir = (
        Path(args.reports_dir).expanduser().resolve()
        if str(args.reports_dir).strip()
        else (_base / "reports" / toy.id)
    )
    cases_xlsx = Path(args.cases_xlsx).expanduser().resolve() if str(args.cases_xlsx).strip() else toy.cases_xlsx
    mcp_tools_file = (
        Path(args.mcp_tools_file).expanduser().resolve() if str(args.mcp_tools_file).strip() else toy.mcp_tools_file
    )
    cfg = Config(
        ota_url=args.ota_url,
        ws_url=args.ws_url,
        device_mac=selected_mac,
        client_id=args.client_id,
        device_name=args.device_name,
        token=toy.resolve_token(args.token),
        cases_xlsx=cases_xlsx,
        mcp_tools_file=mcp_tools_file,
        reports_dir=reports_dir,
        recv_timeout=args.recv_timeout,
        send_interval=args.send_interval,
        response_collect_timeout=args.response_collect_timeout,
        judge_model=args.judge_model,
        judge_pass_threshold=args.judge_pass_threshold,
        api_key=args.api_key,
        deepseek_judge_model=args.deepseek_judge_model,
        deepseek_api_key=args.deepseek_api_key,
        deepseek_base_url=args.deepseek_base_url,
        enable_deepseek_judge=not bool(args.disable_deepseek_judge),
        export_xlsx=export_xlsx,
        judge_concurrency=max(1, int(args.judge_concurrency)),
        board_ip=str(args.board_ip),
        board_type=str(args.board_type),
        firmware_version=str(args.firmware_version).strip() or DEFAULT_FIRMWARE_VERSION,
        device_mac_random=mac_is_random,
        toy_id=toy.id,
    )
    if args.fast:
        cfg.send_interval = 0.0
        cfg.hello_drain_per_wait = 0.2
        cfg.hello_drain_idle_rounds = 3
        cfg.hello_mcp_ready_timeout = 6.0
        cfg.collect_quiet_rounds = 4
        cfg.collect_quiet_seconds = 1.0
        cfg.collect_phase2_max = 1.5
        cfg.mcp_no_call_grace_seconds = 0.5
        cfg.mcp_completion_quiet_seconds = 0.6
        if int(args.judge_concurrency) == 6:
            cfg.judge_concurrency = 8
    return cfg, bool(args.run)
