"""流数据处理审计领域服务：对比接入量和下游落地量，审计流数据处理的数据偏差和完整性。

数据链路位置：审计对账层，全链最后的质量关，检验整个数据链路是否产生数据偏差或断流事件。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class StreamAuditService(BaseModuleService):
    MODULE = "stream_audit"
    ENTITY = "审计明细"
    OPERATION_TERM = "接入"
    LIST_FIELDS = ["处理时间", "数据源", "消息总数", "成功数", "失败原因分布"]
    REQUIRED_FIELDS = ["处理时间", "数据源", "消息总数"]
    STATUS_ORDER = ["正常", "偏差超标", "数据断流"]
    ACTION_RULES = {"查看详情": "偏差超标", "忽略告警": "数据断流"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    # ---- 业务扩展：端到端审计对账 ----

    def compare_stream_health(self, source: str) -> dict[str, Any]:
        """全链路健康检查：对比接入量与落地量的数据偏差。"""
        audit_rows = store.rows(self.MODULE)
        matched = [a for a in audit_rows if source in str(a.get("数据源", ""))]
        total_msgs = sum(int(a.get("消息总数", "0")) if str(a.get("消息总数", "")).isdigit() else 0 for a in matched)
        total_success = sum(int(a.get("成功数", "0")) if str(a.get("成功数", "")).isdigit() else 0 for a in matched)
        now = datetime.now(timezone.utc).isoformat()
        if total_msgs == 0:
            status = "数据断流"
            abnormal = True
        elif total_success < total_msgs * 0.7:
            status = "偏差超标"
            abnormal = True
        else:
            status = "正常"
            abnormal = False
        entry = {
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "处理时间": now, "数据源": source,
            "消息总数": str(total_msgs), "成功数": str(total_success),
            "失败原因分布": f"偏差{total_msgs - total_success}条" if total_msgs > total_success else "",
            "status": status, "pending": True, "abnormal": abnormal,
        }
        audit_rows.append(entry)
        return {"ok": True, "source": source, "total": total_msgs, "success": total_success,
                "deviation_rate": round((total_msgs - total_success) / max(total_msgs, 1) * 100, 1),
                "status": status}

    def validate_data_lineage(self, source: str) -> dict[str, Any]:
        """验证数据血缘链路中各模块的完整性。"""
        checkpoints = store.rows("record_checkpoint")
        watermarks = store.rows("watermark_tracker")
        payloads = store.rows("payload_store")
        sinks = store.rows("data_sink")
        # 检查点归属坚持等值精确匹配，避免同名前缀任务（如 A 与 A-备份链路）串用。
        has_checkpoint = any(
            str(c.get("关联接入任务", "")) == source and c.get("status") == "已确认"
            for c in checkpoints
        )
        has_watermark = any(source in str(w.get("数据源", "")) for w in watermarks)
        has_payload = any(source in str(p.get("采集批次", "")) for p in payloads)
        has_sink = any(source in str(s.get("配置名称", "")) for s in sinks)
        return {"source": source, "checkpoint": has_checkpoint, "watermark": has_watermark,
                "payload": has_payload, "sink": has_sink,
                "complete": has_checkpoint and has_watermark and has_payload and has_sink}

    def trigger_alarm_on_deviation(self, entry_id: int) -> str | None:
        """偏差超标时触发吞吐量告警。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") not in ("偏差超标", "数据断流"):
            return None
        source = entry.get("数据源", "")
        alarms = store.rows("throughput_alarm")
        for alarm in alarms:
            if alarm.get("status") == "已生效" and source in alarm.get("监控数据源", ""):
                alarm["abnormal"] = True
                return f"已触发告警: {alarm.get('规则名称', '')}"
        return None

    def export_audit_report(self, source: str, time_range: tuple[str, str]) -> dict[str, Any]:
        """导出一份审计报告到流式数据导出。"""
        export_rows = store.rows("stream_export")
        eid = max((int(r.get("id", 0)) for r in export_rows), default=0) + 1
        export_rows.append({
            "id": eid, "任务名称": f"审计报告-{source}",
            "数据源": source, "导出格式": "csv",
            "时间范围": f"{time_range[0]}~{time_range[1]}",
            "文件大小": "0",
            "status": "排队中", "pending": True, "abnormal": False,
        })
        return {"ok": True, "export_id": eid, "source": source}

    def create_replay_from_audit(self, entry_id: int) -> dict[str, Any]:
        """当审计发现偏差时，创建一组回放任务来修复数据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "偏差超标":
            return {"ok": False, "message": "未达到回放条件"}
        source = entry.get("数据源", "")
        msgs = int(str(entry.get("消息总数", "0")))
        successes = int(str(entry.get("成功数", "0")))
        if msgs <= successes:
            return {"ok": False, "message": "无偏差数据需要回放"}
        replay_rows = store.rows("batch_replay")
        rid = max((int(r.get("id", 0)) for r in replay_rows), default=0) + 1
        now = datetime.now(timezone.utc).isoformat()
        replay_rows.append({
            "id": rid, "回放范围": f"偏差{msgs - successes}条",
            "开始时间": now, "结束时间": now,
            "目标Topic": source, "回放速率": "100",
            "status": "待回放", "pending": True, "abnormal": False,
        })
        return {"ok": True, "message": f"已创建回放任务{rid}", "replay_id": rid}