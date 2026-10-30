import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_integration import config
from evaluation_support import load_rules
from judge import build_judge_endpoints, DualLLMJudge, LLMJudge, post_judge_results
from judge_store import (create_judge_model, update_judge_model, list_judge_models, registry_hash,
                         snapshot_judge_models, validate_judge_model)


class DynamicJudgeTests(unittest.TestCase):
    def model(self, identity="custom"):
        return dict(id=identity, name="显示名", model="compatible-model", base_url="https://judge.invalid/v1",
                    credential_ref="env:MOCK_JUDGE_KEY", enabled=True, extra_body={})

    def test_virtual_defaults_and_legacy_config(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual([m["id"] for m in list_judge_models(root)], ["qwen", "deepseek"])
            self.assertEqual(registry_hash(root), "")
            cfg = config()
            cfg.api_key = "test-only-memory"
            cfg.judge_model = "legacy-model"
            endpoints = build_judge_endpoints(cfg, root)
            self.assertEqual(endpoints[0].model, "legacy-model")
            self.assertEqual(endpoints[1].extra_body, {"thinking": {"type": "disabled"}})
            cfg.selected_judge_ids = ["qwen"]
            self.assertEqual(build_judge_endpoints(cfg, root)[0].api_key, "test-only-memory")

    def test_crud_conflict_and_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            item = create_judge_model("显示名", "m", "https://judge.invalid/v1", "MOCK_JUDGE_KEY",
                                      judge_id="custom", root=root)
            before = registry_hash(root)
            frozen = snapshot_judge_models([item["id"]], root)
            updated = update_judge_model("custom", expected_hash=before, name="新名称", enabled=False, root=root)
            self.assertFalse(updated["enabled"])
            self.assertEqual(frozen[0]["name"], "显示名")
            self.assertNotIn("api_key", frozen[0])
            with self.assertRaises(ValueError):
                update_judge_model("custom", expected_hash=before, model="lost", root=root)
            with self.assertRaises(ValueError):
                snapshot_judge_models(["custom"], root)
            with self.assertRaises(ValueError):
                snapshot_judge_models(["qwen", "qwen"], root)
            with self.assertRaises(ValueError):
                snapshot_judge_models(["missing"], root)
            cfg = config()
            cfg.selected_judge_ids = ["custom"]
            cfg.judge_endpoints_snapshot = frozen
            cfg.judge_credentials = {"custom": "memory-only"}
            self.assertEqual(build_judge_endpoints(cfg, root)[0].api_key, "memory-only")

    def test_strict_validation(self):
        for url in ["file:///tmp/x", "https://user:pass@host/v1", "https://host/?key=x", "https://host/#x",
                    "https://host:99999", "https://host/../x", "https://host\\evil", "https://host/\nx"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_judge_model(dict(self.model(), base_url=url))
        for change in [dict(api_key="not-allowed"), dict(enabled="yes"), dict(credential_ref="sk-not-a-reference"),
                       dict(extra_body={"api_key": "never"}), dict(extra_body={"thinking": {"type": "script"}})]:
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                validate_judge_model(dict(self.model(), **change))

    def test_encrypted_reference_and_environment_resolution(self):
        with tempfile.TemporaryDirectory() as root, patch("local_credentials.save_token", return_value="local:" + "a" * 32) as save:
            item = create_judge_model("local", "m", "https://judge.invalid", token="mock-memory", root=root)
            save.assert_called_once_with("mock-memory", Path(root).resolve())
            text = (Path(root) / "settings/judge_models.json").read_text(encoding="utf-8")
            self.assertNotIn("mock-memory", text)
            cfg = config()
            cfg.selected_judge_ids = [item["id"]]
            cfg.judge_endpoints_snapshot = [item]
            with patch("local_credentials.read_token", return_value="decrypted-mock"):
                self.assertEqual(build_judge_endpoints(cfg, root)[0].api_key, "decrypted-mock")
            cfg.judge_endpoints_snapshot = [self.model()]
            cfg.selected_judge_ids = ["custom"]
            with patch.dict("os.environ", {"MOCK_JUDGE_KEY": "env-mock"}):
                self.assertEqual(build_judge_endpoints(cfg, root)[0].api_key, "env-mock")

    def test_any_failure_fails_and_arithmetic_mean(self):
        cfg = config()
        cfg.selected_judge_ids = ["a", "b", "c"]
        cfg.judge_endpoints_snapshot = [self.model(i) for i in cfg.selected_judge_ids]
        cfg.judge_credentials = {i: "mock-memory" for i in cfg.selected_judge_ids}
        def score(judge, **kwargs):
            return judge._with_identity(dict(judge_available=True, score={"a": .3, "b": .9, "c": .9}[judge.endpoint.name], reason="mock", **{"pass": True}))
        with patch.object(LLMJudge, "basic_qa", score):
            result = DualLLMJudge(cfg).basic_qa("q", "e", "a")
        self.assertAlmostEqual(result["score"], .7)
        self.assertEqual(list(result["score_by_judge"]), ["a", "b", "c"])
        self.assertEqual(result["judges"][0]["display_name"], "显示名")
        def partial(judge, **kwargs):
            return judge._unavailable({}, "mock failure") if judge.endpoint.name == "b" else score(judge)
        with patch.object(LLMJudge, "basic_qa", partial):
            result = DualLLMJudge(cfg).basic_qa("q", "e", "a")
        self.assertFalse(result["judge_available"])
        self.assertNotIn("score", result)

    def test_snapshot_validation_and_unexpected_judge_exception(self):
        cfg = config()
        cfg.selected_judge_ids = ["custom"]
        cfg.judge_endpoints_snapshot = [self.model("other")]
        with self.assertRaises(ValueError):
            build_judge_endpoints(cfg)
        cfg.judge_endpoints_snapshot = [self.model()]
        cfg.judge_credentials = {"custom": "mock-memory"}
        with patch.object(LLMJudge, "basic_qa", side_effect=RuntimeError("mock-memory")):
            result = DualLLMJudge(cfg).basic_qa("q", "e", "a")
        self.assertFalse(result["judge_available"])
        self.assertNotIn("mock-memory", str(result))
        self.assertNotIn("mock-memory", repr(build_judge_endpoints(cfg)[0]))

    def test_pure_rules_no_client_and_empty_models(self):
        cfg = config()
        cfg.selected_judge_ids = []
        self.assertEqual(build_judge_endpoints(cfg), [])
        results = [dict(category="MCP调用", handler="mcp_call", turn_results=[dict(expect_tool_call=True, **{"pass": True})])]
        with patch.object(LLMJudge, "_create_client", side_effect=AssertionError("must not initialize")):
            info = post_judge_results(results, cfg)
        self.assertEqual(info["judge_job_count"], 0)
        self.assertFalse(DualLLMJudge(cfg).basic_qa("q", "e", "a")["judge_available"])

    def test_custom_handler_and_prompt_preserve_category(self):
        cfg = config()
        rules = load_rules(cfg)
        rules["category_handlers"] = {"自定义共情": "emotion"}
        rules["category_bases"] = {"自定义共情": "情感交互"}
        rules["rubrics"]["自定义共情"] = "CUSTOM RUBRIC"
        rules["thresholds"]["自定义共情"] = .8
        cfg.rules_snapshot = rules
        cfg.selected_judge_ids = ["custom"]
        cfg.judge_endpoints_snapshot = [self.model()]
        cfg.judge_credentials = {"custom": "mock-key"}
        turns = [dict(case_id="1", combined_server_text="a", utterances=[], expected_outcome="e")]
        results = [dict(category="自定义共情", handler="custom", turn_results=turns)]
        prompts = []
        def chat(judge, client, prompt):
            prompts.append(prompt)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(dict(score=.9, reason="ok"))))])
        with patch.object(LLMJudge, "_create_client", return_value=(object(), None)), patch.object(LLMJudge, "_chat", chat):
            info = post_judge_results(results, cfg)
        self.assertEqual(info["judge_job_count"], 1)
        self.assertTrue(turns[0]["pass"])
        self.assertIn("自定义共情", prompts[0])
        self.assertIn("CUSTOM RUBRIC", prompts[0])

    def test_exception_key_redacted(self):
        cfg = config()
        cfg.selected_judge_ids = ["custom"]
        cfg.judge_endpoints_snapshot = [self.model()]
        cfg.judge_credentials = {"custom": "sensitive-mock-only"}
        judge = DualLLMJudge(cfg).judges[0]
        with patch.object(judge, "_create_client", return_value=(object(), None)), \
             patch.object(judge, "_chat", side_effect=RuntimeError("sensitive-mock-only")):
            self.assertNotIn("sensitive-mock-only", str(judge.basic_qa("q", "e", "a")))


if __name__ == "__main__":
    unittest.main()
