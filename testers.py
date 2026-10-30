#!/usr/bin/env python3
"""按 Excel「类型」采集回复；评分在第二阶段由 judge 统一做。"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from config import DialogueGroup, TestContext, dialogue_group_to_dict, dialogue_turn_expected_tool, turn_requires_mcp
from mcp import MCPInspector, combined_text_for_mcp_no_tool_llm_judge

# 各个不同类别的实际测试逻辑（按类别封装）
# 测试逻辑修改：初始测试逻辑为每条样例测试后直接进行判断，但会出现ws报错/现已改为获取所有的样例测试结果后同一进行判断
class BasicQATester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "basic_qa",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            for um in turn.user_messages:
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(self.ctx.transport, self.ctx.cfg.response_collect_timeout)
                utterance_results.append(
                    {
                        "user_message": um,
                        "server_text": server_text,
                        "response_complete": response_complete,
                    }
                )
            combined = "\n".join(u["server_text"] for u in utterance_results)
            turn_results.append(
                {
                    "sheet_row": turn.sheet_row,
                    "case_id": turn.case_id,
                    "expected_outcome": turn.expected_outcome,
                    "expect_tool_call": turn.expect_tool_call,
                    "tool_name": turn.tool_name,
                    "tool_parameters": turn.tool_parameters,
                    "utterances": utterance_results,
                    "combined_server_text": combined,
                    "judge_result": {"judge_pending": True},
                    "pass": False,
                }
            )

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "basic_qa",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }


class MCPCallTester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx
        self.mcp = MCPInspector(ctx)

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "mcp_call",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            require_mcp = turn_requires_mcp(turn.expect_tool_call) # 获取是否需要进行工具调用
            turn_pass = True
            combined_text_parts: List[str] = []
            for um in turn.user_messages:
                start_idx = len(self.ctx.state.router_logs)
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                expected_tool = dialogue_turn_expected_tool(turn)

                if require_mcp:
                    collect_result = await self.mcp.collect_result_after_send(start_idx)
                    mcp_calls = collect_result.get("mcp_calls", [])
                    saw_mcp = bool(collect_result.get("saw_mcp", False))
                    assistant_visible = str(collect_result.get("assistant_visible_text", "") or "")
                    assistant_from_logs = str(collect_result.get("assistant_text_from_logs", "") or "")
                    server_actual = (assistant_from_logs or assistant_visible).strip()
                    if server_actual:
                        combined_text_parts.append(server_actual)
                    stop_reason = str(collect_result.get("stop_reason", "") or "")
                    if not saw_mcp:
                        passed = False
                        if stop_reason == "response_complete_no_mcp":
                            reason = "未调用工具：回复结束后仍未收到 MCP tools/call"
                        else:
                            reason = "未调用工具：等待 MCP tools/call 超时"
                    elif not mcp_calls:
                        passed = False
                        reason = "收到 MCP 消息，但没有 tools/call"
                    elif not bool(collect_result.get("mcp_reply_confirmed", False)):
                        passed = False
                        reason = "收到 tools/call，但工具结果未确认发送完成"
                    else:
                        matched_calls = [c for c in mcp_calls if self.mcp.match_expected_tool(expected_tool, c)]
                        passed = len(matched_calls) > 0
                        reason = "matched expected tool call" if passed else "tools/call does not match expected tool"

                    turn_pass = turn_pass and passed
                    row: Dict[str, Any] = {
                        "user_message": um,
                        "mcp_required": True,
                        "expected_tool": expected_tool,
                        "saw_mcp": saw_mcp,
                        "mcp_call_count": len(mcp_calls),
                        "mcp_calls": mcp_calls,
                        "pass": passed,
                        "reason": reason,
                        "timeout": bool(collect_result.get("timeout", False)),
                        "stop_reason": stop_reason,
                        "mcp_reply_confirmed": bool(collect_result.get("mcp_reply_confirmed", False)),
                        "response_complete": bool(collect_result.get("response_complete", False)),
                        "assistant_visible_text": assistant_visible,
                        "assistant_text_from_logs": assistant_from_logs,
                        "mcp_non_call_events": collect_result.get("mcp_non_call_events", []),
                    }
                    if not passed:
                        row["server_actual_when_no_valid_mcp_call"] = server_actual
                    utterance_results.append(row)
                else:
                    server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(
                        self.ctx.transport, self.ctx.cfg.response_collect_timeout
                    )
                    st = str(server_text or "").strip()
                    if st:
                        combined_text_parts.append(st)
                    new_logs = self.ctx.state.router_logs[start_idx:]
                    saw_mcp = any(isinstance(x, dict) and x.get("type") == "mcp" for x in new_logs)
                    row = {
                        "user_message": um,
                        "mcp_required": False,
                        "expected_tool": expected_tool,
                        "server_text": server_text,
                        "server_text_for_judge": st,
                        "response_complete": response_complete,
                        "saw_mcp": saw_mcp,
                        "mcp_call_count": 0,
                        "mcp_calls": [],
                        "pass": True,
                        "reason": "mcp not required per xlsx; pending llm judge",
                        "assistant_visible_text": "",
                        "assistant_text_from_logs": "",
                        "mcp_non_call_events": [],
                    }
                    utterance_results.append(row)

            combined_server_text = "\n".join(p for p in combined_text_parts if p)
            final_pass = turn_pass if require_mcp else False
            turn_row: Dict[str, Any] = {
                "sheet_row": turn.sheet_row,
                "case_id": turn.case_id,
                "expected_outcome": turn.expected_outcome,
                "expect_tool_call": turn.expect_tool_call,
                "tool_name": turn.tool_name,
                "tool_parameters": turn.tool_parameters,
                "utterances": utterance_results,
                "combined_server_text": combined_server_text,
                "mcp_required": require_mcp,
                "pass": final_pass,
            }
            if not require_mcp:
                turn_row["judge_result"] = {"judge_pending": True}
            turn_results.append(turn_row)

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "mcp_call",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }


class EmotionTester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "emotion",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            for um in turn.user_messages:
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(self.ctx.transport, self.ctx.cfg.response_collect_timeout)
                utterance_results.append(
                    {
                        "user_message": um,
                        "server_text": server_text,
                        "response_complete": response_complete,
                    }
                )

            combined = "\n".join(u["server_text"] for u in utterance_results)
            turn_results.append(
                {
                    "sheet_row": turn.sheet_row,
                    "case_id": turn.case_id,
                    "expected_outcome": turn.expected_outcome,
                    "expect_tool_call": turn.expect_tool_call,
                    "tool_name": turn.tool_name,
                    "tool_parameters": turn.tool_parameters,
                    "utterances": utterance_results,
                    "combined_server_text": combined,
                    "judge_result": {"judge_pending": True},
                    "pass": False,
                }
            )

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "emotion",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }

# 知识库：只采集一轮回复，二阶段按普通问答对照预期结果打分。
class KnowledgeBaseTester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "knowledge_base",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            for um in turn.user_messages:
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(
                    self.ctx.transport, self.ctx.cfg.response_collect_timeout
                )
                utterance_results.append(
                    {
                        "user_message": um,
                        "server_text": server_text,
                        "response_complete": response_complete,
                    }
                )
            combined = "\n".join(u["server_text"] for u in utterance_results)
            question_text = " / ".join(str(m) for m in turn.user_messages if isinstance(m, str) and m.strip())
            turn_results.append(
                {
                    "sheet_row": turn.sheet_row,
                    "case_id": turn.case_id,
                    "expected_outcome": turn.expected_outcome,
                    "expect_tool_call": turn.expect_tool_call,
                    "tool_name": turn.tool_name,
                    "tool_parameters": turn.tool_parameters,
                    "question_text": question_text,
                    "knowledge_base_dual_round": False,
                    "utterances": utterance_results,
                    "combined_server_text": combined,
                    "judge_result": {"judge_pending": True},
                    "pass": False,
                }
            )

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "knowledge_base",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }


class ShortTermMemoryTester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx
        self.mcp = MCPInspector(ctx)

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "short_term_memory",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        history_pairs: List[str] = []

        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            mcp_checks: List[Dict[str, Any]] = []
            require_mcp = turn_requires_mcp(turn.expect_tool_call)

            for um in turn.user_messages:
                start_idx = len(self.ctx.state.router_logs)
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(self.ctx.transport, self.ctx.cfg.response_collect_timeout)

                mcp_result: Dict[str, Any] = {
                    "required": require_mcp,
                    "pass": True,
                    "reason": "mcp check not required" if not require_mcp else "mcp check pending",
                }
                if require_mcp:
                    mcp_collect = await self.mcp.collect_result_after_send(start_idx)
                    mcp_calls = mcp_collect.get("mcp_calls", [])
                    saw_mcp = bool(mcp_collect.get("saw_mcp", False))
                    expected_tool = dialogue_turn_expected_tool(turn)
                    avis = str(mcp_collect.get("assistant_visible_text", "") or "")
                    alogs = str(mcp_collect.get("assistant_text_from_logs", "") or "")
                    actual_merge = (alogs or avis or server_text).strip()
                    if not saw_mcp:
                        mcp_result = {
                            "required": True,
                            "pass": False,
                            "reason": "no mcp message received",
                            "saw_mcp": saw_mcp,
                            "mcp_calls": mcp_calls,
                        }
                    elif not mcp_calls:
                        mcp_result = {
                            "required": True,
                            "pass": False,
                            "reason": "mcp received but no tools/call found",
                            "saw_mcp": saw_mcp,
                            "mcp_calls": mcp_calls,
                        }
                    elif not bool(mcp_collect.get("mcp_reply_confirmed", False)):
                        mcp_result = {
                            "required": True,
                            "pass": False,
                            "reason": "tools/call received but tool result was not confirmed sent",
                            "saw_mcp": saw_mcp,
                            "mcp_calls": mcp_calls,
                        }
                    else:
                        matched_calls = [c for c in mcp_calls if self.mcp.match_expected_tool(expected_tool, c)]
                        mcp_result = {
                            "required": True,
                            "pass": len(matched_calls) > 0,
                            "reason": "matched expected tool call"
                            if len(matched_calls) > 0
                            else "tools/call does not match expected tool",
                            "saw_mcp": saw_mcp,
                            "mcp_calls": mcp_calls,
                            "expected_tool": expected_tool,
                        }
                    mcp_result["assistant_visible_text"] = avis
                    mcp_result["assistant_text_from_logs"] = alogs
                    mcp_result["mcp_non_call_events"] = mcp_collect.get("mcp_non_call_events", [])
                    if not mcp_result.get("pass", False):
                        mcp_result["server_actual_when_no_valid_mcp_call"] = actual_merge

                utterance_results.append(
                    {
                        "user_message": um,
                        "server_text": server_text,
                        "response_complete": response_complete,
                        "mcp_check": mcp_result,
                    }
                )
                mcp_checks.append(mcp_result)
                history_assistant = server_text
                if not history_assistant.strip():
                    mcp_calls_for_history = mcp_result.get("mcp_calls", []) if isinstance(mcp_result, dict) else []
                    if mcp_calls_for_history:
                        history_assistant = self.mcp.format_calls_brief(mcp_calls_for_history)
                    else:
                        history_assistant = "[无可见文本回复]"
                history_pairs.append(f"用户:{um}\n助手:{history_assistant}")

            combined = "\n".join(u["server_text"] for u in utterance_results)
            mcp_pass = all(bool(x.get("pass", True)) for x in mcp_checks)

            turn_results.append(
                {
                    "sheet_row": turn.sheet_row,
                    "case_id": turn.case_id,
                    "expected_outcome": turn.expected_outcome,
                    "expect_tool_call": turn.expect_tool_call,
                    "tool_name": turn.tool_name,
                    "tool_parameters": turn.tool_parameters,
                    "mcp_required": require_mcp,
                    "utterances": utterance_results,
                    "combined_server_text": combined,
                    "memory_judge": {"judge_pending": True},
                    "mcp_checks": mcp_checks,
                    "mcp_pass": mcp_pass,
                    "pass": False,
                }
            )

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "short_term_memory",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }


class LongTermMemoryTester:
    def __init__(self, ctx: TestContext):
        self.ctx = ctx

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        if not self.ctx.state.websocket:
            return {
                "handler": "long_term_memory",
                "status": "skipped",
                "reason": "websocket not connected",
                "turn_results": [],
            }

        turn_results: List[Dict[str, Any]] = []
        history_pairs: List[str] = []

        for turn in group.turns:
            utterance_results: List[Dict[str, Any]] = []
            for um in turn.user_messages:
                self.ctx.state.reset_response_collection()
                await self.ctx.pipeline.send_text_message(um)
                server_text, response_complete = await self.ctx.pipeline.collect_server_response_after_send(self.ctx.transport, self.ctx.cfg.response_collect_timeout)
                utterance_results.append(
                    {
                        "user_message": um,
                        "server_text": server_text,
                        "response_complete": response_complete,
                    }
                )
                history_pairs.append(f"用户:{um}\n助手:{server_text}")

            combined = "\n".join(u["server_text"] for u in utterance_results)

            turn_results.append(
                {
                    "sheet_row": turn.sheet_row,
                    "case_id": turn.case_id,
                    "expected_outcome": turn.expected_outcome,
                    "expect_tool_call": turn.expect_tool_call,
                    "tool_name": turn.tool_name,
                    "tool_parameters": turn.tool_parameters,
                    "utterances": utterance_results,
                    "combined_server_text": combined,
                    "judge_result": {"judge_pending": True},
                    "pass": False,
                }
            )

        group_pass = all(bool(t.get("pass", False)) for t in turn_results) if turn_results else False
        return {
            "handler": "long_term_memory",
            "status": "completed",
            "turn_results": turn_results,
            "group_pass": group_pass,
        }

# 非预期样例测试类
class DefaultTester:
    def __init__(self, _ctx: TestContext):
        self.ctx = _ctx

    async def run(self, group: DialogueGroup) -> Dict[str, Any]:
        _ = group
        return {"handler": "default", "status": "not_implemented", "note": "unknown category"}

# 通过testhandler构建不同种类的测试与实际调用的映射
TestHandler = Callable[[DialogueGroup], Awaitable[Dict[str, Any]]] #样本列表+工具调用映射列表

def build_test_handlers(ctx: TestContext) -> tuple[Dict[str, TestHandler], TestHandler]:
    basic_qa = BasicQATester(ctx)
    mcp = MCPCallTester(ctx)
    emotion = EmotionTester(ctx)
    kb = KnowledgeBaseTester(ctx)
    short_mem = ShortTermMemoryTester(ctx)
    long_mem = LongTermMemoryTester(ctx)
    default = DefaultTester(ctx)
    handlers: Dict[str, TestHandler] = {
        "基础问答": basic_qa.run,
        # 与「基础问答」同链路：单轮文本采集 + 二阶段 basic_qa 裁判，不单独建类
        "反向安全": basic_qa.run,
        "MCP调用": mcp.run,
        "情感交互": emotion.run,
        "知识库": kb.run,
        "短期记忆": short_mem.run,
        "长期记忆": long_mem.run,
    }
    return handlers, default.run

class ConversationTestDispatcher:
    """根据 Excel「类型」列选择对应测试函数。"""

    def __init__(
        self,
        handlers: Dict[str, TestHandler],
        default_handler: TestHandler,
    ) -> None:
        self._handlers = handlers
        self._default = default_handler

    def resolve(self, category: str) -> TestHandler:
        return self._handlers.get(category.strip(), self._default) if category else self._default

    async def run_group(self, ctx: TestContext, group: DialogueGroup) -> Dict[str, Any]:
        _ = ctx
        handler = self.resolve(group.category)
        result = await handler(group)
        result.setdefault("category", group.category)
        result.setdefault("group_id", group.group_id)
        result["case_spec"] = dialogue_group_to_dict(group)
        return result

