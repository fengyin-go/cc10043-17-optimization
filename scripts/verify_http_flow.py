"""HTTP 端到端验证：通过真实 uvicorn 服务走完整路由（含固定路径不被动态路由抢占）。

用法：先启动后端，再执行 python3 -I scripts/verify_http_flow.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8017/api"
passed: list[str] = []
failed: list[str] = []


def call(method: str, path: str, body: dict | None = None, expect_fail: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
            if expect_fail:
                check(f"{method} {path} 应被拒绝", False, str(payload))
            return payload
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        if expect_fail:
            check(f"{method} {path} 已拒绝", True, str(payload.get("detail", ""))[:60])
            return payload
        check(f"{method} {path} 意外失败", False, str(payload))
        return payload


def check(name: str, condition: bool, detail: str = "") -> None:
    (passed if condition else failed).append(name)
    print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")


# 1. 候选接口：草稿120 与近名点300 均不进任务A 候选
eligible = call("GET", "/record_checkpoint/eligible?task=" + urllib.parse.quote("订单接入任务A"))
ids = [int(item["id"]) for item in eligible["items"]]
check("HTTP 候选仅点100", ids == [5], str(ids))
near = call("GET", "/record_checkpoint/eligible?task=" + urllib.parse.quote("订单接入任务A-备份链路"))
check("HTTP 近名任务独立候选点300", [i["id"] for i in near["items"]] == [7])

# 2. 工作台列表带凭据状态与引用计数
listing = call("GET", "/record_checkpoint?size=20")
by_id = {int(row["id"]): row for row in listing["items"]}
check("HTTP 列表含凭据状态", by_id[5].get("凭据状态") == "可用候选", by_id[5].get("凭据状态"))
check("HTTP 草稿标注不可用", by_id[6].get("凭据状态") == "待确认·不可用")
check("HTTP 归档点80 可反查种子受理单", by_id[4].get("引用受理单数") == 1)

# 3. 受理续采（规则4，任务A，分区0）
accept = call("POST", "/resume_policy/accept",
              {"policy_id": 4, "task": "订单接入任务A", "partition": "0"})
acc = accept["acceptance"]
check("HTTP 受理冻结起点101", acc["起始Offset"] == 101)
check("HTTP 受理冻结点100 V2", acc["接续检查点值"] == "100" and acc["接续版本号"] == 2)
acc_id = int(acc["id"])

# 4. 无确认点的任务受理被拒
call("POST", "/resume_policy/accept",
     {"policy_id": 4, "task": "幽灵任务"}, expect_fail=True)

# 5. 首次读取前批准点120
confirm = call("POST", "/record_checkpoint/6/actions", {"action": "确认检查点"})
check("HTTP 批准120返回冲突说明", "冲突" in confirm["message"] and "不回改" in confirm["message"],
      confirm["message"])
# 受理单凭据未回改
acc_view = call("GET", f"/resume_policy/acceptances/{acc_id}")
check("HTTP 受理单仍冻结点100", acc_view["接续检查点值"] == "100" and acc_view["起始Offset"] == 101)

# 6. 两段实读，校验分区数据与归属
seg1 = call("POST", f"/resume_policy/acceptances/{acc_id}/read-segment")
seg2 = call("POST", f"/resume_policy/acceptances/{acc_id}/read-segment")
check("HTTP 段1=101-120 分区0", seg1["segment"]["起始Offset"] == 101
      and seg1["segment"]["结束Offset"] == 120 and seg1["segment"]["分区"] == "0")
check("HTTP 段2=121-140 分区0", seg2["segment"]["起始Offset"] == 121
      and seg2["segment"]["结束Offset"] == 140 and seg2["segment"]["分区"] == "0")
acc_after_reads = call("GET", f"/resume_policy/acceptances/{acc_id}")
check("HTTP 两段均归属同一受理单",
      {s["受理单id"] for s in acc_after_reads["segments"]} == {acc_id})
check("HTTP 每段都记录采用的检查点版本",
      all(s["采用版本号"] == 2 and s["采用检查点id"] == 5 for s in acc_after_reads["segments"]))

# 7. 跨任务换点（点300）被拒
call("POST", f"/resume_policy/acceptances/{acc_id}/handover",
     {"new_checkpoint_id": 7}, expect_fail=True)

# 8. 合法交接点120 → 后继单（前置校验：本单凭据仍冻结在点100）
check("HTTP 交接前凭据仍是点100", acc_view["接续检查点值"] == "100",
      "若失败说明服务内存非种子态，需重启后端再跑")
ho = call("POST", f"/resume_policy/acceptances/{acc_id}/handover",
          {"new_checkpoint_id": 6})
check("HTTP 交接后继起点141", ho["successor"]["起始Offset"] == 141)
check("HTTP 交接后继冻结点120", ho["successor"]["接续检查点值"] == "120")
check("HTTP 交接含责任说明", "责任" in ho["handover"]["责任交接"],
      ho["handover"]["责任交接"][:80])
check("HTTP 交接记录含本轮实读区间", ho["handover"]["本轮实读区间"] == [101, 140])

# 9. 公布本轮
pub = call("POST", f"/resume_policy/acceptances/{acc_id}/publish")
check("HTTP 公布区间101-140", pub["acceptance"]["公布区间"] == [101, 140])
call("POST", f"/resume_policy/acceptances/{acc_id}/publish", body={}, expect_fail=True)

# 10. 任务受理单视图与引用反查
task_view = call("GET", "/resume_policy/tasks/acceptances?task=" + urllib.parse.quote("订单接入任务A"))
check("HTTP 任务视图含全部受理单", task_view["total"] >= 2)
check("HTTP 任务视图带最新候选点", task_view["latest_eligible_checkpoint"]["检查点值"] == "120")
refs = call("GET", "/record_checkpoint/5/references")
check("HTTP 点100 引用反查含本单",
      acc_id in [int(row["id"]) for row in refs["acceptances"]])
# 归档点80 仍能反查种子单
refs80 = call("GET", "/record_checkpoint/4/references")
check("HTTP 归档点80 反查RA-0001",
      [row["受理单号"] for row in refs80["acceptances"]] == ["RA-0001"])
check("HTTP 反查带历史段81-100",
      refs80["acceptances"][0]["segments"][0]["结束Offset"] == 100)

print(f"\nHTTP 验证：通过 {len(passed)} 项，失败 {len(failed)} 项")
if failed:
    sys.exit(1)
print("HTTP 端到端全部通过")
