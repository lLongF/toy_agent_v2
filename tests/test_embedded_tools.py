"""嵌入式工具的纯本地回归；不请求设备或裁判 API。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config import Config, DialogueGroup, DialogueTurn
from judge import post_judge_results
from mcp import MCPInspector
from testers import MCPCallTester, ShortTermMemoryTester
from tools_source import parse_embedded_tools, validate_expected_tools


def tool(name="weather"):
    return {"name": name, "description": "天气", "inputSchema": {"type": "object"}}


def turn(required=True, name="weather", messages=None):
    return DialogueTurn(2, "case", ["天气"] if messages is None else messages, "预期", required, name, "weather")


class EmbeddedToolTests(unittest.TestCase):
    def test_supported_formats(self):
        definition = tool()
        for text in (json.dumps([definition]), repr([definition]), repr(definition) + ",",
                     json.dumps({"tools": [definition]}),
                     json.dumps({"result": {"tools": [definition]}}),
                     json.dumps([{"type": "function", "function": {"name": "weather", "description": "天气", "parameters": {"type": "object"}}}])):
            with self.subTest(text=text):
                self.assertEqual(parse_embedded_tools(text), [definition])
        self.assertEqual(parse_embedded_tools(""), [])

    def test_reject_invalid_and_executable(self):
        invalid = [[tool(), tool()], [{**tool(), "description": None}],
                   [{**tool(), "inputSchema": []}], [{**tool(), "name": " "}],
                   [{**tool(), "inputSchema": {"type": "array"}}], [42]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_embedded_tools(json.dumps(value))
        with self.assertRaises(ValueError):
            parse_embedded_tools("__import__('os').system('echo unsafe')")

    def test_expected_validation(self):
        for category in ("MCP调用", "短期记忆"):
            validate_expected_tools([DialogueGroup("g", category, [turn()])], [tool()])
        for category, name in (("基础问答", "weather"), ("长期记忆", "weather"), ("MCP调用", None), ("MCP调用", "unknown")):
            with self.subTest(category=category, name=name), self.assertRaises(ValueError):
                validate_expected_tools([DialogueGroup("g", category, [turn(name=name)])], [tool()])

    def test_exact_matching(self):
        for expected, name, result in ((None, "weather", False), ("", "weather", False),
                                       ("weather", "Weather", False), ("weather", "weather_extra", False),
                                       ("weather", "other", False), ("weather", "weather", True)):
            self.assertEqual(MCPInspector.match_expected_tool(expected, {"tool_name": name, "tool_args": {"text": "weather"}}), result)

    def test_mixed_only_constructs_language_groups(self):
        cfg = Config(*([""] * 6), Path("unused"), Path("unused"), Path("unused"), 1, 0, 1, "mock", .7)
        results = [
            {"handler": "mcp_call", "category": "MCP调用", "turn_results": [{"expect_tool_call": True, "pass": True}]},
            {"handler": "basic_qa", "category": "基础问答", "turn_results": [{"utterances": [], "combined_server_text": "answer"}]}]
        with patch("judge.DualLLMJudge") as mocked, patch("judge.load_rules", return_value={"thresholds": {}}), patch("judge.case_threshold", return_value=.7):
            mocked.return_value.judges = []
            mocked.return_value.basic_qa.return_value = {"pass": True}
            stats = post_judge_results(results, cfg)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(stats["judge_job_count"], 1)
        self.assertEqual(results[1]["turn_results"][0]["evaluation_mode"], "llm")

    def test_pure_rules_never_build_or_load(self):
        results = [{"handler": "mcp_call", "category": "MCP调用", "turn_results": [{"expect_tool_call": True, "pass": True, "rule_score": 1}]}]
        with patch("judge.DualLLMJudge", side_effect=AssertionError("judge built")), patch("judge.load_rules", side_effect=AssertionError("rules loaded")):
            stats = post_judge_results(results, SimpleNamespace(judge_concurrency=1))
        self.assertEqual(stats["judge_job_count"], 0)
        self.assertEqual(stats["judge_names"], [])
        self.assertTrue(results[0]["group_pass"])


class EmbeddedCollectionTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        state = SimpleNamespace(websocket=True, router_logs=[], reset_response_collection=lambda: None)
        pipeline = SimpleNamespace(send_text_message=AsyncMock(), collect_server_response_after_send=AsyncMock(return_value=("回答", True)))
        return SimpleNamespace(state=state, pipeline=pipeline, transport=None, cfg=SimpleNamespace(response_collect_timeout=0.1))

    async def test_rule_score_and_send_failure_separate(self):
        tester = MCPCallTester(self.context())
        tester.mcp.collect_result_after_send = AsyncMock(return_value={"saw_mcp": True, "mcp_calls": [{"tool_name": "weather"}], "mcp_reply_confirmed": False})
        result = await tester.run(DialogueGroup("g", "MCP调用", [turn()]))
        row = result["turn_results"][0]
        self.assertEqual(row["rule_score"], 1)
        self.assertTrue(row["pass"])
        self.assertEqual(row["evaluation_mode"], "embedded_rule")
        self.assertIn("execution_error", row)

    async def test_no_call_zero_nonrequired_llm_and_protocol_no_fake_score(self):
        tester = MCPCallTester(self.context())
        tester.mcp.collect_result_after_send = AsyncMock(return_value={"saw_mcp": False, "mcp_calls": []})
        row = (await tester.run(DialogueGroup("g", "MCP调用", [turn()]))) ["turn_results"][0]
        self.assertEqual(row["rule_score"], 0)
        row = (await tester.run(DialogueGroup("g", "MCP调用", [turn(False)])))["turn_results"][0]
        self.assertEqual(row["evaluation_mode"], "llm")
        self.assertNotIn("rule_score", row)
        tester.mcp.collect_result_after_send = AsyncMock(side_effect=ValueError("bad protocol"))
        row = (await tester.run(DialogueGroup("g", "MCP调用", [turn()])))["turn_results"][0]
        self.assertIn("execution_error", row)
        self.assertNotIn("rule_score", row)

    async def test_memory_preserves_model_plus_exact_hard_rule(self):
        tester = ShortTermMemoryTester(self.context())
        tester.mcp.collect_result_after_send = AsyncMock(return_value={"saw_mcp": True, "mcp_calls": [{"tool_name": "weather_extra", "tool_args": {"text": "weather"}}], "mcp_reply_confirmed": True})
        row = (await tester.run(DialogueGroup("g", "短期记忆", [turn()])))["turn_results"][0]
        self.assertFalse(row["mcp_pass"])
        self.assertTrue(row["memory_judge"]["judge_pending"])

    async def test_real_collector_drains_after_reply_failure(self):
        state = SimpleNamespace(router_logs=[], response_complete_event=asyncio.Event(), get_full_server_response_text=lambda: "tail")
        state.response_complete_event.set()
        state.router_logs.append({"type": "mcp", "mcp_method": "tools/call", "message": {"payload": {"id": 1, "params": {"name": "weather"}}}})
        state.router_logs.append({"type": "mcp_client_reply", "mcp_id": 1, "sent": False})
        received = []
        async def recv(timeout):
            received.append(True)
            await asyncio.sleep(.005)
            return None
        ctx = SimpleNamespace(state=state, transport=SimpleNamespace(recv_loop_once=recv), cfg=SimpleNamespace(response_collect_timeout=.02, mcp_no_call_grace_seconds=.005, mcp_completion_quiet_seconds=.005, mcp_post_stop_drain_seconds=.005))
        result = await MCPInspector(ctx).collect_result_after_send(0)
        self.assertTrue(received)
        self.assertFalse(result["mcp_reply_confirmed"])
        self.assertEqual(result["mcp_calls"][0]["tool_name"], "weather")

    async def test_empty_turn_is_not_pass_or_scored(self):
        row = (await MCPCallTester(self.context()).run(DialogueGroup("g", "MCP调用", [turn(messages=[])])))["turn_results"][0]
        self.assertFalse(row["pass"])
        self.assertNotIn("rule_score", row)


if __name__ == "__main__":
    unittest.main()
