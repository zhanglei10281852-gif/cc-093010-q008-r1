from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import timedelta
from typing import Any

from app.catalog.repository import CatalogRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.matching.repository import MatchingRepository
from app.matching.scoring import evaluate_pair

DEFAULT_HOSPITAL_RESPONSE_SECONDS = 48 * 3600
DEFAULT_ENTERPRISE_CONFIRM_SECONDS = 12 * 3600
ACTIVE_STATUSES = ("pending", "conditional", "accepted")
HOLD_STATUSES = ("pending", "conditional", "accepted")


def _positive_seconds(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(f"配置 {name} 必须是整数秒") from exc
    if not 60 <= value <= 30 * 24 * 3600:
        raise ValidationError(f"配置 {name} 必须在 60 秒到 30 天之间")
    return value


class MatchingService:
    """候选推荐、限时预留、双方确认、容量台账与到期/变更联动。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MatchingRepository(self.connection)

    # ---- 画像与科室 ----

    def set_product_eligibility(self, product_code: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
        product = self.repository_owned().product_by_code(product_code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        self._validate_windows(data.get("open_from"), data.get("open_until"))
        with transaction(immediate=True) as connection:
            return MatchingRepository(connection).upsert_profile(product["id"], data, actor, to_storage(self.clock.now()))

    def add_site_department(self, site_code: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
        del actor
        site = CatalogRepository(self.connection).site_by_code(site_code)
        if site is None:
            raise NotFoundError("试点场地不存在")
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            exists = connection.execute(
                "SELECT 1 FROM site_departments WHERE site_id=? AND code=?", (site["id"], data["code"]),
            ).fetchone()
            if exists:
                raise ConflictError("场地内科室编码已存在")
            return repository.add_department(site["id"], data, to_storage(self.clock.now()))

    def update_site_department(self, site_code: str, department_id: int, changes: dict[str, Any], actor: str) -> dict[str, Any]:
        del actor
        site = CatalogRepository(self.connection).site_by_code(site_code)
        if site is None:
            raise NotFoundError("试点场地不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的科室字段")
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            department = connection.execute(
                "SELECT * FROM site_departments WHERE id=? AND site_id=?", (department_id, site["id"]),
            ).fetchone()
            if department is None:
                raise NotFoundError("场地科室不存在")
            updated = repository.update_department(department_id, values, now)
            if "capabilities" in values or "max_risk_level" in values or "ethics_ready" in values or "active" in values:
                self._revoke_for_site_change(connection, site, f"科室 {department['name']} 能力或承接条件变化", now_value)
            return updated or {}

    # ---- 推荐 ----

    def recommend(self, payload: dict[str, Any]) -> dict[str, Any]:
        catalog = CatalogRepository(self.connection)
        products = self._select_products(catalog, payload.get("product_codes"))
        sites = self._select_sites(catalog, payload.get("site_codes"))
        if not products or not sites:
            raise ValidationError("推荐至少需要一个产品和一个场地")
        now_value = self.clock.now()
        now = to_storage(now_value)
        profiles = {
            int(row["product_id"]): dict(row)
            for row in self.connection.execute("SELECT * FROM product_eligibility_profiles").fetchall()
        }
        departments = self.repository.departments_for_sites([site["id"] for site in sites])
        batch_code = payload.get("batch_code") or (
            "RC-" + hashlib.sha1(f"{now}:{payload.get('requested_by','')}:{os.urandom(8).hex()}".encode()).hexdigest()[:10]
        )
        items: list[dict[str, Any]] = []
        for product in products:
            evaluations = [
                evaluate_pair(
                    dict(product), dict(site),
                    profile=profiles.get(int(product["id"])),
                    departments=departments.get(int(site["id"]), []),
                    requested_region=payload.get("requested_region", ""),
                    requested_population=payload.get("requested_population", ""),
                    now=now,
                )
                for site in sites
            ]
            evaluations.sort(key=lambda item: (item.eligible, item.score), reverse=True)
            for rank, evaluation in enumerate(evaluations, start=1):
                item = evaluation.as_dict()
                item.update(rank=rank, requested_region=payload.get("requested_region", ""), requested_population=payload.get("requested_population", ""))
                items.append(item)
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            saved_ids = repository.save_recommendations(batch_code, items, payload.get("requested_by", ""), now)
            for item, recommendation_id in zip(items, saved_ids):
                item["id"] = recommendation_id
                if item["eligible"]:
                    repository.append_ledger(
                        {"site_id": item["site_id"], "product_id": item["product_id"], "event_type": "recommended",
                         "reason": f"批次 {batch_code} 第 {item['rank']} 顺位，得分 {item['score']}",
                         "actor": payload.get("requested_by", ""),
                         "detail": {"batch_code": batch_code, "recommendation_id": recommendation_id, "rank": item["rank"], "score": item["score"]}},
                        now,
                    )
        return {"batch_code": batch_code, "items": self._enrich_items(items, products, sites)}

    # ---- 预留与双方确认 ----

    def reserve(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            catalog = CatalogRepository(connection)
            # 幂等重放优先：同一运营员+幂等键直接返回原预留，不再重复占容量
            replay = repository.reservation_by_idempotency(actor, payload["idempotency_key"])
            if replay is not None:
                target_product, target_site = self._target_codes(catalog, payload)
                if replay["product_id"] != target_product["id"] or replay["site_id"] != target_site["id"]:
                    raise ConflictError("同一幂等键对应了不同的产品或场地")
                return self._hydrated(repository, replay)
            product, site, department_id, recommendation_id = self._reservation_targets(catalog, repository, payload)
            quantity = int(payload.get("quantity", 1))
            self._ensure_capacity(repository, site["id"], quantity)
            hospital_seconds = int(payload.get("hospital_response_seconds") or _positive_seconds("HEALTH_HOSPITAL_RESPONSE_SECONDS", DEFAULT_HOSPITAL_RESPONSE_SECONDS))
            enterprise_seconds = int(payload.get("enterprise_confirm_seconds") or _positive_seconds("HEALTH_ENTERPRISE_CONFIRM_SECONDS", DEFAULT_ENTERPRISE_CONFIRM_SECONDS))
            hospital_due = to_storage(now_value + timedelta(seconds=hospital_seconds))
            code = "RSV-" + hashlib.sha1(f"{actor}:{payload['idempotency_key']}:{now}".encode()).hexdigest()[:12].upper()
            reservation = repository.create_reservation(
                {"reservation_code": code, "product_id": product["id"], "site_id": site["id"],
                 "department_id": department_id, "recommendation_id": recommendation_id,
                 "requested_by": actor, "quantity": quantity, "idempotency_key": payload["idempotency_key"],
                 "hospital_due_at": hospital_due, "enterprise_confirm_seconds": enterprise_seconds,
                 "note": payload.get("note", "")},
                now,
            )
            repository.append_ledger(
                {"site_id": site["id"], "reservation_id": reservation["id"], "product_id": product["id"],
                 "event_type": "reserved", "held_delta": quantity, "reason": "运营员发起限时预留，等待医院答复",
                 "actor": actor, "detail": {"reservation_code": code, "hospital_due_at": hospital_due}},
                now,
            )
            repository.add_message(
                reservation["id"], "hospital", site["code"],
                f"新试点预留待答复：{product['name']}",
                f"运营方 {actor} 申请在贵场地为产品 {product['name']}（{product['code']}）预留 {quantity} 个名额，请于 {hospital_due} 前接受、拒绝或附加条件。",
                "预留等待医院答复", now,
            )
            return self._hydrated(repository, repository.reservation_by_id(reservation["id"]))

    def hospital_respond(self, reservation_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        decision = payload["decision"]
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            reservation = self._require_reservation(repository, reservation_code)
            self._check_due(reservation, now)
            if reservation["status"] != "pending":
                raise ConflictError(f"预留当前状态为 {reservation['status']}，医院不能重复答复")
            catalog = CatalogRepository(connection)
            product = catalog.product_by_id(reservation["product_id"])
            site = catalog.site_by_id(reservation["site_id"])
            actor = payload.get("responded_by") or site["code"]
            if decision == "rejected":
                reason = payload.get("reason", "").strip()
                if len(reason) < 4:
                    raise ValidationError("医院拒绝时必须说明原因")
                self._release(repository, reservation, "hospital_rejected", -int(reservation["quantity"]), 0,
                              "医院拒绝预留：" + reason, actor, now)
                repository.transition_reservation(
                    reservation["id"], expected_status=("pending",),
                    updates={"status": "rejected", "hospital_responded_by": actor, "hospital_responded_at": now,
                             "released_at": now, "release_reason": reason[:500]},
                    now=now,
                )
                repository.add_message(reservation["id"], "enterprise", product["organization"],
                                       f"预留被场地拒绝：{product['name']}",
                                       f"场地 {site['name']} 拒绝了预留 {reservation['reservation_code']}：{reason}",
                                       "医院拒绝", now)
            else:
                conditions = payload.get("conditions", [])
                if decision == "conditional" and not conditions:
                    raise ValidationError("附加条件答复必须给出至少一条条件")
                enterprise_due = to_storage(now_value + timedelta(seconds=int(reservation["enterprise_confirm_seconds"])))
                status = "conditional" if decision == "conditional" else "accepted"
                repository.transition_reservation(
                    reservation["id"], expected_status=("pending",),
                    updates={"status": status, "hospital_responded_by": actor, "hospital_responded_at": now,
                             "conditions_json": json.dumps(conditions, ensure_ascii=False), "enterprise_due_at": enterprise_due},
                    now=now,
                )
                event = "hospital_conditional" if decision == "conditional" else "hospital_accepted"
                text = "医院附加条件：" + "；".join(conditions) if conditions else "医院接受预留"
                repository.append_ledger(
                    {"site_id": site["id"], "reservation_id": reservation["id"], "product_id": product["id"],
                     "event_type": event, "reason": text + f"，企业需于 {enterprise_due} 前确认", "actor": actor,
                     "detail": {"conditions": conditions, "enterprise_due_at": enterprise_due}},
                    now,
                )
                repository.add_message(
                    reservation["id"], "enterprise", product["organization"],
                    f"场地已{('附加条件' if decision == 'conditional' else '接受')}：{product['name']}",
                    f"场地 {site['name']} 对预留 {reservation['reservation_code']} 的答复为{text}。请在 {enterprise_due} 前确认，逾期名额自动释放。",
                    text, now,
                )
            return self._hydrated(repository, repository.reservation_by_id(reservation["id"]))

    def enterprise_confirm(self, reservation_code: str, confirmed_by: str, accepted_conditions: bool = True) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            reservation = self._require_reservation(repository, reservation_code)
            self._check_due(reservation, now)
            if reservation["status"] not in ("accepted", "conditional"):
                raise ConflictError(f"预留当前状态为 {reservation['status']}，企业无法确认；名额未被占用")
            if reservation["status"] == "conditional" and not accepted_conditions:
                raise ConflictError("企业尚未接受医院附加条件，不能完成确认")
            catalog = CatalogRepository(connection)
            product = catalog.product_by_id(reservation["product_id"])
            site = catalog.site_by_id(reservation["site_id"])
            affected = repository.transition_reservation(
                reservation["id"], expected_status=("accepted", "conditional"),
                updates={"status": "confirmed", "enterprise_confirmed_by": confirmed_by, "enterprise_confirmed_at": now},
                now=now,
            )
            if affected != 1:
                # 并发的第二个确认必然落到这里：名额只有一个
                raise ConflictError("名额已被处理，并发确认不会产生第二个成功名额")
            repository.append_ledger(
                {"site_id": site["id"], "reservation_id": reservation["id"], "product_id": product["id"],
                 "event_type": "enterprise_confirmed", "held_delta": -int(reservation["quantity"]),
                 "confirmed_delta": int(reservation["quantity"]),
                 "reason": f"企业 {confirmed_by} 已确认，名额正式占用", "actor": confirmed_by,
                 "detail": {"reservation_code": reservation["reservation_code"]}},
                now,
            )
            repository.add_message(reservation["id"], "hospital", site["code"],
                                   f"企业已确认，名额正式生效：{product['name']}",
                                   f"预留 {reservation['reservation_code']} 已由企业 {confirmed_by} 确认，{reservation['quantity']} 个名额正式占用。",
                                   "企业确认生效", now)
            repository.add_message(reservation["id"], "operator", reservation["requested_by"],
                                   f"预留已生效：{reservation['reservation_code']}",
                                   f"企业 {confirmed_by} 确认了 {product['name']} 在 {site['name']} 的名额。",
                                   "企业确认生效", now)
            return self._hydrated(repository, repository.reservation_by_id(reservation["id"]))

    def cancel(self, reservation_code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            reservation = self._require_reservation(repository, reservation_code)
            if reservation["status"] not in ACTIVE_STATUSES:
                raise ConflictError(f"预留当前状态为 {reservation['status']}，不能取消")
            self._release(repository, reservation, "cancelled", -int(reservation["quantity"]), 0,
                          f"运营方 {actor} 取消预留：{reason}", actor, now)
            repository.transition_reservation(
                reservation["id"], expected_status=ACTIVE_STATUSES,
                updates={"status": "cancelled", "released_at": now, "release_reason": reason[:500]},
                now=now,
            )
            self._notify_release(repository, reservation, "运营方取消预留", reason, now)
            return self._hydrated(repository, repository.reservation_by_id(reservation["id"]))

    # ---- 到期释放（可控时间规则） ----

    def sweep_expired(self, *, actor: str = "system-clock") -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expired: list[str] = []
        with transaction(immediate=True) as connection:
            repository = MatchingRepository(connection)
            for row in repository.due_reservations(now):
                reservation = dict(row)
                if reservation["status"] == "pending":
                    reason = f"医院答复时限已到（{reservation['hospital_due_at']}），名额自动释放"
                else:
                    reason = f"企业确认时限已到（{reservation['enterprise_due_at']}），名额自动释放"
                self._release(repository, reservation, "expired", -int(reservation["quantity"]), 0, reason, actor, now)
                repository.transition_reservation(
                    reservation["id"], expected_status=ACTIVE_STATUSES,
                    updates={"status": "expired", "released_at": now, "release_reason": reason},
                    now=now,
                )
                self._notify_release(repository, reservation, "预留到期自动释放", reason, now)
                expired.append(reservation["reservation_code"])
        return {"expired": expired, "checked_at": now}

    # ---- 产品暂停 / 场地能力变化：只撤销尚未生效的匹配 ----

    def suspend_product(self, product_code: str, actor: str, reason: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            product = catalog.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            connection.execute("UPDATE health_products SET active=0,updated_at=? WHERE id=?", (now, product["id"]))
            repository = MatchingRepository(connection)
            revoked = self._revoke_for_product(repository, dict(product), f"产品暂停：{reason}", actor, now_value)
            return {"product_code": product_code, "active": 0, "revoked_reservations": revoked}

    def resume_product(self, product_code: str, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            product = catalog.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            connection.execute("UPDATE health_products SET active=1,updated_at=? WHERE id=?", (now, product["id"]))
            return {"product_code": product_code, "active": 1}

    def change_site_capabilities(self, site_code: str, capabilities: list[str], actor: str, reason: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            site = catalog.site_by_code(site_code)
            if site is None:
                raise NotFoundError("试点场地不存在")
            connection.execute(
                "UPDATE pilot_sites SET capabilities_json=?,updated_at=? WHERE id=?",
                (json.dumps(sorted(set(capabilities)), ensure_ascii=False), now, site["id"]),
            )
            site = dict(connection.execute("SELECT * FROM pilot_sites WHERE id=?", (site["id"],)).fetchone())
            revoked = self._revoke_for_site_change(connection, site, f"场地能力变化：{reason}", now_value)
            return {"site_code": site_code, "capabilities": sorted(set(capabilities)), "revoked_reservations": revoked}

    # ---- 查询：仪表盘、容量台账、消息 ----

    def dashboard(self, *, within_seconds: int = 3600) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        soon = to_storage(now_value + timedelta(seconds=within_seconds))
        pending = self.repository.list_reservations(statuses=("pending",), limit=500)
        conditional = self.repository.list_reservations(statuses=("conditional", "accepted"), limit=500)
        hospital_due_soon = [item for item in pending if item["hospital_due_at"] <= soon]
        enterprise_due_soon = [item for item in conditional if item["enterprise_due_at"] and item["enterprise_due_at"] <= soon]
        overdue = [item["reservation_code"] for item in self.repository.due_reservations(now)]
        return {
            "checked_at": now,
            "awaiting_hospital_reply": len(pending),
            "awaiting_enterprise_confirm": len(conditional),
            "hospital_due_soon": [self._brief(item) for item in sorted(hospital_due_soon, key=lambda x: x["hospital_due_at"])],
            "enterprise_due_soon": [self._brief(item) for item in sorted(enterprise_due_soon, key=lambda x: x["enterprise_due_at"] or "")],
            "overdue_pending_sweep": overdue,
        }

    def list_reservations(self, **filters: Any) -> list[dict[str, Any]]:
        return [self._hydrated_dict(item) for item in self.repository.list_reservations(**filters)]

    def search_reservations(
        self, *, statuses: list[str] | None = None, site_code: str | None = None,
        product_code: str | None = None, requested_by: str | None = None,
    ) -> list[dict[str, Any]]:
        catalog = CatalogRepository(self.connection)
        filters: dict[str, Any] = {}
        if statuses:
            filters["statuses"] = statuses
        if site_code:
            site = catalog.site_by_code(site_code)
            if site is None:
                raise NotFoundError("试点场地不存在")
            filters["site_id"] = site["id"]
        if product_code:
            product = catalog.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            filters["product_id"] = product["id"]
        if requested_by:
            filters["requested_by"] = requested_by
        return self.list_reservations(**filters)

    def get_reservation(self, code: str) -> dict[str, Any]:
        reservation = self.repository.reservation_by_code(code)
        if reservation is None:
            raise NotFoundError("容量预留不存在")
        return self._hydrated(self.repository, reservation)

    def capacity_timeline(self, start: str | None, end: str | None, site_code: str | None = None) -> dict[str, Any]:
        site_id = None
        if site_code:
            site = CatalogRepository(self.connection).site_by_code(site_code)
            if site is None:
                raise NotFoundError("试点场地不存在")
            site_id = site["id"]
        items = self.repository.ledger_window(start=start, end=end, site_id=site_id)
        return {"window": {"start": start, "end": end}, "items": items}

    def messages(self, recipient_role: str, recipient: str, unread_only: bool = False) -> dict[str, Any]:
        return {"items": self.repository.list_messages(recipient_role, recipient, unread_only)}

    def mark_read(self, message_id: int, recipient_role: str, recipient: str) -> dict[str, Any]:
        affected = self.repository.mark_message_read(message_id, recipient_role, recipient, to_storage(self.clock.now()))
        return {"updated": affected}

    # ---- 内部辅助 ----

    def repository_owned(self) -> CatalogRepository:
        return CatalogRepository(self.connection)

    def _select_products(self, catalog: CatalogRepository, codes: list[str] | None) -> list[dict[str, Any]]:
        if codes:
            products = [catalog.product_by_code(code) for code in codes]
            if any(item is None for item in products):
                raise NotFoundError("部分健康创新产品不存在")
            return [item for item in products if item]  # type: ignore[list-item]
        return catalog.list_products(category=None, status=None, active_only=False, limit=500)

    def _select_sites(self, catalog: CatalogRepository, codes: list[str] | None) -> list[dict[str, Any]]:
        if codes:
            sites = [catalog.site_by_code(code) for code in codes]
            if any(item is None for item in sites):
                raise NotFoundError("部分试点场地不存在")
            return [item for item in sites if item]  # type: ignore[list-item]
        return catalog.list_sites(status=None, site_type=None, capability=None)

    @staticmethod
    def _validate_windows(open_from: str | None, open_until: str | None) -> None:
        if open_from and open_until and open_until <= open_from:
            raise ValidationError("开放结束时间不能早于开始时间")

    def _reservation_targets(
        self, catalog: CatalogRepository, repository: MatchingRepository, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], int | None, int | None]:
        recommendation_id = payload.get("recommendation_id")
        department_id = None
        if recommendation_id is not None:
            recommendation = repository.recommendation_by_id(int(recommendation_id))
            if recommendation is None:
                raise NotFoundError("匹配推荐不存在")
            if not recommendation["eligible"]:
                raise ValidationError("该候选存在硬性阻断，不能发起预留", context={"blockers": json.loads(recommendation["blockers_json"])})
            product = catalog.product_by_id(int(recommendation["product_id"]))
            site = catalog.site_by_id(int(recommendation["site_id"]))
            department_id = recommendation["department_id"]
            requested_region = recommendation["requested_region"] or ""
            requested_population = recommendation["requested_population"] or ""
        else:
            product = catalog.product_by_code(payload["product_code"])
            site = catalog.site_by_code(payload["site_code"])
            requested_region = ""
            requested_population = ""
        if product is None or site is None:
            raise NotFoundError("健康创新产品或试点场地不存在")
        if not product["active"] or product["regulatory_status"] == "暂停":
            raise ConflictError("产品已暂停，不能发起新预留")
        if site["status"] != "active":
            raise ConflictError("场地当前不可承接新预留")
        # 发起时按当前状态重新评估，防止推荐之后产品或场地条件已经变化
        profile = repository.profile_by_product(int(product["id"]))
        departments = repository.departments_for_sites([int(site["id"])]).get(int(site["id"]), [])
        evaluation = evaluate_pair(
            product, site, profile=profile, departments=departments,
            requested_region=requested_region, requested_population=requested_population,
            now=to_storage(self.clock.now()),
        )
        if not evaluation.eligible:
            raise ConflictError("候选当前已不满足匹配条件，不能发起预留", context={"blockers": evaluation.blockers})
        if department_id is None:
            department_id = evaluation.department_id
        return product, site, department_id, recommendation_id

    @staticmethod
    def _target_codes(catalog: CatalogRepository, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if payload.get("recommendation_id") is not None:
            recommendation = catalog.connection.execute(
                "SELECT product_id,site_id FROM matching_recommendations WHERE id=?",
                (int(payload["recommendation_id"]),),
            ).fetchone()
            if recommendation is None:
                raise NotFoundError("匹配推荐不存在")
            product = catalog.product_by_id(int(recommendation["product_id"]))
            site = catalog.site_by_id(int(recommendation["site_id"]))
        else:
            product = catalog.product_by_code(payload["product_code"])
            site = catalog.site_by_code(payload["site_code"])
        if product is None or site is None:
            raise NotFoundError("健康创新产品或试点场地不存在")
        return product, site

    @staticmethod
    def _ensure_capacity(repository: MatchingRepository, site_id: int, quantity: int) -> None:
        site = CatalogRepository(repository.connection).site_by_id(site_id)
        active = repository.active_for_site(site_id)
        held = sum(int(item["quantity"]) for item in active if item["status"] in HOLD_STATUSES)
        confirmed = sum(int(item["quantity"]) for item in active if item["status"] == "confirmed")
        if held + confirmed + quantity > int(site["max_concurrent"]):
            raise ConflictError(
                f"场地剩余容量不足：上限 {site['max_concurrent']}，已确认 {confirmed}，预留中 {held}，本次申请 {quantity}",
            )

    @staticmethod
    def _require_reservation(repository: MatchingRepository, code: str) -> dict[str, Any]:
        reservation = repository.reservation_by_code(code)
        if reservation is None:
            raise NotFoundError("容量预留不存在")
        return reservation

    @staticmethod
    def _check_due(reservation: dict[str, Any], now: str) -> None:
        if reservation["status"] == "pending" and reservation["hospital_due_at"] <= now:
            raise ConflictError("医院答复时限已过，请等待系统释放名额")
        if reservation["status"] in ("accepted", "conditional") and reservation["enterprise_due_at"] and reservation["enterprise_due_at"] <= now:
            raise ConflictError("企业确认时限已过，请等待系统释放名额")

    def _release(
        self, repository: MatchingRepository, reservation: dict[str, Any], event_type: str,
        held_delta: int, confirmed_delta: int, reason: str, actor: str, now: str,
    ) -> None:
        repository.append_ledger(
            {"site_id": reservation["site_id"], "reservation_id": reservation["id"],
             "product_id": reservation["product_id"], "event_type": event_type,
             "held_delta": held_delta, "confirmed_delta": confirmed_delta,
             "reason": reason, "actor": actor,
             "detail": {"reservation_code": reservation["reservation_code"], "previous_status": reservation["status"]}},
            now,
        )

    def _notify_release(self, repository: MatchingRepository, reservation: dict[str, Any], subject: str, reason: str, now: str) -> None:
        catalog = CatalogRepository(repository.connection)
        product = catalog.product_by_id(reservation["product_id"])
        site = catalog.site_by_id(reservation["site_id"])
        repository.add_message(reservation["id"], "hospital", site["code"],
                               f"{subject}：{product['name']}", f"预留 {reservation['reservation_code']}：{reason}", reason, now)
        repository.add_message(reservation["id"], "enterprise", product["organization"],
                               f"{subject}：{product['name']}", f"预留 {reservation['reservation_code']}：{reason}", reason, now)

    def _revoke_for_product(self, repository: MatchingRepository, product: dict[str, Any], reason: str, actor: str, now_value) -> list[str]:
        now = to_storage(now_value)
        codes: list[str] = []
        for reservation in repository.active_for_product(product["id"]):
            self._release(repository, reservation, "cancelled", -int(reservation["quantity"]), 0, reason, actor, now)
            repository.transition_reservation(
                reservation["id"], expected_status=ACTIVE_STATUSES,
                updates={"status": "cancelled", "released_at": now, "release_reason": reason[:500]},
                now=now,
            )
            self._notify_release(repository, reservation, "产品暂停，未生效预留撤销", reason, now)
            codes.append(reservation["reservation_code"])
        return codes

    def _revoke_for_site_change(self, connection: sqlite3.Connection, site: dict[str, Any], reason: str, now_value) -> list[str]:
        now = to_storage(now_value)
        repository = MatchingRepository(connection)
        catalog = CatalogRepository(connection)
        profiles = {
            int(row["product_id"]): dict(row)
            for row in connection.execute("SELECT * FROM product_eligibility_profiles").fetchall()
        }
        departments = repository.departments_for_sites([site["id"]]).get(site["id"], [])
        codes: list[str] = []
        for reservation in repository.active_for_site(site["id"]):
            if reservation["status"] == "confirmed":
                # 已正式占用的名额不受场地能力变化影响
                continue
            product = catalog.product_by_id(reservation["product_id"])
            evaluation = evaluate_pair(
                product, site, profile=profiles.get(int(product["id"])), departments=departments, now=now,
            )
            if evaluation.eligible:
                continue
            detail = "；".join(evaluation.blockers)
            full_reason = f"{reason}，重新评估不再满足：{detail}"
            self._release(repository, reservation, "cancelled", -int(reservation["quantity"]), 0, full_reason, "system-matching", now)
            repository.transition_reservation(
                reservation["id"], expected_status=ACTIVE_STATUSES,
                updates={"status": "cancelled", "released_at": now, "release_reason": full_reason[:500]},
                now=now,
            )
            self._notify_release(repository, reservation, "场地能力变化，未生效预留撤销", full_reason, now)
            codes.append(reservation["reservation_code"])
        return codes

    @staticmethod
    def _brief(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "reservation_code": item["reservation_code"], "product_code": item.get("product_code"),
            "site_code": item.get("site_code"), "status": item["status"],
            "hospital_due_at": item["hospital_due_at"], "enterprise_due_at": item["enterprise_due_at"],
        }

    def _enrich_items(self, items: list[dict[str, Any]], products: list[dict[str, Any]], sites: list[dict[str, Any]]) -> list[dict[str, Any]]:
        product_names = {int(item["id"]): {"code": item["code"], "name": item["name"], "category": item["category"], "risk_level": item["risk_level"]} for item in products}
        site_names = {int(item["id"]): {"code": item["code"], "name": item["name"], "region": item["region"], "site_type": item["site_type"]} for item in sites}
        enriched = []
        for item in items:
            enriched.append({**item, "product": product_names[item["product_id"]], "site": site_names[item["site_id"]]})
        return enriched

    @staticmethod
    def _hydrated_dict(item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        for key in ("conditions_json",):
            if key in result and isinstance(result[key], str):
                result["conditions"] = json.loads(result[key] or "[]")
        return result

    def _hydrated(self, repository: MatchingRepository, reservation: dict[str, Any]) -> dict[str, Any]:
        result = dict(reservation)
        result["conditions"] = json.loads(result.get("conditions_json") or "[]")
        catalog = CatalogRepository(repository.connection)
        product = catalog.product_by_id(int(reservation["product_id"]))
        site = catalog.site_by_id(int(reservation["site_id"]))
        result["product_code"] = product["code"]
        result["product_name"] = product["name"]
        result["site_code"] = site["code"]
        result["site_name"] = site["name"]
        return result
