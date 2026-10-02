"""定时任务管理 API（NL2Cron，考点六十四）。

端点：
- POST /scheduler/nl2cron        用户口述 → 解析/校验/三档分支
- POST /scheduler/jobs           确认创建（high 分支直接创建；mid 澄清后带补充参数）
- GET  /scheduler/jobs           列出我的任务（admin 可看全部）
- POST /scheduler/jobs/{id}/review   开发人员审核（approve/reject）
- POST /scheduler/jobs/{id}/status   暂停/恢复/禁用
- DELETE /scheduler/jobs/{id}    删除
- GET  /scheduler/domains        能力域目录（引导用户选择）
- GET  /scheduler/jobs/{id}/logs 任务审计/执行记录
- GET  /scheduler/harvest       失败案例采集池（用户点踩，考点六十六）
- POST /scheduler/harvest/{id}/draft     LLM 预填转正草稿（expected_*）
- POST /scheduler/harvest/{id}/promote   开发人员确认转正 → evaluation_cases
- POST /scheduler/harvest/{id}/discard   弃用采集条目
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.observability.logging import get_logger
from app.scheduler import nl2cron, service
from app.scheduler.registry import list_domains

logger = get_logger("scheduler.api")
router = APIRouter(prefix="/scheduler", tags=["scheduler"])

# 简单角色判定（MVP：用用户名前缀模拟；真实环境接 user_roles 表）
# 开发人员（审核权限）：名字含 "dev"/"admin" 或显式传入 reviewer 标记
_ADMIN_HINT = ("admin", "dev", "developer", "reviewer")


def _is_reviewer(user_id: str) -> bool:
    return (user_id or "").lower() in _ADMIN_HINT or any(h in (user_id or "").lower() for h in _ADMIN_HINT)


def _require_reviewer(user_id: str) -> None:
    if not _is_reviewer(user_id):
        raise HTTPException(status_code=403, detail="需要开发人员权限才能审核定时任务")


# ---------- 请求/响应模型 ----------

class NL2CronRequest(BaseModel):
    text: str = Field(..., min_length=2, max_length=500, description="用户口述，如'每天早上9点分析美国市场销量'")
    user_id: str = "default"


class JobCreateRequest(BaseModel):
    capability: str
    time_expr: str
    params: dict[str, Any] = Field(default_factory=dict)
    user_id: str = "default"
    name: Optional[str] = None


class ReviewRequest(BaseModel):
    user_id: str = Field(..., description="操作者（开发人员）")
    decision: str = Field(..., description="approve / reject")
    note: Optional[str] = None


class StatusRequest(BaseModel):
    user_id: str
    status: str = Field(..., description="active / paused / disabled")


class DeleteRequest(BaseModel):
    user_id: str


# ---------- 端点 ----------

@router.post("/nl2cron")
def nl2cron_endpoint(req: NL2CronRequest) -> dict[str, Any]:
    """用户口述 → LLM 解析 + 确定性校验 + 三档分支。"""
    parsed = nl2cron.parse_nl(req.text)
    if parsed is None:
        return {
            "branch": "catalog",
            "reason": "parse_failed",
            "domains": list_domains(),
            "message": "没解析出定时任务意图，以下是我能定时帮你做的：",
        }
    check = nl2cron.validate_parse(parsed)
    if not check["valid"]:
        return {"branch": "reject", "reason": check["reason"], "domains": list_domains(),
                "message": f"无法创建：{check['reason']}。你可以补充信息换个说法，或从目录里选："}
    route = nl2cron.route(parsed)
    base = {
        "parsed": parsed,
        "capability": check["capability"],
        "domain_name": check["domain_name"],
        "time_expr": check["time_expr"],
        "cron": check["cron"],
        "cron_human": check["cron_human"],
        "params": check["params"],
        "risk": check["risk"],
        "confidence": check["confidence"],
    }
    if route["branch"] == "create":
        return {**base, "branch": "create",
                "message": f"识别为「{check['domain_name']}」，将 {check['cron_human']} 执行。确认创建吗？"}
    if route["branch"] == "clarify":
        return {**base, "branch": "clarify",
                "message": f"我猜你想做「{check['domain_name']}」，但把握一般——请确认要监控/分析的具体指标或问题。"}
    return {"branch": "catalog", "domains": list_domains(),
            "message": "没把握匹配到能力域，以下是我能定时帮你做的："}


@router.post("/jobs")
def create_job_endpoint(req: JobCreateRequest) -> dict[str, Any]:
    """确认创建（high 分支确认后调用）。创建后进入 pending 等待开发人员审核。"""
    # 重新走确定性校验（不信任前端/用户直接传的 capability）
    parsed = {
        "capability": req.capability,
        "time_expr": req.time_expr,
        "params": req.params,
        "confidence": 1.0,
    }
    check = nl2cron.validate_parse(parsed)
    if not check["valid"]:
        raise HTTPException(status_code=400, detail=check["reason"])
    name = req.name or f"{check['domain_name']}-{req.time_expr}"
    return service.create_job(
        user_id=req.user_id,
        name=name,
        capability=check["capability"],
        params=check["params"],
        time_expr=check["time_expr"],
        cron=check["cron"],
        risk=check["risk"],
        confidence=check["confidence"],
    )


@router.get("/jobs")
def list_jobs_endpoint(user_id: str = Query(""), status: Optional[str] = Query(None)) -> dict[str, Any]:
    """列出任务（默认全部，可传 user_id 只看某人）。"""
    return {"jobs": service.list_jobs(user_id=user_id or None, status=status)}


@router.get("/domains")
def domains_endpoint() -> dict[str, Any]:
    """能力域目录（引导用户选择）。"""
    return {"domains": list_domains()}


@router.post("/jobs/{job_id}/review")
def review_job_endpoint(job_id: int, req: ReviewRequest) -> dict[str, Any]:
    """开发人员审核定时任务（approve → 注册调度；reject → 终止）。"""
    _require_reviewer(req.user_id)
    return service.review_job(job_id, req.user_id, req.decision, req.note)


@router.post("/jobs/{job_id}/status")
def status_job_endpoint(job_id: int, req: StatusRequest) -> dict[str, Any]:
    """暂停/恢复/禁用（owner 或开发人员）。"""
    return service.set_job_status(job_id, req.status, req.user_id)


@router.post("/jobs/{job_id}/delete")
def delete_job_endpoint(job_id: int, req: DeleteRequest) -> dict[str, Any]:
    """删除任务（软删，保留审计）。POST 而非 DELETE，兼容各 HTTP 客户端。"""
    return service.delete_job(job_id, req.user_id)


@router.get("/jobs/{job_id}/logs")
def job_logs_endpoint(job_id: int, limit: int = Query(50, le=200)) -> dict[str, Any]:
    """任务审计/执行记录。"""
    from app.memory.db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_type, detail, run_at, duration_ms, result FROM scheduler_run_logs "
            "WHERE job_id=%s ORDER BY run_at DESC LIMIT %s", (job_id, limit)
        ).fetchall()
    return {"logs": [
        {"event_type": r[0], "detail": r[1], "run_at": r[2].isoformat() if r[2] else None,
         "duration_ms": r[3], "result": r[4]} for r in rows
    ]}


# ---------- 失败案例采集池（用户点踩回流，考点六十六） ----------

class HarvestDraftRequest(BaseModel):
    user_id: str = Field(..., description="操作者（开发人员）")


class HarvestPromoteRequest(BaseModel):
    user_id: str = Field(..., description="操作者（开发人员）")
    expected: dict[str, Any] = Field(..., description="确认后的期望值：category/expected_agents/expected_sql_pattern/expected_answer_key")


class HarvestDiscardRequest(BaseModel):
    user_id: str = Field(..., description="操作者（开发人员）")


@router.get("/harvest")
def harvest_list_endpoint(status: Optional[str] = Query(None), limit: int = Query(100, le=500)) -> dict[str, Any]:
    """失败案例采集池列表（默认 pending，可过滤）。"""
    from app.scheduler.harvest import list_harvest
    return {"items": list_harvest(status=status, limit=limit)}


@router.post("/harvest/{harvest_id}/draft")
def harvest_draft_endpoint(harvest_id: int, req: HarvestDraftRequest) -> dict[str, Any]:
    """LLM 预填转正草稿（只做建议，不直接入库；防'以错为正'固化错误期望）。"""
    _require_reviewer(req.user_id)
    from app.scheduler.harvest import draft_expected
    return draft_expected(harvest_id)


@router.post("/harvest/{harvest_id}/promote")
def harvest_promote_endpoint(harvest_id: int, req: HarvestPromoteRequest) -> dict[str, Any]:
    """开发人员确认草稿后转正：插入 evaluation_cases（category='online'）。"""
    _require_reviewer(req.user_id)
    from app.scheduler.harvest import promote
    return promote(harvest_id, req.user_id, req.expected)


@router.post("/harvest/{harvest_id}/discard")
def harvest_discard_endpoint(harvest_id: int, req: HarvestDiscardRequest) -> dict[str, Any]:
    """弃用采集条目（保留审计）。"""
    _require_reviewer(req.user_id)
    from app.scheduler.harvest import discard
    return discard(harvest_id, req.user_id)
