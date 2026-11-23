"""检查点确认流转与续采受理的共享领域逻辑。

领域约束（任何路由/服务都不得绕过本模块直接拼候选）：

1. 检查点只按「关联接入任务」精确归属，禁止同名前缀子串串用；
2. 只有「已确认」检查点能进入可用候选，待确认（草稿）与已过期（停用/归档）一律排除；
3. 续采受理单在受理瞬间冻结接续凭据（检查点版本快照），受理后新批准的断点不得回改
   本轮起点，已交给本轮的区间始终有人负责；
4. 每个实际读取段都归属于唯一受理单，并记录分区、实际区间与采用的检查点；
5. 检查点停用/归档只改状态、不删数据，历史受理单与交接记录可永久反查。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

CP_MODULE = "record_checkpoint"
ACCEPTANCE_LEDGER = "resume_acceptance"
SEGMENT_LEDGER = "resume_read_segment"
HANDOVER_LEDGER = "checkpoint_handover"

# 受理单状态机：进行中 →（交接换点）→ 已交接（后继单自动开立）→ 公布本轮 → 已公布
ACC_OPEN = "进行中"
ACC_HANDED_OVER = "已交接"
ACC_PUBLISHED = "已公布"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger(name: str) -> list[dict[str, Any]]:
    return store.ledger(name)


def _next_id(name: str) -> int:
    return max((int(row.get("id", 0)) for row in _ledger(name)), default=0) + 1


def parse_point(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 检查点归属与候选

def task_checkpoints(task_name: str) -> list[dict[str, Any]]:
    """返回确切归属某接入任务的检查点：等值匹配，不做前缀/包含匹配。"""
    return [
        row for row in store.rows(CP_MODULE)
        if str(row.get("关联接入任务", "")) == task_name
    ]


def eligible_checkpoints(task_name: str) -> list[dict[str, Any]]:
    """可用候选：仅精确归属本任务且状态为「已确认」的检查点，按版本号升序。"""
    rows = [row for row in task_checkpoints(task_name) if row.get("status") == "已确认"]
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("版本号") or 0),
            str(row.get("确认时间") or row.get("创建时间") or ""),
        ),
    )


def latest_eligible(task_name: str) -> dict[str, Any] | None:
    candidates = eligible_checkpoints(task_name)
    return candidates[-1] if candidates else None


def next_version(task_name: str) -> int:
    return max(
        (int(row.get("版本号") or 0) for row in task_checkpoints(task_name)),
        default=0,
    ) + 1


# ---------------------------------------------------------------- 受理单与读取段

def acceptances(task_name: str | None = None) -> list[dict[str, Any]]:
    rows = _ledger(ACCEPTANCE_LEDGER)
    if task_name is None:
        return list(rows)
    return [row for row in rows if str(row.get("接入任务", "")) == task_name]


def open_acceptances(task_name: str) -> list[dict[str, Any]]:
    return [
        row for row in acceptances(task_name)
        if row.get("状态") == ACC_OPEN
    ]


def get_acceptance(acceptance_id: int) -> dict[str, Any] | None:
    for row in _ledger(ACCEPTANCE_LEDGER):
        if int(row.get("id", 0)) == acceptance_id:
            return row
    return None


def segments(acceptance_id: int) -> list[dict[str, Any]]:
    return sorted(
        [row for row in _ledger(SEGMENT_LEDGER)
         if int(row.get("受理单id", 0)) == acceptance_id],
        key=lambda row: int(row.get("id", 0)),
    )


def handovers(acceptance_id: int) -> list[dict[str, Any]]:
    return sorted(
        [row for row in _ledger(HANDOVER_LEDGER)
         if int(row.get("受理单id", 0)) == acceptance_id],
        key=lambda row: int(row.get("id", 0)),
    )


def create_acceptance(
    *,
    task: str,
    partition: str,
    policy: dict[str, Any],
    start_offset: int,
    checkpoint: dict[str, Any],
    predecessor_id: int | None = None,
) -> dict[str, Any]:
    """受理续采：冻结当时的检查点版本快照，本轮起点与凭据不再随后续审批变化。"""
    acceptance_id = _next_id(ACCEPTANCE_LEDGER)
    acceptance = {
        "id": acceptance_id,
        "受理单号": f"RA-{acceptance_id:04d}",
        "接入任务": task,
        "分区": partition,
        "续采规则id": int(policy.get("id", 0)),
        "续采规则名称": policy.get("规则名称", ""),
        "状态": ACC_OPEN,
        # —— 接续凭据冻结区 ——
        "接续检查点id": int(checkpoint.get("id", 0)),
        "接续检查点值": str(checkpoint.get("检查点值", "")),
        "接续版本号": checkpoint.get("版本号"),
        "起始Offset": start_offset,
        "已覆盖至": start_offset - 1,
        "前置受理单": predecessor_id,
        "后继受理单": None,
        "受理时间": now_iso(),
        "公布时间": None,
        "公布区间": None,
        "备注": "",
    }
    _ledger(ACCEPTANCE_LEDGER).append(acceptance)
    return acceptance


def append_read_segment(acceptance: dict[str, Any], step: int) -> dict[str, Any]:
    """按受理单冻结的凭据读取下一段，区间与分区责任全部落在本受理单上。"""
    if acceptance.get("状态") != ACC_OPEN:
        raise ValueError(f"受理单 {acceptance.get('受理单号')} 已{acceptance.get('状态')}，不能继续读取")
    if step <= 0:
        raise ValueError("续采步长必须为正整数")
    start = int(acceptance["已覆盖至"]) + 1
    end = start + step - 1
    frozen_cp = store.find(CP_MODULE, int(acceptance["接续检查点id"]))
    credential_live = frozen_cp is not None and frozen_cp.get("status") == "已确认"
    segment_id = _next_id(SEGMENT_LEDGER)
    segment = {
        "id": segment_id,
        "受理单id": int(acceptance["id"]),
        "受理单号": acceptance["受理单号"],
        "接入任务": acceptance["接入任务"],
        "分区": acceptance["分区"],
        "起始Offset": start,
        "结束Offset": end,
        "读取条数": end - start + 1,
        "采用检查点id": int(acceptance["接续检查点id"]),
        "采用检查点值": str(acceptance["接续检查点值"]),
        "采用版本号": acceptance.get("接续版本号"),
        "凭据当前状态": frozen_cp.get("status") if frozen_cp else "记录缺失",
        "轮次序号": len(segments(int(acceptance["id"]))) + 1,
        "读取时间": now_iso(),
    }
    if not credential_live:
        segment["凭据告警"] = (
            f"接续检查点（{segment['采用检查点值']}）当前状态为"
            f"「{segment['凭据当前状态']}」，本段区间责任仍归属本受理单"
        )
    _ledger(SEGMENT_LEDGER).append(segment)
    acceptance["已覆盖至"] = end
    return segment


# ---------------------------------------------------------------- 冲突、交接与公布

def conflicts_for(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """新批准断点与在途受理单的区间冲突：只提示、不夺权，本轮仍按冻结凭据执行。"""
    point = parse_point(checkpoint.get("检查点值"))
    if point is None:
        return []
    result = []
    for acceptance in open_acceptances(str(checkpoint.get("关联接入任务", ""))):
        start = int(acceptance["起始Offset"])
        if start <= point:
            result.append({
                "受理单号": acceptance["受理单号"],
                "受理单id": int(acceptance["id"]),
                "受理起点": start,
                "重叠区间": [start, point],
                "处置": (
                    f"本轮继续按受理时冻结的检查点"
                    f"{acceptance['接续检查点值']}执行，新检查点候任下一轮接续"
                ),
            })
    return result


def handover(acceptance_id: int, new_checkpoint_id: int) -> dict[str, Any]:
    """换点责任交接：旧单保留实读区间责任，未实读部分明确兜底，再自动开立后继单。"""
    acceptance = get_acceptance(acceptance_id)
    if acceptance is None:
        raise ValueError("受理单不存在")
    if acceptance.get("状态") != ACC_OPEN:
        raise ValueError(f"受理单 {acceptance['受理单号']} 已{acceptance.get('状态')}，不能交接")
    new_cp = store.find(CP_MODULE, new_checkpoint_id)
    if new_cp is None:
        raise ValueError("新检查点不存在或已删除")
    if new_cp.get("status") != "已确认":
        raise ValueError("只有已确认检查点可以承接续采，待确认/已过期点不得换入")
    if str(new_cp.get("关联接入任务", "")) != str(acceptance.get("接入任务", "")):
        raise ValueError("检查点归属任务与受理单不一致，禁止跨任务换点")
    new_point = parse_point(new_cp.get("检查点值"))
    old_point = parse_point(acceptance.get("接续检查点值"))
    if new_point is None or old_point is None:
        raise ValueError("检查点值无法解析为数值断点")
    if new_point <= old_point:
        raise ValueError(f"新断点 {new_point} 不高于当前接续断点 {old_point}，无需换点")

    start = int(acceptance["起始Offset"])
    owned_end = int(acceptance["已覆盖至"])
    next_start = max(new_point + 1, owned_end + 1)
    read_segments = segments(acceptance_id)
    if not read_segments:
        # 尚未发生实读：整段受理区间都未落地，责任整体移交新点，后继单负责新断点之前的复核回补。
        gap = [start, new_point]
        duty = (
            f"{acceptance['受理单号']}尚无实读记录，受理区间 {start}-{new_point} 责任整体交接给"
            f"新检查点 {new_point} 的后继受理单兜底；下一轮自 {next_start} 起"
        )
    elif owned_end < new_point:
        gap = [owned_end + 1, new_point]
        duty = (
            f"{acceptance['受理单号']}实读 {start}-{owned_end} 责任不移交；"
            f"未实读 {gap[0]}-{gap[1]} 责任交接给新检查点 {new_point} 的上游确认兜底，"
            f"下游去重复核发现缺漏仍回补本单；下一轮自 {next_start} 起"
        )
    else:
        gap = None
        duty = (
            f"{acceptance['受理单号']}实读 {start}-{owned_end} 责任不移交，已覆盖新断点 {new_point}，"
            f"区间无空缺、无无人处理段；下一轮自 {next_start} 起"
        )
    record = {
        "id": _next_id(HANDOVER_LEDGER),
        "受理单id": acceptance_id,
        "受理单号": acceptance["受理单号"],
        "接入任务": acceptance["接入任务"],
        "分区": acceptance["分区"],
        "原检查点id": int(acceptance["接续检查点id"]),
        "原检查点值": str(acceptance["接续检查点值"]),
        "新检查点id": new_checkpoint_id,
        "新检查点值": str(new_cp.get("检查点值", "")),
        "本轮实读区间": [start, owned_end],
        "兜底区间": gap,
        "下轮起点": next_start,
        "责任交接": duty,
        "交接时间": now_iso(),
    }
    _ledger(HANDOVER_LEDGER).append(record)

    policy = store.find("resume_policy", int(acceptance["续采规则id"])) or {}
    successor = create_acceptance(
        task=str(acceptance["接入任务"]),
        partition=str(acceptance["分区"]),
        policy=policy,
        start_offset=next_start,
        checkpoint=new_cp,
        predecessor_id=acceptance_id,
    )
    acceptance["状态"] = ACC_HANDED_OVER
    acceptance["后继受理单"] = int(successor["id"])
    acceptance["备注"] = f"已换点至检查点 {new_cp.get('检查点值')}，后继受理单 {successor['受理单号']}"
    return {"handover": record, "successor": successor}


def publish_round(acceptance_id: int) -> dict[str, Any]:
    """公布本轮实际覆盖区间、采用检查点与换点交接，作为责任结算依据。"""
    acceptance = get_acceptance(acceptance_id)
    if acceptance is None:
        raise ValueError("受理单不存在")
    if acceptance.get("状态") == ACC_PUBLISHED:
        raise ValueError("本轮已经公布，不能重复公布")
    coverage = [int(acceptance["起始Offset"]), int(acceptance["已覆盖至"])]
    acceptance["公布区间"] = coverage
    acceptance["公布时间"] = now_iso()
    acceptance["状态"] = ACC_PUBLISHED

    audit_rows = store.rows("stream_audit")
    audit_rows.append({
        "id": max((int(row.get("id", 0)) for row in audit_rows), default=0) + 1,
        "处理时间": now_iso(),
        "数据源": f"续采受理:{acceptance['受理单号']}",
        "消息总数": str(sum(int(seg["读取条数"]) for seg in segments(acceptance_id))),
        "成功数": str(sum(int(seg["读取条数"]) for seg in segments(acceptance_id))),
        "失败原因分布": "",
        "status": "正常",
        "pending": False,
        "abnormal": False,
    })
    return acceptance


# ---------------------------------------------------------------- 引用留痕与凭据展示

def checkpoint_references(checkpoint_id: int) -> list[dict[str, Any]]:
    """反查引用过某检查点的受理单（含已交接/已公布、检查点已归档的场景）。"""
    referenced_ids = {
        int(row["id"]) for row in acceptances()
        if int(row.get("接续检查点id") or 0) == checkpoint_id
    }
    for row in _ledger(HANDOVER_LEDGER):
        if checkpoint_id in (int(row.get("原检查点id") or 0), int(row.get("新检查点id") or 0)):
            referenced_ids.add(int(row["受理单id"]))
    return [row for row in acceptances() if int(row["id"]) in referenced_ids]


def credential_state(checkpoint: dict[str, Any]) -> str:
    status = checkpoint.get("status")
    if status == "待确认":
        return "待确认·不可用"
    if status == "已过期":
        return "已归档·留痕可查"
    cp_id = int(checkpoint.get("id", 0))
    in_use = any(
        int(row.get("接续检查点id") or 0) == cp_id
        for row in open_acceptances(str(checkpoint.get("关联接入任务", "")))
    )
    if in_use:
        return "在用凭据"
    if conflicts_for(checkpoint):
        return "冲突候任"
    return "可用候选"


def enrich_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """给工作台列表补充凭据状态、冲突提示与引用计数（不改写原始记录）。"""
    view = dict(checkpoint)
    view["凭据状态"] = credential_state(checkpoint)
    view["冲突提示"] = conflicts_for(checkpoint)
    view["引用受理单数"] = len(checkpoint_references(int(checkpoint.get("id", 0))))
    return view


def acceptance_detail(acceptance: dict[str, Any]) -> dict[str, Any]:
    view = dict(acceptance)
    acceptance_id = int(acceptance["id"])
    view["segments"] = segments(acceptance_id)
    view["handovers"] = handovers(acceptance_id)
    return view
