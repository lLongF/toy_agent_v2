"""Isolated registry/collector tests: never modify project assets or call services."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import openpyxl

from asset_store import case_issues, digest, save_cases
from category_store import (BUILTIN_HANDLERS, case_types_hash, create_case_type,
                            list_case_types, registry_path, update_case_type)
from config import DialogueGroup
from evaluation_support import load_rules, render_prompt
from testers import ConversationTestDispatcher, build_test_handlers

PROJECT = Path(__file__).resolve().parents[1]


class CustomCategoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def cfg(self, overrides=None, cases=None):
        thresholds = self.root / "thresholds.json"
        thresholds.write_text(json.dumps({"thresholds": overrides or {}, "case_thresholds": cases or []}), encoding="utf-8")
        return SimpleNamespace(judge_template_file=PROJECT / "templates" / "template.md",
                               thresholds_file=thresholds, judge_pass_threshold=.6)

    def test_missing_registry_is_read_only_seven_builtins(self):
        entries = list_case_types(self.root)
        self.assertEqual([e["name"] for e in entries], list(BUILTIN_HANDLERS))
        self.assertTrue(all(e["builtin"] for e in entries))
        self.assertEqual(case_types_hash(self.root), "")
        self.assertFalse(registry_path(self.root).exists())

    def test_aliases_and_independent_rules_snapshot(self):
        create_case_type("儿童安全", "反向安全", "仅按儿童安全预期评分", .85, root=self.root)
        create_case_type("设备服务", "mcp_call", "无需工具时按文本预期评分", .81, root=self.root)
        rules = load_rules(self.cfg(), root=self.root)
        self.assertEqual(rules["category_handlers"]["儿童安全"], "basic_qa")
        self.assertEqual(rules["category_bases"]["儿童安全"], "反向安全")
        self.assertEqual(rules["category_bases"]["设备服务"], "MCP调用")
        self.assertEqual(rules["thresholds"]["儿童安全"], .85)
        self.assertEqual(rules["thresholds"]["基础问答"], .6)
        self.assertEqual(rules["thresholds"]["设备服务"], .81)
        self.assertEqual(rules["mcp_rule_threshold"], 1)
        self.assertIn("仅按儿童安全预期评分", render_prompt(rules, "儿童安全", {}, "score"))
        update_case_type("儿童安全", expected_hash=case_types_hash(self.root), rubric="新版标准", threshold=.9, root=self.root)
        self.assertEqual(rules["rubrics"]["儿童安全"], "仅按儿童安全预期评分")
        self.assertEqual(load_rules(self.cfg({"儿童安全": .95}), root=self.root)["thresholds"]["儿童安全"], .95)

    def test_validation_duplicates_unknown_and_bad_thresholds(self):
        for name, base, rubric, threshold in [
            ("基础问答", "basic_qa", "标准", .7), ("basic_qa", "basic_qa", "标准", .7),
            ("x", "unknown", "标准", .7), ("x", "emotion", "", .7),
            ("x", "emotion", "标准", float("nan")), ("x", "emotion", "标准", True),
            (" x ", "emotion", "标准", .7)]:
            with self.subTest(name=name, base=base, threshold=threshold), self.assertRaises(ValueError):
                create_case_type(name, base, rubric, threshold, root=self.root)
        create_case_type("x", "emotion", "标准", root=self.root)
        with self.assertRaises(ValueError):
            create_case_type("X", "emotion", "标准", root=self.root)
        with self.assertRaises(ValueError):
            create_case_type("y", "x", "标准", root=self.root)
        with self.assertRaises(ValueError):
            load_rules(self.cfg({"unknown": .8}), root=self.root)
        with self.assertRaises(ValueError):
            load_rules(self.cfg(cases=[{"toy_id": "t", "category": "unknown", "case_id": "1", "threshold": .8}]), root=self.root)
        with self.assertRaises(ValueError):
            update_case_type("基础问答", expected_hash=case_types_hash(self.root), rubric="替换", root=self.root)

    def test_cas_and_backup(self):
        create_case_type("x", "emotion", "标准", expected_hash="", root=self.root)
        old = case_types_hash(self.root)
        update_case_type("x", expected_hash=old, threshold=.8, root=self.root)
        self.assertTrue(list((self.root / "settings" / ".backups").glob("*case_types.json")))
        with self.assertRaises(ValueError):
            update_case_type("x", expected_hash=old, threshold=.9, root=self.root)
        with self.assertRaises(ValueError):
            create_case_type("y", "emotion", "标准", expected_hash=old, root=self.root)
        self.assertEqual(list_case_types(self.root)[-1]["threshold"], .8)

    def test_corrupt_registry_fails_closed(self):
        path = registry_path(self.root)
        path.parent.mkdir()
        for content in ["{bad", '{"version":1,"case_types":[{"name":"x"}]}']:
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                list_case_types(self.root)

    def test_assets_only_accept_registered_types(self):
        path = self.root / "cases.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Test case"
        sheet.append(["类型", "用例编号", "消息列表", "预期结果"])
        sheet.append(["童话", "1", "讲故事", "适龄"])
        workbook.save(path)
        workbook.close()
        self.assertTrue(case_issues(path, root=self.root))
        row = {"源行号": 2, "类型": "童话", "用例编号": "1", "消息列表": "讲故事", "预期结果": "适龄"}
        with self.assertRaises(ValueError):
            save_cases(path, [row], digest(path), allowed_root=self.root)
        create_case_type("童话", "basic_qa", "适龄叙述", root=self.root)
        self.assertEqual(case_issues(path, root=self.root), [])
        save_cases(path, [row], digest(path), allowed_root=self.root)
        row["类型"] = "未登记"
        with self.assertRaises(ValueError):
            save_cases(path, [row], digest(path), allowed_root=self.root)

    def test_dispatch_uses_snapshot_preserves_name(self):
        for index, base in enumerate(("basic_qa", "emotion", "knowledge_base", "short_term_memory", "long_term_memory", "mcp_call")):
            create_case_type("自定义" + str(index), base, "独立标准", root=self.root)
        cfg = self.cfg()
        cfg.rules_snapshot = load_rules(cfg, root=self.root)
        ctx = SimpleNamespace(cfg=cfg, state=SimpleNamespace(websocket=None))
        handlers, fallback = build_test_handlers(ctx)
        dispatcher = ConversationTestDispatcher(handlers, fallback)
        for entry in list_case_types(self.root)[7:]:
            group = DialogueGroup(entry["name"], entry["name"], [])
            result = asyncio.run(dispatcher.run_group(ctx, group))
            self.assertEqual(result["category"], entry["name"])
            self.assertEqual(result["handler"], entry["handler"])
            self.assertEqual(group.category, entry["name"])
        # New registry changes cannot alter an already loaded run snapshot.
        create_case_type("稍后新增", "emotion", "标准", root=self.root)
        self.assertNotIn("稍后新增", build_test_handlers(ctx)[0])


if __name__ == "__main__":
    unittest.main()
