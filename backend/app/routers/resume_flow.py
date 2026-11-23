"""检查点确认流转与续采受理的专用接口。

只做参数接收与结果包装，所有业务判断都在 services 层完成；
本路由先于通用资源路由注册，固定路径不会被 /api/{module}/{id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import checkpoint_flow as flow
from app.services.record_checkpoint import RecordCheckpointService
from app.services.resume_policy import ResumePolicyService
from app.store import store

router = APIRouter(prefix="/api", tags=["检查点续采流转"])

checkpoint_service = RecordCheckpointService()
resume_service = ResumePolicyService()


class AcceptPayload(BaseModel):
    policy_id: int = Field(..., description="已启用的续采规则 id")
    task: str = Field(..., description="接入任务全名（精确匹配，不做前缀包含）")
    partition: str = "0"
    last_offset: int | None = None


class HandoverPayload(BaseModel):
    new_checkpoint_id: int


def _unwrap(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok", True):
        raise HTTPException(status_code=400, detail=result.get("message", "业务校验未通过"))
    return result


# ------------------------------------------------------------ 检查点候选与引用反查

@router.get("/record_checkpoint/eligible")
def eligible_checkpoints(task: str = Query(..., description="接入任务全名")) -> dict[str, Any]:
    """可用候选：仅精确归属本任务的已确认检查点；草稿、归档、近名任务一律不返回。"""
    candidates = checkpoint_service.list_eligible(task)
    return {
        "task": task,
        "total": len(candidates),
        "items": candidates,
        "rule": "仅已确认且关联接入任务完全相等的检查点进入候选",
    }


@router.get("/record_checkpoint/{checkpoint_id}/references")
def checkpoint_references(checkpoint_id: int) -> dict[str, Any]:
    """检查点停用/归档后反查历史引用它的受理单、读取段与交接记录。"""
    checkpoint = store.find("record_checkpoint", checkpoint_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail=f"检查点 {checkpoint_id} 不存在")
    acceptance_rows = flow.checkpoint_references(checkpoint_id)
    details = []
    for row in acceptance_rows:
        detail = flow.acceptance_detail(row)
        detail["referring_segments"] = [
            seg for seg in detail["segments"]
            if int(seg.get("采用检查点id", 0)) == checkpoint_id
        ]
        details.append(detail)
    return {
        "checkpoint": flow.enrich_checkpoint(checkpoint),
        "reference_count": len(acceptance_rows),
        "acceptances": details,
        "handovers": [
            row for row in store.ledger(flow.HANDOVER_LEDGER)
            if checkpoint_id in (int(row.get("原检查点id") or 0), int(row.get("新检查点id") or 0))
        ],
    }


# ------------------------------------------------------------ 续采受理、读取、交接、公布

@router.get("/resume_policy/tasks/acceptances")
def task_acceptances(task: str = Query(...), status: str | None = None) -> dict[str, Any]:
    """按确切任务名查询受理单（含每段实读区间、采用检查点、交接记录）。"""
    rows = flow.acceptances(task)
    if status:
        rows = [row for row in rows if row.get("状态") == status]
    return {
        "task": task,
        "total": len(rows),
        "items": [flow.acceptance_detail(row) for row in rows],
        "latest_eligible_checkpoint": (
            flow.enrich_checkpoint(cp) if (cp := flow.latest_eligible(task)) else None
        ),
    }


@router.post("/resume_policy/accept")
def accept_resume(payload: AcceptPayload) -> dict[str, Any]:
    """受理续采：用已确认检查点版本计算起点并冻结凭据，开立受理单。"""
    return _unwrap(resume_service.accept_resume(
        payload.policy_id, payload.task, payload.partition, payload.last_offset))


@router.post("/resume_policy/acceptances/{acceptance_id}/read-segment")
def read_segment(acceptance_id: int) -> dict[str, Any]:
    """读取下一段：区间、分区、采用检查点版本全部归属到本受理单。"""
    return _unwrap(resume_service.read_next_segment(acceptance_id))


@router.post("/resume_policy/acceptances/{acceptance_id}/handover")
def handover_checkpoint(acceptance_id: int, payload: HandoverPayload) -> dict[str, Any]:
    """换点责任交接：旧单实读责任不移交，自动开立采用新检查点的后继单。"""
    return _unwrap(resume_service.handover_checkpoint(acceptance_id, payload.new_checkpoint_id))


@router.post("/resume_policy/acceptances/{acceptance_id}/publish")
def publish_round(acceptance_id: int) -> dict[str, Any]:
    """公布本轮实际覆盖区间、采用检查点与交接责任。"""
    return _unwrap(resume_service.publish_round(acceptance_id))


@router.get("/resume_policy/acceptances/{acceptance_id}")
def get_acceptance(acceptance_id: int) -> dict[str, Any]:
    acceptance = flow.get_acceptance(acceptance_id)
    if acceptance is None:
        raise HTTPException(status_code=404, detail=f"受理单 {acceptance_id} 不存在")
    return flow.acceptance_detail(acceptance)
