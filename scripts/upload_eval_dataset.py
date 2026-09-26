# -*- coding: utf-8 -*-
"""评估结果上报 LangSmith dataset（OPT-02 收尾，2026-09-26）。

把 evaluation_runs / evaluation_scores 的评分结果回流到 LangSmith dataset，
供面板可视化对比多批次回归（bad case 变化 / 分数趋势）。

- 数据源：Postgres evaluation_runs + evaluation_scores（run_evaluation.py 产物）
- 目标：LangSmith dataset（默认 sweetagent-eval），每条用例一个 example：
    inputs   = {"question": 用例问题}
    outputs  = {"scores": {routing_accuracy / sql_accuracy / answer_accuracy}}
    metadata = {case_id, category, run_pk, run_id, sql_pattern, 各指标分}
- example_id 用 uuid5(case_id) 稳定生成：同一用例重复上报幂等（已存在则更新）。

用法（项目根目录）：
    .venv\\Scripts\\python scripts\\upload_eval_dataset.py               # 上报最新 completed 批次
    .venv\\Scripts\\python scripts\\upload_eval_dataset.py --run-pk 3   # 指定批次
    .venv\\Scripts\\python scripts\\upload_eval_dataset.py --dataset sweetagent-eval-0926  # 指定数据集
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import psycopg

sys.path.insert(0, ".")

from app.config.settings import settings  # noqa: E402
from app.observability.logging import setup_logging  # noqa: E402
from app.observability.tracing import init_langsmith  # noqa: E402

setup_logging()

DEFAULT_DATASET = "sweetagent-eval"
_METRICS = ("routing_accuracy", "sql_accuracy", "answer_accuracy")


def _jsonable(v: Any) -> Any:
    """psycopg 返回的 Decimal/None 等转成 JSON 可序列化值。"""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def load_run(conn, run_pk: int) -> tuple[dict, dict]:
    """读取评估批次：run 信息 + 按 case 组织的评分。"""
    row = conn.execute(
        "SELECT id, run_id, status, notes, finished_at FROM evaluation_runs WHERE id = %s",
        (run_pk,),
    ).fetchone()
    if not row:
        raise SystemExit(f"evaluation_runs 中不存在 id={run_pk}")
    run = {
        "pk": int(row[0]), "run_id": row[1], "status": row[2],
        "notes": row[3], "finished_at": str(row[4] or ""),
    }
    rows = conn.execute(
        "SELECT case_id, metric, score, detail FROM evaluation_scores "
        "WHERE run_id = %s ORDER BY case_id",
        (run_pk,),
    ).fetchall()
    cases: dict[int, dict] = {}
    for case_id, metric, score, detail in rows:
        cases.setdefault(int(case_id), {})[metric] = {
            "score": _jsonable(score), "detail": detail,
        }
    # 用例元信息（question / category / expected）
    case_rows = conn.execute(
        "SELECT id, category, question, expected_sql_pattern FROM evaluation_cases",
    ).fetchall()
    meta = {
        int(r[0]): {"category": r[1], "question": r[2], "sql_pattern": r[3]}
        for r in case_rows
    }
    return run, cases, meta


def upload(client: Any, run: dict, cases: dict, meta: dict, dataset_name: str) -> None:
    dataset = client.create_dataset(
        dataset_name,
        description=(
            f"sweetAgent 评测结果回流（源批次 run_pk={run['pk']} run_id={run['run_id']} "
            f"status={run['status']} finished_at={run['finished_at']}）"
        ),
    )
    print(f"✅ dataset 就绪: {dataset_name} (id={getattr(dataset, 'id', '?')})")

    created = updated = 0
    for case_id in sorted(cases):
        c = cases[case_id]
        m = meta.get(case_id, {})
        capture = c.get("raw_capture", {})
        question = m.get("question") or "?"
        outputs = {
            "scores": {k: c.get(k, {}).get("score") for k in _METRICS},
            "sql_pattern": m.get("sql_pattern"),
            "tokens": (capture.get("tokens") or {}),
            "cost": capture.get("cost"),
            "latency_ms": capture.get("latency_ms"),
            "stage": capture.get("stage"),
        }
        metadata = {
            "case_id": case_id,
            "category": m.get("category"),
            "run_pk": run["pk"],
            "run_id": run["run_id"],
            "sql_accuracy": c.get("sql_accuracy", {}).get("score"),
            "routing_accuracy": c.get("routing_accuracy", {}).get("score"),
            "answer_accuracy": c.get("answer_accuracy", {}).get("score"),
        }
        example_id = uuid.uuid5(uuid.NAMESPACE_URL, f"sweetagent-eval:{case_id}")
        try:
            client.create_example(
                inputs={"question": question},
                outputs=outputs,
                metadata=metadata,
                example_id=example_id,
                dataset_id=dataset.id,
            )
            created += 1
        except Exception:
            # 已存在 -> 更新（保证幂等）
            ex = client.list_examples(dataset_id=dataset.id, example_ids=[example_id])
            if ex:
                client.update_example(
                    example_id=example_id,
                    inputs={"question": question},
                    outputs=outputs,
                    metadata=metadata,
                )
                updated += 1
            else:
                raise
        print(
            f"   case {case_id} [{m.get('category')}] "
            f"routing={outputs['scores'].get('routing_accuracy')} "
            f"sql={outputs['scores'].get('sql_accuracy')} "
            f"answer={outputs['scores'].get('answer_accuracy')}"
        )

    print(f"\n✅ 上报完成：新增 {created} 条，更新 {updated} 条")
    print(f"   面板查看：https://smith.langchain.com  →  Datasets  →  {dataset_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="评估结果上报 LangSmith dataset")
    parser.add_argument("--run-pk", type=int, help="指定 evaluation_runs.id；默认取最新 completed 批次")
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    args = parser.parse_args()

    client = init_langsmith()
    if client is None:
        raise SystemExit("LangSmith 未启用（检查 .env 的 LANGSMITH_TRACING / API_KEY）")

    with psycopg.connect(settings.DATABASE_URL) as conn:
        if args.run_pk:
            run_pk = args.run_pk
        else:
            row = conn.execute(
                "SELECT id FROM evaluation_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1",
            ).fetchone()
            if not row:
                raise SystemExit("没有已完成（completed）的评估批次；先跑 run_evaluation.py")
            run_pk = int(row[0])
        run, cases, meta = load_run(conn, run_pk)

    print(f"===== 上报评估批次 run_pk={run_pk} run_id={run['run_id']} → dataset={args.dataset} =====")
    upload(client, run, cases, meta, args.dataset)


if __name__ == "__main__":
    main()
