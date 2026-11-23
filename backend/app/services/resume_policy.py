"""续采策略领域服务：定义中断恢复后的续采起点策略和失败降级行为。

数据链路位置：恢复策略层，为中断的采集任务提供续采规则，决定恢复起点和失败处理方式。

本次强化的受理流转规则：
- 起点只依据「已确认且精确归属本任务」的检查点计算，草稿/归档点与同名前缀任务一律不参与；
- 受理瞬间冻结检查点版本与起始 Offset，受理后再批准的新断点不回改本轮；
- 每段实读区间（含分区）与采用的检查点版本逐段留痕，明确归到具体受理单；
- 换点走责任交接：旧单保留实读责任、自动开立后继单，并支持公布本轮实际覆盖区间。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import checkpoint_flow as flow
from app.services.base import BaseModuleService
from app.store import store


class ResumePolicyService(BaseModuleService):
    MODULE = "resume_policy"
    ENTITY = "续采规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "续采起点类型", "续采步长", "失败上限", "降级行为"]
    REQUIRED_FIELDS = ["规则名称", "续采起点类型", "续采步长"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "已启用", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ---- 内部工具 ----

    def _require_policy(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            raise ValueError("续采规则不可用：不存在或未启用")
        return entry

    @staticmethod
    def _step(entry: dict[str, Any]) -> int:
        try:
            step = int(str(entry.get("续采步长", "0")))
        except ValueError:
            step = 0
        return max(step, 1)

    @staticmethod
    def _start_type(entry: dict[str, Any]) -> str:
        return str(entry.get("起始点类型", entry.get("续采起点类型", "断点")))

    # ---- 续采计划与受理（凭据冻结） ----

    def plan_resume(
        self,
        entry_id: int,
        task_name: str,
        partition: str = "0",
        last_offset: int | None = None,
    ) -> dict[str, Any]:
        """受理前的续采计算：只采纳精确归属本任务的已确认检查点版本。"""
        entry = self._require_policy(entry_id)
        start_type = self._start_type(entry)
        step = self._step(entry)
        checkpoint = flow.latest_eligible(task_name)
        if checkpoint is None:
            return {
                "ok": False,
                "message": f"接入任务「{task_name}」没有已确认检查点，待确认草稿与归档点不可作为续采凭据",
                "task": task_name,
            }
        cp_point = flow.parse_point(checkpoint.get("检查点值"))
        if cp_point is None:
            return {"ok": False, "message": f"检查点 {checkpoint['id']} 的值无法解析为数值断点"}
        # 起点类型按语义匹配（如「Offset断点」属于断点续采），任务归属仍坚持等值精确匹配。
        if "断点" in start_type:
            resume_from = cp_point + 1
        elif "最早" in start_type:
            resume_from = 0
        elif "偏移" in start_type:
            resume_from = max(cp_point - step, 0)
        else:
            resume_from = cp_point
        if last_offset is not None and last_offset > resume_from:
            # 外部已知消费进度比断点更靠时，以更靠后的安全位置为起点，避免重复区间扩大。
            resume_from = last_offset
        return {
            "ok": True,
            "task": task_name,
            "partition": partition,
            "resume_from": resume_from,
            "start_type": start_type,
            "step": step,
            "checkpoint": flow.enrich_checkpoint(checkpoint),
            "open_acceptances": [
                {"受理单号": row["受理单号"], "起始Offset": row["起始Offset"],
                 "已覆盖至": row["已覆盖至"]}
                for row in flow.open_acceptances(task_name)
            ],
        }

    def accept_resume(
        self,
        entry_id: int,
        task_name: str,
        partition: str = "0",
        last_offset: int | None = None,
    ) -> dict[str, Any]:
        """受理续采工作：冻结检查点版本快照并开立受理单，本轮区间自此确定。"""
        plan = self.plan_resume(entry_id, task_name, partition, last_offset)
        if not plan.get("ok"):
            return plan
        entry = self._require_policy(entry_id)
        checkpoint = store.find("record_checkpoint", int(plan["checkpoint"]["id"]))
        acceptance = flow.create_acceptance(
            task=task_name,
            partition=partition,
            policy=entry,
            start_offset=int(plan["resume_from"]),
            checkpoint=checkpoint,
        )
        return {
            "ok": True,
            "message": (
                f"已受理 {task_name} 分区{partition}，冻结检查点"
                f"V{checkpoint.get('版本号')}（{checkpoint.get('检查点值')}），"
                f"本轮自 Offset {acceptance['起始Offset']} 起；受理后新批准断点不回改本轮"
            ),
            "acceptance": flow.acceptance_detail(acceptance),
        }

    def read_next_segment(self, acceptance_id: int) -> dict[str, Any]:
        """读取下一段：按受理单冻结的凭据执行，区间、分区、检查点逐段留痕。"""
        acceptance = flow.get_acceptance(acceptance_id)
        if acceptance is None:
            return {"ok": False, "message": "受理单不存在或已归档"}
        policy = store.find(self.MODULE, int(acceptance.get("续采规则id", 0)))
        if policy is None or policy.get("status") != "已启用":
            return {"ok": False, "message": "原续采规则已停用，需先交接换规后再读取"}
        try:
            segment = flow.append_read_segment(acceptance, self._step(policy))
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {
            "ok": True,
            "message": (
                f"{acceptance['受理单号']} 分区{segment['分区']} 实读 "
                f"{segment['起始Offset']}-{segment['结束Offset']}（{segment['读取条数']}条），"
                f"采用检查点 V{segment['采用版本号']} / {segment['采用检查点值']}"
            ),
            "segment": segment,
            "acceptance": flow.acceptance_detail(acceptance),
        }

    def handover_checkpoint(self, acceptance_id: int, new_checkpoint_id: int) -> dict[str, Any]:
        """换点责任交接：公布实读/兜底区间，并自动开立采用新检查点的后继受理单。"""
        try:
            result = flow.handover(acceptance_id, new_checkpoint_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        record = result["handover"]
        successor = result["successor"]
        return {
            "ok": True,
            "message": record["责任交接"],
            "handover": record,
            "successor": flow.acceptance_detail(successor),
        }

    def publish_round(self, acceptance_id: int) -> dict[str, Any]:
        """公布本轮实际覆盖区间、采用检查点版本与换点交接明细。"""
        try:
            acceptance = flow.publish_round(acceptance_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "本轮续采覆盖区间已公布", "acceptance": flow.acceptance_detail(acceptance)}

    # ---- 兼容保留：旧接口内部已切换为「已受理版本」语义 ----

    def compute_resume_point(self, entry_id: int, last_offset: int) -> dict[str, Any]:
        """根据续采策略计算恢复起点（基于已确认检查点，需显式给出任务名时请改用 plan_resume）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"ok": False, "message": "续采规则不可用"}
        start_type = self._start_type(entry)
        step = self._step(entry)
        if start_type == "断点":
            resume_from = last_offset + 1
        elif start_type == "最早":
            resume_from = 0
        elif start_type == "偏移":
            resume_from = max(last_offset - step, 0)
        else:
            resume_from = last_offset
        return {"ok": True, "resume_from": resume_from, "start_type": start_type, "step": step}

    def handle_failure_limit(self, entry_id: int, failure_count: int) -> dict[str, Any]:
        """检查失败次数是否达到上限，触发降级行为。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已启用":
            return {"threshold_reached": False, "action": None}
        try:
            limit = int(str(entry.get("失败上限", "10")))
        except ValueError:
            limit = 10
        if failure_count >= limit:
            degrade = entry.get("降级行为", "暂停")
            return {"threshold_reached": True, "action": degrade, "failures": failure_count, "limit": limit}
        return {"threshold_reached": False, "action": None, "failures": failure_count, "limit": limit}

    def validate_checkpoint_compat(self, entry_id: int) -> dict[str, Any]:
        """校验续采规则与检查点的兼容性（只统计已确认、可用的检查点）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"compatible": False, "message": "规则不存在"}
        start_type = entry.get("续采起点类型", "")
        checkpoints = store.rows("record_checkpoint")
        matching = [
            cp for cp in checkpoints
            if cp.get("status") == "已确认" and start_type in str(cp.get("检查点类型", ""))
        ]
        return {"compatible": len(matching) > 0, "start_type": start_type,
                "matching_checkpoints": len(matching)}

    def link_to_isolation(self, entry_id: int, failed_msgs: int) -> str | None:
        """续采失败时的消息隔离路由。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        for _ in range(min(failed_msgs, 3)):
            isolation_rows = store.rows("failure_isolation")
            fid = max((int(r.get("id", 0)) for r in isolation_rows), default=0) + 1
            isolation_rows.append({
                "id": fid, "失败消息ID": f"resume-err-{fid}",
                "失败原因": f"续采失败: {entry.get('规则名称', '')}",
                "原始Topic": entry.get("续采起点类型", ""),
                "隔离时间": now, "重试次数": "0",
                "status": "待处理", "pending": True, "abnormal": False,
            })
        return now
