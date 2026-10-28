#!/usr/bin/env python3
"""LLM 裁判：Qwen + DeepSeek V4 Pro 并行打分后按均分判定。"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from config import Config, combined_text_for_mcp_no_tool_llm_judge, turn_requires_mcp
from mcp import MCPInspector
from evaluation_support import load_rules, render_prompt, valid_score, case_threshold
from judge_store import snapshot_judge_models, validate_judge_model, resolve_judge_credential


@dataclass
class JudgeEndpoint:
    """单个评分模型的接入配置。"""
    name: str
    model: str
    api_key: str = field(repr=False)
    base_url: str
    extra_body: Dict[str, Any] = field(default_factory=dict)
    display_name: str = ""
    credential_error: str = ""


def build_judge_endpoints(cfg: Config, root=None) -> List[JudgeEndpoint]:
    """Frozen snapshot > explicit selection > historical CLI configuration."""
    snapshot = getattr(cfg, "judge_endpoints_snapshot", None)
    selected = getattr(cfg, "selected_judge_ids", None)
    if snapshot or selected is not None:
        if snapshot:
            if not isinstance(snapshot, list):
                raise ValueError("裁判快照必须为列表")
            models = [validate_judge_model(item) for item in snapshot]
            ids = [m["id"] for m in models]
            if len(set(i.casefold() for i in ids)) != len(ids):
                raise ValueError("裁判快照 id 重复")
            if selected is not None and ids != selected:
                raise ValueError("裁判快照与所选 id 不匹配")
            if any(not m["enabled"] for m in models):
                raise ValueError("裁判快照包含禁用模型")
        else:
            models = snapshot_judge_models(selected, root=root, cfg=cfg)
        endpoints = []
        for item in models:
            error = ""
            try:
                credentials = dict(getattr(cfg, "judge_credentials", {}) or {})
                legacy_field = {"qwen": ("DASHSCOPE_API_KEY", "api_key"),
                                "deepseek": ("DEEPSEEK_API_KEY", "deepseek_api_key")}.get(item["id"])
                if legacy_field and item["credential_ref"] in (legacy_field[0], "env:" + legacy_field[0]):
                    legacy_key = getattr(cfg, legacy_field[1], "")
                    if legacy_key:
                        credentials.setdefault(item["id"], legacy_key)
                key = resolve_judge_credential(item, credentials, root=root)
            except Exception:
                key, error = "", "裁判凭据引用无法解析"
            endpoints.append(JudgeEndpoint(name=item["id"], display_name=item["name"], model=item["model"],
                                           api_key=key, base_url=item["base_url"],
                                           extra_body=item["extra_body"], credential_error=error))
        return endpoints
    endpoints: List[JudgeEndpoint] = []
    qwen_key = (cfg.api_key or "").strip() or (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    qwen_url = (
        (cfg.judge_base_url or "").strip()
        or (os.getenv("DASHSCOPE_BASE_URL") or "").strip()
        or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    endpoints.append(
        JudgeEndpoint(
            name="qwen",
            model=cfg.judge_model,
            api_key=qwen_key,
            base_url=qwen_url,
        )
    )
    ds_key = (cfg.deepseek_api_key or "").strip() or (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if cfg.enable_deepseek_judge:
        endpoints.append(
            JudgeEndpoint(
                name="deepseek",
                model=(cfg.deepseek_judge_model or "deepseek-v4-pro").strip() or "deepseek-v4-pro",
                api_key=ds_key,
                base_url=(cfg.deepseek_base_url or "https://api.deepseek.com").strip() or "https://api.deepseek.com",
                extra_body={"thinking": {"type": "disabled"}},
            )
        )
    return endpoints


def _mean_optional(values: List[Any]) -> Optional[float]:
    nums: List[float] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return None
    return sum(nums) / len(nums)


def _judge_numeric_score(raw: Dict[str, Any]) -> Optional[float]:
    """单裁判用于均分的分数：优先 score，否则知识库三分数取平均。"""
    if raw.get("score") is not None:
        return _mean_optional([raw.get("score")])
    return _mean_optional([raw.get("score_1"), raw.get("score_2"), raw.get("diversity_score")])


def merge_judge_results(per_judge: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    """合并多个裁判的打分：两边都可用时，平均分 ≥ 阈值即通过。"""
    checked = []
    for result in per_judge:
        result = dict(result)
        if result.get("judge_available"):
            try:
                for key in (["score"] if result.get("score") is not None else ["score_1", "score_2", "diversity_score"]):
                    valid_score(result.get(key))
                valid_score(_judge_numeric_score(result))
            except ValueError as exc:
                result.update(judge_available=False, score=None, **{"pass": False, "reason": str(exc)})
        checked.append(result)
    per_judge = checked
    names = [
        str(r.get("judge_name") or r.get("model") or f"judge{i}")
        for i, r in enumerate(per_judge, start=1)
    ]
    all_available = bool(per_judge) and all(bool(r.get("judge_available")) for r in per_judge)

    score_parts: List[str] = []
    reason_parts: List[str] = []
    score_by_judge: Dict[str, Any] = {}
    for name, raw in zip(names, per_judge):
        if raw.get("score") is not None:
            score_parts.append(f"{name}={raw.get('score')}")
            score_by_judge[name] = raw.get("score")
        else:
            bits: List[str] = []
            if raw.get("score_1") is not None:
                bits.append(f"第1轮={raw.get('score_1')}")
            if raw.get("score_2") is not None:
                bits.append(f"第2轮={raw.get('score_2')}")
            if raw.get("diversity_score") is not None:
                bits.append(f"多样性={raw.get('diversity_score')}")
            if bits:
                score_parts.append(f"{name}({'; '.join(bits)})")
            score_by_judge[name] = {
                k: raw.get(k)
                for k in ("score_1", "score_2", "diversity_score")
                if raw.get(k) is not None
            }
        reason = str(raw.get("reason", "") or "").strip()
        avail = "正常" if raw.get("judge_available") else "不可用"
        passed = "通过" if raw.get("pass") else "不通过"
        reason_parts.append(f"[{name}|{avail}|{passed}] {reason}".strip())

    avg_score = _mean_optional([_judge_numeric_score(r) for r in per_judge]) if all_available else None
    overall_pass = bool(all_available and avg_score is not None and avg_score >= cfg.judge_pass_threshold)
    merged: Dict[str, Any] = {
        "judge_available": all_available,
        "pass": overall_pass,
        "reason": " | ".join(reason_parts),
        "threshold": cfg.judge_pass_threshold,
        "model": ",".join(str(r.get("model") or "") for r in per_judge),
        "judges": per_judge,
        "score_by_judge": score_by_judge,
    }
    if avg_score is not None:
        merged["score"] = avg_score
        score_parts.append(f"综合={avg_score:.4g}")
    avg_s1 = _mean_optional([r.get("score_1") for r in per_judge])
    avg_s2 = _mean_optional([r.get("score_2") for r in per_judge])
    avg_div = _mean_optional([r.get("diversity_score") for r in per_judge])
    if avg_s1 is not None:
        merged["score_1"] = avg_s1
    if avg_s2 is not None:
        merged["score_2"] = avg_s2
    if avg_div is not None:
        merged["diversity_score"] = avg_div
    if score_parts:
        merged["score_display"] = "; ".join(score_parts)

    def all_bool(key: str) -> Optional[bool]:
        if not any(key in r for r in per_judge):
            return None
        return all(bool(r.get(key)) for r in per_judge)

    def any_bool(key: str) -> Optional[bool]:
        if not any(key in r for r in per_judge):
            return None
        return any(bool(r.get(key)) for r in per_judge)

    for key in ("is_context_consistent", "memory_recall_correct", "pass_1", "pass_2", "diversity_pass"):
        value = all_bool(key)
        if value is not None:
            merged[key] = value
    for key in ("is_off_topic", "is_emotion_mismatch", "has_memory_gap", "is_redundant_questioning"):
        value = any_bool(key)
        if value is not None:
            merged[key] = value
    return merged


# 基于 Qwen + DeepSeek V4 Pro 对各类测试打分（回应匹配度、情感倾向等）
class LLMJudge:
    """单个评分模型的大模型评分工具。"""

    def __init__(self, cfg: Config, endpoint: Optional[JudgeEndpoint] = None):
        self.cfg = cfg
        if endpoint is None:
            endpoints = build_judge_endpoints(cfg)
            endpoint = endpoints[0]
        self.endpoint = endpoint
        self._client: Any = None
        self._client_err: Optional[str] = None
        self._client_ready = False

    def _prompt(self, method: str, inputs: dict, schema: str) -> str:
        rules = self.cfg.rules_snapshot or load_rules(self.cfg)
        category = self.cfg.judge_category
        if category == "基础问答" and category not in rules.get("category_bases", {}):
            category = {"emotion": "情感交互", "short_memory": "短期记忆", "long_memory": "长期记忆",
                        "knowledge_base": "知识库"}.get(method, category)
        return render_prompt(rules, category, inputs, schema)

    # json对象解析
    @staticmethod
    def _parse_json_object_from_text(raw_text: str) -> Dict[str, Any]:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise
    def _get_client(self):
        if not self._client_ready:
            self._client, self._client_err = self._create_client()
            self._client_ready = True
        return self._client, self._client_err

    def _chat(self, client: Any, prompt: str) -> Any:
        kwargs: Dict[str, Any] = {
            "model": self.endpoint.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.endpoint.extra_body:
            kwargs["extra_body"] = self.endpoint.extra_body
        return client.chat.completions.create(**kwargs)

    @staticmethod
    def _response_text(resp: Any) -> str:
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if content:
            return str(content)
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            return str(reasoning)
        return ""

    def _safe_reason(self, reason: str) -> str:
        key = self.endpoint.api_key
        return str(reason).replace(key, "[REDACTED]") if key else str(reason)

    def _unavailable(self, extra: Dict[str, Any], reason: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "judge_available": False,
            "pass": False,
            "score": None,
            "reason": self._safe_reason(reason),
            "display_name": self.endpoint.display_name or self.endpoint.name,
            "model": self.endpoint.model,
            "judge_name": self.endpoint.name,
        }
        out.update(extra)
        return out

    def _with_identity(self, result: Dict[str, Any]) -> Dict[str, Any]:
        result.setdefault("model", self.endpoint.model)
        result.setdefault("judge_name", self.endpoint.name)
        result.setdefault("display_name", self.endpoint.display_name or self.endpoint.name)
        if "reason" in result:
            result["reason"] = self._safe_reason(result["reason"])
        return result

    # 测试端创建
    def _create_client(self):
        api_key = (self.endpoint.api_key or "").strip()
        if self.endpoint.credential_error:
            return None, self.endpoint.credential_error
        if not api_key:
            hint = (
                "set --deepseek-api-key or DEEPSEEK_API_KEY"
                if self.endpoint.name == "deepseek"
                else "set --api-key or DASHSCOPE_API_KEY"
            )
            return None, f"judge client init failed: missing api key for {self.endpoint.name} ({hint})"

        base_url = (self.endpoint.base_url or "").strip()
        if not base_url:
            return None, f"judge client init failed: missing base_url for {self.endpoint.name}"

        try:
            # 当前 Windows httpx/OpenAI SDK 对 NO_PROXY 的 [::1] 条目解析为非法端口。
            # 仅清理方括号形式，保留代理和其他 NO_PROXY 地址；不改变业务请求目标。
            import os
            no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
            if no_proxy:
                cleaned = ",".join(part.strip() for part in no_proxy.split(",")
                                    if part.strip() not in {"[::1]", "[::1]:80", "[::1]:443"})
                os.environ["NO_PROXY"] = cleaned
                os.environ["no_proxy"] = cleaned
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=120.0,
            )
            return client, None
        except Exception as exc:
            return None, self._safe_reason(f"judge client init failed: {exc}")

    def basic_qa(self, question_text: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        client, err = self._get_client()
        if client is None:
            return self._unavailable({"is_off_topic": True}, err or "judge client unavailable")
        try:
            prompt = self._prompt('basic_qa', {"用户问题": question_text, "预期要求": expected_outcome, "模型回答": actual_answer_text}, 'score(number), pass(boolean), is_off_topic(boolean), reason(string)')
            resp = self._chat(client, prompt)
            parsed = self._parse_json_object_from_text(self._response_text(resp))
            score = valid_score(parsed.get("score"))
            is_off_topic = bool(parsed.get("is_off_topic", False))
            llm_pass = True  # 由程序依据分数和既有诊断标志计算，不信任模型 pass。
            return self._with_identity({
                "judge_available": True,
                "pass": llm_pass and score >= self.cfg.judge_pass_threshold and not is_off_topic,
                "score": score,
                "reason": str(parsed.get("reason", "")),
                "is_off_topic": is_off_topic,
                "threshold": self.cfg.judge_pass_threshold,
            })
        except Exception as exc:
            return self._unavailable({"is_off_topic": True}, f"judge failed: {exc}")

    def emotion(self, question_text: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        client, err = self._get_client()
        if client is None:
            return self._unavailable({"is_emotion_mismatch": True}, err or "judge client unavailable")
        try:
            prompt = self._prompt('emotion', {"用户输入": question_text, "预期要求": expected_outcome, "模型回答": actual_answer_text}, 'score(number), pass(boolean), is_emotion_mismatch(boolean), reason(string)')
            resp = self._chat(client, prompt)
            parsed = self._parse_json_object_from_text(self._response_text(resp))
            score = valid_score(parsed.get("score"))
            mismatch = bool(parsed.get("is_emotion_mismatch", False))
            llm_pass = True  # 由程序依据分数和既有诊断标志计算，不信任模型 pass。
            return self._with_identity({
                "judge_available": True,
                "pass": llm_pass and score >= self.cfg.judge_pass_threshold and not mismatch,
                "score": score,
                "reason": str(parsed.get("reason", "")),
                "is_emotion_mismatch": mismatch,
                "threshold": self.cfg.judge_pass_threshold,
            })
        except Exception as exc:
            return self._unavailable({"is_emotion_mismatch": True}, f"judge failed: {exc}")

    def knowledge_base(
        self,
        question_text: str,
        expected_outcome: str,
        actual_answer_text_1: str,
        actual_answer_text_2: str,
    ) -> Dict[str, Any]:
        client, err = self._get_client()
        if client is None:
            return self._unavailable(
                {
                    "diversity_pass": False,
                    "score_1": 0.0,
                    "score_2": 0.0,
                    "diversity_score": 0.0,
                    "pass_1": False,
                    "pass_2": False,
                },
                err or "judge client unavailable",
            )
        try:
            prompt = self._prompt('knowledge_base', {"用户问题": question_text, "预期要求": expected_outcome, "第一次回答": actual_answer_text_1, "第二次回答": actual_answer_text_2}, 'score_1(number), score_2(number), diversity_score(number), pass_1(boolean), pass_2(boolean), diversity_pass(boolean), pass(boolean), reason(string)')
            resp = self._chat(client, prompt)
            parsed = self._parse_json_object_from_text(self._response_text(resp))
            score_1 = valid_score(parsed.get("score_1"))
            score_2 = valid_score(parsed.get("score_2"))
            diversity_score = valid_score(parsed.get("diversity_score"))
            pass_1 = bool(parsed.get("pass_1", score_1 >= self.cfg.judge_pass_threshold))
            pass_2 = bool(parsed.get("pass_2", score_2 >= self.cfg.judge_pass_threshold))
            diversity_pass = bool(parsed.get("diversity_pass", diversity_score >= self.cfg.judge_pass_threshold))
            composite = _mean_optional([score_1, score_2, diversity_score]) or 0.0
            final_pass = bool(parsed.get("pass", pass_1 and pass_2 and diversity_pass))
            return self._with_identity({
                "judge_available": True,
                "pass": final_pass and pass_1 and pass_2 and diversity_pass,
                "score": composite,
                "score_1": score_1,
                "score_2": score_2,
                "diversity_score": diversity_score,
                "pass_1": pass_1,
                "pass_2": pass_2,
                "diversity_pass": diversity_pass,
                "threshold": self.cfg.judge_pass_threshold,
                "reason": str(parsed.get("reason", "")),
            })
        except Exception as exc:
            return self._unavailable(
                {
                    "score_1": 0.0,
                    "score_2": 0.0,
                    "diversity_score": 0.0,
                    "pass_1": False,
                    "pass_2": False,
                    "diversity_pass": False,
                },
                f"judge failed: {exc}",
            )

    def short_memory(self, conversation_history: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        client, err = self._get_client()
        if client is None:
            return self._unavailable(
                {
                    "is_context_consistent": False,
                    "has_memory_gap": True,
                    "is_redundant_questioning": True,
                },
                err or "judge client unavailable",
            )
        try:
            prompt = self._prompt('short_memory', {"对话历史": conversation_history, "预期要求": expected_outcome, "模型回答": actual_answer_text}, 'score(number), pass(boolean), is_context_consistent(boolean), has_memory_gap(boolean), is_redundant_questioning(boolean), reason(string)')
            resp = self._chat(client, prompt)
            parsed = self._parse_json_object_from_text(self._response_text(resp))
            score = valid_score(parsed.get("score"))
            is_context_consistent = bool(parsed.get("is_context_consistent", False))
            has_memory_gap = bool(parsed.get("has_memory_gap", True))
            is_redundant_questioning = bool(parsed.get("is_redundant_questioning", True))
            llm_pass = True  # 由程序依据分数和既有诊断标志计算，不信任模型 pass。
            return self._with_identity({
                "judge_available": True,
                "pass": llm_pass and score >= self.cfg.judge_pass_threshold and is_context_consistent and not has_memory_gap and not is_redundant_questioning,
                "score": score,
                "is_context_consistent": is_context_consistent,
                "has_memory_gap": has_memory_gap,
                "is_redundant_questioning": is_redundant_questioning,
                "reason": str(parsed.get("reason", "")),
                "threshold": self.cfg.judge_pass_threshold,
            })
        except Exception as exc:
            return self._unavailable(
                {
                    "is_context_consistent": False,
                    "has_memory_gap": True,
                    "is_redundant_questioning": True,
                },
                f"judge failed: {exc}",
            )

    def long_memory(self, conversation_history: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        client, err = self._get_client()
        if client is None:
            return self._unavailable(
                {
                    "memory_recall_correct": False,
                    "is_context_consistent": False,
                    "is_off_topic": True,
                },
                err or "judge client unavailable",
            )
        try:
            prompt = self._prompt('long_memory', {"对话历史": conversation_history, "预期要求": expected_outcome, "模型回答": actual_answer_text}, 'score(number), pass(boolean), memory_recall_correct(boolean), is_context_consistent(boolean), is_off_topic(boolean), reason(string)')
            resp = self._chat(client, prompt)
            parsed = self._parse_json_object_from_text(self._response_text(resp))
            score = valid_score(parsed.get("score"))
            recall_ok = bool(parsed.get("memory_recall_correct", False))
            context_ok = bool(parsed.get("is_context_consistent", False))
            is_off_topic = bool(parsed.get("is_off_topic", False))
            llm_pass = True  # 由程序依据分数和既有诊断标志计算，不信任模型 pass。
            return self._with_identity({
                "judge_available": True,
                "pass": llm_pass and score >= self.cfg.judge_pass_threshold and recall_ok and context_ok and not is_off_topic,
                "score": score,
                "memory_recall_correct": recall_ok,
                "is_context_consistent": context_ok,
                "is_off_topic": is_off_topic,
                "reason": str(parsed.get("reason", "")),
                "threshold": self.cfg.judge_pass_threshold,
            })
        except Exception as exc:
            return self._unavailable(
                {
                    "memory_recall_correct": False,
                    "is_context_consistent": False,
                    "is_off_topic": True,
                },
                f"judge failed: {exc}",
            )

class DualLLMJudge:
    """Qwen 与 DeepSeek 并行打分，再合并为一条结果。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.judges = [LLMJudge(cfg, endpoint) for endpoint in build_judge_endpoints(cfg)]

    def _get_client(self) -> None:
        for judge in self.judges:
            judge._get_client()

    def _run(self, method: str, **kwargs: Any) -> Dict[str, Any]:
        per_judge: List[Dict[str, Any]] = []
        if not self.judges:
            return merge_judge_results([], self.cfg)
        def run_one(judge):
            try:
                return getattr(judge, method)(**kwargs)
            except Exception as exc:
                # A single failed selected judge must never be silently excluded.
                return judge._unavailable({}, f"judge failed: {exc}")

        if len(self.judges) == 1:
            per_judge.append(run_one(self.judges[0]))
        else:
            with ThreadPoolExecutor(max_workers=len(self.judges)) as pool:
                futures = [pool.submit(run_one, judge) for judge in self.judges]
                for fut in futures:
                    per_judge.append(fut.result())
        return merge_judge_results(per_judge, self.cfg)

    def basic_qa(self, question_text: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        return self._run(
            "basic_qa",
            question_text=question_text,
            expected_outcome=expected_outcome,
            actual_answer_text=actual_answer_text,
        )

    def emotion(self, question_text: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        return self._run(
            "emotion",
            question_text=question_text,
            expected_outcome=expected_outcome,
            actual_answer_text=actual_answer_text,
        )

    def knowledge_base(
        self,
        question_text: str,
        expected_outcome: str,
        actual_answer_text_1: str,
        actual_answer_text_2: str,
    ) -> Dict[str, Any]:
        return self._run(
            "knowledge_base",
            question_text=question_text,
            expected_outcome=expected_outcome,
            actual_answer_text_1=actual_answer_text_1,
            actual_answer_text_2=actual_answer_text_2,
        )

    def short_memory(self, conversation_history: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        return self._run(
            "short_memory",
            conversation_history=conversation_history,
            expected_outcome=expected_outcome,
            actual_answer_text=actual_answer_text,
        )

    def long_memory(self, conversation_history: str, expected_outcome: str, actual_answer_text: str) -> Dict[str, Any]:
        return self._run(
            "long_memory",
            conversation_history=conversation_history,
            expected_outcome=expected_outcome,
            actual_answer_text=actual_answer_text,
        )


# 基于所有样例结果进行全量判断
def post_judge_results(results: List[Dict[str, Any]], cfg: Config) -> Dict[str, Any]:
    """第二阶段：全量采集完成后，统一执行 LLM 判分并回写 pass 字段。"""
    rules = None
    judge_instances = []
    judged_turns = 0
    jobs: List[Callable[[], None]] = []

    for group in results:
        category = str(group.get("category") or "基础问答")
        handler = str(group.get("handler", ""))
        group_rules = rules or getattr(cfg, "rules_snapshot", {})
        if not group_rules and handler not in {"basic_qa", "knowledge_base", "emotion", "short_term_memory", "long_term_memory", "mcp_call"}:
            group_rules = load_rules(cfg)
        if group_rules:
            handler = group_rules.get("category_handlers", {}).get(category, handler)
        turns = group.get("turn_results") or []
        language_turns = [t for t in turns if isinstance(t, dict)
                          and not t.get("execution_error")
                          and not (handler == "mcp_call" and turn_requires_mcp(t.get("expect_tool_call")))]
        supported = handler in {"basic_qa", "knowledge_base", "emotion", "short_term_memory", "long_term_memory", "mcp_call"}
        if not language_turns or not supported:
            if handler == "mcp_call":
                judged_turns += len([t for t in turns if isinstance(t, dict)])
            continue
        if rules is None:
            rules = cfg.rules_snapshot or load_rules(cfg)
        group_cfg = replace(cfg, judge_category=category, rules_snapshot=rules,
                            judge_pass_threshold=rules["thresholds"].get(category, cfg.judge_pass_threshold))
        group_judge = DualLLMJudge(group_cfg)
        group_judge._get_client()
        judge_instances.append(group_judge)
        per_threshold = {group_cfg.judge_pass_threshold: group_judge}

        def judge_for_turn(turn):
            threshold = case_threshold(rules, cfg.toy_id, category, turn.get("case_id", ""), turn.get("sheet_row"))
            if threshold not in per_threshold:
                per_threshold[threshold] = DualLLMJudge(replace(group_cfg, judge_pass_threshold=threshold))
                per_threshold[threshold]._get_client()
            return per_threshold[threshold]
        turn_results = [t for t in turns if not isinstance(t, dict) or not t.get("execution_error")]
        for turn in language_turns:
            turn["evaluation_mode"] = "llm"
        if handler == "basic_qa":
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                utterances = turn.get("utterances") or []
                question_text = " / ".join(str(u.get("user_message", "")) for u in utterances if isinstance(u, dict))
                combined = str(turn.get("combined_server_text", "") or "")
                expected = str(turn.get("expected_outcome", "") or "")

                def run_basic(judge=judge_for_turn(turn), turn=turn, question_text=question_text, expected=expected, combined=combined) -> None:
                    judge_result = judge.basic_qa(
                        question_text=question_text,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["judge_result"] = judge_result
                    turn["pass"] = bool(judge_result.get("pass", False))

                jobs.append(run_basic)
                judged_turns += 1
        elif handler == "knowledge_base":
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                qt = str(turn.get("question_text", "") or "").strip()
                if not qt:
                    utterances = turn.get("utterances") or []
                    qt = " / ".join(str(u.get("user_message", "")) for u in utterances if isinstance(u, dict))
                combined = str(turn.get("combined_server_text", "") or "").strip()
                expected = str(turn.get("expected_outcome", "") or "")

                def run_kb(judge=judge_for_turn(turn), turn=turn, qt=qt, expected=expected, combined=combined) -> None:
                    judge_result = judge.basic_qa(
                        question_text=qt,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["judge_result"] = judge_result
                    turn["pass"] = bool(judge_result.get("pass", False))

                jobs.append(run_kb)
                judged_turns += 1
        elif handler == "emotion":
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                utterances = turn.get("utterances") or []
                question_text = " / ".join(str(u.get("user_message", "")) for u in utterances if isinstance(u, dict))
                combined = str(turn.get("combined_server_text", "") or "")
                expected = str(turn.get("expected_outcome", "") or "")

                def run_emotion(judge=judge_for_turn(turn), turn=turn, question_text=question_text, expected=expected, combined=combined) -> None:
                    judge_result = judge.emotion(
                        question_text=question_text,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["judge_result"] = judge_result
                    turn["pass"] = bool(judge_result.get("pass", False))

                jobs.append(run_emotion)
                judged_turns += 1
        elif handler == "short_term_memory":
            history_pairs: List[str] = []
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                utterances = turn.get("utterances") or []
                for u in utterances:
                    if not isinstance(u, dict):
                        continue
                    um = str(u.get("user_message", "") or "")
                    st = str(u.get("server_text", "") or "")
                    if not st.strip():
                        mcp_check = u.get("mcp_check") if isinstance(u.get("mcp_check"), dict) else {}
                        mcp_calls_for_history = mcp_check.get("mcp_calls", []) if isinstance(mcp_check, dict) else []
                        if mcp_calls_for_history:
                            st = MCPInspector.format_calls_brief(mcp_calls_for_history)
                        else:
                            st = "[无可见文本回复]"
                    history_pairs.append(f"用户:{um}\n助手:{st}")
                combined = str(turn.get("combined_server_text", "") or "")
                recent_history = "\n".join(history_pairs[-8:])
                expected = str(turn.get("expected_outcome", "") or "")
                mcp_checks = turn.get("mcp_checks", []) or []
                mcp_pass = all(
                    (not bool(x.get("required", False))) or bool(x.get("pass", False))
                    for x in mcp_checks
                    if isinstance(x, dict)
                )
                if not mcp_checks:
                    mcp_pass = True

                def run_short(
                    judge=judge_for_turn(turn),
                    turn=turn,
                    recent_history=recent_history,
                    expected=expected,
                    combined=combined,
                    mcp_pass=mcp_pass,
                ) -> None:
                    memory_judge = judge.short_memory(
                        conversation_history=recent_history,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["memory_judge"] = memory_judge
                    turn["mcp_pass"] = mcp_pass
                    turn["pass"] = bool(memory_judge.get("pass", False)) and mcp_pass

                jobs.append(run_short)
                judged_turns += 1
        elif handler == "long_term_memory":
            history_pairs = []
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                utterances = turn.get("utterances") or []
                for u in utterances:
                    if not isinstance(u, dict):
                        continue
                    history_pairs.append(
                        f"用户:{str(u.get('user_message', '') or '')}\n助手:{str(u.get('server_text', '') or '')}"
                    )
                combined = str(turn.get("combined_server_text", "") or "")
                conversation_history = "\n".join(history_pairs)
                expected = str(turn.get("expected_outcome", "") or "")

                def run_long(
                    judge=judge_for_turn(turn),
                    turn=turn,
                    conversation_history=conversation_history,
                    expected=expected,
                    combined=combined,
                ) -> None:
                    judge_result = judge.long_memory(
                        conversation_history=conversation_history,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["judge_result"] = judge_result
                    turn["pass"] = bool(judge_result.get("pass", False))

                jobs.append(run_long)
                judged_turns += 1
        # mcp调用样例中非工具调用样例判别
        elif handler == "mcp_call":
            for turn in turn_results:
                if not isinstance(turn, dict):
                    continue
                judged_turns += 1
                if turn_requires_mcp(turn.get("expect_tool_call")):
                    continue
                utterances = turn.get("utterances") or []
                question_text = " / ".join(str(u.get("user_message", "")) for u in utterances if isinstance(u, dict))
                combined = combined_text_for_mcp_no_tool_llm_judge(turn)
                expected = str(turn.get("expected_outcome", "") or "")

                def run_mcp_text(judge=judge_for_turn(turn), turn=turn, question_text=question_text, expected=expected, combined=combined) -> None:
                    judge_result = judge.basic_qa(
                        question_text=question_text,
                        expected_outcome=expected,
                        actual_answer_text=combined,
                    )
                    turn["judge_result"] = judge_result
                    turn["pass"] = bool(judge_result.get("pass", False))

                jobs.append(run_mcp_text)

    workers = max(1, int(cfg.judge_concurrency or 1))
    endpoints = judge_instances[0].judges if judge_instances else []
    judge_names = [j.endpoint.name for j in endpoints]
    if jobs:
        print(
            f"第二阶段评分：{len(jobs)} 条，并发 {min(workers, len(jobs))}，"
            f"裁判={'+'.join(judge_names)}",
            flush=True,
        )
        if workers == 1 or len(jobs) == 1:
            for job in jobs:
                job()
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
                futures = [pool.submit(job) for job in jobs]
                for fut in as_completed(futures):
                    fut.result()

    for group in results:
        turn_results = group.get("turn_results") or []
        if turn_results:
            group["group_pass"] = all(bool(t.get("pass", False)) for t in turn_results if isinstance(t, dict))

    return {
        "judged_turn_count": judged_turns,
        "judge_job_count": len(jobs),
        "judge_concurrency": workers,
        "judge_names": judge_names,
        "judge_models": [j.endpoint.model for j in endpoints],
    }
