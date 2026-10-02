"""调度服务（NL2Cron，考点六十四）：注册/审核/执行/审计/限流/幂等/重试/防雪崩。

工程要求落地：
- 审计：created/approved/exec_*/retry/duplicate_skipped 全写 scheduler_run_logs
- 限流：全局任务上限 + 每用户上限 + 最小触发间隔（time_parser 已校验）
- 幂等：唯一键（user,name,domain,cron）防重复创建；scheduler_locks 防同一任务并发重复触发
- 重试：执行失败 tenacity 指数退避（有限次），连续失败 ≥ 阈值自动暂停 + 审计
- 防雪崩：APScheduler 单例 + 最大并行数（信号量，超并发排队）
- 审核：pending → approved/rejected（开发人员），approved 后才注册进调度器
- 重启恢复：启动时从 scheduler_jobs 表重放注册（DB 是唯一权威源）
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb
from tenacity import retry, stop_after_attempt, wait_exponential

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import settings
from app.memory.db import resolve_user_id
from app.observability.logging import get_logger
from app.scheduler.registry import get_domain

logger = get_logger("scheduler.service")

# 调度器单例（防雪崩：全局只有一个 BackgroundScheduler）
_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()

# 最大并行执行数（信号量：超并发任务排队等待，不叠加触发）
_concurrency = threading.BoundedSemaphore(settings.SCHEDULER_MAX_CONCURRENCY)

# DB 连接超时（秒）：DB 不可用时快速降级，不让启动/执行被拖死
_DB_CONNECT_TIMEOUT = 3


# ============================================================
# 数据库访问（scheduler_jobs / run_logs / locks）
# ============================================================

def _connect_db():
    """带连接超时的写连接（DB 不可用快速失败，不拖死调度链路）。"""
    return psycopg.connect(settings.DATABASE_URL, connect_timeout=_DB_CONNECT_TIMEOUT)


def _row_to_job(row) -> dict[str, Any]:
    cols = [
        "id", "user_id", "name", "capability_domain", "params", "time_expr",
        "cron_expr", "timezone", "risk_level", "status", "review_note",
        "reviewed_by", "reviewed_at", "last_run_at", "last_run_status",
        "consecutive_fail", "run_count", "enabled", "created_at", "updated_at",
    ]
    return {c: row[i] for i, c in enumerate(cols)}


def _audit(job_id: int, event_type: str, detail: Optional[dict] = None,
           duration_ms: Optional[int] = None, result: Optional[str] = None) -> None:
    """审计：写 scheduler_run_logs（失败不抛异常，仅记日志——审计不能拖垮主链路）。"""
    try:
        with _connect_db() as conn:
            conn.execute(
                "INSERT INTO scheduler_run_logs (job_id, event_type, detail, duration_ms, result) "
                "VALUES (%s, %s, %s, %s, %s)",
                (job_id, event_type, Jsonb(detail or {}), duration_ms,
                 (result or "")[:4000] if result else None),
            )
    except Exception as exc:
        logger.warning("scheduler.audit.fail", job_id=job_id, event=event_type, error=str(exc))


def create_job(user_id: str, name: str, capability: str, params: dict[str, Any],
               time_expr: str, cron: str, risk: str, confidence: float) -> dict[str, Any]:
    """创建定时任务（幂等 + 限流 + 审计）。状态初始 pending（待开发人员审核）。"""
    uid = resolve_user_id(user_id)

    with _connect_db() as conn:
        # 限流：每用户任务数上限
        cnt = conn.execute(
            "SELECT COUNT(*) FROM scheduler_jobs WHERE user_id = %s", (uid,)
        ).fetchone()[0]
        if cnt >= settings.SCHEDULER_MAX_JOBS_PER_USER:
            return {"ok": False, "reason": "limit_per_user"}
        # 限流：全局任务数上限
        total = conn.execute("SELECT COUNT(*) FROM scheduler_jobs").fetchone()[0]
        if total >= settings.SCHEDULER_MAX_JOBS_TOTAL:
            return {"ok": False, "reason": "limit_total"}
        # 幂等：同名同能力域同 cron 不重复创建（唯一键）
        dup = conn.execute(
            "SELECT id FROM scheduler_jobs WHERE user_id=%s AND name=%s "
            "AND capability_domain=%s AND cron_expr=%s AND status != 'deleted'",
            (uid, name, capability, cron),
        ).fetchone()
        if dup:
            return {"ok": False, "reason": "duplicate", "job_id": dup[0]}

        job_id = conn.execute(
            "INSERT INTO scheduler_jobs (user_id, name, capability_domain, params, time_expr, "
            "cron_expr, risk_level, status, review_note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', NULL) "
            "RETURNING id",
            (uid, name, capability, Jsonb(params), time_expr, cron, risk),
        ).fetchone()[0]
    _audit(job_id, "created", {"capability": capability, "params": params, "confidence": confidence,
                               "user_id": user_id, "cron": cron, "time_expr": time_expr})
    logger.info("scheduler.job.created", job_id=job_id, user_id=user_id, capability=capability)
    return {"ok": True, "job_id": job_id, "status": "pending"}


def review_job(job_id: int, reviewer_user_id: str, decision: str, note: Optional[str] = None) -> dict[str, Any]:
    """开发人员审核：approve/reject。approved → 注册进调度器；rejected → 终止。"""
    rev_id = resolve_user_id(reviewer_user_id)
    decision = decision.lower()
    if decision not in ("approve", "reject"):
        return {"ok": False, "reason": "invalid_decision"}

    with _connect_db() as conn:
        row = conn.execute(
            "SELECT id, status, capability_domain, cron_expr, user_id, timezone FROM scheduler_jobs "
            "WHERE id = %s FOR UPDATE", (job_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if row[1] not in ("pending", "rejected"):
            return {"ok": False, "reason": "not_pending"}
        new_status = "approved" if decision == "approve" else "rejected"
        conn.execute(
            "UPDATE scheduler_jobs SET status=%s, review_note=%s, reviewed_by=%s, reviewed_at=now() "
            "WHERE id=%s",
            (new_status, note, rev_id, job_id),
        )
        if decision == "approve":
            # 审核通过 → 注册进调度器
            _schedule_job(job_id, row[4], row[2], row[3], row[5])
    _audit(job_id, decision, {"reviewer": reviewer_user_id, "note": note})
    logger.info("scheduler.job.reviewed", job_id=job_id, decision=decision, reviewer=reviewer_user_id)
    return {"ok": True, "status": new_status}


def list_jobs(user_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
    """列出任务（可选按用户/状态过滤；管理员可看全部）。"""
    sql = "SELECT * FROM scheduler_jobs"
    conds, args = [], []
    if user_id:
        conds.append("user_id = %s")
        args.append(resolve_user_id(user_id))
    if status:
        conds.append("status = %s")
        args.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT 200"
    with _connect_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row_to_job(r) for r in rows]


def set_job_status(job_id: int, status: str, operator_user_id: str) -> dict[str, Any]:
    """暂停/恢复/禁用（active/paused/disabled）。"""
    if status not in ("active", "paused", "disabled"):
        return {"ok": False, "reason": "invalid_status"}
    with _connect_db() as conn:
        row = conn.execute(
            "SELECT id, status, capability_domain, cron_expr, user_id, timezone FROM scheduler_jobs "
            "WHERE id=%s", (job_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        conn.execute("UPDATE scheduler_jobs SET status=%s, updated_at=now() WHERE id=%s",
                     (status, job_id))
        # 调度器同步：暂停=remove job，恢复=add job
        _remove_scheduled(job_id)
        if status == "active":
            _schedule_job(job_id, row[4], row[2], row[3], row[5])
    _audit(job_id, status, {"operator": operator_user_id})
    return {"ok": True, "status": status}


def delete_job(job_id: int, operator_user_id: str) -> dict[str, Any]:
    """软删任务（保留审计轨迹）。"""
    with _connect_db() as conn:
        row = conn.execute("SELECT id FROM scheduler_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "reason": "not_found"}
        conn.execute("UPDATE scheduler_jobs SET status='deleted', updated_at=now() WHERE id=%s",
                     (job_id,))
        conn.execute("DELETE FROM scheduler_locks WHERE job_id=%s", (job_id,))
    _remove_scheduled(job_id)
    _audit(job_id, "deleted", {"operator": operator_user_id})
    return {"ok": True}


# ============================================================
# 调度器（APScheduler 单例 + 幂等锁 + 重试 + 防雪崩）
# ============================================================

def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(timezone=settings.SCHEDULER_TIMEZONE)
            _scheduler.start()
            logger.info("scheduler.started", timezone=settings.SCHEDULER_TIMEZONE)
        return _scheduler


def _schedule_job(job_id: int, user_id: int, capability: str, cron: str, tz: str) -> None:
    """把任务注册进 APScheduler（防重复：先 remove 同名再 add）。"""
    sched = get_scheduler()
    job_key = f"job_{job_id}"
    _remove_scheduled(job_id)
    try:
        sched.add_job(
            _execute_wrapper,
            CronTrigger.from_crontab(cron, timezone=tz or settings.SCHEDULER_TIMEZONE),
            args=[job_id],
            id=job_key,
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,  # 防雪崩：错过多次只补一次
        )
        logger.info("scheduler.registered", job_id=job_id, cron=cron)
    except Exception as exc:
        logger.warning("scheduler.register.fail", job_id=job_id, error=str(exc))


def _remove_scheduled(job_id: int) -> None:
    if _scheduler is not None:
        try:
            _scheduler.remove_job(f"job_{job_id}")
        except Exception:
            pass


@retry(stop=stop_after_attempt(settings.SCHEDULER_MAX_RETRIES),
       wait=wait_exponential(multiplier=1, max=30),
       reraise=False)
def _run_with_retry(job_id: int, domain_key: str, params: dict[str, Any]) -> dict[str, Any]:
    """执行任务（指数退避重试，重试次数有限）。"""
    domain = get_domain(domain_key)
    if domain is None:
        return {"ok": False, "error": "unknown_domain"}
    return domain.handler(params)


def _execute_wrapper(job_id: int) -> None:
    """APScheduler 触发入口：限流 + 幂等锁 + 执行 + 审计 + 失败重试/自动暂停。"""
    # 限流：并发上限（信号量等待，不叠加触发）
    with _concurrency:
        # 幂等锁：同一任务同一时刻只允许一个执行实例
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        try:
            with _connect_db() as conn:
                row = conn.execute(
                    "SELECT capability_domain, params, status, enabled FROM scheduler_jobs "
                    "WHERE id=%s AND status IN ('approved','active') AND enabled=TRUE", (job_id,)
                ).fetchone()
                if row is None:
                    return  # 任务已停用/删除
                domain_key, params, status, _enabled = row[0], row[1], row[2], row[3]
                # 加锁：锁存在且未过期 → 重复触发，跳过
                lock = conn.execute(
                    "SELECT expires_at FROM scheduler_locks WHERE job_id=%s FOR UPDATE", (job_id,)
                ).fetchone()
                if lock and lock[0] > now:
                    _audit(job_id, "duplicate_skipped", {"reason": "lock_held"})
                    return
                expires = now + timedelta(seconds=settings.SCHEDULER_LOCK_TTL_SECONDS)
                conn.execute(
                    "INSERT INTO scheduler_locks (job_id, locked_at, lock_token, expires_at) "
                    "VALUES (%s, now(), %s, %s) "
                    "ON CONFLICT (job_id) DO UPDATE SET lock_token=%s, expires_at=%s",
                    (job_id, token, expires, token, expires),
                )
        except Exception as exc:
            logger.warning("scheduler.lock.fail", job_id=job_id, error=str(exc))
            return

        start = time.perf_counter()
        try:
            result = _run_with_retry(job_id, domain_key, params)
            duration_ms = int((time.perf_counter() - start) * 1000)
            ok = bool(result.get("ok"))
            with _connect_db() as conn:
                if ok:
                    conn.execute(
                        "UPDATE scheduler_jobs SET last_run_at=now(), last_run_status='success', "
                        "run_count=run_count+1, consecutive_fail=0, updated_at=now() WHERE id=%s",
                        (job_id,),
                    )
                    _audit(job_id, "exec_success", {"result": result.get("final_answer") or result.get("hit_count")},
                           duration_ms=duration_ms)
                else:
                    err = str(result.get("error", ""))[:400]
                    conn.execute(
                        "UPDATE scheduler_jobs SET last_run_at=now(), last_run_status='failed', "
                        "consecutive_fail=consecutive_fail+1, updated_at=now() WHERE id=%s", (job_id,)
                    )
                    _audit(job_id, "exec_failed", {"error": err}, duration_ms=duration_ms)
            # 自动暂停：连续失败超阈值（防雪崩/持续烧钱）
            if not ok:
                with _connect_db() as conn:
                    fails = conn.execute(
                        "SELECT consecutive_fail FROM scheduler_jobs WHERE id=%s", (job_id,)
                    ).fetchone()[0]
                if fails >= settings.SCHEDULER_FAIL_AUTO_PAUSE:
                    with _connect_db() as conn:
                        conn.execute(
                            "UPDATE scheduler_jobs SET status='paused', updated_at=now() WHERE id=%s",
                            (job_id,),
                        )
                    _remove_scheduled(job_id)
                    _audit(job_id, "paused", {"reason": "consecutive_fail",
                                              "fails": fails, "note": "连续失败自动暂停"})
                    logger.warning("scheduler.auto_paused", job_id=job_id, fails=fails)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            _audit(job_id, "exec_failed", {"error": str(exc)[:400]}, duration_ms=duration_ms)
            logger.warning("scheduler.exec.fail", job_id=job_id, error=str(exc))
        finally:
            # 释放锁
            try:
                with _connect_db() as conn:
                    conn.execute("DELETE FROM scheduler_locks WHERE job_id=%s AND lock_token=%s",
                                 (job_id, token))
            except Exception:
                pass


def restore_jobs() -> int:
    """启动时从 DB 重放注册（DB 是唯一权威源，防重启丢失）。返回恢复数量。"""
    count = 0
    with _connect_db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, capability_domain, cron_expr, timezone FROM scheduler_jobs "
            "WHERE status IN ('approved','active') AND enabled=TRUE"
        ).fetchall()
    for r in rows:
        _schedule_job(r[0], r[1], r[2], r[3], r[4])
        count += 1
    logger.info("scheduler.restored", count=count)
    return count
