from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.matching.service import MatchingService


def _product_high() -> dict:
    return {
        "code": "dt-rehab",
        "name": "卒中数字疗法软件",
        "organization": "神境数字医疗",
        "origin_country": "中国",
        "category": "数字疗法",
        "intended_use": "用于卒中后认知与上肢功能康复的处方数字疗法，需要临床科室监测",
        "risk_level": "high",
        "regulatory_status": "研究",
    }


def _product_medium() -> dict:
    return {
        "code": "exo-walk",
        "name": "步态助力外骨骼",
        "organization": "迈步机器人",
        "origin_country": "中国",
        "category": "康复设备",
        "intended_use": "用于康复科步态训练和展会体验的可穿戴助力设备",
        "risk_level": "medium",
        "regulatory_status": "已注册",
    }


def _setup_catalog(client) -> None:
    assert client.post("/api/catalog/products", json=_product_high()).status_code == 201
    assert client.post("/api/catalog/products", json=_product_medium()).status_code == 201
    # 三甲医院：康复医学科具备数字疗法能力、可承高风险且伦理就绪
    assert client.post("/api/catalog/sites", json={
        "code": "hospital-a", "name": "市第一人民医院", "site_type": "医院",
        "region": "浙江杭州", "capabilities": ["digital-therapy", "gait-training"], "max_concurrent": 4,
    }).status_code == 201
    # 社区医院：只有理疗室，最高承接低风险，无伦理准备
    assert client.post("/api/catalog/sites", json={
        "code": "hospital-b", "name": "拱墅社区医院", "site_type": "医院",
        "region": "浙江杭州", "capabilities": ["physiotherapy"], "max_concurrent": 2,
    }).status_code == 201
    # 外省展会体验点，无科室登记
    assert client.post("/api/catalog/sites", json={
        "code": "expo-west", "name": "西部数贸会展点", "site_type": "展会体验点",
        "region": "四川成都", "capabilities": ["digital-therapy"], "max_concurrent": 3,
    }).status_code == 201

    assert client.put("/api/matching/products/dt-rehab/eligibility?actor=eco-lead", json={
        "eligible_populations": ["卒中恢复期成人"],
        "required_capabilities": ["digital-therapy"],
        "allowed_regions": ["浙江杭州"],
        "ethics_review_status": "已通过",
        "ethics_committee": "市一医院伦理委员会",
        "open_from": "2026-09-01T00:00:00+00:00",
        "open_until": "2026-12-31T00:00:00+00:00",
    }).status_code == 200
    assert client.put("/api/matching/products/exo-walk/eligibility?actor=eco-lead", json={
        "eligible_populations": ["步态障碍患者"],
        "required_capabilities": ["gait-training"],
    }).status_code == 200

    assert client.post("/api/matching/sites/hospital-a/departments?actor=eco-lead", json={
        "code": "rehab-dept", "name": "康复医学科",
        "capabilities": ["digital-therapy", "gait-training", "physiotherapy"],
        "max_risk_level": "high", "ethics_ready": True,
    }).status_code == 201
    assert client.post("/api/matching/sites/hospital-b/departments?actor=eco-lead", json={
        "code": "physio-room", "name": "理疗室",
        "capabilities": ["physiotherapy"],
        "max_risk_level": "low", "ethics_ready": False,
    }).status_code == 201


def test_recommendation_orders_with_reasons_and_blocks_unsafe_department(client):
    _setup_catalog(client)
    response = client.post("/api/matching/recommendations", json={
        "product_codes": ["dt-rehab", "exo-walk"],
        "requested_region": "浙江杭州",
        "requested_population": "卒中恢复期成人",
        "requested_by": "eco-lead",
    })
    assert response.status_code == 201, response.text
    items = response.json()["items"]
    by_product = {}
    for item in items:
        by_product.setdefault(item["product"]["code"], []).append(item)

    high_pairs = {item["site"]["code"]: item for item in by_product["dt-rehab"]}
    assert high_pairs["hospital-a"]["eligible"] is True
    assert high_pairs["hospital-a"]["rank"] == 1
    assert high_pairs["hospital-a"]["department_id"] is not None
    assert high_pairs["hospital-a"]["score"] > 80
    assert any("伦理" in reason for reason in high_pairs["hospital-a"]["reasons"])
    # 高风险产品落到无对应能力/伦理的科室：硬性阻断，不进入合格候选
    assert high_pairs["hospital-b"]["eligible"] is False
    assert any("能力" in blocker or "伦理" in blocker or "风险" in blocker for blocker in high_pairs["hospital-b"]["blockers"])
    # 高风险 + 产品伦理未过 + 地区/无科室问题：展会点同样阻断
    assert high_pairs["expo-west"]["eligible"] is False
    assert any("地区" in blocker or "伦理" in blocker or "科室" in blocker for blocker in high_pairs["expo-west"]["blockers"])

    # 外骨骼按其自身适用人群评估：三甲康复科可承接，社区低风险理疗室不可承接
    medium_response = client.post("/api/matching/recommendations", json={
        "product_codes": ["exo-walk"],
        "requested_region": "浙江杭州",
        "requested_population": "步态障碍患者",
        "requested_by": "eco-lead",
    })
    assert medium_response.status_code == 201
    medium_pairs = {item["site"]["code"]: item for item in medium_response.json()["items"]}
    assert medium_pairs["hospital-a"]["eligible"] is True
    assert medium_pairs["hospital-b"]["eligible"] is False


def _eligible_recommendation(client, product_code: str = "dt-rehab", site_code: str = "hospital-a") -> dict:
    response = client.post("/api/matching/recommendations", json={
        "product_codes": [product_code], "site_codes": [site_code],
        "requested_region": "浙江杭州", "requested_population": "卒中恢复期成人", "requested_by": "eco-lead",
    })
    assert response.status_code == 201
    return response.json()["items"][0]


def test_reservation_hospital_accept_enterprise_confirm_flow_and_idempotency(client):
    _setup_catalog(client)
    recommendation = _eligible_recommendation(client)
    payload = {
        "recommendation_id": recommendation["id"],
        "idempotency_key": "rsv-key-0001", "quantity": 1, "note": "数贸会后试点",
    }
    first = client.post("/api/matching/reservations?actor=operator-li", json=payload)
    assert first.status_code == 202, first.text
    reservation = first.json()
    assert reservation["status"] == "pending"
    code = reservation["reservation_code"]
    # 同幂等键重复发起：不产生第二条
    replay = client.post("/api/matching/reservations?actor=operator-li", json=payload)
    assert replay.status_code == 202
    assert replay.json()["id"] == reservation["id"]

    accepted = client.post(f"/api/matching/reservations/{code}/hospital-response", json={
        "decision": "accepted", "responded_by": "hospital-a-office",
    })
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["enterprise_due_at"]

    confirmed = client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
        "confirmed_by": "shenjing-rep",
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    # 已生效后再次确认：不会出现第二个成功名额
    duplicate = client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
        "confirmed_by": "shenjing-rep",
    })
    assert duplicate.status_code == 409


def test_concurrent_enterprise_confirmations_yield_single_success(client):
    _setup_catalog(client)
    recommendation = _eligible_recommendation(client)
    created = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": recommendation["id"], "idempotency_key": "rsv-key-conc",
    }).json()
    code = created["reservation_code"]
    assert client.post(f"/api/matching/reservations/{code}/hospital-response", json={
        "decision": "accepted", "responded_by": "hospital-a-office",
    }).status_code == 200

    results: list[int] = []
    barrier = threading.Barrier(2)

    def confirm() -> None:
        barrier.wait()
        service = MatchingService()
        try:
            service.enterprise_confirm(code, f"rep-{threading.get_ident()}")
            results.append(200)
        except Exception as exc:  # noqa: BLE001
            results.append(getattr(exc, "status_code", 500))
        finally:
            close_connection()

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [200, 409]
    final = client.get(f"/api/matching/reservations/{code}").json()
    assert final["status"] == "confirmed"
    assert final["version"] >= 2


def test_capacity_is_enforced_and_hospital_rejection_releases_slot(client):
    _setup_catalog(client)
    first = _eligible_recommendation(client)
    r1 = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": first["id"], "idempotency_key": "cap-key-0001", "quantity": 3,
    })
    assert r1.status_code == 202
    # hospital-b 容量 2，且高风险产品本身不合格；用 hospital-a 再申请 2 个超过上限 4
    overflow = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": first["id"], "idempotency_key": "cap-key-0002", "quantity": 2,
    })
    assert overflow.status_code == 409
    rejected = client.post(f"/api/matching/reservations/{r1.json()['reservation_code']}/hospital-response", json={
        "decision": "rejected", "responded_by": "hospital-a-office", "reason": "科室本季度排班已满",
    })
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    # 容量释放后可再申请
    retry = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": first["id"], "idempotency_key": "cap-key-0003", "quantity": 4,
    })
    assert retry.status_code == 202
    # 拒绝缺少原因不允许
    bad_reject = client.post(f"/api/matching/reservations/{retry.json()['reservation_code']}/hospital-response", json={
        "decision": "rejected", "responded_by": "hospital-a-office", "reason": "不行",
    })
    assert bad_reject.status_code == 422


def test_conditional_response_requires_condition_acceptance(client):
    _setup_catalog(client)
    recommendation = _eligible_recommendation(client)
    created = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": recommendation["id"], "idempotency_key": "cond-key-001",
    }).json()
    code = created["reservation_code"]
    missing = client.post(f"/api/matching/reservations/{code}/hospital-response", json={
        "decision": "conditional", "responded_by": "hospital-a-office",
    })
    assert missing.status_code == 422
    conditional = client.post(f"/api/matching/reservations/{code}/hospital-response", json={
        "decision": "conditional", "responded_by": "hospital-a-office",
        "conditions": ["需增配一名住院医师在场", "首两周每日复盘不良事件"],
    })
    assert conditional.status_code == 200 and conditional.json()["status"] == "conditional"
    refused = client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
        "confirmed_by": "shenjing-rep", "accept_conditions": False,
    })
    assert refused.status_code == 409
    agreed = client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
        "confirmed_by": "shenjing-rep", "accept_conditions": True,
    })
    assert agreed.status_code == 200 and agreed.json()["status"] == "confirmed"


def test_expired_holds_are_released_by_controlled_clock(client):
    _setup_catalog(client)
    clock = FrozenClock(datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    service = MatchingService(get_connection(), clock)
    recommendation = service.recommend({
        "product_codes": ["dt-rehab"], "site_codes": ["hospital-a"],
        "requested_region": "浙江杭州", "requested_population": "卒中恢复期成人", "requested_by": "eco-lead",
    })["items"][0]
    reservation = service.reserve({
        "recommendation_id": recommendation["id"], "idempotency_key": "exp-key-0001",
        "hospital_response_seconds": 3600, "enterprise_confirm_seconds": 3600,
    }, "operator-li")
    code = reservation["reservation_code"]
    # 医院逾期：时限内无操作，sweep 释放
    clock.advance(seconds=3601)
    swept = service.sweep_expired()
    assert code in swept["expired"]
    import pytest
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        service.hospital_respond(code, {"decision": "accepted", "responded_by": "hospital-a-office"})

    # 企业确认逾期：医院接受后企业不确认，到时同样释放
    clock.current = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
    second = service.reserve({
        "recommendation_id": recommendation["id"], "idempotency_key": "exp-key-0002",
        "hospital_response_seconds": 7200, "enterprise_confirm_seconds": 3600,
    }, "operator-li")
    code2 = second["reservation_code"]
    service.hospital_respond(code2, {"decision": "accepted", "responded_by": "hospital-a-office"})
    clock.advance(seconds=3601)
    swept2 = service.sweep_expired()
    assert code2 in swept2["expired"]
    with pytest.raises(ConflictError):
        service.enterprise_confirm(code2, "shenjing-rep")


def _confirmed_reservation(client, idem_key: str) -> str:
    recommendation = _eligible_recommendation(client)
    created = client.post("/api/matching/reservations?actor=operator-li", json={
        "recommendation_id": recommendation["id"], "idempotency_key": idem_key,
    }).json()
    code = created["reservation_code"]
    assert client.post(f"/api/matching/reservations/{code}/hospital-response", json={
        "decision": "accepted", "responded_by": "hospital-a-office",
    }).status_code == 200
    assert client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
        "confirmed_by": "shenjing-rep",
    }).status_code == 200
    return code


def test_product_suspension_revokes_only_not_yet_effective_matches(client):
    _setup_catalog(client)
    confirmed_code = _confirmed_reservation(client, "susp-key-done")
    pending = client.post("/api/matching/reservations?actor=operator-li", json={
        "product_code": "dt-rehab", "site_code": "hospital-a", "idempotency_key": "susp-key-pending",
    })
    assert pending.status_code == 202
    pending_code = pending.json()["reservation_code"]

    suspend = client.post("/api/matching/products/dt-rehab/suspend", json={
        "actor": "eco-lead", "reason": "厂商主动召回固件",
    })
    assert suspend.status_code == 200
    assert pending_code in suspend.json()["revoked_reservations"]
    assert confirmed_code not in suspend.json()["revoked_reservations"]

    assert client.get(f"/api/matching/reservations/{pending_code}").json()["status"] == "cancelled"
    assert client.get(f"/api/matching/reservations/{confirmed_code}").json()["status"] == "confirmed"
    # 撤销原因送回相关方
    enterprise_msgs = client.get("/api/matching/messages?recipient_role=enterprise&recipient=神境数字医疗").json()["items"]
    assert any("召回固件" in item["body"] for item in enterprise_msgs)
    hospital_msgs = client.get("/api/matching/messages?recipient_role=hospital&recipient=hospital-a").json()["items"]
    assert any("召回固件" in item["body"] for item in hospital_msgs)
    # 暂停期间不能发起新预留
    blocked = client.post("/api/matching/reservations?actor=operator-li", json={
        "product_code": "dt-rehab", "site_code": "hospital-a", "idempotency_key": "susp-key-blocked",
    })
    assert blocked.status_code == 409
    # 恢复后已确认名额仍在，但可重新发起预留
    assert client.post("/api/matching/products/dt-rehab/resume", json={"actor": "eco-lead"}).status_code == 200
    rebook = client.post("/api/matching/reservations?actor=operator-li", json={
        "product_code": "dt-rehab", "site_code": "hospital-a", "idempotency_key": "susp-key-rebook",
    })
    assert rebook.status_code == 202


def test_site_capability_change_revokes_matches_that_no_longer_qualify(client):
    _setup_catalog(client)
    pending = client.post("/api/matching/reservations?actor=operator-li", json={
        "product_code": "dt-rehab", "site_code": "hospital-a", "idempotency_key": "capchange-01",
    })
    assert pending.status_code == 202
    pending_code = pending.json()["reservation_code"]
    # 场地失去数字疗法能力：未生效预留撤销
    changed = client.patch("/api/matching/sites/hospital-a/capabilities", json={
        "capabilities": ["gait-training"], "actor": "eco-lead", "reason": "设备楼层改造暂停数字疗法",
    })
    assert changed.status_code == 200
    assert pending_code in changed.json()["revoked_reservations"]
    assert client.get(f"/api/matching/reservations/{pending_code}").json()["status"] == "cancelled"
    # 恢复能力后，外骨骼（gait-training）这类仍满足的未生效预留不应被误撤
    client.patch("/api/matching/sites/hospital-a/capabilities", json={
        "capabilities": ["digital-therapy", "gait-training"], "actor": "eco-lead", "reason": "改造完成",
    })
    other = client.post("/api/matching/reservations?actor=operator-li", json={
        "product_code": "exo-walk", "site_code": "hospital-a", "idempotency_key": "capchange-02",
    })
    assert other.status_code == 202
    other_code = other.json()["reservation_code"]
    again = client.patch("/api/matching/sites/hospital-a/capabilities", json={
        "capabilities": ["digital-therapy", "gait-training", "physiotherapy"], "actor": "eco-lead", "reason": "增补理疗能力",
    })
    assert again.status_code == 200
    assert other_code not in again.json()["revoked_reservations"]


def test_state_survives_restart_and_dashboard_lists_pending_and_due(client):
    from datetime import UTC, datetime
    from app.core.clock import FrozenClock
    from app.matching.service import MatchingService

    _setup_catalog(client)
    clock = FrozenClock(datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    service = MatchingService(get_connection(), clock)
    reco = service.recommend({
        "product_codes": ["dt-rehab"], "site_codes": ["hospital-a"],
        "requested_region": "浙江杭州", "requested_population": "卒中恢复期成人", "requested_by": "eco-lead",
    })["items"][0]
    created = service.reserve({
        "recommendation_id": reco["id"], "idempotency_key": "restart-key-01", "hospital_response_seconds": 1800,
    }, "operator-li")
    code = created["reservation_code"]
    # 值班人员重启服务：新建服务实例（同库文件），仍能看到等待答复与即将到期
    clock.advance(seconds=1700)
    restarted = MatchingService(get_connection(), clock)
    dashboard = restarted.dashboard(within_seconds=300)
    assert dashboard["awaiting_hospital_reply"] == 1
    assert [item["reservation_code"] for item in dashboard["hospital_due_soon"]] == [code]
    # 时间窗容量台账：推荐 -> 预留 每步可核对
    timeline = restarted.capacity_timeline(start=None, end=None, site_code="hospital-a")
    events = [item["event_type"] for item in timeline["items"]]
    assert "recommended" in events and events.count("reserved") == 1
    reserved = next(item for item in timeline["items"] if item["event_type"] == "reserved")
    assert reserved["held_after"] == 1
    assert reserved["site_code"] == "hospital-a"
    # 真正重启进程视角：关闭并重建连接后数据仍在
    close_connection()
    after_reboot = MatchingService(clock=clock)
    assert after_reboot.get_reservation(code)["status"] == "pending"


def test_capacity_timeline_records_full_lifecycle(client):
    _setup_catalog(client)
    code = _confirmed_reservation(client, "timeline-key-01")
    timeline = client.get("/api/matching/capacity-timeline?site_code=hospital-a").json()["items"]
    sequence = [item["event_type"] for item in timeline]
    assert sequence == ["recommended", "reserved", "hospital_accepted", "enterprise_confirmed"]
    confirmed = timeline[-1]
    assert confirmed["held_after"] == 0
    assert confirmed["confirmed_after"] == 1
    assert confirmed["reservation_code"] == code


def test_site_downgrade_keeps_confirmed_reservation_and_nonnegative_balance(client):
    _setup_catalog(client)
    code = _confirmed_reservation(client, "site-downgrade-done")
    changed = client.patch("/api/matching/sites/hospital-a/capabilities", json={
        "capabilities": ["physiotherapy"], "actor": "eco-lead", "reason": "整层改造停用所有数字疗法",
    })
    assert changed.status_code == 200
    assert code not in changed.json()["revoked_reservations"]
    assert client.get(f"/api/matching/reservations/{code}").json()["status"] == "confirmed"
    timeline = client.get("/api/matching/capacity-timeline?site_code=hospital-a").json()["items"]
    assert timeline[-1]["held_after"] == 0
    assert timeline[-1]["confirmed_after"] == 1
