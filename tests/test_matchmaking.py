from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.catalog.service import CatalogService
from app.core.clock import FrozenClock
from app.core.errors import ConflictError
from app.database import get_connection
from app.matchmaking.service import MatchmakingService

START = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)
WINDOW = [{"starts_at": "2026-10-01T00:00:00+00:00", "ends_at": "2026-10-31T00:00:00+00:00"}]


def make_product(service: CatalogService, code: str, **overrides) -> dict:
    payload = {
        "code": code, "name": f"产品-{code}", "organization": "示例康复科技",
        "origin_country": "中国浙江", "category": "康复设备",
        "intended_use": "用于医院康复科开展辅助训练与临床观察，需要专业场地配合实施",
        "intended_populations": ["脑卒中患者"], "risk_level": "medium", "regulatory_status": "展示",
    }
    payload.update(overrides)
    return service.create_product(payload)


def make_site(service: CatalogService, code: str, **overrides) -> dict:
    payload = {
        "code": code, "name": f"场地-{code}", "site_type": "医院", "region": "浙江",
        "capabilities": ["rehab-device"], "served_populations": ["脑卒中患者"],
        "max_concurrent": 2, "ethics_prepared_level": "full",
        "supported_risk_levels": ["low", "medium", "high"], "open_windows": [],
    }
    payload.update(overrides)
    return service.create_site(payload)


def services(clock: FrozenClock | None = None):
    clock = clock or FrozenClock(START)
    connection = get_connection()
    return clock, CatalogService(connection, clock), MatchmakingService(connection, clock)


def test_recommendation_orders_candidates_with_reasons(client):
    _, catalog, matching = services()
    make_product(catalog, "dtx-high", category="数字疗法", risk_level="high", intended_populations=["卒中后患者"])
    make_product(catalog, "rehab-low", risk_level="low")
    make_site(catalog, "h-elite", capabilities=["digital-therapeutic", "rehab-device"],
              served_populations=["脑卒中患者", "卒中后患者"], ethics_prepared_level="full", open_windows=WINDOW)
    make_site(catalog, "h-basic", capabilities=["rehab-device"], ethics_prepared_level="basic",
              supported_risk_levels=["low", "medium"], max_concurrent=5)
    result = matching.recommend({"category": "数字疗法"})
    top = result["candidates"][0]
    assert top["rank"] == 1 and top["eligible"] is True
    assert top["product_code"] == "dtx-high" and top["site_code"] == "h-elite"
    assert top["total_score"] > 80
    # 每个候选都带逐维理由
    assert {"capability", "ethics", "risk_capacity", "population", "region", "open_window", "capacity"} <= set(top["scores"])
    # 高风险产品落到只有 basic 伦理、不承接 high 的科室：被判为不可选并给出原因
    blocked = [c for c in result["candidates"] if c["site_code"] == "h-basic"][0]
    assert blocked["eligible"] is False and blocked["rank"] is None
    assert any("伦理准备不足" in b or "高风险" in b for b in blocked["blockers"])


def test_reservation_requires_capability_ethics_and_risk_capacity(client):
    _, catalog, matching = services()
    make_product(catalog, "dtx-high", category="数字疗法", risk_level="high")
    make_site(catalog, "h-community", capabilities=["consumer-health"], ethics_prepared_level="none",
              supported_risk_levels=["low"], served_populations=["健康人群"])
    response_error = None
    try:
        matching.reserve({"product_code": "dtx-high", "site_code": "h-community", "ttl_seconds": 3600,
                          "idempotency_key": "risk-0001"}, "operator-1")
    except Exception as exc:  # noqa: BLE001
        response_error = exc
    assert response_error is not None and response_error.code == "conflict"
    blockers = response_error.context["blockers"]
    assert any("高风险" in b or "资格" in b for b in blockers)
    assert any("伦理准备不足" in b for b in blockers)
    assert any("digital-therapeutic" in b for b in blockers)
    assert any("人群" in b for b in blockers)


def test_concurrent_reservations_create_only_one_slot(client):
    _, catalog, matching = services()
    make_product(catalog, "p-a")
    make_product(catalog, "p-b")
    make_site(catalog, "h-1", max_concurrent=1)
    first = matching.reserve({"product_code": "p-a", "site_code": "h-1", "ttl_seconds": 3600,
                              "idempotency_key": "enterprise-a"}, "operator-1")
    assert first["status"] == "reserved"
    # 运营把同一场地的唯一名额又承诺给第二家企业：必须被容量规则拒绝
    conflict = None
    try:
        matching.reserve({"product_code": "p-b", "site_code": "h-1", "ttl_seconds": 3600,
                          "idempotency_key": "enterprise-b"}, "operator-2")
    except Exception as exc:  # noqa: BLE001
        conflict = exc
    assert conflict is not None and conflict.code == "conflict"
    # 同一运营的重复提交是幂等的，返回同一条记录而不是第二个名额
    repeated = matching.reserve({"product_code": "p-a", "site_code": "h-1", "ttl_seconds": 3600,
                                 "idempotency_key": "enterprise-a"}, "operator-1")
    assert repeated["id"] == first["id"]
    assert matching.repository.occupied_slots(first["site_id"]) == 1


def test_hospital_accept_enterprise_confirm_full_lifecycle(client):
    _, catalog, matching = services()
    make_product(catalog, "p-life")
    make_site(catalog, "h-life", max_concurrent=2)
    reserved = matching.reserve({"product_code": "p-life", "site_code": "h-life", "ttl_seconds": 3600,
                                 "idempotency_key": "life-001", "hospital_contact": "dr-wang"}, "operator-1")
    assert reserved["status"] == "reserved" and reserved["expires_at"] > reserved["created_at"]
    accepted = matching.hospital_respond(reserved["matching_code"], {"decision": "accept", "actor": "dr-wang", "note": "科里同意"})
    assert accepted["status"] == "hospital_accepted"
    # 医院不能重复答复
    with pytest.raises(ConflictError):
        matching.hospital_respond(reserved["matching_code"], {"decision": "accept", "actor": "dr-wang"})
    confirmed = matching.enterprise_confirm(reserved["matching_code"], {"decision": "confirm", "actor": "ent-life"})
    assert confirmed["status"] == "confirmed" and confirmed["confirmed_at"]
    # 医院与企业任何一方在已确认后再操作都不能产生第二条成功名额
    with pytest.raises(ConflictError):
        matching.enterprise_confirm(reserved["matching_code"], {"decision": "confirm", "actor": "ent-life"})
    detail = matching.get_matching(reserved["matching_code"])
    assert [event["event_type"] for event in detail["events"]] == ["reserved", "hospital_accepted", "confirmed"]
    roles = {note["recipient_role"] for note in detail["notifications"]}
    assert {"hospital", "enterprise", "operator"} <= roles


def test_hospital_reject_and_conditional_release_or_continue(client):
    _, catalog, matching = services()
    make_product(catalog, "p-r")
    make_product(catalog, "p-c")
    make_site(catalog, "h-r", max_concurrent=1)
    rejected = matching.reserve({"product_code": "p-r", "site_code": "h-r", "ttl_seconds": 3600,
                                 "idempotency_key": "rej-1"}, "operator-1")
    result = matching.hospital_respond(rejected["matching_code"], {"decision": "reject", "actor": "dr-li", "note": "暂不开展"})
    assert result["status"] == "hospital_rejected" and result["released_at"]
    # 容量释放后同一场地可再安排
    again = matching.reserve({"product_code": "p-c", "site_code": "h-r", "ttl_seconds": 3600,
                              "idempotency_key": "rej-2"}, "operator-1")
    assert again["status"] == "reserved"
    conditional = matching.hospital_respond(again["matching_code"],
                                            {"decision": "conditional", "actor": "dr-li", "condition": "需增派一名随访护士"})
    assert conditional["status"] == "hospital_conditional"
    confirmed = matching.enterprise_confirm(again["matching_code"], {"decision": "confirm", "actor": "ent-c"})
    assert confirmed["status"] == "confirmed"


def test_expiry_time_rule_releases_only_unconfirmed(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-expire")
    make_product(catalog, "p-keep")
    make_site(catalog, "h-expire")
    pending = matching.reserve({"product_code": "p-expire", "site_code": "h-expire", "ttl_seconds": 900,
                                "idempotency_key": "exp-1"}, "operator-1")
    confirmed_one = matching.reserve({"product_code": "p-keep", "site_code": "h-expire", "ttl_seconds": 10_000,
                                      "idempotency_key": "exp-2"}, "operator-2")
    matching.hospital_respond(confirmed_one["matching_code"], {"decision": "accept"})
    matching.enterprise_confirm(confirmed_one["matching_code"], {"decision": "confirm"})
    clock.advance(seconds=901)
    report = matching.expire_due()
    assert pending["matching_code"] in report["expired"]
    expired = matching.get_matching(pending["matching_code"])
    assert expired["status"] == "expired" and expired["release_reason"]
    # 已生效名额不受到期任务影响
    assert matching.get_matching(confirmed_one["matching_code"])["status"] == "confirmed"
    # 重复执行到期任务是幂等的
    assert matching.expire_due()["expired"] == []


def test_restart_service_keeps_waiting_and_expiring_records(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-wait-h")
    make_product(catalog, "p-wait-e")
    make_site(catalog, "h-wait")
    wait_hospital = matching.reserve({"product_code": "p-wait-h", "site_code": "h-wait", "ttl_seconds": 3600,
                                      "idempotency_key": "wait-1"}, "operator-1")
    wait_enterprise = matching.reserve({"product_code": "p-wait-e", "site_code": "h-wait", "ttl_seconds": 100_000,
                                        "idempotency_key": "wait-2"}, "operator-2")
    matching.hospital_respond(wait_enterprise["matching_code"], {"decision": "accept"})
    # 值班人员重启服务：新实例仍能看到等待医院、等待企业和即将到期
    restarted = MatchmakingService(get_connection(), FrozenClock(START))
    inbox = restarted.pending_inbox()
    hospital_codes = {item["matching_code"] for item in inbox["waiting_hospital"]}
    enterprise_codes = {item["matching_code"] for item in inbox["waiting_enterprise"]}
    assert wait_hospital["matching_code"] in hospital_codes
    assert wait_enterprise["matching_code"] in enterprise_codes
    assert any(item["matching_code"] == wait_hospital["matching_code"] for item in inbox["expiring_within_24h"])


def test_product_suspension_revokes_only_pending_matchings(client):
    _, catalog, matching = services()
    make_product(catalog, "p-sus")
    make_site(catalog, "h-sus", max_concurrent=3)
    make_site(catalog, "h-sus-done", max_concurrent=3)
    pending = matching.reserve({"product_code": "p-sus", "site_code": "h-sus", "ttl_seconds": 3600,
                                "idempotency_key": "sus-1"}, "operator-1")
    confirmed = matching.reserve({"product_code": "p-sus", "site_code": "h-sus-done", "ttl_seconds": 3600,
                                  "idempotency_key": "sus-2"}, "operator-2")
    matching.hospital_respond(confirmed["matching_code"], {"decision": "accept"})
    matching.enterprise_confirm(confirmed["matching_code"], {"decision": "confirm"})
    catalog.update_product("p-sus", {"regulatory_status": "暂停"})
    assert matching.get_matching(pending["matching_code"])["status"] == "revoked"
    revoked = matching.get_matching(pending["matching_code"])
    assert "暂停" in revoked["release_reason"]
    # 已生效名额不被撤销，相关方仍可在记录中看到
    assert matching.get_matching(confirmed["matching_code"])["status"] == "confirmed"
    pending_detail = matching.get_matching(pending["matching_code"])
    assert pending_detail["events"][-1]["event_type"] == "auto_revoked"
    notified_roles = {n["recipient_role"] for n in pending_detail["notifications"]}
    assert {"hospital", "enterprise"} <= notified_roles


def test_site_capability_change_revokes_unfit_pending_and_enforces_capacity(client):
    _, catalog, matching = services()
    make_product(catalog, "p-cap", risk_level="medium")
    make_product(catalog, "p-extra", risk_level="low")
    make_site(catalog, "h-cap", max_concurrent=2)
    fit = matching.reserve({"product_code": "p-cap", "site_code": "h-cap", "ttl_seconds": 3600,
                            "idempotency_key": "cap-1"}, "operator-1")
    extra = matching.reserve({"product_code": "p-extra", "site_code": "h-cap", "ttl_seconds": 3600,
                              "idempotency_key": "cap-2"}, "operator-2")
    # 场地下调容量到 1：超出的未决名额被撤销，仍适配的保留
    updated = catalog.update_site("h-cap", {"max_concurrent": 1})
    revoked_codes = {item["matching_code"] for item in updated["auto_revoked_matchings"]}
    assert extra["matching_code"] in revoked_codes
    assert fit["matching_code"] not in revoked_codes
    # 场地移除能力：剩余未决名额因不再适配被撤销
    catalog.update_site("h-cap", {"capabilities": ["consumer-health"]})
    assert matching.get_matching(fit["matching_code"])["status"] == "revoked"


def test_capacity_timeline_records_every_change(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-tl")
    make_site(catalog, "h-tl", max_concurrent=1)
    reserved = matching.reserve({"product_code": "p-tl", "site_code": "h-tl", "ttl_seconds": 900,
                                 "idempotency_key": "tl-1"}, "operator-1")
    matching.hospital_respond(reserved["matching_code"], {"decision": "accept"})
    matching.enterprise_confirm(reserved["matching_code"], {"decision": "confirm"})
    start = "2026-10-05T00:00:00+00:00"
    end = "2026-10-05T04:00:00+00:00"
    timeline = matching.capacity_timeline({"start": start, "end": end, "site_code": "h-tl"})
    effects = [(step["event"], step["capacity_effect"]) for step in timeline["steps"]]
    assert effects == [("reserved", "hold"), ("hospital_accepted", "hold"), ("confirmed", "commit")]


def test_capacity_teline_release_steps(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-rel")
    make_site(catalog, "h-rel")
    reserved = matching.reserve({"product_code": "p-rel", "site_code": "h-rel", "ttl_seconds": 900,
                                 "idempotency_key": "rel-1"}, "operator-1")
    clock.advance(seconds=901)
    matching.expire_due()
    timeline = matching.capacity_timeline({
        "start": "2026-10-05T00:00:00+00:00", "end": "2026-10-05T04:00:00+00:00", "site_code": "h-rel",
    })
    assert timeline["steps"][-1]["capacity_effect"] == "release"
    assert matching.get_matching(reserved["matching_code"])["status"] == "expired"


def test_late_response_after_expiry_is_rejected_and_capacity_freed(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-late")
    make_site(catalog, "h-late", max_concurrent=1)
    reserved = matching.reserve({"product_code": "p-late", "site_code": "h-late", "ttl_seconds": 900,
                                 "idempotency_key": "late-1"}, "operator-1")
    clock.advance(seconds=901)
    with pytest.raises(ConflictError):
        matching.hospital_respond(reserved["matching_code"], {"decision": "accept"})
    assert matching.get_matching(reserved["matching_code"])["status"] == "expired"
    # 容量已释放，可重新发起预留
    clock.advance(seconds=1)
    replacement = matching.reserve({"product_code": "p-late", "site_code": "h-late", "ttl_seconds": 900,
                                    "idempotency_key": "late-2"}, "operator-1")
    assert replacement["status"] == "reserved"


def test_restart_inbox_expires_due_records(client):
    clock = FrozenClock(START)
    connection = get_connection()
    catalog = CatalogService(connection, clock)
    matching = MatchmakingService(connection, clock)
    make_product(catalog, "p-restart")
    make_site(catalog, "h-restart", max_concurrent=2)
    due = matching.reserve({"product_code": "p-restart", "site_code": "h-restart", "ttl_seconds": 900,
                            "idempotency_key": "restart-1"}, "operator-1")
    clock.advance(seconds=1000)
    # 值班人员重新启动服务后打开收件箱：到期名额被自动释放，不再出现在等待列表
    restarted = MatchmakingService(get_connection(), FrozenClock(clock.current))
    inbox = restarted.pending_inbox()
    assert due["matching_code"] in inbox["expired_now"]
    assert all(item["matching_code"] != due["matching_code"] for item in inbox["waiting_hospital"])
    assert restarted.get_matching(due["matching_code"])["status"] == "expired"


def test_parallel_enterprise_confirms_yield_single_success(client):
    import threading
    _, catalog, _ = services()
    make_product(catalog, "p-parallel")
    make_site(catalog, "h-parallel", max_concurrent=1)
    # 用独立连接/线程模拟企业与医院的并发确认竞争
    base = MatchmakingService(get_connection(), FrozenClock(START))
    reserved = base.reserve({"product_code": "p-parallel", "site_code": "h-parallel", "ttl_seconds": 3600,
                             "idempotency_key": "par-1"}, "operator-1")
    base.hospital_respond(reserved["matching_code"], {"decision": "accept"})

    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def worker(actor: str) -> None:
        # get_connection 按线程返回独立连接，BEGIN IMMEDIATE 负责串行化写入
        svc = MatchmakingService(get_connection(), FrozenClock(START))
        barrier.wait()
        try:
            result = svc.enterprise_confirm(reserved["matching_code"], {"decision": "confirm", "actor": actor})
            outcomes.append(f"ok:{result['status']}")
        except ConflictError:
            outcomes.append("conflict")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [threading.Thread(target=worker, args=(f"ent-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok:confirmed") == 1, outcomes
    assert outcomes.count("conflict") == 3, outcomes
    detail = base.get_matching(reserved["matching_code"])
    assert detail["status"] == "confirmed"
    assert detail["version"] >= 2
    assert base.repository.occupied_slots(detail["site_id"]) == 1


def test_parallel_reservations_for_single_capacity_yield_one_winner(client):
    import threading
    _, catalog, _ = services()
    make_product(catalog, "e-a")
    make_product(catalog, "e-b")
    make_site(catalog, "h-only", max_concurrent=1)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def reserve(product: str, operator: str, key: str) -> None:
        svc = MatchmakingService(get_connection(), FrozenClock(START))
        barrier.wait()
        try:
            result = svc.reserve({"product_code": product, "site_code": "h-only", "ttl_seconds": 3600,
                                  "idempotency_key": key}, operator)
            outcomes.append(f"ok:{result['matching_code']}")
        except ConflictError as exc:
            outcomes.append(f"conflict:{exc.message}")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [
        threading.Thread(target=reserve, args=("e-a", "op-a", "key-a")),
        threading.Thread(target=reserve, args=("e-b", "op-b", "key-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    success = [item for item in outcomes if item.startswith("ok:")]
    assert len(success) == 1, outcomes
    assert len([item for item in outcomes if item.startswith("conflict")]) == 1, outcomes


def test_notifications_can_be_marked_delivered(client):
    _, catalog, matching = services()
    make_product(catalog, "p-note")
    make_site(catalog, "h-note")
    matching.reserve({"product_code": "p-note", "site_code": "h-note", "ttl_seconds": 3600,
                      "idempotency_key": "note-1"}, "operator-1")
    pending = matching.notifications("hospital", 10)
    assert pending["count"] >= 1
    notification_id = pending["items"][0]["id"]
    result = matching.mark_delivered(notification_id)
    assert result["delivered"] is True
    assert matching.notifications("hospital", 10)["count"] == 0
