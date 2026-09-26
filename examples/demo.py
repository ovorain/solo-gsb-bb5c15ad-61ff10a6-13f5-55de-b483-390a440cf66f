"""End-to-end demo using only the Python standard library.

Run with the API already listening on http://127.0.0.1:8000:

    python examples/demo.py

It creates a time-limited session, runs a mini adaptive quiz, demonstrates
idempotent retry with a repeated response_id, shows /next de-duplication,
print session progress and the answer history, and demonstrates the
duration_seconds validation (60..86400).  It then demonstrates content-tag
minimum quotas (``tag_minimums``), including the
``tag_minimums_unfulfillable`` finish reason when a quota cannot be met,
the optional precision stop rule (``target_se`` /
``min_precision_items``, ``finish_reason = precision_reached``), and
finally pause/resume (frozen remaining time, 409 while paused, pending
item and exposures preserved after resume, unlimited-time sessions).
The last section demonstrates item withdrawal
(``PATCH /api/items/{id}/availability``): withdrawn items leave new
selections, a withdrawn pending item is cleared and re-selected by /next
without rolling back its exposure, new answers for it are rejected with
409 while a stored ``response_id`` replay still returns the first result.
It also demonstrates the item revision history: a formal import appends a
version only for a new id or an actual content change, availability flips
append withdraw/re_enable versions, while dry runs, failed batches and
same-value retries create no version; old libraries receive an idempotent
version-1 baseline at first startup.

The frozen-tag section shows that each answer persists the item's
``content_tag`` at answer time and that progress, the /next quota
deficit/endgame restriction and the precision-stop quota gate all count
those frozen tags: retagging an answered item by import (or withdrawing
it) afterwards neither moves the answer between tags, nor makes a quota
look already met, nor ends the session early.

The served-version section shows that the revision served to a session is
frozen on the session at selection time: a formal import changing the
pending item afterwards neither swaps the stem/options returned by repeated
/next calls nor re-judges the answer by the new content (options, correct
answer and IRT parameters all come from the frozen version), a fresh
session is served the new version, and a re-selection after the pending
item is withdrawn binds the replacement's current version.

The frozen-IRT-parameters section shows that every answer freezes the
served revision's difficulty/discrimination onto its response record and
that the standard error is recomputed at the current theta from those
frozen values: a formal import that recalibrates already-answered items
leaves the SE of a session without new answers bit-identical and cannot
spuriously trigger precision_reached, while a fresh answer after a
recalibration freezes the new served parameters.
"""

from __future__ import annotations

import json
import math
import random
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8000"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def expect_http_error(
    method: str, path: str, payload: dict | None = None
) -> None:
    """Show that a request is rejected (used for validation / 409 cases)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        print(f"  -> 被拒绝 HTTP {exc.code}: {json.dumps(body['detail'], ensure_ascii=False)}")
    else:
        raise AssertionError("应当被服务端拒绝")


def show(label: str, obj: object) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    health = call("GET", "/health")
    show("GET /health", health)

    # duration_seconds 必须在 60..86400 之间；越界由 Pydantic 以 422 拒绝。
    expect_http_error(
        "POST", "/api/sessions",
        {"initial_theta": 0.0, "max_items": 8, "duration_seconds": 30},
    )

    # 本场测验限时 600 秒；响应中 duration_seconds 与 UTC expires_at 均会返回。
    session = call(
        "POST", "/api/sessions",
        {"initial_theta": 0.0, "max_items": 8, "exposure_limit": 5,
         "duration_seconds": 600},
    )
    sid = session["id"]
    show("POST /api/sessions", session)
    assert session["duration_seconds"] == 600
    assert session["expires_at"], "设置时限后应返回 UTC 截止时间"
    print(f"  -> 时限 600 秒，UTC 截止时间 expires_at = {session['expires_at']}")
    print("  -> 到期后 /next 与提交答案会返回 409，会话置为 finished(time_expired)")

    rng = random.Random(7)
    theta = session["theta"]

    for turn in range(1, 6):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        item = chosen["item"]
        show(
            f"第 {turn} 题 (POST .../next)",
            {
                "item_id": item["id"],
                "stem": item["stem"],
                "information": round(chosen["information"], 4),
                "exposure_count": chosen["exposure_count"],
            },
        )

        # Re-calling /next before answering must return the SAME item and
        # must not increment exposure again.
        again = call("POST", f"/api/sessions/{sid}/next")
        assert again["item"]["id"] == item["id"], "重复 /next 应返回同一题"
        assert again["exposure_count"] == chosen["exposure_count"], \
            "重复 /next 不应重复计曝光"

        # Simulated examinee using the same 2PL model: answer correctly with
        # probability P(theta), otherwise pick a random distractor key.
        z = item["discrimination"] * (theta - item["difficulty"])
        p = 1 / (1 + math.exp(-z))
        keys = [opt["key"] for opt in item["options"]]
        # The served item hides the answer, but the demo needs to "be right"
        # sometimes: read it from the admin endpoint, else pick a distractor.
        detail = call("GET", f"/api/items/{item['id']}")
        if rng.random() < p:
            answer = detail["answer"]
        else:
            answer = rng.choice([k for k in keys if k != detail["answer"]])
        response_id = f"demo-resp-{turn}-sid-{sid[:8]}"

        result = call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": response_id, "item_id": item["id"], "response": answer},
        )
        theta = result["theta_after"]
        show(
            f"提交作答 (POST .../responses)",
            {
                "response_id": result["id"],
                "item_id": result["item_id"],
                "response": result["response"],
                "correct": result["correct"],
                "theta_before": round(result["theta_before"], 4),
                "theta_after": round(result["theta_after"], 4),
            },
        )

        # Idempotency: replay the exact same request body.
        replay = call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": response_id, "item_id": item["id"], "response": answer},
        )
        assert replay["created_at"] == result["created_at"], "应返回首次记录"
        assert replay["theta_after"] == result["theta_after"], "不应重复更新"
        print(f"  -> 重复 response_id 返回首次结果，能力值未二次更新 ✔")

    progress = call("GET", f"/api/sessions/{sid}/progress")
    show("GET .../progress (会话进度)", {
        "status": progress["status"],
        "theta": round(progress["theta"], 4),
        "answered_count": progress["answered_count"],
        "percent_complete": progress["percent_complete"],
        "duration_seconds": progress["duration_seconds"],
        "expires_at": progress["expires_at"],
        "answered_item_ids": progress["answered_item_ids"],
        "current_item": (progress["current_item"] or {}).get("id"),
    })

    history = call("GET", f"/api/sessions/{sid}/responses")
    show("GET .../responses (答题记录)", {
        "count": history["count"],
        "results": [
            {
                "item_id": r["item_id"],
                "correct": r["correct"],
                "theta_after": round(r["theta_after"], 4),
            }
            for r in history["results"]
        ],
    })

    bank = call("GET", "/api/items?limit=3")
    show("GET /api/items?limit=3 (题库与曝光)", bank)

    demo_tag_minimums()
    demo_precision_stop()
    demo_pause_resume()
    demo_withdrawal()
    demo_frozen_tags()
    demo_served_version()
    demo_frozen_irt_params()
    demo_revisions()


def demo_pause_resume() -> None:
    """Show pause/resume: frozen clock, 409 while paused, state preserved."""
    # --- 限时会话：暂停冻结剩余秒数，恢复时重算 UTC 截止时间 ---
    session = call(
        "POST", "/api/sessions",
        {"max_items": 5, "exposure_limit": 10_000, "duration_seconds": 600},
    )
    sid = session["id"]
    chosen = call("POST", f"/api/sessions/{sid}/next")
    item = chosen["item"]
    print(f"  -> 取到第 1 题 {item['id']}（全局曝光 {chosen['exposure_count']}），暂不作答")

    paused = call("POST", f"/api/sessions/{sid}/pause")
    show("POST .../pause（限时会话）", {
        "status": paused["status"],
        "paused": paused["paused"],
        "paused_at": paused["paused_at"],
        "remaining_seconds": paused["remaining_seconds"],
        "expires_at": paused["expires_at"],
        "current_item_id": paused["current_item_id"],
    })
    assert paused["status"] == "paused" and paused["paused"] is True
    assert 595 <= paused["remaining_seconds"] <= 600
    frozen = paused["remaining_seconds"]

    # 暂停期间取题与新作答都被 409 拒绝；重复暂停也是 409。
    expect_http_error("POST", f"/api/sessions/{sid}/next")
    expect_http_error(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": "demo-pause-new", "item_id": item["id"], "response": "A"},
    )
    expect_http_error("POST", f"/api/sessions/{sid}/pause")
    print("  -> 暂停期间 /next、新作答、重复暂停均返回 409 ✔")

    # 挂起题与剩余秒数在暂停期间保持不变。
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["paused"] is True
    assert progress["current_item"]["id"] == item["id"]
    assert progress["remaining_seconds"] == frozen
    assert call("GET", f"/api/items/{item['id']}")["exposure_count"] == \
        chosen["exposure_count"], "暂停被拒后不应增加曝光"
    print("  -> 暂停查询显示 paused 与冻结的 remaining_seconds，挂起题/曝光不变 ✔")

    resumed = call("POST", f"/api/sessions/{sid}/resume")
    show("POST .../resume（恢复）", {
        "status": resumed["status"],
        "paused": resumed["paused"],
        "paused_at": resumed["paused_at"],
        "remaining_seconds": resumed["remaining_seconds"],
        "expires_at": resumed["expires_at"],
        "current_item_id": resumed["current_item_id"],
    })
    assert resumed["status"] == "active" and resumed["paused"] is False
    assert resumed["paused_at"] is None
    assert resumed["expires_at"] != session["expires_at"], "截止时间应已重算"
    assert resumed["remaining_seconds"] <= frozen
    # 重复恢复 -> 409。
    expect_http_error("POST", f"/api/sessions/{sid}/resume")
    print("  -> 恢复后 expires_at 按冻结剩余秒数重算，重复恢复返回 409 ✔")

    # 恢复后挂起题仍是同一题，重复 /next 不重复计曝光。
    again = call("POST", f"/api/sessions/{sid}/next")
    assert again["item"]["id"] == item["id"]
    assert again["exposure_count"] == chosen["exposure_count"], \
        "恢复后重复取待答题不应再次计曝光"
    print("  -> 恢复后保留原待答题，重复 /next 返回同一题且不增加曝光 ✔")

    # 恢复后可正常作答，已有进度延续。
    detail = call("GET", f"/api/items/{item['id']}")
    answered = call(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": f"demo-pause-{sid[:8]}-1",
         "item_id": item["id"], "response": detail["answer"]},
    )
    assert answered["correct"] is True
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["answered_count"] == 1
    assert progress["answered_item_ids"] == [item["id"]]
    print("  -> 恢复后作答成功，答题记录与进度延续 ✔")

    # --- 无时限会话同样可暂停/恢复，remaining_seconds 恒为 null ---
    untimed = call(
        "POST", "/api/sessions",
        {"max_items": 3, "exposure_limit": 10_000},
    )
    assert untimed["expires_at"] is None
    p = call("POST", f"/api/sessions/{untimed['id']}/pause")
    assert p["status"] == "paused" and p["remaining_seconds"] is None
    expect_http_error("POST", f"/api/sessions/{untimed['id']}/next")
    r = call("POST", f"/api/sessions/{untimed['id']}/resume")
    assert r["status"] == "active" and r["remaining_seconds"] is None
    assert call("POST", f"/api/sessions/{untimed['id']}/next")["item"]
    print("  -> 无时限会话也能暂停/恢复，remaining_seconds 始终为 null ✔")

    # --- 已结束会话不能暂停或恢复（均 409）；到期会话也不能靠暂停续命 ---
    one = call(
        "POST", "/api/sessions",
        {"max_items": 1, "exposure_limit": 10_000},
    )
    oid = one["id"]
    oi = call("POST", f"/api/sessions/{oid}/next")["item"]
    od = call("GET", f"/api/items/{oi['id']}")
    call(
        "POST", f"/api/sessions/{oid}/responses",
        {"response_id": f"demo-pause-done-{oid[:8]}",
         "item_id": oi["id"], "response": od["answer"]},
    )
    assert call("GET", f"/api/sessions/{oid}")["status"] == "finished"
    expect_http_error("POST", f"/api/sessions/{oid}/pause")
    expect_http_error("POST", f"/api/sessions/{oid}/resume")
    print("  -> 已结束会话暂停/恢复均返回 409 ✔")
    print("  -> 到期会话调用暂停会与 /next、/responses 一样被直接结束为 "
          "time_expired（409），无法借暂停续命（详见 README）")



def demo_precision_stop() -> None:
    """Show target_se/min_precision_items and the precision_reached finish."""
    # 校验：两个参数必须成对出现；target_se 须为正有限数；
    # min_precision_items 不能大于 max_items -> 均返回 422。
    expect_http_error(
        "POST", "/api/sessions", {"target_se": 0.5},
    )
    expect_http_error(
        "POST", "/api/sessions", {"min_precision_items": 2},
    )
    expect_http_error(
        "POST", "/api/sessions",
        {"target_se": 0, "min_precision_items": 2},
    )
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "target_se": 0.5, "min_precision_items": 4},
    )

    # 目标标准误 0.8、至少作答 3 题后才允许触发精度停测。
    # exposure_limit 放大到 10000：避免与本场其他演示共享的全局曝光计数
    # 影响选题，让本段每次运行的轨迹都一致。
    session = call(
        "POST", "/api/sessions",
        {"max_items": 12, "exposure_limit": 10_000,
         "target_se": 0.8, "min_precision_items": 3},
    )
    sid = session["id"]
    assert session["target_se"] == 0.8
    assert session["min_precision_items"] == 3
    assert session["standard_error"] is None
    print("  -> 创建精度停测会话：target_se=0.8，min_precision_items=3")

    # 模拟一个真实能力固定在 theta=0 的考生：答对概率始终按 theta=0 计算，
    # 这样 θ 估计会在 0 附近波动，标准误会随作答题数稳定下降。
    rng = random.Random(123)
    theta_true = 0.0
    trace: list[dict] = []
    for turn in range(1, 13):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        assert chosen["target_se"] == 0.8
        assert chosen["min_precision_items"] == 3
        if chosen["item"] is None:
            break
        item = chosen["item"]
        detail = call("GET", f"/api/items/{item['id']}")
        keys = [opt["key"] for opt in item["options"]]
        z = item["discrimination"] * (theta_true - item["difficulty"])
        p = 1 / (1 + math.exp(-z))
        answer = (
            detail["answer"]
            if rng.random() < p
            else rng.choice([k for k in keys if k != detail["answer"]])
        )
        call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": f"demo-se-{turn}-sid-{sid[:8]}",
             "item_id": item["id"], "response": answer},
        )
        progress = call("GET", f"/api/sessions/{sid}/progress")
        trace.append({
            "turn": turn,
            "answered": progress["answered_count"],
            "standard_error": (
                round(progress["standard_error"], 4)
                if progress["standard_error"] is not None else None
            ),
            "status": progress["status"],
            "finish_reason": progress["finish_reason"],
        })
        if progress["status"] == "finished":
            assert progress["finish_reason"] == "precision_reached"
            assert progress["answered_count"] >= 3
            assert progress["standard_error"] <= 0.8 + 1e-12
            break
    else:  # pragma: no cover - the target is deliberately easy to reach
        raise AssertionError("精度停测应当被触发")

    show("精度停测（SE 轨迹，到达 0.8 且已答 >=3 题即停）", {
        "target_se": 0.8,
        "min_precision_items": 3,
        "trace": trace,
    })
    n_stopped = trace[-1]["answered"]
    print(
        f"  -> 第 {n_stopped} 题作答后 SE={trace[-1]['standard_error']} "
        "<= 0.8，会话以 precision_reached 结束 ✔"
    )

    # 重复提交最后一次 response_id：回放首次结果，不重复触发状态变化。
    history = call("GET", f"/api/sessions/{sid}/responses")
    last = history["results"][-1]
    replay = call(
        "POST", f"/api/sessions/{sid}/responses",
        {"response_id": last["id"], "item_id": last["item_id"],
         "response": last["response"]},
    )
    assert replay["created_at"] == last["created_at"]
    progress = call("GET", f"/api/sessions/{sid}/progress")
    assert progress["status"] == "finished"
    assert progress["finish_reason"] == "precision_reached"
    assert progress["answered_count"] == n_stopped
    print("  -> 重复 response_id 回放，状态与已答数不变 ✔")


def demo_tag_minimums() -> None:
    """Show tag_minimums: restricted endgame and the unfulfillable finish."""
    # 校验：配额之和不能超过 max_items -> 422
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "tag_minimums": {"算术": 2, "代数": 2}},
    )
    # 校验：键必须是题库现有非空 content_tag -> 422
    expect_http_error(
        "POST", "/api/sessions",
        {"max_items": 3, "tag_minimums": {"不存在的标签": 1}},
    )

    # 6 道题全部要求来自「数论」：剩余答题位始终等于缺口总数，
    # 因此每题都必须在欠额标签内按信息量/ID 规则选出。
    session = call(
        "POST", "/api/sessions",
        {"max_items": 6, "exposure_limit": 50, "tag_minimums": {"数论": 6}},
    )
    sid = session["id"]
    assert session["tag_minimums"] == {"数论": 6}
    tags: list[str] = []
    for turn in range(1, 7):
        chosen = call("POST", f"/api/sessions/{sid}/next")
        item = chosen["item"]
        tags.append(item["content_tag"])
        detail = call("GET", f"/api/items/{item['id']}")
        call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": f"demo-tag-{sid[:8]}-{turn}",
             "item_id": item["id"], "response": detail["answer"]},
        )
    progress = call("GET", f"/api/sessions/{sid}/progress")
    show("tag_minimums 配额会话（数论 x6）", {
        "served_tags": tags,
        "status": progress["status"],
        "finish_reason": progress["finish_reason"],
        "tag_minimums": progress["tag_minimums"],
        "tag_answered": progress["tag_answered"],
    })
    assert all(t == "数论" for t in tags), tags
    assert progress["finish_reason"] == "max_items"

    # 不可补齐：要求 1 道「概率统计」题（exposure_limit=1）。随后用若干
    # 一次性配额会话把该标签题全部曝光到上限；若本库此前已跑过演示，第一
    # 个消耗会话就会直接结束，循环同样终止（重复运行结果稳定）。
    quota_session = call(
        "POST", "/api/sessions",
        {"max_items": 3, "exposure_limit": 1,
         "tag_minimums": {"概率统计": 1}},
    )
    while True:
        burner = call(
            "POST", "/api/sessions",
            {"max_items": 1, "exposure_limit": 1,
             "tag_minimums": {"概率统计": 1}},
        )
        picked = call("POST", f"/api/sessions/{burner['id']}/next")
        if picked["item"] is None:
            assert picked["finish_reason"] == "tag_minimums_unfulfillable"
            break
        assert picked["item"]["content_tag"] == "概率统计"
    done = call("POST", f"/api/sessions/{quota_session['id']}/next")
    show("欠额标签已无可用题 -> 会话结束", {
        "status": done["status"],
        "finish_reason": done["finish_reason"],
        "item": done["item"],
        "note": done["note"],
    })
    assert done["status"] == "finished"
    assert done["finish_reason"] == "tag_minimums_unfulfillable"
    assert done["item"] is None, "结束时不额外选题/曝光"
    print("  -> 欠额标签题数不足时以 tag_minimums_unfulfillable 结束，未增加曝光 ✔")


def demo_withdrawal() -> None:
    """Show item withdrawal: PATCH availability, pending re-select, 409."""
    # 三道专用演示题：区分度远高于演示题库（a<=1.8），在 theta=0 的新会话里
    # 必然按 DEMO-WD-1 -> DEMO-WD-2 -> DEMO-WD-3 的顺序被选中，结果确定。
    # 导入为幂等 upsert：重复运行只更新题干参数，保留曝光数与启用状态。
    def wd_item(iid: str, discrimination: float) -> dict:
        return {
            "id": iid, "difficulty": 0.0, "discrimination": discrimination,
            "content_tag": "撤回演示", "stem": f"撤回演示题 {iid}：1 + 1 = ?",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": "A",
        }

    imported = call(
        "POST", "/api/items/import",
        {"items": [wd_item("DEMO-WD-1", 5.0), wd_item("DEMO-WD-2", 4.9),
                   wd_item("DEMO-WD-3", 4.8)]},
    )
    assert imported["valid"] is True
    # 重复运行时上一轮可能把演示题留在了撤回状态：先统一启用（演示启用路径）。
    for iid in ("DEMO-WD-1", "DEMO-WD-2", "DEMO-WD-3"):
        enabled = call("PATCH", f"/api/items/{iid}/availability",
                       {"is_active": True})
        assert enabled["is_active"] is True
    print("  -> 导入 3 道演示题并全部启用（重复导入保留启用状态与曝光数）✔")

    # 未知题目 PATCH -> 404
    expect_http_error(
        "PATCH", "/api/items/NO-SUCH-ITEM/availability", {"is_active": False},
    )

    # --- 已作答题目被撤回：已保存的 response_id 重放仍返回首次结果 ---
    sa = call("POST", "/api/sessions",
              {"max_items": 5, "exposure_limit": 10_000})
    sida = sa["id"]
    first = call("POST", f"/api/sessions/{sida}/next")
    assert first["item"]["id"] == "DEMO-WD-1"
    ans = call(
        "POST", f"/api/sessions/{sida}/responses",
        {"response_id": f"demo-wd-{sida[:8]}-1",
         "item_id": "DEMO-WD-1", "response": "A"},
    )
    withdrawn = call("PATCH", "/api/items/DEMO-WD-1/availability",
                     {"is_active": False})
    assert withdrawn["is_active"] is False
    replay = call(
        "POST", f"/api/sessions/{sida}/responses",
        {"response_id": f"demo-wd-{sida[:8]}-1",
         "item_id": "DEMO-WD-1", "response": "A"},
    )
    assert replay["created_at"] == ans["created_at"]
    assert replay["theta_after"] == ans["theta_after"]
    print("  -> 作答后撤回题目：已保存 response_id 重放仍返回首次结果（200）✔")

    # --- 挂起题被撤回：新作答 409；/next 同事务清除挂起项并重选，曝光不回退 ---
    sb = call("POST", "/api/sessions",
              {"max_items": 5, "exposure_limit": 10_000})
    sidb = sb["id"]
    pending = call("POST", f"/api/sessions/{sidb}/next")
    assert pending["item"]["id"] == "DEMO-WD-2"
    exposure_at_select = pending["exposure_count"]
    call("PATCH", "/api/items/DEMO-WD-2/availability", {"is_active": False})

    # 对已撤回题提交新作答 -> 409，不写答题记录、不更新能力
    expect_http_error(
        "POST", f"/api/sessions/{sidb}/responses",
        {"response_id": f"demo-wd-{sidb[:8]}-new",
         "item_id": "DEMO-WD-2", "response": "A"},
    )
    history = call("GET", f"/api/sessions/{sidb}/responses")
    assert history["count"] == 0, "撤回题的新作答不能写入答题记录"
    assert call("GET", f"/api/sessions/{sidb}")["theta"] == 0.0, "能力值不应更新"
    print("  -> 对撤回题提交新作答返回 409，未写答题记录、能力值不变 ✔")

    reselected = call("POST", f"/api/sessions/{sidb}/next")
    show("挂起题撤回后再次 /next（同事务清除挂起项并重选）", {
        "item": reselected["item"]["id"],
        "note": reselected["note"],
    })
    assert reselected["item"]["id"] == "DEMO-WD-3"
    assert "DEMO-WD-2" in (reselected["note"] or "")
    after = call("GET", "/api/items/DEMO-WD-2")
    assert after["exposure_count"] == exposure_at_select, \
        "撤回题已计曝光不应回退"
    print(f"  -> 重选出 DEMO-WD-3；DEMO-WD-2 曝光保持 {exposure_at_select} 不回退 ✔")

    # 收尾：把演示题留在撤回状态，保证重复运行本脚本时其他演示段落不受影响。
    for iid in ("DEMO-WD-1", "DEMO-WD-2", "DEMO-WD-3"):
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    bank = call("GET", "/api/items?limit=100")
    demo_rows = {i["id"]: i["is_active"] for i in bank["items"]
                 if i["id"].startswith("DEMO-WD-")}
    show("题库列表返回启用状态（演示题已重新撤回）", demo_rows)
    assert all(v is False for v in demo_rows.values())
    print("  -> 题库列表/详情均返回 is_active；演示题已撤回，不影响后续演示 ✔")


def call_status(method: str, path: str, payload: dict | None = None):
    """Like call() but returns (status_code, body) without raising."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def demo_frozen_tags() -> None:
    """Show answers freeze content_tag; later retags/withdrawals can't rewrite quotas."""
    print("\n--- 作答时冻结 content_tag：导入改标签 / 撤回不追溯改写配额 ---")

    # fz_item is the b=0, high-discrimination item used by group A.
    def fz_item(iid: str, discrimination: float, tag: str) -> dict:
        return {
            "id": iid, "difficulty": 0.0, "discrimination": discrimination,
            "content_tag": tag, "stem": f"冻结标签演示题 {iid}：1 + 1 = ?",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": "A",
        }

    # Groups B and C use b aligned with the all-correct theta trajectory
    # (0,1,2,3 at a=2): each next item is exactly maximally informative at
    # the theta reached after the previous answer, so a session deterministically
    # serves the four ids in order regardless of the (a<=1.8) demo bank.
    def track_item(iid: str, difficulty: float, tag: str) -> dict:
        return {
            "id": iid, "difficulty": difficulty, "discrimination": 2.0,
            "content_tag": tag, "stem": f"冻结标签演示题 {iid}（b={difficulty:g}）",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": "A",
        }

    # Dedicated items per scenario.  Exposure counts are global and persist
    # across sessions (and demo reruns), so the scenarios never share ids.
    # Groups not yet used are withdrawn before each scenario, isolating its
    # candidate pool and making the (information, id) picks reproducible:
    # scenario 1 uses the b=0 group A; scenarios 2/3 use the theta-tracking
    # groups B and C respectively.
    fz_specs_a = [
        ("DEMO-FZA-1", 5.0, "冻结甲"),
        ("DEMO-FZA-2", 4.9, "冻结甲"),
        ("DEMO-FZA-3", 4.8, "冻结乙"),
        ("DEMO-FZA-4", 4.7, "冻结乙"),
    ]
    # (id, b, tag); discrimination is fixed at 2.0 by track_item.
    fz_specs_b = [
        ("DEMO-FZB-1", 0.0, "冻结甲"),
        ("DEMO-FZB-2", 1.0, "冻结甲"),
        ("DEMO-FZB-3", 2.0, "冻结乙"),
        ("DEMO-FZB-4", 3.0, "冻结乙"),
    ]
    fz_specs_c = [
        ("DEMO-FZC-1", 0.0, "冻结甲"),
        ("DEMO-FZC-2", 1.0, "冻结乙"),
        ("DEMO-FZC-3", 2.0, "冻结乙"),
        ("DEMO-FZC-4", 3.0, "冻结乙"),
    ]
    imported_a = call(
        "POST", "/api/items/import",
        {"items": [fz_item(iid, a, tag) for iid, a, tag in fz_specs_a]},
    )
    imported_bc = call(
        "POST", "/api/items/import",
        {"items": [track_item(iid, b, tag)
                   for specs in (fz_specs_b, fz_specs_c)
                   for iid, b, tag in specs]},
    )
    assert imported_a["valid"] and imported_bc["valid"]
    # Repeating the demo may leave items in any state from an earlier run:
    # enable every item first, each scenario then withdraws the groups it
    # does not use.
    for specs in (fz_specs_a, fz_specs_b):
        for iid, _, _ in specs:
            assert call("PATCH", f"/api/items/{iid}/availability",
                        {"is_active": True})["is_active"] is True
    for iid, _, _ in fz_specs_c:
        assert call("PATCH", f"/api/items/{iid}/availability",
                    {"is_active": True})["is_active"] is True

    def answer_correct(sid: str, response_id: str, item: dict) -> dict:
        detail = call("GET", f"/api/items/{item['id']}")
        return call(
            "POST", f"/api/sessions/{sid}/responses",
            {"response_id": response_id,
             "item_id": item["id"], "response": detail["answer"]},
        )

    # 场景 1/2 不使用 group C：先整组撤回，隔离候选题池。
    for iid, _, _ in fz_specs_c:
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})

    # --- 场景 1：作答后改标签 + 撤回，已答归属不变、配额不会被误判已满足 ---
    # max_items=2、配额 甲1/乙1：答完第 1 题后剩余答题位==缺口总数（1），
    # 下一题必然走「只从欠额标签选题」的末段限制。
    s1 = call("POST", "/api/sessions",
              {"max_items": 2, "exposure_limit": 10_000,
               "tag_minimums": {"冻结甲": 1, "冻结乙": 1}})
    s1id = s1["id"]
    first = call("POST", f"/api/sessions/{s1id}/next")["item"]
    assert first["id"] == "DEMO-FZA-1"
    r1 = answer_correct(s1id, f"demo-fz-{s1id[:8]}-1", first)
    assert r1["content_tag"] == "冻结甲", "提交响应应持久化作答时的标签"

    # 事后把这道已答题的标签从「冻结甲」改成「冻结乙」（导入更新）。
    call("POST", "/api/items/import",
         {"items": [fz_item("DEMO-FZA-1", 5.0, "冻结乙")]})
    progress = call("GET", f"/api/sessions/{s1id}/progress")
    history = call("GET", f"/api/sessions/{s1id}/responses")
    frozen = history["results"][0]
    assert frozen["content_tag"] == "冻结甲", "已答记录必须保留冻结标签"
    assert progress["tag_answered"].get("冻结甲") == 1, \
        "进度的已答归属不得随题库改标签"
    assert progress["tag_answered"].get("冻结乙", 0) == 0
    show("作答后导入改标签（冻结甲->冻结乙）：归属不被追溯改写", {
        "题库当前标签": call("GET", "/api/items/DEMO-FZA-1")["content_tag"],
        "答题记录冻结标签": frozen["content_tag"],
        "tag_answered": progress["tag_answered"],
        "tag_minimums": progress["tag_minimums"],
    })
    print("  -> 题库标签已改，但答题记录与进度仍按冻结的「冻结甲」统计 ✔")

    # 再撤回该已答题：欠额仍为「冻结乙」1 道（FZA-3/FZA-4 可用），末段
    # 只能从「冻结乙」选题 -> 选 FZA-3；不得因撤回而误判配额已满足或提前结束。
    call("PATCH", "/api/items/DEMO-FZA-1/availability", {"is_active": False})
    second = call("POST", f"/api/sessions/{s1id}/next")
    assert second["item"] is not None and second["status"] == "active", \
        "撤回已答题不得导致配额无法补齐而提前结束"
    assert second["item"]["id"] == "DEMO-FZA-3", \
        "剩余答题位 == 缺口总数时只能从欠额标签「冻结乙」选题"
    r2 = answer_correct(s1id, f"demo-fz-{s1id[:8]}-2", second["item"])
    assert r2["content_tag"] == "冻结乙"
    progress = call("GET", f"/api/sessions/{s1id}/progress")
    assert progress["status"] == "finished"
    assert progress["finish_reason"] == "max_items"
    assert progress["tag_answered"] == {"冻结甲": 1, "冻结乙": 1}
    print("  -> 撤回已答题后欠额仍按冻结标签统计，末段只从「冻结乙」选出 "
          "DEMO-FZA-3，未提前结束且配额恰好补齐 ✔")
    # 幂等重放：改标签/撤回后重放仍返回首次结果（含冻结标签）。
    replay = call(
        "POST", f"/api/sessions/{s1id}/responses",
        {"response_id": f"demo-fz-{s1id[:8]}-1",
         "item_id": "DEMO-FZA-1", "response": "A"},
    )
    assert replay["created_at"] == r1["created_at"]
    assert replay["content_tag"] == "冻结甲"
    print("  -> 改标签并撤回后，已存 response_id 重放仍返回首次结果 ✔")

    # --- 场景 2：末段欠额限制按冻结标签，改标签后仍只从欠额标签选题 ---
    # 场景 1 的题目全部撤回（含已答的 FZA-1），确保本组按信息量排序被选中。
    for iid, _, _ in fz_specs_a:
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    # 配额「冻结乙」2 道。前两题（全局信息量最高的 DEMO-FZB-1/FZB-2）
    # 作答时标签都是「冻结甲」，故缺口一直是 2；答完 2 题后剩余答题位 ==
    # 缺口总数，此后只能出「冻结乙」题。
    s2 = call("POST", "/api/sessions",
              {"max_items": 4, "exposure_limit": 10_000,
               "tag_minimums": {"冻结乙": 2}})
    s2id = s2["id"]
    served: list[str] = []
    for turn in (1, 2):
        chosen = call("POST", f"/api/sessions/{s2id}/next")
        served.append(chosen["item"]["id"])
        answer_correct(s2id, f"demo-fz2-{s2id[:8]}-{turn}", chosen["item"])
    assert served == ["DEMO-FZB-1", "DEMO-FZB-2"], served

    # 两道已答题冻结标签都是「冻结甲」，缺口=2、剩余位=2 -> 末段只能出
    # 「冻结乙」。此时把已答的 FZB-1 事后改成「冻结乙」也不得改变已答
    # 归属（冻结的是「冻结甲」）。
    call("POST", "/api/items/import",
         {"items": [track_item("DEMO-FZB-1", 0.0, "冻结乙")]})
    progress = call("GET", f"/api/sessions/{s2id}/progress")
    assert progress["tag_answered"].get("冻结乙", 0) == 0, \
        "事后改标签不得让「冻结乙」配额看似已满足"
    assert progress["tag_answered"].get("冻结甲") == 2
    third = call("POST", f"/api/sessions/{s2id}/next")["item"]
    assert third is not None and third["id"] == "DEMO-FZB-3", \
        "FZB-1 虽被改标为「冻结乙」，但它已作答；欠额末段应选出 FZB-3"
    answer_correct(s2id, f"demo-fz2-{s2id[:8]}-3", third)
    fourth = call("POST", f"/api/sessions/{s2id}/next")["item"]
    assert fourth is not None and fourth["id"] == "DEMO-FZB-4"
    answer_correct(s2id, f"demo-fz2-{s2id[:8]}-4", fourth)
    progress = call("GET", f"/api/sessions/{s2id}/progress")
    assert progress["status"] == "finished"
    assert progress["finish_reason"] == "max_items"
    assert progress["tag_answered"]["冻结乙"] == 2
    print("  -> 事后把已答题改成欠额标签：配额未被误判满足，末段仍只从"
          "欠额标签选出 FZB-3/FZB-4 ✔")

    # --- 场景 3：精度停测的配额门槛按冻结标签，不会因改标签提前停测 ---
    # 场景 2（以及场景 1）的题目全部撤回，启用 group C 并隔离候选题池。
    for specs in (fz_specs_a, fz_specs_b):
        for iid, _, _ in specs:
            call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    for iid, _, _ in fz_specs_c:
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": True})
    # 本组 b 沿全对轨迹 0,1,2,3 排列（a=2），会话必然依次取 FZC-1..4。
    # target_se 取很大（5.0）、min_precision_items=1：第一题作答后 SE 就已
    # 远低于阈值，于是「标签配额是否满足」成为停测的唯一决定因素
    # （本组每题 SE 恒远小于 5，结果可复现）。
    s3 = call("POST", "/api/sessions",
              {"max_items": 8, "exposure_limit": 10_000,
               "target_se": 5.0, "min_precision_items": 1,
               "tag_minimums": {"冻结乙": 2}})
    s3id = s3["id"]
    # 答第 1 题（FZC-1，冻结甲）后，把它改成「冻结乙」：旧的按题库当前
    # 标签统计的逻辑会把这道已答题误计入「冻结乙」。
    c1 = call("POST", f"/api/sessions/{s3id}/next")["item"]
    assert c1["id"] == "DEMO-FZC-1"
    answer_correct(s3id, f"demo-fz3-{s3id[:8]}-1", c1)
    call("POST", "/api/items/import",
         {"items": [track_item("DEMO-FZC-1", 0.0, "冻结乙")]})
    # 第 2 题 FZC-2（冻结乙）：作答后 SE 已达标、题数 >=1。旧逻辑会把
    # 「冻结乙」算作 改标的 FZC-1 + 真乙 FZC-2 = 2，误判配额已满足而在
    # 第 2 题后提前 precision_reached；冻结标签下只有 FZC-2 一道（1/2），
    # 必须继续。
    c2 = call("POST", f"/api/sessions/{s3id}/next")
    assert c2["item"] is not None and c2["item"]["id"] == "DEMO-FZC-2"
    answer_correct(s3id, f"demo-fz3-{s3id[:8]}-2", c2["item"])
    progress = call("GET", f"/api/sessions/{s3id}/progress")
    assert progress["status"] == "active", \
        "冻结标签下「冻结乙」仅 FZC-2 一道（1/2），第 2 题后不得提前停测"
    assert progress["tag_answered"].get("冻结乙") == 1
    assert progress["tag_answered"].get("冻结甲") == 1
    # 第 3 题 FZC-3（冻结乙）：这是真正补齐配额 2/2 的一题。注意它被
    # 选中本身说明第 2 题后会话未被旧 bug 提前结束；作答后 SE 达标且
    # 冻结标签下配额满足 -> precision_reached。
    c3 = call("POST", f"/api/sessions/{s3id}/next")
    assert c3["item"] is not None and c3["item"]["id"] == "DEMO-FZC-3"
    answer_correct(s3id, f"demo-fz3-{s3id[:8]}-3", c3["item"])
    progress = call("GET", f"/api/sessions/{s3id}/progress")
    assert progress["status"] == "finished", \
        "第 3 题作答后冻结标签下「冻结乙」=FZC-2+FZC-3=2，配额真正满足，应停测"
    assert progress["finish_reason"] == "precision_reached"
    assert progress["tag_answered"] == {"冻结甲": 1, "冻结乙": 2}
    assert progress["answered_count"] == 3
    print("  -> 精度停测的配额门槛按冻结标签：改标 FZC-1 不会让第 2 题后"
          "配额看似满足（旧逻辑会在此误判提前停测），真正的第二道"
          "「冻结乙」FZC-3 作答后才 precision_reached ✔")


def demo_served_version() -> None:
    """Show the served revision is frozen on the session at selection time.

    A formal import that changes the *pending* item afterwards must not
    re-judge the in-flight answer by the new content: repeated /next calls
    keep returning the delivered stem/options, and the answer is validated
    and scored (options, correct answer, IRT parameters) against the frozen
    version.  A fresh session is served the new version, and the
    re-selection after a pending-item withdrawal binds the replacement's
    current version.
    """
    print("\n--- 取题版本冻结：取题后导入改题，在途作答仍按送达版本判分 ---")

    # 两道专用演示题：DEMO-SV-1（a=5.0）与 DEMO-SV-2（a=4.0）在 theta=0 的
    # 新会话里信息量（6.25 / 4.0）远高于演示题库与其余演示遗留题（<=1.0），
    # 选题结果确定；重复运行只更新内容，曝光数与启用状态由导入保留。
    sv1_v1 = {
        "id": "DEMO-SV-1", "difficulty": 0.0, "discrimination": 5.0,
        "content_tag": "版本演示", "stem": "版本演示题 v1：1 + 1 = ?",
        "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
        "answer": "A",
    }
    sv1_v2 = {
        "id": "DEMO-SV-1", "difficulty": 0.2, "discrimination": 5.0,
        "content_tag": "版本演示", "stem": "版本演示题 v2：2 + 2 = ?",
        "options": [{"key": "A", "text": "3"}, {"key": "B", "text": "4"}],
        "answer": "B",
    }
    sv2_v1 = {
        "id": "DEMO-SV-2", "difficulty": 0.0, "discrimination": 4.0,
        "content_tag": "版本演示", "stem": "版本演示题 DEMO-SV-2：3 + 3 = ?",
        "options": [{"key": "A", "text": "5"}, {"key": "B", "text": "6"}],
        "answer": "B",
    }
    sv2_v2 = {
        "id": "DEMO-SV-2", "difficulty": 0.3, "discrimination": 4.0,
        "content_tag": "版本演示", "stem": "版本演示题 DEMO-SV-2 v2：4 + 4 = ?",
        "options": [{"key": "A", "text": "8"}, {"key": "B", "text": "7"}],
        "answer": "A",
    }
    imported = call(
        "POST", "/api/items/import", {"items": [sv1_v1, sv2_v1]},
    )
    assert imported["valid"] is True
    # 重复运行时上一轮把演示题留在了撤回状态：先统一启用。
    for iid in ("DEMO-SV-1", "DEMO-SV-2"):
        assert call("PATCH", f"/api/items/{iid}/availability",
                    {"is_active": True})["is_active"] is True

    # --- 取题后正式导入改题：/next 仍返回送达版本，作答按送达版本判分 ---
    s1 = call("POST", "/api/sessions",
              {"max_items": 3, "exposure_limit": 10_000})
    s1id = s1["id"]
    chosen = call("POST", f"/api/sessions/{s1id}/next")
    assert chosen["item"]["id"] == "DEMO-SV-1"
    assert chosen["item"]["stem"] == sv1_v1["stem"]
    assert chosen["item"]["options"] == sv1_v1["options"]
    exposure_at_select = chosen["exposure_count"]
    print(f"  -> 取到 DEMO-SV-1（v1：答案 A，曝光 {exposure_at_select}），暂不作答")

    # 正式导入把 DEMO-SV-1 改成 v2：题干/选项/答案/难度全部变化。
    call("POST", "/api/items/import", {"items": [sv1_v2]})
    bank_now = call("GET", "/api/items/DEMO-SV-1")
    assert bank_now["stem"] == sv1_v2["stem"] and bank_now["answer"] == "B"
    print("  -> 正式导入把 DEMO-SV-1 改为 v2（题库当前答案已变为 B）✔")

    # 重复 /next 仍返回送达时的 v1 题干/选项，不重复计曝光，信息量按 v1 参数。
    again = call("POST", f"/api/sessions/{s1id}/next")
    assert again["item"]["stem"] == sv1_v1["stem"]
    assert again["item"]["options"] == sv1_v1["options"]
    assert again["exposure_count"] == exposure_at_select
    assert abs(again["information"] - 6.25) < 1e-9  # v1: a=5.0, b=0.0
    progress = call("GET", f"/api/sessions/{s1id}/progress")
    assert progress["current_item"]["stem"] == sv1_v1["stem"]
    show("题库已更新为 v2，重复 /next 仍返回送达的 v1 内容", {
        "题库当前题干": bank_now["stem"],
        "重复 /next 返回题干": again["item"]["stem"],
        "重复 /next 返回选项": again["item"]["options"],
        "曝光不变": again["exposure_count"] == exposure_at_select,
    })
    print("  -> 重复 /next 与进度查询均返回送达版本，不重复计曝光 ✔")

    # 按 v1 作答「A」（v1 正确答案）：必须按送达版本判对——若误按题库当前
    # v2 判分，A 会被判错（v2 答案为 B），且 θ 会按 v2 参数更新。
    result = call(
        "POST", f"/api/sessions/{s1id}/responses",
        {"response_id": f"demo-sv-{s1id[:8]}-1",
         "item_id": "DEMO-SV-1", "response": "A"},
    )
    assert result["correct"] is True and result["score"] == 1
    assert result["correct_answer"] == "A", "应按送达版本 v1 的答案判分"
    assert abs(result["p_correct"] - 0.5) < 1e-9       # v1 参数 a=5,b=0
    assert abs(result["theta_after"] - 0.4) < 1e-9     # 0 + 5*0.5/6.25
    assert result["content_tag"] == "版本演示"
    session_now = call("GET", f"/api/sessions/{s1id}")
    assert abs(session_now["theta"] - result["theta_after"]) < 1e-12, \
        "保存的作答结果须与能力更新一致"
    history = call("GET", f"/api/sessions/{s1id}/responses")
    assert history["results"][0]["correct_answer"] == "A"
    assert history["results"][0]["score"] == 1
    show("按送达版本判分（题库当前答案已是 B）", {
        "提交": "A（v1 的正确答案）",
        "correct": result["correct"],
        "correct_answer": result["correct_answer"],
        "p_correct": result["p_correct"],
        "theta_after": round(result["theta_after"], 4),
    })
    print("  -> 按送达版本 v1 判对：correct_answer=A、θ 按 v1 参数更新，"
          "记录与能力一致 ✔")

    # 已保存 response_id 的重放仍返回首次结果（HTTP 200）。
    status, replay = call_status(
        "POST", f"/api/sessions/{s1id}/responses",
        {"response_id": f"demo-sv-{s1id[:8]}-1",
         "item_id": "DEMO-SV-1", "response": "A"},
    )
    assert status == 200
    assert replay["created_at"] == result["created_at"]
    assert replay["theta_after"] == result["theta_after"]
    print("  -> 已存 response_id 重放仍返回首次结果（200）✔")

    # --- 新取题使用新版本：另一会话取到 v2 并按 v2 判分 ---
    s2 = call("POST", "/api/sessions",
              {"max_items": 3, "exposure_limit": 10_000})
    s2id = s2["id"]
    chosen2 = call("POST", f"/api/sessions/{s2id}/next")
    assert chosen2["item"]["id"] == "DEMO-SV-1"
    assert chosen2["item"]["stem"] == sv1_v2["stem"]
    assert chosen2["item"]["options"] == sv1_v2["options"]
    result2 = call(
        "POST", f"/api/sessions/{s2id}/responses",
        {"response_id": f"demo-sv-{s2id[:8]}-1",
         "item_id": "DEMO-SV-1", "response": "B"},
    )
    assert result2["correct"] is True and result2["correct_answer"] == "B"
    print("  -> 新会话取到 v2 内容并按 v2 判分（答案 B 判对）✔")

    # --- 挂起题撤回后的重选改关联新题的当前版本 ---
    s3 = call("POST", "/api/sessions",
              {"max_items": 3, "exposure_limit": 10_000})
    s3id = s3["id"]
    chosen3 = call("POST", f"/api/sessions/{s3id}/next")
    assert chosen3["item"]["id"] == "DEMO-SV-1"  # v2 信息量仍高于 DEMO-SV-2
    call("PATCH", "/api/items/DEMO-SV-1/availability", {"is_active": False})
    reselected = call("POST", f"/api/sessions/{s3id}/next")
    assert reselected["item"]["id"] == "DEMO-SV-2"
    assert "DEMO-SV-1" in (reselected["note"] or "")
    assert reselected["item"]["stem"] == sv2_v1["stem"]
    print("  -> 挂起题撤回后重选出 DEMO-SV-2（送达其当前版本）✔")

    # 重选后再改 DEMO-SV-2：/next 仍返回重选时送达的内容，作答按该版本判分。
    call("POST", "/api/items/import", {"items": [sv2_v2]})
    again3 = call("POST", f"/api/sessions/{s3id}/next")
    assert again3["item"]["stem"] == sv2_v1["stem"]
    assert again3["item"]["options"] == sv2_v1["options"]
    result3 = call(
        "POST", f"/api/sessions/{s3id}/responses",
        {"response_id": f"demo-sv-{s3id[:8]}-1",
         "item_id": "DEMO-SV-2", "response": "B"},
    )
    assert result3["correct"] is True and result3["correct_answer"] == "B"
    assert abs(result3["theta_after"] - 0.5) < 1e-9  # 冻结版本 a=4,b=0
    print("  -> 撤回重选关联送达版本：改题后 /next 仍返回原内容，"
          "按原版本判分 ✔")

    # 收尾：撤回演示题，保证重复运行本脚本时其他演示段落不受影响。
    for iid in ("DEMO-SV-1", "DEMO-SV-2"):
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    print("  -> 演示题已撤回，不影响后续演示 ✔")


def demo_frozen_irt_params() -> None:
    """Show the SE is computed from per-answer frozen IRT parameters.

    A formal import that recalibrates already-answered items must not shift
    the standard error of a session that saw no new answers (no drift), and
    must neither trigger nor miss ``precision_reached`` retroactively; a
    fresh answer after the recalibration freezes the new served parameters.
    """
    print("\n--- 作答参数冻结：导入改参不影响已答题的标准误与精度停测 ---")

    # 三道专用演示题：a=2.0，b 沿全对轨迹 0/1/2 排列（a=2 时全对 θ 每题 +1），
    # 会话必然依次取 DEMO-SE-1/2/3；演示题库 a<=1.8（信息量 <=0.81）不会干扰。
    def se_item(iid: str, difficulty: float, discrimination: float = 2.0) -> dict:
        return {
            "id": iid, "difficulty": difficulty, "discrimination": discrimination,
            "content_tag": "精度演示", "stem": f"精度演示题 {iid}：1 + 1 = ?",
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": "A",
        }

    imported = call(
        "POST", "/api/items/import",
        {"items": [se_item("DEMO-SE-1", 0.0), se_item("DEMO-SE-2", 1.0),
                   se_item("DEMO-SE-3", 2.0)]},
    )
    assert imported["valid"] is True
    # 重复运行时上一轮可能改过参数或把演示题留在撤回状态：上面的导入已重置
    # 参数，这里统一启用；同时撤回冻结标签演示遗留的 FZC 组（同为 a=2.0，
    # 会与本组并列干扰选题）。
    for iid in ("DEMO-SE-1", "DEMO-SE-2", "DEMO-SE-3"):
        assert call("PATCH", f"/api/items/{iid}/availability",
                    {"is_active": True})["is_active"] is True
    for iid in ("DEMO-FZC-1", "DEMO-FZC-2", "DEMO-FZC-3", "DEMO-FZC-4"):
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})

    # target_se=0.8、min_precision_items=2：按冻结参数计算，本组作答轨迹下
    # SE 始终 > 0.8，精度停测不应触发；若按题库现值重算（旧行为），下面的
    # 改参会先把 SE 压低、再在第 3 题作答后把 SE 算成 ≈0.45 而误触发停测。
    s1 = call("POST", "/api/sessions",
              {"max_items": 5, "exposure_limit": 10_000,
               "target_se": 0.8, "min_precision_items": 2})
    s1id = s1["id"]

    def answer_a(turn: int) -> dict:
        chosen = call("POST", f"/api/sessions/{s1id}/next")
        item = chosen["item"]
        assert item is not None
        result = call(
            "POST", f"/api/sessions/{s1id}/responses",
            {"response_id": f"demo-se-{s1id[:8]}-{turn}",
             "item_id": item["id"], "response": "A"},
        )
        assert result["correct"] is True
        return {"item": item, "result": result}

    a1 = answer_a(1)
    assert a1["item"]["id"] == "DEMO-SE-1"
    # 作答记录冻结送达版本参数（a=2.0, b=0.0），随响应一并返回。
    assert a1["result"]["difficulty"] == 0.0
    assert a1["result"]["discrimination"] == 2.0
    a2 = answer_a(2)
    assert a2["item"]["id"] == "DEMO-SE-2"
    se_before = call("GET", f"/api/sessions/{s1id}/progress")["standard_error"]
    assert se_before is not None and se_before > 0.8
    print(f"  -> 已答 2 题（θ=2.0），SE={round(se_before, 4)} > 0.8，会话继续")

    # 正式导入把两道已答题改参（a 2.0->3.0、b 移到 3.0）：题库现值已变，
    # 但无新增作答的本场 SE 必须逐位不变（旧行为会按新参数重算成 ≈1.11）。
    call("POST", "/api/items/import",
         {"items": [se_item("DEMO-SE-1", 3.0, discrimination=3.0),
                    se_item("DEMO-SE-2", 3.0, discrimination=3.0)]})
    progress = call("GET", f"/api/sessions/{s1id}/progress")
    assert progress["standard_error"] == se_before, \
        "导入改参不得改变无新增作答会话的标准误"
    history = call("GET", f"/api/sessions/{s1id}/responses")
    assert history["results"][0]["difficulty"] == 0.0
    assert history["results"][0]["discrimination"] == 2.0
    show("两道已答题被导入改参（a 2.0→3.0、b→3.0）：SE 不漂移", {
        "题库当前参数": {
            "difficulty": call("GET", "/api/items/DEMO-SE-1")["difficulty"],
            "discrimination":
                call("GET", "/api/items/DEMO-SE-1")["discrimination"],
        },
        "首条作答冻结参数": {
            "difficulty": history["results"][0]["difficulty"],
            "discrimination": history["results"][0]["discrimination"],
        },
        "standard_error": progress["standard_error"],
    })
    print("  -> 题库参数已改，但作答记录保留冻结参数，SE 逐位不变 ✔")

    # 第 3 题作答后：冻结参数下 SE≈1.41 仍高于 0.8，不得停测（旧行为会把
    # 已答题按改后参数算进 SE，SE≈0.45 <= 0.8 而误触发 precision_reached）。
    a3 = answer_a(3)
    assert a3["item"]["id"] == "DEMO-SE-3"
    progress = call("GET", f"/api/sessions/{s1id}/progress")
    assert progress["status"] == "active", \
        "冻结参数下 SE 仍高于 target_se，不得误触发 precision_reached"
    assert progress["standard_error"] is not None
    assert progress["standard_error"] > 0.8
    print(f"  -> 第 3 题后 SE={round(progress['standard_error'], 4)} 仍按冻结"
          "参数计算且 > 0.8，未误触发 precision_reached，会话继续 ✔")

    # 新作答冻结新参数：再次改参后，新会话作答同一题冻结的是新版本参数。
    call("POST", "/api/items/import",
         {"items": [se_item("DEMO-SE-1", 0.0, discrimination=5.0)]})
    s2 = call("POST", "/api/sessions",
              {"max_items": 1, "exposure_limit": 10_000})
    s2id = s2["id"]
    chosen = call("POST", f"/api/sessions/{s2id}/next")
    assert chosen["item"]["id"] == "DEMO-SE-1"  # a=5.0 在 θ=0 信息量 6.25，必然中选
    r = call("POST", f"/api/sessions/{s2id}/responses",
             {"response_id": f"demo-se-{s2id[:8]}-1",
              "item_id": "DEMO-SE-1", "response": "A"})
    assert r["difficulty"] == 0.0 and r["discrimination"] == 5.0
    print("  -> 再次改参后新会话作答同一题：记录冻结的是新版本参数（a=5.0）✔")

    # 第一个会话的 SE 仍不受后续改参与新会话作答影响。
    progress1 = call("GET", f"/api/sessions/{s1id}/progress")
    assert progress1["standard_error"] == progress["standard_error"]
    print("  -> 第一个会话的 SE 依旧不变，改参与新作答均不追溯影响已答题 ✔")

    # 收尾：撤回演示题，保证重复运行本脚本时其他演示段落不受影响。
    for iid in ("DEMO-SE-1", "DEMO-SE-2", "DEMO-SE-3"):
        call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    print("  -> 演示题已撤回，不影响后续演示 ✔")


def demo_revisions() -> None:
    """Show the append-only item revision history end to end."""
    print("\n--- 题库修订历史 ---")
    iid = "DEMO-RV-1"

    def rev_item(stem: str, answer: str = "A") -> dict:
        return {
            "id": iid, "difficulty": 0.42, "discrimination": 1.2,
            "content_tag": "修订演示", "stem": stem,
            "options": [{"key": "A", "text": "2"}, {"key": "B", "text": "3"}],
            "answer": answer,
        }

    # 0) 每个现存题在首启时已有一个 baseline 版本（30 道演示题均为 v1），
    #    且重复启动不会重复生成。
    seed_revs = call("GET", "/api/items/ITEM-001/revisions")
    assert seed_revs["count"] >= 1
    assert seed_revs["results"][0]["version"] == 1
    assert seed_revs["results"][0]["change_source"] == "baseline"
    print(f"  -> 演示题 ITEM-001 有 {seed_revs['count']} 个版本，"
          "首个为首启 baseline（重复启动不重复生成）✔")

    # 1) 预检（dry_run）不产生任何版本（此时该题可能尚未存在，GET 为 404）。
    def rev_count() -> int:
        """Version count of the demo item; 0 while it does not exist yet."""
        status_code, body = call_status("GET", f"/api/items/{iid}/revisions")
        return body["count"] if status_code == 200 else 0

    n0 = rev_count()
    pre = call("POST", "/api/items/import",
               {"dry_run": True, "items": [rev_item("预检题，不应写库")]})
    assert pre["valid"] is True
    assert rev_count() == n0, "预检不应产生版本"
    print("  -> dry_run 预检不写库、不产生版本 ✔")

    # 2) 正式导入：新题 -> import_create（重复运行时当前内容已被上轮改成
    #    其他版本，本次同样算作一次内容变化 -> import_update）；总之版本 +1。
    call("POST", "/api/items/import",
         {"items": [rev_item("修订演示题 v1 内容")]})
    revs = call("GET", f"/api/items/{iid}/revisions")["results"]
    assert len(revs) == n0 + 1
    n_after_create = len(revs)
    if n0 == 0:
        assert revs[0]["change_source"] == "import_create"
        print("  -> 首次运行：正式导入新题产生 import_create v1 ✔")
    else:
        print("  -> 重复运行脚本：历史版本保留，本次导入继续追加新版本 ✔")

    call("POST", "/api/items/import",
         {"items": [rev_item("修订演示题 v1 内容")]})
    revs = call("GET", f"/api/items/{iid}/revisions")["results"]
    assert len(revs) == n_after_create, "同值重试不应产生版本"
    print("  -> 同值内容重复导入不产生新版本 ✔")

    # 3) 整批校验失败（答案不在选项键中）-> 422，整批不变，不产生版本。
    status, bad = call_status(
        "POST", "/api/items/import",
        {"items": [rev_item("坏批次不应写库", answer="Z")]},
    )
    assert status == 422 and bad["valid"] is False
    revs = call("GET", f"/api/items/{iid}/revisions")["results"]
    assert len(revs) == n_after_create, "失败批次不应产生版本"
    print("  -> 整批校验失败返回 422，整批不变、不产生版本 ✔")

    # 4) 内容变化（题干/答案）的正式导入 -> import_update，版本连续。
    call("POST", "/api/items/import",
         {"items": [rev_item("修订演示题 v2 内容（改题干）")]})
    call("POST", "/api/items/import",
         {"items": [rev_item("修订演示题 v3 内容（改答案）", answer="B")]})
    revs = call("GET", f"/api/items/{iid}/revisions")["results"]
    assert len(revs) == n_after_create + 2
    assert [r["version"] for r in revs] == list(range(1, len(revs) + 1)), \
        "版本号必须连续"
    assert [r["change_source"] for r in revs[-2:]] == ["import_update"] * 2
    print(f"  -> 两次内容变化各产生一个 import_update 版本（现共 {len(revs)} 版）✔")

    # 5) 启用状态实际变化 -> withdraw / re_enable；同值重试无新版本。
    call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    n_before_same = len(call("GET", f"/api/items/{iid}/revisions")["results"])
    again = call("PATCH", f"/api/items/{iid}/availability", {"is_active": False})
    assert again["is_active"] is False
    assert len(call("GET", f"/api/items/{iid}/revisions")["results"]) \
        == n_before_same, "同值撤回重试不应产生版本"
    call("PATCH", f"/api/items/{iid}/availability", {"is_active": True})
    revs = call("GET", f"/api/items/{iid}/revisions")["results"]
    assert [r["change_source"] for r in revs[-2:]] == ["withdraw", "re_enable"]
    assert revs[-2]["is_active"] is False and revs[-1]["is_active"] is True
    print("  -> 撤回/启用实际翻转产生 withdraw/re_enable 版本；"
          "同值重试不产生 ✔")

    # 6) 指定版本快照：完整字段、不含曝光次数；历史版本不被后续导入改写。
    #    题库行照常返回 exposure_count（该题可能在以往演示中已被作答），
    #    但任何版本快照都不携带这个随作答变化的字段。
    detail = call("GET", f"/api/items/{iid}")
    assert detail["exposure_count"] >= 0
    # 找到「v1 内容」那次导入对应的历史版本（首次运行即 v1；重复运行时
    # v1 是上一轮更早的版本，但本次导入的内容一定在历史中且此后不变）。
    target_meta = next(
        r for r in revs
        if call("GET", f"/api/items/{iid}/revisions/{r['version']}")["stem"]
        == "修订演示题 v1 内容"
    )
    v1 = call("GET", f"/api/items/{iid}/revisions/{target_meta['version']}")
    assert v1["stem"] == "修订演示题 v1 内容"
    for field in ("stem", "options", "answer", "difficulty",
                  "discrimination", "content_tag", "is_active"):
        assert field in v1, f"快照必须包含 {field}"
    assert "exposure_count" not in v1, "快照不得包含曝光次数"
    assert v1["options"] == [{"key": "A", "text": "2"},
                             {"key": "B", "text": "3"}]
    show(f"GET .../revisions/{target_meta['version']}（历史版本完整快照，不含曝光次数）", v1)
    print("  -> 快照含题干/选项/答案/难度/区分度/标签/启用状态，"
          "不含曝光次数；后续导入未改写历史版本 ✔")

    # 7) 未知题目 / 未知版本 -> 404。
    status_code, body = call_status("GET", "/api/items/NO-SUCH-ITEM/revisions")
    assert status_code == 404, body
    status_code, body = call_status(
        "GET", f"/api/items/{iid}/revisions/9999")
    assert status_code == 404, body
    print("  -> 未知题目与未知版本均返回 404 ✔")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"无法连接 {BASE_URL}，请先启动服务（uvicorn app.main:app 或 docker compose up）。\n{exc}"
        )
