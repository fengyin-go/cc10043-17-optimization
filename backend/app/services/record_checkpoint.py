"""记录检查点领域服务：管理接入任务的断点位置，支持恢复和回溯，提供检查点的确认和过期管理。

数据链路位置：断点管理层，为接入任务提供可靠的检查点记录和恢复依据，是续采策略的数据基础。

本次强化的确认流转规则：
- 检查点按「关联接入任务」精确归属，候选只认等值匹配，杜绝同名前缀串用；
- 只有「已确认」检查点可作为续采候选，待确认草稿与已过期归档一律不进候选；
- 确认时分配单调递增的任务内版本号，并即时检测与在途受理单的区间冲突（只提示不夺权）；
- 废弃只做状态留痕，历史受理单可通过引用反查继续追到该点。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import checkpoint_flow as flow
from app.services.base import BaseModuleService
from app.store import store


class RecordCheckpointService(BaseModuleService):
    MODULE = "record_checkpoint"
    ENTITY = "检查点"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["关联接入任务", "检查点类型", "检查点值", "创建时间", "确认状态"]
    REQUIRED_FIELDS = ["关联接入任务", "检查点类型", "检查点值"]
    STATUS_ORDER = ["待确认", "已确认", "已过期"]
    ACTION_RULES = {"确认检查点": "已确认", "废弃检查点": "已过期"}
    NEGATIVE_ACTIONS = []

    # ---- 通用骨架的领域化覆写 ----

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)
        return [flow.enrich_checkpoint(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        return flow.enrich_checkpoint(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 新建即草稿，不占版本号、不进候选；确认时才分配版本。
        entry["版本号"] = None
        entry["确认时间"] = None
        entry["确认状态"] = "待确认"
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        current = entry.get("status")
        if current == target:
            return None, f"检查点已是「{target}」状态，无需重复{action}"
        if action == "确认检查点":
            if current == "已过期":
                return None, "已归档检查点不能直接确认，需重新登记断点"
            entry["status"] = "已确认"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["版本号"] = flow.next_version(str(entry["关联接入任务"]))
            entry["确认时间"] = flow.now_iso()
            entry["确认状态"] = "已确认"
            conflicts = flow.conflicts_for(entry)
            if conflicts:
                detail = "；".join(
                    f"与{item['受理单号']}区间{item['重叠区间'][0]}-{item['重叠区间'][1]}冲突"
                    for item in conflicts
                )
                return entry, f"检查点已确认（版本V{entry['版本号']}），{detail}：本轮不回改，新点候任下一轮"
            return entry, f"检查点已确认（版本V{entry['版本号']}），可作为续采候选"
        # 废弃检查点：保留记录与引用关系，状态留痕可反查
        entry["status"] = "已过期"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["确认状态"] = "已过期"
        refs = flow.checkpoint_references(entry_id)
        if refs:
            orders = "、".join(str(row["受理单号"]) for row in refs)
            return entry, f"检查点已归档留痕，历史引用受理单 {orders} 仍可反查"
        return entry, "检查点已废弃归档"

    # ---- 业务扩展：断点记录与恢复 ----

    def create_checkpoint(self, task_name: str, point_type: str, point_value: str) -> dict[str, Any]:
        """创建新的检查点记录（草稿态，确认前不可用于续采）。"""
        rows = store.rows(self.MODULE)
        cp_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        now = datetime.now(timezone.utc).isoformat()
        checkpoint = {
            "id": cp_id, "关联接入任务": task_name, "检查点类型": point_type,
            "检查点值": point_value, "创建时间": now, "确认状态": "待确认",
            "status": "待确认", "pending": True, "abnormal": False,
            "版本号": None, "确认时间": None,
        }
        rows.append(checkpoint)
        return {"ok": True, "message": "检查点已创建（待确认，确认前不进续采候选）", "checkpoint": checkpoint}

    def get_latest_checkpoint(self, task_name: str) -> dict[str, Any] | None:
        """获取指定接入任务的最新「已确认」检查点：精确归属匹配，杜绝同名前缀串用。"""
        return flow.latest_eligible(task_name)

    def list_eligible(self, task_name: str) -> list[dict[str, Any]]:
        """可用候选清单：仅精确归属本任务的已确认检查点，按版本号升序。"""
        return [flow.enrich_checkpoint(row) for row in flow.eligible_checkpoints(task_name)]

    def validate_checkpoint_chain(self, entry_id: int) -> dict[str, Any]:
        """校验检查点链的完整性：是否存在断链或顺序异常（按确切任务范围校验）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "检查点不存在"}
        task = str(entry.get("关联接入任务", ""))
        chain = sorted(flow.task_checkpoints(task), key=lambda r: str(r.get("创建时间", "")))
        gaps = []
        for i in range(1, len(chain)):
            prev_val = chain[i - 1].get("检查点值", "")
            curr_val = chain[i].get("检查点值", "")
            if prev_val >= curr_val:
                gaps.append(f"检查点值非递增: {prev_val} → {curr_val}")
        return {
            "ok": len(gaps) == 0,
            "task": task,
            "chain_length": len(chain),
            "gaps": gaps,
        }

    def route_to_resume(self, entry_id: int) -> dict[str, Any] | None:
        """检查点路由到续采策略，返回匹配的续采规则。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        point_type = entry.get("检查点类型", "")
        policies = store.rows("resume_policy")
        for policy in policies:
            if policy.get("status") == "已启用" and point_type in policy.get("续采起点类型", ""):
                return {"policy": policy, "checkpoint": entry}
        return None

    def audit_expired_checkpoints(self) -> list[int]:
        """审计过期检查点并批量标记。"""
        rows = store.rows(self.MODULE)
        expired_ids = []
        now = datetime.now(timezone.utc)
        for row in rows:
            if row.get("status") != "待确认":
                continue
            created_str = str(row.get("创建时间", ""))
            try:
                created = datetime.fromisoformat(created_str)
                if (now - created).days > 30:
                    row["status"] = "已过期"
                    row["确认状态"] = "已过期"
                    row["pending"] = False
                    expired_ids.append(int(row.get("id", 0)))
            except ValueError:
                continue
        if expired_ids:
            audit_rows = store.rows("stream_audit")
            aid = max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1
            audit_rows.append({
                "id": aid, "处理时间": now.isoformat(), "数据源": "record_checkpoint",
                "消息总数": str(len(expired_ids)), "成功数": str(len(expired_ids)),
                "失败原因分布": "", "status": "正常", "pending": True, "abnormal": False,
            })
        return expired_ids

    def commit_offset(self, entry_id: int) -> str | None:
        """将已确认检查点提交到偏移量管理。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已确认":
            return None
        offset_rows = store.rows("offset_manager")
        oid = max((int(r.get("id", 0)) for r in offset_rows), default=0) + 1
        now = datetime.now(timezone.utc).isoformat()
        offset_rows.append({
            "id": oid, "消费组": entry.get("关联接入任务", ""),
            "Topic/Queue": entry.get("关联接入任务", ""),
            "分区/分片": "0", "当前Offset": entry.get("检查点值", "0"),
            "消费延迟": "0", "status": "正常", "pending": True, "abnormal": False,
        })
        return now
