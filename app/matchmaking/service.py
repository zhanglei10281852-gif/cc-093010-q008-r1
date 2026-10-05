from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.matchmaking.ranking import CATEGORY_CAPABILITY, MatchingRanker
from app.matchmaking.repository import PENDING_STATUSES, MatchmakingRepository

# 预留的受控时间规则：默认保留时长与允许范围（秒）
DEFAULT_TTL_SECONDS = 72 * 3600
MIN_TTL_SECONDS = 15 * 60
MAX_TTL_SECONDS = 14 * 24 * 3600

PENDING_STATUS_SET = set(PENDING_STATUSES)
RISK_TEXT = {"low": "低风险", "medium": "中风险", "high": "高风险"}


class MatchmakingService:
    """产品-场地候选推荐、限时预留、医院/企业双方确认与容量释放。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MatchmakingRepository(self.connection)
        self.ranker = MatchingRanker()

    # ---------- 候选推荐（只读，带理由排序） ----------

    def recommend(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        window_start = payload.get("window_start")
        window_end = payload.get("window_end")
        if bool(window_start) != bool(window_end):
            raise ValidationError("计划时间窗必须同时提供 window_start 与 window_end")
        if window_start and window_end:
            parsed_start = from_storage(window_start)
            parsed_end = from_storage(window_end)
            if parsed_start is None or parsed_end is None or parsed_end <= parsed_start:
                raise ValidationError("计划时间窗的结束时间必须晚于开始时间")
        products = self.repository.candidate_products(
            category=payload.get("category"), risk_level=payload.get("risk_level"),
            include_inactive=bool(payload.get("include_inactive", False)),
        )
        if payload.get("product_code"):
            products = [item for item in products if item["code"] == payload["product_code"].strip().lower()]
        sites = self.repository.candidate_sites(region=payload.get("region"), site_type=payload.get("site_type"))
        if payload.get("site_code"):
            sites = [item for item in sites if item["code"] == payload["site_code"].strip().lower()]
        if not products:
            raise NotFoundError("没有符合筛选条件的产品")
        if not sites:
            raise NotFoundError("没有符合筛选条件的场地")
        # 容量按当前真实占用计算（与预留写入同一口径）
        occupied = {int(site["id"]): self.repository.occupied_slots(int(site["id"])) for site in sites}
        cards = self.ranker.rank(
            products, sites, now=now_value,
            slot_quantity=int(payload.get("slot_quantity", 1)),
            window_start=payload.get("window_start"), window_end=payload.get("window_end"),
            occupied=occupied,
        )
        eligible = [card for card in cards if card["eligible"]]
        return {
            "generated_at": to_storage(now_value),
            "candidate_count": len(cards),
            "eligible_count": len(eligible),
            "candidates": cards,
        }

    # ---------- 限时预留（运营发起） ----------

    def reserve(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        ttl = int(payload.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
        if not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
            raise ValidationError(f"预留时长必须在 {MIN_TTL_SECONDS} 到 {MAX_TTL_SECONDS} 秒之间")
        slot_quantity = int(payload.get("slot_quantity", 1))
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires_at = to_storage(now_value + timedelta(seconds=ttl))
        with transaction(immediate=True) as connection:
            repository = MatchmakingRepository(connection)
            product = repository.product_by_code(payload["product_code"])
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            site = repository.site_by_code(payload["site_code"])
            if site is None:
                raise NotFoundError("试点场地不存在")
            # 幂等：同一运营同一幂等键返回原预留
            existing = self._find_idempotent(connection, actor, payload.get("idempotency_key", ""))
            if existing is not None:
                return repository.detail(int(existing["id"])) or {}
            # 再次带理由校验，防止绕过推荐直接预留错配对象（高风险推荐给无能力科室）
            self._assert_reservable(product, site, now_value, slot_quantity, repository)
            if repository.pending_matching(int(product["id"]), int(site["id"])) is not None:
                raise ConflictError("该产品在该场地已有等待答复的名额，不能重复发起预留")
            matching_code = self._matching_code(product["code"], site["code"], now)
            try:
                matching = repository.insert_matching(
                    matching_code=matching_code, product_id=int(product["id"]), site_id=int(site["id"]),
                    slot_quantity=slot_quantity, requested_by=actor,
                    idempotency_key=payload.get("idempotency_key", ""),
                    hospital_contact=payload.get("hospital_contact", ""), expires_at=expires_at, now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("预留写入冲突，可能已被并发请求占用") from exc
            repository.add_event(
                int(matching["id"]), "reserved", actor, "", "reserved",
                payload.get("reason", "运营发起限时预留"),
                {"ttl_seconds": ttl, "slot_quantity": slot_quantity, "expires_at": expires_at}, now,
            )
            repository.add_notification(int(matching["id"]), "hospital", "reserved",
                                        f"新试点名额待答复：{product['name']} @ {site['name']}",
                                        f"预留将于 {expires_at} 到期，请在限时内接受、拒绝或附加条件", now)
            repository.add_notification(int(matching["id"]), "enterprise", "reserved",
                                        f"名额预留已发起：{product['name']} @ {site['name']}",
                                        "等待医院答复，医院接受后需贵方确认才正式占用容量", now)
            return repository.detail(int(matching["id"])) or {}

    # ---------- 医院答复 ----------

    def hospital_respond(self, matching_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        decision = payload["decision"]
        actor = payload.get("actor", "hospital")
        now_value = self.clock.now()
        now = to_storage(now_value)
        expired_matching: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            repository = MatchmakingRepository(connection)
            matching = self._require_active_pending(repository, matching_code)
            if self._expire_if_due(repository, matching, now_value, now):
                # 先提交到期释放，事务外再告知答复无效
                expired_matching = matching
            else:
                return self._hospital_apply(repository, matching, decision, actor, payload, now)
        raise ConflictError(f"预留已于 {expired_matching['expires_at']} 到期，医院答复不再有效")

    @staticmethod
    def _hospital_apply(repository: MatchmakingRepository, matching: dict[str, Any], decision: str, actor: str, payload: dict[str, Any], now: str) -> dict[str, Any]:
        if decision == "accept":
            updates = {"status": "hospital_accepted", "hospital_responded_at": now, "hospital_condition": ""}
            to_status, event, reason = "hospital_accepted", "hospital_accepted", payload.get("note", "医院接受名额")
        elif decision == "conditional":
            condition = payload.get("condition", "").strip()
            if len(condition) < 4:
                raise ValidationError("附加条件时需要填写具体条件")
            updates = {"status": "hospital_conditional", "hospital_responded_at": now, "hospital_condition": condition}
            to_status, event, reason = "hospital_conditional", "hospital_conditional", condition
        else:
            updates = {"status": "hospital_rejected", "hospital_responded_at": now,
                       "released_at": now, "release_reason": payload.get("note", "医院拒绝名额")}
            to_status, event, reason = "hospital_rejected", "hospital_rejected", updates["release_reason"]
        affected = repository.transition(int(matching["id"]), expected=("reserved",), updates=updates, actor=actor, now=now)
        if affected != 1:
            raise ConflictError("名额已不在等待医院答复状态，医院答复未被接受")
        repository.add_event(int(matching["id"]), event, actor, matching["status"], to_status, reason,
                             {"note": payload.get("note", "")}, now)
        if decision == "reject":
            MatchmakingService._notify_release(repository, matching, "hospital_rejected", reason, now,
                                               hospital_rejected=True)
        else:
            tip = "医院已接受，请企业确认后正式占用容量" if decision == "accept" else f"医院附加条件：{condition}，请企业评估确认"
            repository.add_notification(int(matching["id"]), "enterprise", event,
                                        f"医院已答复：{matching['matching_code']}", tip, now)
            repository.add_notification(int(matching["id"]), "operator", event,
                                        f"医院已答复：{matching['matching_code']}", tip, now)
        return repository.detail(int(matching["id"])) or {}

    # ---------- 企业确认 / 拒绝 ----------

    def enterprise_confirm(self, matching_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        actor = payload.get("actor", "enterprise")
        now_value = self.clock.now()
        now = to_storage(now_value)
        expired_matching: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            repository = MatchmakingRepository(connection)
            matching = self._require_active_pending(repository, matching_code)
            if matching["status"] != "hospital_accepted" and matching["status"] != "hospital_conditional":
                raise ConflictError("医院尚未接受，企业不能确认名额")
            if self._expire_if_due(repository, matching, now_value, now):
                expired_matching = matching
            else:
                return self._enterprise_apply(repository, matching, payload, actor, now)
        raise ConflictError(f"预留已于 {expired_matching['expires_at']} 到期，企业确认不再有效")

    @staticmethod
    def _enterprise_apply(repository: MatchmakingRepository, matching: dict[str, Any], payload: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        if payload.get("decision") == "decline":
            reason = payload.get("reason", "企业放弃名额")
            affected = repository.transition(
                int(matching["id"]), expected=("hospital_accepted", "hospital_conditional"),
                updates={"status": "enterprise_declined", "enterprise_responded_at": now,
                         "released_at": now, "release_reason": reason}, actor=actor, now=now,
            )
            if affected != 1:
                raise ConflictError("名额状态已变化，企业答复未被接受")
            repository.add_event(int(matching["id"]), "enterprise_declined", actor, matching["status"], "enterprise_declined", reason, {}, now)
            MatchmakingService._notify_release(repository, matching, "enterprise_declined", reason, now)
            return repository.detail(int(matching["id"])) or {}
        affected = repository.transition(
            int(matching["id"]), expected=("hospital_accepted", "hospital_conditional"),
            updates={"status": "confirmed", "enterprise_responded_at": now, "confirmed_at": now},
            actor=actor, now=now,
        )
        if affected != 1:
            # 并发时只有一方能成功
            raise ConflictError("名额已被其他流程处理，企业确认未成功")
        detail = {"condition_accepted": matching["status"] == "hospital_conditional",
                  "hospital_condition": matching["hospital_condition"]}
        repository.add_event(int(matching["id"]), "confirmed", actor, matching["status"], "confirmed",
                             payload.get("reason", "企业确认，名额正式生效"), detail, now)
        repository.add_notification(int(matching["id"]), "hospital", "confirmed",
                                    f"名额已正式确认：{matching['matching_code']}",
                                    "企业已确认，容量正式占用，可按计划开点", now)
        repository.add_notification(int(matching["id"]), "operator", "confirmed",
                                    f"名额已正式确认：{matching['matching_code']}", "双方确认完成", now)
        return repository.detail(int(matching["id"])) or {}

    # ---------- 运营撤销（仅未生效） ----------

    def revoke(self, matching_code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = MatchmakingRepository(connection)
            matching = repository.matching_by_code(matching_code)
            if matching is None:
                raise NotFoundError("试点名额记录不存在")
            if matching["status"] not in PENDING_STATUS_SET:
                raise ConflictError("只能撤销尚未生效（等待医院或企业答复）的名额")
            affected = repository.transition(
                int(matching["id"]), expected=tuple(PENDING_STATUS_SET),
                updates={"status": "revoked", "released_at": now, "release_reason": reason}, actor=actor, now=now,
            )
            if affected != 1:
                raise ConflictError("名额状态已变化，撤销未成功")
            repository.add_event(int(matching["id"]), "revoked", actor, matching["status"], "revoked", reason, {}, now)
            self._notify_release(repository, matching, "revoked", reason, now)
            return repository.detail(int(matching["id"])) or {}

    # ---------- 到期释放（受控时间规则） ----------

    def expire_due(self, actor: str = "system-time-rule") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        expired: list[str] = []
        with transaction(immediate=True) as connection:
            repository = MatchmakingRepository(connection)
            due = repository.due_expirations(now)
            for matching in due:
                affected = repository.transition(
                    int(matching["id"]), expected=tuple(PENDING_STATUS_SET),
                    updates={"status": "expired", "released_at": now,
                             "release_reason": f"预留于 {matching['expires_at']} 到期未完成双方确认"},
                    actor=actor, now=now,
                )
                if affected == 1:
                    repository.add_event(int(matching["id"]), "expired", actor, matching["status"], "expired",
                                         "限时预留到期，容量自动释放", {"expires_at": matching["expires_at"]}, now)
                    self._notify_release(repository, matching, "expired",
                                         f"预留到期（{matching['expires_at']}），名额已释放", now)
                    expired.append(str(matching["matching_code"]))
        return {"expired": expired, "checked_at": now}

    # ---------- 产品暂停 / 场地能力变化：只撤销尚未生效的匹配 ----------

    def release_pending_for_product(self, connection: sqlite3.Connection, product_id: int, reason: str, actor: str, now: str) -> list[int]:
        return self._release_pending(connection, product_id=product_id, site_id=None, reason=reason, actor=actor, now=now)

    def release_pending_for_site(self, connection: sqlite3.Connection, site_id: int, reason: str, actor: str, now: str) -> list[int]:
        return self._release_pending(connection, product_id=None, site_id=site_id, reason=reason, actor=actor, now=now)

    def sweep_pending_for_site(self, connection: sqlite3.Connection, site_id: int, actor: str, now: str) -> list[dict[str, Any]]:
        """场地能力变化后，重新校验该场地尚未生效的名额；不再适配或超出新容量的才撤销，已确认名额不动。"""
        repository = MatchmakingRepository(connection)
        site = repository.site_by_id(site_id)
        if site is None:
            return []
        rows = connection.execute(
            f"SELECT * FROM pilot_matchings WHERE site_id=? AND status IN ({','.join('?' for _ in PENDING_STATUSES)}) ORDER BY created_at,id",
            (site_id, *PENDING_STATUSES),
        ).fetchall()
        # 已确认名额先占用容量，未决名额按创建先后排队，超出缩减后容量的撤销
        committed = int(connection.execute(
            "SELECT COALESCE(SUM(slot_quantity),0) FROM pilot_matchings WHERE site_id=? AND status='confirmed'",
            (site_id,),
        ).fetchone()[0])
        capacity = int(site["max_concurrent"])
        running = committed
        swept: list[dict[str, Any]] = []
        for row in rows:
            matching = dict(row)
            product = repository.product_by_id(int(matching["product_id"]))
            blockers: list[str] = []
            if product is None:
                blockers.append("产品已不存在")
            else:
                blockers.extend(self._reservation_blockers(product, site, 1))
            quantity = int(matching["slot_quantity"])
            if not blockers and running + quantity > capacity:
                blockers.append(f"场地容量下调至 {capacity}，该未决名额无法容纳")
            if blockers:
                reason = "场地能力变化：" + "；".join(blockers)
                affected = repository.transition(
                    int(matching["id"]), expected=tuple(PENDING_STATUS_SET),
                    updates={"status": "revoked", "released_at": now, "release_reason": reason}, actor=actor, now=now,
                )
                if affected == 1:
                    repository.add_event(int(matching["id"]), "auto_revoked", actor, matching["status"], "revoked", reason,
                                         {"cause": "site_capability_changed"}, now)
                    repository.add_notification(int(matching["id"]), "hospital", "auto_revoked",
                                                f"名额因场地能力变化被撤销：{matching['matching_code']}", reason, now)
                    repository.add_notification(int(matching["id"]), "enterprise", "auto_revoked",
                                                f"名额因场地能力变化被撤销：{matching['matching_code']}", reason, now)
                    swept.append({"matching_id": int(matching["id"]), "matching_code": matching["matching_code"], "reason": reason})
                continue
            running += quantity
        return swept

    def _release_pending(self, connection: sqlite3.Connection, *, product_id: int | None, site_id: int | None, reason: str, actor: str, now: str) -> list[int]:
        repository = MatchmakingRepository(connection)
        clauses = [f"status IN ({','.join('?' for _ in PENDING_STATUSES)})"]
        params: list[Any] = [*PENDING_STATUSES]
        if product_id is not None:
            clauses.append("product_id=?")
            params.append(product_id)
        if site_id is not None:
            clauses.append("site_id=?")
            params.append(site_id)
        rows = connection.execute(
            "SELECT * FROM pilot_matchings WHERE " + " AND ".join(clauses) + " ORDER BY id", tuple(params),
        ).fetchall()
        released: list[int] = []
        for row in rows:
            matching = dict(row)
            affected = repository.transition(
                int(matching["id"]), expected=tuple(PENDING_STATUS_SET),
                updates={"status": "revoked", "released_at": now, "release_reason": reason}, actor=actor, now=now,
            )
            if affected != 1:
                continue
            mid = int(matching["id"])
            repository.add_event(mid, "auto_revoked", actor, matching["status"], "revoked", reason,
                                 {"cause": "product_suspended" if product_id is not None else "site_capability_changed"}, now)
            repository.add_notification(mid, "hospital", "auto_revoked",
                                        f"名额因目录变化被撤销：{matching['matching_code']}", reason, now)
            repository.add_notification(mid, "enterprise", "auto_revoked",
                                        f"名额因目录变化被撤销：{matching['matching_code']}", reason, now)
            released.append(mid)
        return released

    # ---------- 查询 ----------

    def list_matchings(self, payload: dict[str, Any]) -> dict[str, Any]:
        statuses = tuple(payload["statuses"]) if payload.get("statuses") else None
        items = self.repository.list_matchings(
            statuses=statuses, site_code=payload.get("site_code"), product_code=payload.get("product_code"),
            pending_only=bool(payload.get("pending_only", False)), limit=int(payload.get("limit", 100)),
        )
        return {"items": items, "count": len(items)}

    def get_matching(self, matching_code: str) -> dict[str, Any]:
        matching = self.repository.matching_by_code(matching_code)
        if matching is None:
            raise NotFoundError("试点名额记录不存在")
        detail = self.repository.detail(int(matching["id"]))
        assert detail is not None
        return detail

    def pending_inbox(self) -> dict[str, Any]:
        """值班人员重启服务后查看：先由时间规则释放到期名额，再汇总等待答复与即将到期。"""
        # 重启即应用一次受控时间规则，确保看到的状态与当前时间一致
        expiry_report = self.expire_due()
        now_value = self.clock.now()
        now = to_storage(now_value)
        soon = to_storage(now_value + timedelta(hours=24))
        pending = self.repository.list_matchings(statuses=PENDING_STATUSES, site_code=None, product_code=None, pending_only=False, limit=500)
        waiting_hospital = [item for item in pending if item["status"] == "reserved"]
        waiting_enterprise = [item for item in pending if item["status"] in {"hospital_accepted", "hospital_conditional"}]
        expiring = [item for item in pending if item["expires_at"] <= soon]
        return {
            "checked_at": now,
            "expired_now": expiry_report["expired"],
            "waiting_hospital": waiting_hospital,
            "waiting_enterprise": waiting_enterprise,
            "expiring_within_24h": expiring,
        }

    def capacity_timeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按时间窗核对容量从推荐、预留到释放或生效的每一步变化。"""
        start = payload.get("start")
        end = payload.get("end")
        if not start or not end:
            raise ValidationError("必须提供时间窗 start 与 end")
        if from_storage(end) <= from_storage(start):
            raise ValidationError("时间窗结束时间必须晚于开始时间")
        site_code = payload.get("site_code")
        events = self.repository.events_in_window(start, end)
        if site_code:
            site = self.repository.site_by_code(site_code)
            if site is None:
                raise NotFoundError("试点场地不存在")
            site_codes = self.repository.matching_codes_for_site(int(site["id"]))
            events = [event for event in events if event["matching_code"] in site_codes]
        capacity_effect = {"reserved": "hold", "hospital_accepted": "hold", "hospital_conditional": "hold",
                           "confirmed": "commit", "hospital_rejected": "release", "enterprise_declined": "release",
                           "expired": "release", "revoked": "release", "auto_revoked": "release"}
        steps = []
        for event in events:
            steps.append({
                "at": event["created_at"], "matching_code": event["matching_code"],
                "event": event["event_type"], "from_status": event["from_status"],
                "to_status": event["to_status"], "actor": event["actor"],
                "capacity_effect": capacity_effect.get(event["event_type"], "note"),
                "reason": event["reason"], "detail": event["detail"],
            })
        return {"start": start, "end": end, "site_code": site_code, "steps": steps, "step_count": len(steps)}

    def notifications(self, recipient_role: str | None, limit: int) -> dict[str, Any]:
        items = self.repository.pending_notifications(recipient_role, limit)
        return {"items": items, "count": len(items)}

    def mark_delivered(self, notification_id: int) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            affected = MatchmakingRepository(connection).mark_notification_delivered(notification_id, now)
        if affected != 1:
            raise NotFoundError("待送达通知不存在或已送达")
        return {"id": notification_id, "delivered": True, "delivered_at": now}

    # ---------- 内部工具 ----------

    @staticmethod
    def _find_idempotent(connection: sqlite3.Connection, actor: str, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        row = connection.execute(
            "SELECT * FROM pilot_matchings WHERE requested_by=? AND idempotency_key=?", (actor, key),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _require_active_pending(repository: MatchmakingRepository, matching_code: str) -> dict[str, Any]:
        matching = repository.matching_by_code(matching_code)
        if matching is None:
            raise NotFoundError("试点名额记录不存在")
        if matching["status"] not in PENDING_STATUS_SET:
            raise ConflictError(f"名额当前状态为 {matching['status']}，不能再作出答复")
        return matching

    def _expire_if_due(self, repository: MatchmakingRepository, matching: dict[str, Any], now_value: Any, now: str) -> bool:
        """事务内检查：未决名额若已超过到期时间则转为 expired 并通知，返回 True 表示已过期。"""
        if matching["status"] not in PENDING_STATUS_SET:
            return False
        expires = from_storage(matching["expires_at"])
        if expires is None or now_value <= expires:
            return False
        affected = repository.transition(
            int(matching["id"]), expected=tuple(PENDING_STATUS_SET),
            updates={"status": "expired", "released_at": now,
                     "release_reason": f"预留于 {matching['expires_at']} 到期未完成双方确认"},
            actor="system-time-rule", now=now,
        )
        if affected == 1:
            repository.add_event(int(matching["id"]), "expired", "system-time-rule", matching["status"], "expired",
                                 "限时预留到期，容量自动释放", {"expires_at": matching["expires_at"]}, now)
            self._notify_release(repository, matching, "expired",
                                 f"预留到期（{matching['expires_at']}），名额已释放", now)
        return True

    def _assert_reservable(self, product: dict[str, Any], site: dict[str, Any], now_value: Any, slot_quantity: int, repository: MatchmakingRepository) -> None:
        del now_value
        occupied = repository.occupied_slots(int(site["id"]))
        blockers = self._reservation_blockers(product, site, slot_quantity, occupied=occupied)
        if blockers:
            raise ConflictError("该产品与场地不满足预留条件", context={"blockers": blockers})

    @staticmethod
    def _reservation_blockers(product: dict[str, Any], site: dict[str, Any], slot_quantity: int, *, occupied: int | None = None) -> list[str]:
        blockers: list[str] = []
        if not product.get("active"):
            blockers.append("产品已停用")
        if product.get("regulatory_status") == "暂停":
            blockers.append("产品处于暂停状态")
        if site.get("status") != "active":
            blockers.append(f"场地状态为 {site.get('status')}")
        risk = str(product["risk_level"])
        if risk not in (site.get("supported_risk_levels") or []):
            blockers.append(f"场地不具备承接{RISK_TEXT.get(risk, risk)}产品的资格")
        required_ethics = {"low": "none", "medium": "basic", "high": "full"}[risk]
        ethics_order = {"none": 0, "basic": 1, "full": 2}
        if ethics_order[str(site.get("ethics_prepared_level") or "none")] < ethics_order[required_ethics]:
            blockers.append(f"伦理准备不足（至少需要 {required_ethics}）")
        required_capability = CATEGORY_CAPABILITY.get(str(product["category"]))
        if required_capability and required_capability not in (site.get("capabilities") or []):
            blockers.append(f"场地缺少能力 {required_capability}")
        product_pops = list(product.get("intended_populations") or [])
        site_pops = list(site.get("served_populations") or [])
        if product_pops and not any(item in site_pops for item in product_pops):
            blockers.append("场地服务人群不覆盖产品目标人群")
        if occupied is not None and int(site["max_concurrent"]) - occupied < slot_quantity:
            blockers.append(f"剩余容量不足（剩余 {max(0, int(site['max_concurrent']) - occupied)}）")
        return blockers

    @staticmethod
    def _matching_code(product_code: str, site_code: str, now: str) -> str:
        stamp = now.replace("-", "").replace(":", "").replace("+", "").replace("T", "z")
        suffix = secrets.token_hex(4)
        return f"mt-{product_code}-{site_code}-{stamp}-{suffix}"[:96]

    @staticmethod
    def _notify_release(repository: MatchmakingRepository, matching: dict[str, Any], event: str, reason: str, now: str, *, hospital_rejected: bool = False) -> None:
        code = matching["matching_code"]
        if not hospital_rejected:
            repository.add_notification(int(matching["id"]), "hospital", event, f"名额已释放：{code}", reason, now)
        repository.add_notification(int(matching["id"]), "enterprise", event, f"名额已释放：{code}", reason, now)
        repository.add_notification(int(matching["id"]), "operator", event, f"名额已释放：{code}", reason, now)
