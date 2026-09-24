# -*- coding: utf-8 -*-
"""评测报告生成器（OPT-03 落地，2026-09-24）。

从 evaluation_runs / evaluation_scores / evaluation_cases 读取历史批次，
汇总生成可交付的 Markdown 报告到 docs/eval/：
- 批次对比（最近 N 个完成批次的三维均分 / 用例数 / 耗时 / 估算费用）
- 最新批次逐用例明细 + bad case 归因（缺部门 / 未命中模式 / 裁判未通过）
- 费用与耗时统计

与 run_evaluation.py 的关系：它负责"跑用例 + 打分 + 落库"，本脚本只负责
"把落库的结果变成报告产物"——不改任何现有评估逻辑（最小改动）。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\eval_report.py               # 最近 5 个完成批次
    .venv\\Scripts\\python scripts\\eval_report.py --runs 10     # 最近 10 个
    .venv\\Scripts\\python scripts\\eval_report.py --out docs\\eval
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import psycopg

sys.path.insert(0, ".")

from app.config.settings import settings  # noqa: E402

DSN = os.getenv("SEED_DATABASE_URL", settings.DATABASE_URL)
DEFAULT_RUNS = 5
METRICS = ("routing_accuracy", "sql_accuracy", "answer_accuracy")
METRIC_LABEL = {"routing_accuracy": "路由", "sql_accuracy": "SQL", "answer_accuracy": "答案"}
# 项目根（scripts/ 的上级）；输出目录锚定项目根，不依赖运行时的 cwd
BASE_DIR = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------
# 数据读取（DB）
# ----------------------------------------------------------------------

def load_runs(conn, n: int) -> list[dict]:
    """最近 N 个完成的评估批次（含 notes 汇总）。"""
    rows = conn.execute(
        "SELECT id, run_id, started_at, finished_at, notes FROM evaluation_runs "
        "WHERE status = 'completed' ORDER BY id DESC LIMIT %s",
        (n,),
    ).fetchall()
    runs = []
    for row in rows:
        runs.append({
            "pk": row[0], "run_id": row[1] or f"run-{row[0]}",
            "started_at": row[2], "finished_at": row[3], "notes": row[4] or {},
        })
    return runs


def load_run_metrics(conn, run_pk: int) -> dict[str, Any]:
    """聚合单个批次的三维均分（从 scores 现算，权威）与用例数。"""
    rows = conn.execute(
        "SELECT metric, score FROM evaluation_scores "
        "WHERE run_id = %s AND metric = ANY(%s) AND score IS NOT NULL",
        (run_pk, list(METRICS)),
    ).fetchall()
    scores: dict[str, list[float]] = {}
    for metric, score in rows:
        scores.setdefault(metric, []).append(float(score))
    avg = {
        m: round(sum(scores.get(m, [])) / len(scores[m]), 4) if scores.get(m) else None
        for m in METRICS
    }
    count = conn.execute(
        "SELECT COUNT(DISTINCT case_id) FROM evaluation_scores WHERE run_id = %s",
        (run_pk,),
    ).fetchone()[0]
    return {"metrics": avg, "case_count": int(count)}


def load_case_scores(conn, run_pk: int) -> list[dict]:
    """单批次逐用例得分 + 用例原题。"""
    rows = conn.execute(
        "SELECT s.case_id, s.metric, s.score, s.detail, "
        "       c.question, c.category, c.expected_agents "
        "FROM evaluation_scores s "
        "LEFT JOIN evaluation_cases c ON c.id = s.case_id "
        "WHERE s.run_id = %s AND s.metric = ANY(%s) AND s.score IS NOT NULL "
        "ORDER BY s.case_id",
        (run_pk, list(METRICS)),
    ).fetchall()
    cases: dict[int, dict[str, Any]] = {}
    for case_id, metric, score, detail, question, category, expected in rows:
        c = cases.setdefault(case_id, {
            "case_id": case_id, "question": question or f"用例 {case_id}",
            "category": category or "-", "expected": (expected or {}).get("required", []),
            "scores": {}, "details": {},
        })
        c["scores"][metric] = float(score)
        c["details"][metric] = detail or {}
    return list(cases.values())


def load_raw_tokens(conn, run_pk: int) -> Optional[dict]:
    """从 raw_capture 聚合 token/费用（缺失则返回 None）。"""
    rows = conn.execute(
        "SELECT detail FROM evaluation_scores WHERE run_id = %s AND metric = 'raw_capture'",
        (run_pk,),
    ).fetchall()
    if not rows:
        return None
    total = {"input": 0, "output": 0, "calls": 0, "cost": 0.0}
    for (detail,) in rows:
        d = detail or {}
        tok = d.get("tokens") or {}
        total["input"] += tok.get("input") or 0
        total["output"] += tok.get("output") or 0
        total["calls"] += tok.get("llm_calls") or 0
        total["cost"] += d.get("cost") or 0
    return total


# ----------------------------------------------------------------------
# bad case 归因（纯函数）
# ----------------------------------------------------------------------

def extract_bad_case(case: dict) -> Optional[dict]:
    """从评分 detail 提取扣分原因；全分返回 None。"""
    reasons: list[str] = []
    for metric in METRICS:
        score = case["scores"].get(metric)
        detail = case["details"].get(metric) or {}
        if score is None or score >= 1.0:
            continue
        if metric == "routing_accuracy" and detail.get("missing"):
            reasons.append(f"缺部门: {', '.join(detail['missing'])}")
        elif metric == "sql_accuracy" and detail.get("misses"):
            reasons.append(f"SQL 未命中: {', '.join(detail['misses'])}")
        elif metric == "answer_accuracy":
            if detail.get("mode") == "judge":
                if detail.get("pass") == 0:
                    reasons.append(f"裁判未通过: {(detail.get('reason') or '')[:80]}")
            elif detail.get("misses"):
                reasons.append(f"答案缺关键词: {', '.join(detail['misses'])}")
    if not reasons:
        return None
    return {
        "case_id": case["case_id"],
        "question": case["question"][:60],
        "category": case["category"],
        "scores": {m: case["scores"].get(m) for m in METRICS},
        "reasons": reasons,
    }


# ----------------------------------------------------------------------
# 报告渲染（纯函数，可 mock 验证）
# ----------------------------------------------------------------------

def _fmt(v: Optional[float], ndigits: int = 4) -> str:
    return f"{v:.{ndigits}f}" if v is not None else "N/A"


def render_report(now: str, runs: list[dict], latest_cases: list[dict], bad_cases: list[dict]) -> str:
    """生成 Markdown 报告正文。"""
    lines: list[str] = []
    lines.append("# SweetAgent 评估报告")
    lines.append("")
    lines.append(f"> 生成时间：{now}")
    lines.append(f"> 数据范围：最近 {len(runs)} 个完成批次（来源 evaluation_runs / evaluation_scores）")
    lines.append("")

    # 一、批次对比
    lines.append("## 一、批次对比")
    lines.append("")
    lines.append("| 批次 | run_id | 用例数 | 路由 | SQL | 答案 | 估算费用(¥) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in runs:
        m = r["metrics"]
        tok = r["tokens"]
        cost = f"{tok['cost']:.4f}" if tok else "N/A"
        lines.append(
            f"| {r['pk']} | {r['run_id']} | {m['case_count']} | "
            f"{_fmt(m['metrics']['routing_accuracy'])} | {_fmt(m['metrics']['sql_accuracy'])} | "
            f"{_fmt(m['metrics']['answer_accuracy'])} | {cost} |"
        )
    lines.append("")

    # 二、最新批次明细
    if latest_cases:
        lines.append("## 二、最新批次逐用例明细")
        lines.append("")
        lines.append("| 用例 | 分类 | 路由 | SQL | 答案 | 问题 |")
        lines.append("|---|---|---|---|---|---|")
        for c in latest_cases:
            lines.append(
                f"| {c['case_id']} | {c['category']} | {_fmt(c['scores'].get('routing_accuracy'))} | "
                f"{_fmt(c['scores'].get('sql_accuracy'))} | {_fmt(c['scores'].get('answer_accuracy'))} | "
                f"{c['question'][:40]} |"
            )
        lines.append("")

    # 三、bad case 归因
    if bad_cases:
        lines.append("## 三、bad case 归因")
        lines.append("")
        lines.append("| 用例 | 分类 | 问题 | 路由 | SQL | 答案 | 扣分原因 |")
        lines.append("|---|---|---|---|---|---|---|")
        for b in bad_cases:
            lines.append(
                f"| {b['case_id']} | {b['category']} | {b['question']} | "
                f"{_fmt(b['scores'].get('routing_accuracy'))} | {_fmt(b['scores'].get('sql_accuracy'))} | "
                f"{_fmt(b['scores'].get('answer_accuracy'))} | {'；'.join(b['reasons'])} |"
            )
        lines.append("")
    else:
        lines.append("## 三、bad case 归因")
        lines.append("")
        lines.append("本次最新批次无扣分用例。")
        lines.append("")

    # 四、费用与耗时
    lines.append("## 四、费用与耗时")
    lines.append("")
    latest = runs[0] if runs else None
    if latest and latest["tokens"]:
        tok = latest["tokens"]
        lines.append(f"- 最新批次 token：输入 {tok['input']} / 输出 {tok['output']} / LLM 调用 {tok['calls']} 次")
        lines.append(f"- 估算费用：¥{tok['cost']:.4f}（单价为估算，以 DeepSeek 账单为准）")
    else:
        lines.append("- 最新批次无 token/费用记录（raw_capture 缺失）。")
    lines.append("")
    lines.append("---")
    lines.append("由 `scripts/eval_report.py` 生成，可一键重跑复现。")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="评测报告生成器（OPT-03）")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"取最近 N 个完成批次（默认 {DEFAULT_RUNS}）")
    parser.add_argument("--out", default=str(BASE_DIR / "docs" / "eval"), help=f"报告输出目录（默认 {BASE_DIR / 'docs' / 'eval'}）")
    args = parser.parse_args()

    try:
        conn = psycopg.connect(DSN, connect_timeout=5)
    except Exception as exc:
        print(f"❌ 无法连接数据库（{DSN.split('@')[-1]}）：{type(exc).__name__}: {exc}")
        print("   请先启动 PostgreSQL（sweetnight_agent 库），再运行本脚本。")
        sys.exit(1)

    with conn:
        runs = load_runs(conn, args.runs)
        if not runs:
            print("⚠️  没有已完成（status=completed）的评估批次。")
            print("   请先运行：.venv\\Scripts\\python scripts\\run_evaluation.py [--all | --case 6,10]")
            sys.exit(0)

        for r in runs:
            r["metrics"] = load_run_metrics(conn, r["pk"])
            r["tokens"] = load_raw_tokens(conn, r["pk"])
        latest_cases = load_case_scores(conn, runs[0]["pk"])
        bad_cases = [b for c in latest_cases if (b := extract_bad_case(c))]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"report_{stamp}.md"
    report = render_report(
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), runs, latest_cases, bad_cases
    )
    out_path.write_text(report, encoding="utf-8")

    # 终端摘要
    print(f"✅ 报告已生成：{out_path}")
    r = runs[0]
    rm = r["metrics"]          # {"metrics": {均分}, "case_count": N}
    m = rm["metrics"]
    print(f"   最新批次 run_pk={r['pk']}（{r['run_id']}）：用例 {rm['case_count']} 条，"
          f"路由 {_fmt(m['routing_accuracy'])} / SQL {_fmt(m['sql_accuracy'])} / "
          f"答案 {_fmt(m['answer_accuracy'])}，bad case {len(bad_cases)} 条")
    print(f"   历史批次 {len(runs)} 个，明细见报告文件。")


if __name__ == "__main__":
    main()
