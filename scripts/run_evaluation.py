# -*- coding: utf-8 -*-
"""Agent 评估运行器（待办 13 / development_log 三点六）。

能力：
- 从 evaluation_cases 读用例，真实跑主图 run_question，采集三维分数：
  routing_accuracy（required 部门命中率，多规划不扣分）
  sql_accuracy（必命中表/关键词命中率；rag:<部门> 判知识库检索是否发生）
  answer_accuracy（关键词 n/m 命中；JUDGE: 前缀交 LLM 语义裁判）
- 记录每条用例耗时、LLM token 用量（LangChain callback 采集）、估算费用
- 写 evaluation_runs / evaluation_scores（每用例一条 raw_capture 存完整原始输出，
  支持 --replay 离线重放评分，不重跑主图、不重复花主图费用）
- 全程 structlog 结构化日志

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\run_evaluation.py              # 默认只跑 6、10（省钱）
    .venv\\Scripts\\python scripts\\run_evaluation.py --case 8,11  # 跑指定用例
    .venv\\Scripts\\python scripts\\run_evaluation.py --all        # 跑全部 20 条（注意费用）
    .venv\\Scripts\\python scripts\\run_evaluation.py --replay 3   # 离线重放 run 主键=3 的评分
    .venv\\Scripts\\python scripts\\run_evaluation.py --replay 3 --judge   # 重放并重判 JUDGE 题

费用口径：DeepSeek 单价随版本/峰谷/缓存浮动，脚本内 PRICING 仅为保守估算，
精确费用以 DeepSeek 账单为准；token 原始用量始终落库、不受单价影响。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, ".")

from langchain_core.callbacks.base import BaseCallbackHandler  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.graph.main_graph import run_question  # noqa: E402
from app.llm import get_chat_model  # noqa: E402
from app.observability.logging import get_logger, setup_logging  # noqa: E402

setup_logging()
logger = get_logger("evaluation")

DSN = os.getenv("SEED_DATABASE_URL", settings.DATABASE_URL)
EVAL_USER_ID = "eval"
DEFAULT_CASE_IDS = [6, 10]

# 费用估算单价（元 / 百万 token）。保守取 deepseek-chat 常规档，未计缓存命中折扣与
# 谷时半价（DeepSeek 2026-08 起峰谷定价）；仅用于估算，以账单为准。
PRICING = {"input_per_mtok": 2.0, "output_per_mtok": 8.0}

# JUDGE 三类语义裁判标准（合格行为）
JUDGE_CRITERIA = {
    "abstain": (
        "知识库中未收录该主题。合格回答必须同时满足：1) 明确告知知识库/公司资料中"
        "未收录、无法找到相关内容；2) 不得编造任何具体规定、制度、流程或数字。"
        "先说未收录又给出编造内容的，判不合格。"
    ),
    "no_fabricate": (
        "系统没有该类数据源（本系统只有销售/广告/财务/库存/物流/产品知识库，"
        "没有网站流量、访客画像数据域）。合格回答必须：1) 明确说明系统无法提供该数据；"
        "2) 不得编造任何表名、指标或流量数字。"
    ),
    "empty": (
        "数据库时间范围不覆盖所问时段（种子数据仅覆盖 2026-06 至 2026-09）。"
        "合格回答必须：1) 明确说明没有该时段数据并给出实际数据范围；"
        "2) 不得编造该时段数字，也不得用其他时段的数据冒充。"
    ),
}


# ----------------------------------------------------------------------
# Token 用量采集
# ----------------------------------------------------------------------
class UsageCollector(BaseCallbackHandler):
    """采集图内所有 LLM 调用的 token 用量（经 invoke config callbacks 透传）。"""

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: D401
        self.calls += 1
        # 现代 langchain-openai：message.usage_metadata
        try:
            msg = response.generations[0][0].message
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                self.input_tokens += usage.get("input_tokens", 0) or 0
                self.output_tokens += usage.get("output_tokens", 0) or 0
                return
        except Exception:
            pass
        # 兜底：LLMResult.llm_output.token_usage（OpenAI 风格）
        llm_output = getattr(response, "llm_output", None) or {}
        tu = llm_output.get("token_usage") or llm_output.get("usage") or {}
        self.input_tokens += tu.get("prompt_tokens", 0) or tu.get("input_tokens", 0) or 0
        self.output_tokens += (
            tu.get("completion_tokens", 0) or tu.get("output_tokens", 0) or 0
        )


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * PRICING["input_per_mtok"]
        + output_tokens / 1_000_000 * PRICING["output_per_mtok"],
        4,
    )


# ----------------------------------------------------------------------
# 结果提取
# ----------------------------------------------------------------------
def extract_capture(result: dict[str, Any], latency_ms: int, usage: UsageCollector) -> dict[str, Any]:
    """从主图返回 state 提取评分所需的原始事实。"""
    dept_results = result.get("department_results") or {}
    actual_agents = sorted(dept_results.keys())

    sql_texts: list[str] = []
    for r in dept_results.values():
        for obs in (r.get("observations") or []):
            sql = obs.get("sql")
            if sql:
                sql_texts.append(str(sql))
        for s in (r.get("sql_history") or []):
            if s:
                sql_texts.append(str(s))

    decision = result.get("decision_result") or {}
    answer_text = (result.get("final_answer") or "") + "\n" + json.dumps(
        decision, ensure_ascii=False
    )

    return {
        "stage": result.get("current_stage"),
        "actual_agents": actual_agents,
        "sql_texts": sql_texts,
        "answer_text": answer_text,
        "latency_ms": latency_ms,
        "tokens": {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "llm_calls": usage.calls,
        },
        "cost": estimate_cost(usage.input_tokens, usage.output_tokens),
        "error": (result.get("error_state") or {}).get("error")
        if result.get("current_stage") == "error"
        else None,
    }


# ----------------------------------------------------------------------
# 评分（纯函数，在线与 --replay 共用）
# ----------------------------------------------------------------------
def _norm_text(s: str) -> str:
    """答案文本归一化：小写 + 去空白与标点，供子串匹配。"""
    return re.sub(r"[\s,，。.!！?？:：;；、/\\()（）\-_*#`\"'’]+", "", str(s).lower())


def _norm_sql(s: str) -> str:
    """SQL 归一化：大写 + 压缩空白（保留括号/逗号等语法符号）。"""
    return re.sub(r"\s+", " ", str(s).upper()).strip()


def score_routing(actual_agents: list[str], expected_agents: Optional[dict], stage: str) -> Optional[dict]:
    required = list((expected_agents or {}).get("required") or [])
    if not required:
        return None  # 用例不要求路由（如部分防幻觉题）
    # decision 节点在主图正常完成时必然执行（Manager 强制追加），不计入 department_results
    actual_set = set(actual_agents) | ({"decision"} if stage == "done" else set())
    missing = [a for a in required if a not in actual_set]
    score = round((len(required) - len(missing)) / len(required), 4)
    return {
        "score": score,
        "detail": {"required": required, "actual": sorted(actual_set), "missing": missing},
    }


def score_sql(sql_texts: list[str], pattern: str) -> Optional[dict]:
    if not pattern or pattern.strip() == "-":
        return None
    raw_sql = "\n".join(sql_texts)
    sql_upper = _norm_sql(raw_sql)
    sql_lower = raw_sql.lower()
    items = [p.strip() for p in re.split(r"[、|]", pattern) if p.strip()]
    hits, misses = [], []
    for item in items:
        if item.lower().startswith("rag:"):
            dept = item.split(":", 1)[1].strip().lower()
            ok = f"rag:search(department={dept}" in sql_lower.replace(" ", "")
        else:
            ok = _norm_sql(item) in sql_upper
        (hits if ok else misses).append(item)
    score = round(len(hits) / len(items), 4)
    return {
        "score": score,
        "detail": {"pattern": pattern, "hits": hits, "misses": misses, "sql_count": len(sql_texts)},
    }


def _parse_keywords(key: str) -> tuple[list[str], int]:
    """解析 'kw1、kw2（m中n）' -> (关键词列表, 需命中数)。"""
    m = re.search(r"[（(]\s*(\d+)\s*中\s*(\d+)\s*[）)]", key)
    need = int(m.group(2)) if m else None
    body = key[: m.start()] if m else key
    kws = [k.strip() for k in re.split(r"[、|]", body) if k.strip()]
    return kws, (need if need is not None else len(kws))


def judge_with_llm(judge_type: str, question: str, answer_text: str) -> dict:
    """调 LLM 裁判，返回 {pass: 0|1, reason}。"""
    criteria = JUDGE_CRITERIA.get(judge_type)
    if not criteria:
        return {"pass": 0, "reason": f"unknown judge type: {judge_type}"}
    prompt = (
        "你是严格的 Agent 回答评分员，只能依据给定标准判断，不要主观发挥。\n"
        f"判定标准：{criteria}\n\n"
        f"用户问题：{question}\n"
        f"Agent 回答（可能含结构化字段）：\n{answer_text[:4000]}\n\n"
        "只输出 JSON：合格 {\"pass\": 1}；不合格 {\"pass\": 0, \"reason\": \"一句话说明\"}。"
    )
    judge = get_chat_model(tier="small")  # temperature 默认 0
    t0 = time.time()
    resp = judge.invoke(prompt)
    raw = resp.content if hasattr(resp, "content") else str(resp)
    latency = int((time.time() - t0) * 1000)
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        verdict = json.loads(raw[start:end])
    except Exception:
        logger.warning("evaluation.judge.parse_fail", raw=raw[:200])
        verdict = {"pass": 0, "reason": f"裁判输出无法解析: {raw[:120]}"}
    verdict["latency_ms"] = latency
    return verdict


def score_answer(
    answer_text: str, answer_key: str, question: str, allow_judge: bool = True
) -> dict:
    key = (answer_key or "").strip()
    if key.upper().startswith("JUDGE:"):
        judge_type = key.split(":", 1)[1].strip().split()[0].lower()
        if not allow_judge:
            return {"score": None, "detail": {"mode": "judge", "judge_type": judge_type, "skipped": True}}
        logger.info("evaluation.judge.call", judge_type=judge_type)
        verdict = judge_with_llm(judge_type, question, answer_text)
        passed = int(verdict.get("pass", 0)) == 1
        logger.info(
            "evaluation.judge.result", judge_type=judge_type, **{"pass": int(passed)},
            reason=str(verdict.get("reason", ""))[:200], latency_ms=verdict.get("latency_ms"),
        )
        return {
            "score": 1.0 if passed else 0.0,
            "detail": {
                "mode": "judge", "judge_type": judge_type,
                "pass": int(passed), "reason": verdict.get("reason", ""),
            },
        }
    kws, need = _parse_keywords(key)
    norm_answer = _norm_text(answer_text)
    hits, misses = [], []
    for kw in kws:
        (hits if _norm_text(kw) in norm_answer else misses).append(kw)
    score = 1.0 if len(hits) >= need else round(len(hits) / len(kws), 4)
    return {
        "score": score,
        "detail": {"mode": "keyword", "keywords": kws, "need": need,
                   "hits": hits, "misses": misses},
    }


# ----------------------------------------------------------------------
# 数据库
# ----------------------------------------------------------------------
def load_cases(conn, case_ids: Optional[list[int]] = None) -> list[dict]:
    cur = conn.execute(
        "SELECT id, category, question, expected_agents, expected_sql_pattern, "
        "expected_answer_key FROM evaluation_cases "
        + ("WHERE id = ANY(%s) " if case_ids else "")
        + "ORDER BY id",
        (case_ids,) if case_ids else (),
    )
    return [
        dict(zip(["id", "category", "question", "expected_agents", "sql_pattern", "answer_key"], row))
        for row in cur.fetchall()
    ]


def ensure_schema(conn) -> None:
    """存量库轻量迁移（全新库已在 db/02-schema.sql 定义），幂等。"""
    conn.execute("ALTER TABLE evaluation_cases ADD COLUMN IF NOT EXISTS category VARCHAR(30)")
    conn.execute("ALTER TABLE evaluation_runs ADD COLUMN IF NOT EXISTS notes JSONB")
    conn.commit()


def reset_eval_memory(conn) -> int:
    """清理评估专用用户的历史记忆，保证用例间无状态、结果可重复。

    评估问题会触发记忆提取钩子（rule_hit），若不隔离，后一条用例会注入前一条
    写入的画像/记忆造成污染。仅清用户级三表；business_preferences 为全局表不动。
    """
    row = conn.execute(
        "SELECT id FROM users WHERE username = %s", (EVAL_USER_ID,)
    ).fetchone()
    if not row:
        return 0
    uid = row[0]
    deleted = 0
    for table in ("user_memories", "user_profiles", "user_preferences"):
        deleted += conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (uid,)).rowcount
    return deleted


def create_run(conn, run_id: str, mode: str, case_ids: list[int], notes: dict) -> int:
    row = conn.execute(
        "INSERT INTO evaluation_runs (run_id, status, notes) VALUES (%s, 'running', %s) RETURNING id",
        (run_id, Jsonb({"mode": mode, "case_ids": case_ids, **notes})),
    ).fetchone()
    return int(row[0])


def finish_run(conn, run_pk: int, status: str, notes: dict) -> None:
    conn.execute(
        "UPDATE evaluation_runs SET status = %s, finished_at = now(), notes = %s WHERE id = %s",
        (status, Jsonb(notes), run_pk),
    )


def write_scores(conn, run_pk: int, case_id: int, capture: dict, scored: dict) -> None:
    # 原始捕获（离线重放数据源），score 为 NULL
    conn.execute(
        "INSERT INTO evaluation_scores (run_id, case_id, metric, score, detail) "
        "VALUES (%s, %s, 'raw_capture', NULL, %s)",
        (run_pk, case_id, Jsonb(capture)),
    )
    for metric, result in scored.items():
        if result is None:
            continue
        conn.execute(
            "INSERT INTO evaluation_scores (run_id, case_id, metric, score, detail) "
            "VALUES (%s, %s, %s, %s, %s)",
            (run_pk, case_id, metric, result["score"], Jsonb(result["detail"])),
        )


def score_capture(case: dict, capture: dict, allow_judge: bool = True) -> dict:
    return {
        "routing_accuracy": score_routing(
            capture["actual_agents"], case["expected_agents"], capture.get("stage")
        ),
        "sql_accuracy": score_sql(capture["sql_texts"], case["sql_pattern"] or "-"),
        "answer_accuracy": score_answer(
            capture["answer_text"], case["answer_key"] or "-", case["question"], allow_judge
        ),
    }


# ----------------------------------------------------------------------
# 在线跑用例
# ----------------------------------------------------------------------
def run_online(conn, run_pk: int, cases: list[dict], fresh: bool = False) -> list[dict]:
    records = []
    total_in = total_out = total_cost = 0
    for idx, case in enumerate(cases, 1):
        cid = case["id"]
        if fresh:
            deleted = reset_eval_memory(conn)
            conn.commit()
            logger.info("evaluation.eval_memory.reset", case_id=cid, deleted=deleted)
        logger.info("evaluation.case.start", run_pk=run_pk, case_id=cid,
                    category=case["category"], index=f"{idx}/{len(cases)}")
        print(f"\n[{idx}/{len(cases)}] 用例 {cid}（{case['category']}）：{case['question']}")

        usage = UsageCollector()
        thread_id = f"eval-{cid}-{int(time.time() * 1000)}"
        t0 = time.time()
        try:
            result = run_question(
                case["question"], thread_id=thread_id,
                user_id=EVAL_USER_ID, callbacks=[usage],
            )
            latency_ms = int((time.time() - t0) * 1000)
            capture = extract_capture(result, latency_ms, usage)
        except Exception as exc:
            latency_ms = int((time.time() - t0) * 1000)
            logger.error("evaluation.case.error", case_id=cid, error=str(exc))
            capture = {
                "stage": "error", "actual_agents": [], "sql_texts": [], "answer_text": "",
                "latency_ms": latency_ms,
                "tokens": {"input": usage.input_tokens, "output": usage.output_tokens,
                           "llm_calls": usage.calls},
                "cost": estimate_cost(usage.input_tokens, usage.output_tokens),
                "error": str(exc),
            }

        scored = score_capture(case, capture, allow_judge=True)
        write_scores(conn, run_pk, cid, capture, scored)

        total_in += capture["tokens"]["input"]
        total_out += capture["tokens"]["output"]
        total_cost += capture["cost"]
        logger.info(
            "evaluation.case.done", case_id=cid, latency_ms=capture["latency_ms"],
            agents=capture["actual_agents"], sql_count=len(capture["sql_texts"]),
            tokens_in=capture["tokens"]["input"], tokens_out=capture["tokens"]["output"],
            llm_calls=capture["tokens"]["llm_calls"], cost=capture["cost"],
            routing=(scored["routing_accuracy"] or {}).get("score"),
            sql=(scored["sql_accuracy"] or {}).get("score"),
            answer=(scored["answer_accuracy"] or {}).get("score"),
        )
        _print_case_line(case, capture, scored)
        records.append({"case": case, "capture": capture, "scored": scored})

    logger.info("evaluation.cost.total", tokens_in=total_in, tokens_out=total_out,
                estimated_cost=round(total_cost, 4), pricing=PRICING)
    if fresh:
        deleted = reset_eval_memory(conn)
        conn.commit()
        logger.info("evaluation.eval_memory.reset_final", deleted=deleted)
    return records


def _print_case_line(case, capture, scored) -> None:
    def fmt(r):
        if r is None:
            return "  -  "
        return f"{r['score']:.2f}"
    print(
        f"    路由 {fmt(scored['routing_accuracy'])} | SQL {fmt(scored['sql_accuracy'])} | "
        f"答案 {fmt(scored['answer_accuracy'])} | {capture['latency_ms']/1000:.1f}s | "
        f"token in={capture['tokens']['input']} out={capture['tokens']['output']} "
        f"调用={capture['tokens']['llm_calls']} | 估算 ¥{capture['cost']}"
    )
    for metric, label in [("routing_accuracy", "路由"), ("sql_accuracy", "SQL"), ("answer_accuracy", "答案")]:
        r = scored.get(metric)
        if r is not None and r["score"] < 1.0:
            d = r["detail"]
            if "missing" in d and d["missing"]:
                print(f"      [扣分-{label}] 缺少部门: {d['missing']}")
            if "misses" in d and d["misses"]:
                print(f"      [扣分-{label}] 未命中: {d['misses']}")
            if d.get("mode") == "judge" and not d.get("pass"):
                print(f"      [扣分-{label}] 裁判未通过: {d.get('reason', '')[:120]}")


# ----------------------------------------------------------------------
# 离线重放
# ----------------------------------------------------------------------
def run_replay(conn, source_run_pk: int, new_run_pk: int, allow_judge: bool) -> list[dict]:
    rows = conn.execute(
        "SELECT case_id, detail FROM evaluation_scores WHERE run_id = %s AND metric = 'raw_capture' "
        "ORDER BY case_id",
        (source_run_pk,),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"run {source_run_pk} 没有 raw_capture 记录，无法重放")
    cases_by_id = {c["id"]: c for c in load_cases(conn)}
    records = []
    for case_id, capture in rows:
        case = cases_by_id.get(case_id)
        if not case:
            logger.warning("evaluation.replay.case_missing", case_id=case_id)
            continue
        # JUDGE 默认复用原判定（不花钱）；--judge 才重判
        if not allow_judge and str(case["answer_key"] or "").upper().startswith("JUDGE:"):
            old = conn.execute(
                "SELECT score, detail FROM evaluation_scores WHERE run_id = %s AND case_id = %s "
                "AND metric = 'answer_accuracy'",
                (source_run_pk, case_id),
            ).fetchone()
            scored = score_capture(case, capture, allow_judge=False)
            if old:
                scored["answer_accuracy"] = {"score": float(old[0]), "detail": old[1]}
        else:
            scored = score_capture(case, capture, allow_judge=allow_judge)
        # 复制 raw_capture 并写入新评分，使重放 run 自包含、可再次重放
        capture = {**capture, "replayed_from": source_run_pk}
        write_scores(conn, new_run_pk, case_id, capture, scored)
        records.append({"case": case, "capture": capture, "scored": scored})
        _print_case_line(case, capture, scored)
    logger.info("evaluation.replay.done", source_run_pk=source_run_pk, new_run_pk=new_run_pk,
                cases=len(records))
    return records


def _summary(records: list[dict]) -> dict:
    def avg(metric):
        vals = [r["scored"][metric]["score"] for r in records
                if r["scored"].get(metric) and r["scored"][metric]["score"] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    total_in = sum(r["capture"]["tokens"]["input"] for r in records)
    total_out = sum(r["capture"]["tokens"]["output"] for r in records)
    total_cost = round(sum(r["capture"]["cost"] for r in records), 4)
    total_latency = sum(r["capture"]["latency_ms"] for r in records)
    return {
        "cases": len(records),
        "routing_accuracy": avg("routing_accuracy"),
        "sql_accuracy": avg("sql_accuracy"),
        "answer_accuracy": avg("answer_accuracy"),
        "total_latency_ms": total_latency,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "estimated_cost": total_cost,
        "pricing_note": "单价为估算（见脚本 PRICING），以 DeepSeek 账单为准",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 评估运行器")
    parser.add_argument("--case", help="指定用例 id，逗号分隔，如 6,10")
    parser.add_argument("--all", action="store_true", help="跑全部用例（注意 LLM 费用）")
    parser.add_argument("--replay", type=int, metavar="RUN_PK", help="离线重放指定 run 的评分")
    parser.add_argument("--judge", action="store_true", help="重放时重新调用 JUDGE（否则复用原判定）")
    parser.add_argument("--fresh", action="store_true",
                        help="每条用例前清理 eval 用户记忆（用例间无状态隔离，评估建议开启）")
    args = parser.parse_args()

    with psycopg.connect(DSN) as conn:
        ensure_schema(conn)
        if args.replay:
            run_id = f"replay-{args.replay}-{datetime.now():%Y%m%d%H%M%S}"
            run_pk = create_run(conn, run_id, "replay", [args.replay],
                                {"replayed_from": args.replay, "re_judge": args.judge})
            conn.commit()
            print(f"===== 离线重放 run_pk={args.replay}（新 run_pk={run_pk}，不调用主图）=====")
            records = run_replay(conn, args.replay, run_pk, args.judge)
            summary = _summary(records)
            # 重放不新增主图 token/费用（--judge 重判费用极小，未单独采集）
            finish_run(conn, run_pk, "completed", {"replayed_from": args.replay, **summary})
            conn.commit()
            _print_summary(summary)
            print(f"\n结果已写入 evaluation_runs.id={run_pk}")
            return

        if args.all:
            case_ids = None
        elif args.case:
            case_ids = [int(x) for x in args.case.split(",") if x.strip()]
        else:
            case_ids = DEFAULT_CASE_IDS
            print(f"未指定参数，默认只跑用例 {DEFAULT_CASE_IDS}（省钱）；--all 跑全部。")

        cases = load_cases(conn, case_ids)
        if not cases:
            print("没有匹配的用例。")
            return
        run_id = f"eval-{datetime.now():%Y%m%d%H%M%S}"
        run_pk = create_run(conn, run_id, "online", [c["id"] for c in cases], {})
        conn.commit()
        logger.info("evaluation.run.start", run_pk=run_pk, run_id=run_id,
                    cases=[c["id"] for c in cases])
        print(f"===== 评估批次 run_pk={run_pk} run_id={run_id}，用例 {[c['id'] for c in cases]} =====")

        with conn:
            records = run_online(conn, run_pk, cases, fresh=args.fresh)
            summary = _summary(records)
            finish_run(conn, run_pk, "completed", summary)

        _print_summary(summary)
        print(f"\n明细已写入 evaluation_runs.id={run_pk} / evaluation_scores")
        logger.info("evaluation.run.done", run_pk=run_pk, **{
            k: v for k, v in summary.items() if k != "pricing_note"})

    print("\n提示：评分逻辑调整后可用 --replay {0} 离线重放，不重复花主图费用。".format(run_pk))


def _print_summary(summary: dict) -> None:
    print("\n" + "=" * 64)
    print("评估汇总")
    print("=" * 64)
    print(f"  用例数: {summary['cases']}")
    for m in ("routing_accuracy", "sql_accuracy", "answer_accuracy"):
        v = summary.get(m)
        print(f"  {m:<18}: {v if v is not None else 'N/A'}")
    if "total_latency_ms" in summary:
        print(f"  总耗时: {summary['total_latency_ms']/1000:.1f}s")
    print(f"  token: 输入 {summary['tokens_in']} / 输出 {summary['tokens_out']}")
    print(f"  估算费用: ¥{summary['estimated_cost']}（{summary['pricing_note']}）")


if __name__ == "__main__":
    main()
