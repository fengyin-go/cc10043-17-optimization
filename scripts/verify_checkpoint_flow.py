"""检查点确认流转的场景验证（直接驱动领域服务，每次子进程都是全新内存仓库）。

覆盖：
A. 首次读取之前批准点120：本轮凭据冻结不回改，101-120 不丢；交接后新点服务下一轮；
B. 读完第一段后批准点120：已读段归旧受理单，后续段仍按冻结凭据，再交接；
C. 近名任务点300 严格隔离、草稿不入候选、归档后引用反查、跨任务换点被拒、兜底区间。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/workspace/backend")

from app.services import checkpoint_flow as flow  # noqa: E402
from app.services.record_checkpoint import RecordCheckpointService  # noqa: E402
from app.services.resume_policy import ResumePolicyService  # noqa: E402
from app.store import store  # noqa: E402


def reset_store() -> None:
    """每个场景从种子数据重新开始，保证互不污染。"""
    store.reset_to_seed()
    global cp_svc, rp_svc
    cp_svc = RecordCheckpointService()
    rp_svc = ResumePolicyService()

cp_svc = RecordCheckpointService()
rp_svc = ResumePolicyService()
TASK = "订单接入任务A"
NEAR_TASK = "订单接入任务A-备份链路"
POLICY_ID = 4

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (passed if condition else failed).append(f"{name} {detail}")
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")


def ids(rows):
    return [int(r["id"]) for r in rows]


def scenario_a_approve_before_first_read() -> None:
    print("\n=== 场景A：首次读取之前，点120 被批准 ===")
    reset_store()
    # 初始候选：只有已确认且精确归属的 80(归档)/100；120 草稿、300 近名任务都不能进
    eligible = cp_svc.list_eligible(TASK)
    check("A-候选排除草稿", 6 not in ids(eligible), f"候选={ids(eligible)}")
    check("A-候选排除近名任务", 7 not in ids(eligible), f"近名任务点300 id=7 不得混入")
    check("A-近名任务各自独立", ids(cp_svc.list_eligible(NEAR_TASK)) == [7])
    latest = cp_svc.get_latest_checkpoint(TASK)
    check("A-最新可用点为100", int(latest["id"]) == 5 and latest["检查点值"] == "100")

    # 值班员按点100 受理，从101 接手
    accept = rp_svc.accept_resume(POLICY_ID, TASK, "0")
    check("A-受理成功", accept["ok"], accept.get("message", ""))
    acc = accept["acceptance"]
    acc_id = int(acc["id"])
    check("A-冻结起点101", acc["起始Offset"] == 101, f"起始={acc['起始Offset']}")
    check("A-冻结检查点100", acc["接续检查点id"] == 5 and acc["接续版本号"] == 2)

    # 首次读取之前点120 刚被批准
    _, msg = cp_svc.run_action(6, "确认检查点")
    check("A-批准120返回冲突提示", "冲突" in msg and "不回改" in msg, msg)
    cp120 = store.find("record_checkpoint", 6)
    check("A-120获得新版本号", cp120["版本号"] == 3)
    check("A-120凭据状态=冲突候任", cp_svc.get_entry(6)["凭据状态"] == "冲突候任")
    check("A-100凭据状态=在用凭据", cp_svc.get_entry(5)["凭据状态"] == "在用凭据")
    # 在途受理单仍冻结在 100
    frozen = flow.get_acceptance(acc_id)
    check("A-本轮凭据未被回改", frozen["接续检查点id"] == 5 and frozen["起始Offset"] == 101)

    # 两段实读：101-120、121-140，全部归属本受理单、采用点100
    seg1 = rp_svc.read_next_segment(acc_id)
    seg2 = rp_svc.read_next_segment(acc_id)
    check("A-第一段101-120", seg1["segment"]["起始Offset"] == 101 and seg1["segment"]["结束Offset"] == 120)
    check("A-第二段121-140", seg2["segment"]["起始Offset"] == 121 and seg2["segment"]["结束Offset"] == 140)
    check("A-两段均采用点100", all(s["采用检查点id"] == 5 for s in (seg1["segment"], seg2["segment"])))
    check("A-两段同属一个受理单", {s["受理单id"] for s in flow.segments(acc_id)} == {acc_id})
    check("A-101到120有人处理", any(s["起始Offset"] == 101 and s["结束Offset"] == 120 for s in flow.segments(acc_id)))

    # 跨任务换点必须拒绝（点300 属于近名任务）
    cross = rp_svc.handover_checkpoint(acc_id, 7)
    check("A-跨任务换点被拒", not cross["ok"], cross.get("message", ""))

    # 交接换点 120：已读到140，无空缺，后继单自141 起、冻结点120
    ho = rp_svc.handover_checkpoint(acc_id, 6)
    check("A-交接成功", ho["ok"], ho.get("message", ""))
    check("A-旧单标记已交接", flow.get_acceptance(acc_id)["状态"] == "已交接")
    check("A-无兜底空缺", ho["handover"]["兜底区间"] is None)
    succ = ho["successor"]
    check("A-后继单起点141", succ["起始Offset"] == 141, f"起点={succ['起始Offset']}")
    check("A-后继单冻结点120", succ["接续检查点id"] == 6 and succ["接续版本号"] == 3)
    check("A-后继链接双向留痕", flow.get_acceptance(acc_id)["后继受理单"] == int(succ["id"])
          and succ["前置受理单"] == acc_id)

    # 公布本轮实际覆盖区间
    pub = rp_svc.publish_round(acc_id)
    check("A-公布区间101-140", pub["acceptance"]["公布区间"] == [101, 140])
    check("A-公布状态", pub["acceptance"]["状态"] == "已公布")

    # 后继单继续读取，段归后继受理单、采用点120
    seg3 = rp_svc.read_next_segment(int(succ["id"]))
    check("A-后继段141-160", seg3["segment"]["起始Offset"] == 141 and seg3["segment"]["结束Offset"] == 160)
    check("A-后继段采用点120", seg3["segment"]["采用检查点id"] == 6)

    # 归档点100 后仍可反查引用
    cp_svc.run_action(5, "废弃检查点")
    refs = flow.checkpoint_references(5)
    check("A-归档后可反查受理单", acc_id in ids(refs), f"引用={ids(refs)}")


def scenario_b_approve_after_first_segment() -> None:
    print("\n=== 场景B：读完第一段后，点120 才被批准 ===")
    reset_store()
    accept = rp_svc.accept_resume(POLICY_ID, TASK, "0")
    acc_id = int(accept["acceptance"]["id"])
    seg1 = rp_svc.read_next_segment(acc_id)
    check("B-首段101-120归本单", seg1["segment"]["起始Offset"] == 101
          and seg1["segment"]["受理单id"] == acc_id)

    _, msg = cp_svc.run_action(6, "确认检查点")
    check("B-批准120提示与已受理区间冲突", "冲突" in msg, msg)

    # 第二段仍按冻结的点100 读取，不允许跳点
    seg2 = rp_svc.read_next_segment(acc_id)
    check("B-次段121-140仍归本单且用点100",
          seg2["segment"]["起始Offset"] == 121 and seg2["segment"]["采用检查点id"] == 5)
    check("B-两段实读责任都在旧单",
          {s["起始Offset"] for s in flow.segments(acc_id)} == {101, 121})

    # 新批准点125 落在已覆盖区101-140 内：交接无空缺，后继自141 起
    created = cp_svc.create_checkpoint(TASK, "Offset断点", "125")["checkpoint"]
    cp_svc.run_action(int(created["id"]), "确认检查点")
    ho = rp_svc.handover_checkpoint(acc_id, int(created["id"]))
    check("B-交接成功", ho["ok"], ho.get("message", ""))
    check("B-已覆盖新点无兜底空缺", ho["handover"]["兜底区间"] is None)
    check("B-实读101-140责任不移交", ho["handover"]["本轮实读区间"] == [101, 140])
    check("B-下轮起点141", ho["handover"]["下轮起点"] == 141)
    check("B-后继单冻结点125", ho["successor"]["接续检查点值"] == "125")

    # 专项：受理后一段未读即换点 → 受理区间整体成为兜底区间，不能没人负责
    reset_store()
    accept2 = rp_svc.accept_resume(POLICY_ID, TASK, "0")
    acc2_id = int(accept2["acceptance"]["id"])
    cp_svc.run_action(6, "确认检查点")
    ho2 = rp_svc.handover_checkpoint(acc2_id, 6)
    check("B-未实读交接成功", ho2["ok"], ho2.get("message", ""))
    check("B-未实读兜底101-120留痕", ho2["handover"]["兜底区间"] == [101, 120],
          str(ho2["handover"]["兜底区间"]))
    check("B-未实读下轮起点121", ho2["handover"]["下轮起点"] == 121)


def scenario_c_guards_and_traceability() -> None:
    print("\n=== 场景C：候选隔离、草稿拦截、归档反查、凭据状态 ===")
    reset_store()
    # 草稿点不能受理
    draft = cp_svc.create_checkpoint(TASK, "Offset断点", "200")["checkpoint"]
    plan = rp_svc.plan_resume(POLICY_ID, TASK)
    check("C-草稿不影响可用候选", plan["checkpoint"]["检查点值"] == "100")
    # 无可用点的任务受理被拒
    denied = rp_svc.accept_resume(POLICY_ID, "不存在的任务X")
    check("C-无确认点拒绝受理", not denied["ok"], denied.get("message", ""))
    # 草稿点不能换入
    accept = rp_svc.accept_resume(POLICY_ID, TASK)
    acc_id = int(accept["acceptance"]["id"])
    bad_ho = rp_svc.handover_checkpoint(acc_id, int(draft["id"]))
    check("C-草稿点不能换入", not bad_ho["ok"], bad_ho.get("message", ""))
    # 种子受理单 RA-0001 引用的点80 已归档，仍可反查
    refs80 = flow.checkpoint_references(4)
    check("C-归档点80反查历史受理单", [r["受理单号"] for r in refs80] == ["RA-0001"])
    detail = flow.acceptance_detail(refs80[0])
    check("C-历史段81-100可追溯", detail["segments"][0]["起始Offset"] == 81
          and detail["segments"][0]["结束Offset"] == 100)
    check("C-历史段凭据告警留痕", "已过期" in detail["segments"][0].get("凭据告警", ""))
    # 近名任务点300 永远不进任务A 候选
    check("C-近名隔离", all(cp["关联接入任务"] == TASK for cp in cp_svc.list_eligible(TASK)))
    # 重复确认被拒、归档点不能复活
    again, _ = cp_svc.run_action(5, "确认检查点")
    check("C-重复确认拦截", again is None)
    revive, _ = cp_svc.run_action(4, "确认检查点")
    check("C-归档点不能复活", revive is None)
    # 已公布单不能重复公布、已交接单不能继续读
    rp_svc.publish_round(acc_id)
    dup = rp_svc.publish_round(acc_id)
    check("C-重复公布拦截", not dup["ok"])
    read_closed = rp_svc.read_next_segment(acc_id)
    check("C-已公布单停止读取", not read_closed["ok"])


if __name__ == "__main__":
    scenario_a_approve_before_first_read()
    scenario_b_approve_after_first_segment()
    scenario_c_guards_and_traceability()
    print(f"\n合计通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        for item in failed:
            print("FAILED:", item)
        sys.exit(1)
    print("全部场景验证通过")
