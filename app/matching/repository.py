from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class MatchingRepository:
    """匹配推荐、容量预留、台账与站内消息的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 产品画像与科室 ----

    def upsert_profile(self, product_id: int, data: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO product_eligibility_profiles(product_id,eligible_populations_json,excluded_populations_json,"
            "required_capabilities_json,allowed_regions_json,ethics_review_status,ethics_approved_at,ethics_committee,"
            "open_from,open_until,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET eligible_populations_json=excluded.eligible_populations_json,"
            "excluded_populations_json=excluded.excluded_populations_json,required_capabilities_json=excluded.required_capabilities_json,"
            "allowed_regions_json=excluded.allowed_regions_json,ethics_review_status=excluded.ethics_review_status,"
            "ethics_approved_at=excluded.ethics_approved_at,ethics_committee=excluded.ethics_committee,"
            "open_from=excluded.open_from,open_until=excluded.open_until,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (
                product_id,
                json.dumps(sorted(set(data.get("eligible_populations", []))), ensure_ascii=False),
                json.dumps(sorted(set(data.get("excluded_populations", []))), ensure_ascii=False),
                json.dumps(sorted(set(data.get("required_capabilities", []))), ensure_ascii=False),
                json.dumps(sorted(set(data.get("allowed_regions", []))), ensure_ascii=False),
                data.get("ethics_review_status", "未提交"),
                data.get("ethics_approved_at"),
                data.get("ethics_committee", ""),
                data.get("open_from"),
                data.get("open_until"),
                actor, now, now,
            ),
        )
        return self.profile_by_product(product_id) or {}

    def profile_by_product(self, product_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM product_eligibility_profiles WHERE product_id=?", (product_id,)).fetchone()
        return dict(row) if row else None

    def add_department(self, site_id: int, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO site_departments(site_id,code,name,capabilities_json,max_risk_level,ethics_ready,active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'1',?,?)",
            (
                site_id, data["code"], data["name"],
                json.dumps(sorted(set(data.get("capabilities", []))), ensure_ascii=False),
                data.get("max_risk_level", "low"), 1 if data.get("ethics_ready") else 0, now, now,
            ),
        )
        return self.department_by_id(int(cursor.lastrowid)) or {}

    def update_department(self, department_id: int, changes: dict[str, Any], now: str) -> dict[str, Any] | None:
        values = dict(changes)
        if "capabilities" in values:
            values["capabilities_json"] = json.dumps(sorted(set(values.pop("capabilities"))), ensure_ascii=False)
        if "ethics_ready" in values:
            values["ethics_ready"] = 1 if values["ethics_ready"] else 0
        if not values:
            return self.department_by_id(department_id)
        assignments = [f"{key}=?" for key in values]
        self.connection.execute(
            f"UPDATE site_departments SET {','.join(assignments)},updated_at=? WHERE id=?",
            (*values.values(), now, department_id),
        )
        return self.department_by_id(department_id)

    def department_by_id(self, department_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM site_departments WHERE id=?", (department_id,)).fetchone()
        return dict(row) if row else None

    def departments_for_sites(self, site_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
        ids = list(site_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM site_departments WHERE site_id IN ({placeholders}) ORDER BY site_id,id", ids,
        ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {site_id: [] for site_id in ids}
        for row in rows:
            grouped.setdefault(int(row["site_id"]), []).append(dict(row))
        return grouped

    # ---- 推荐批次 ----

    def save_recommendations(self, batch_code: str, items: list[dict[str, Any]], requested_by: str, now: str) -> list[int]:
        self.connection.execute("DELETE FROM matching_recommendations WHERE batch_code=?", (batch_code,))
        ids: list[int] = []
        for item in items:
            cursor = self.connection.execute(
                "INSERT INTO matching_recommendations(batch_code,product_id,site_id,department_id,rank,score,eligible,"
                "reasons_json,blockers_json,requested_region,requested_population,requested_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_code, item["product_id"], item["site_id"], item.get("department_id"),
                    item["rank"], item["score"], 1 if item["eligible"] else 0,
                    json.dumps(item["reasons"], ensure_ascii=False),
                    json.dumps(item.get("blockers", []), ensure_ascii=False),
                    item.get("requested_region", ""), item.get("requested_population", ""),
                    requested_by, now,
                ),
            )
            ids.append(int(cursor.lastrowid))
        return ids

    def recommendation_by_id(self, recommendation_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM matching_recommendations WHERE id=?", (recommendation_id,)).fetchone()
        return dict(row) if row else None

    # ---- 预留 ----

    def create_reservation(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO capacity_reservations(reservation_code,product_id,site_id,department_id,recommendation_id,"
            "requested_by,status,quantity,idempotency_key,hospital_due_at,enterprise_confirm_seconds,note,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
            (
                data["reservation_code"], data["product_id"], data["site_id"], data.get("department_id"),
                data.get("recommendation_id"), data["requested_by"], data.get("quantity", 1),
                data["idempotency_key"], data["hospital_due_at"], data.get("enterprise_confirm_seconds", 43200),
                data.get("note", ""), now, now,
            ),
        )
        return self.reservation_by_id(int(cursor.lastrowid)) or {}

    def reservation_by_id(self, reservation_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM capacity_reservations WHERE id=?", (reservation_id,)).fetchone()
        return dict(row) if row else None

    def reservation_by_code(self, code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM capacity_reservations WHERE reservation_code=?", (code,)).fetchone()
        return dict(row) if row else None

    def reservation_by_idempotency(self, requested_by: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE requested_by=? AND idempotency_key=?",
            (requested_by, key),
        ).fetchone()
        return dict(row) if row else None

    def transition_reservation(
        self, reservation_id: int, *, expected_status: tuple[str, ...], updates: dict[str, Any], now: str,
    ) -> int:
        """条件更新：仅当当前状态属于 expected_status 时生效，返回受影响行数（0 即并发落败）。"""
        assignments = [f"{key}=?" for key in updates]
        placeholders = ",".join("?" for _ in expected_status)
        cursor = self.connection.execute(
            f"UPDATE capacity_reservations SET {','.join(assignments)},updated_at=?,version=version+1 "
            f"WHERE id=? AND status IN ({placeholders})",
            (*updates.values(), now, reservation_id, *expected_status),
        )
        return int(cursor.rowcount)

    def list_reservations(
        self, *, statuses: Iterable[str] | None = None, site_id: int | None = None,
        product_id: int | None = None, requested_by: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            statuses = list(statuses)
            clauses.append("r.status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if site_id is not None:
            clauses.append("site_id=?")
            params.append(site_id)
        if product_id is not None:
            clauses.append("product_id=?")
            params.append(product_id)
        if requested_by:
            clauses.append("requested_by=?")
            params.append(requested_by)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT r.*,p.code AS product_code,p.name AS product_name,s.code AS site_code,s.name AS site_name "
            "FROM capacity_reservations r JOIN health_products p ON p.id=r.product_id "
            "JOIN pilot_sites s ON s.id=r.site_id" + where + " ORDER BY r.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def due_reservations(self, now: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE status IN ('pending','conditional','accepted') "
            "AND ((status='pending' AND hospital_due_at<=?) "
            "OR (status IN ('conditional','accepted') AND enterprise_due_at IS NOT NULL AND enterprise_due_at<=?)) "
            "ORDER BY id",
            (now, now),
        ).fetchall()]

    def active_for_site(self, site_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE site_id=? AND status IN ('pending','conditional','accepted','confirmed') ORDER BY id",
            (site_id,),
        ).fetchall()]

    def active_for_product(self, product_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE product_id=? AND status IN ('pending','conditional','accepted') ORDER BY id",
            (product_id,),
        ).fetchall()]

    # ---- 容量台账 ----

    def append_ledger(self, entry: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO capacity_ledger(site_id,reservation_id,product_id,event_type,held_delta,confirmed_delta,"
            "reason,actor,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                entry["site_id"], entry.get("reservation_id"), entry.get("product_id"),
                entry["event_type"], entry.get("held_delta", 0), entry.get("confirmed_delta", 0),
                entry.get("reason", ""), entry.get("actor", ""),
                json.dumps(entry.get("detail", {}), ensure_ascii=False, sort_keys=True), now,
            ),
        )

    def ledger_window(self, *, start: str | None, end: str | None, site_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("l.created_at>=?")
            params.append(start)
        if end:
            clauses.append("l.created_at<=?")
            params.append(end)
        if site_id is not None:
            clauses.append("l.site_id=?")
            params.append(site_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT l.*,s.code AS site_code,p.code AS product_code,r.reservation_code "
            "FROM capacity_ledger l JOIN pilot_sites s ON s.id=l.site_id "
            "LEFT JOIN health_products p ON p.id=l.product_id "
            "LEFT JOIN capacity_reservations r ON r.id=l.reservation_id" + where + " ORDER BY l.created_at,l.id",
            tuple(params),
        ).fetchall()
        items = [dict(row) for row in rows]
        held = confirmed = 0
        for item in items:
            held += int(item["held_delta"])
            confirmed += int(item["confirmed_delta"])
            item["held_after"] = held
            item["confirmed_after"] = confirmed
        return items

    # ---- 消息 ----

    def add_message(self, reservation_id: int, recipient_role: str, recipient: str, subject: str, body: str, reason: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO matching_messages(reservation_id,recipient_role,recipient,subject,body,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (reservation_id, recipient_role, recipient, subject, body, reason, now),
        )

    def list_messages(self, recipient_role: str, recipient: str, unread_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM matching_messages WHERE recipient_role=? AND recipient=?"
        params: list[Any] = [recipient_role, recipient]
        if unread_only:
            sql += " AND read_at IS NULL"
        return [dict(row) for row in self.connection.execute(sql + " ORDER BY id DESC LIMIT 200", params).fetchall()]

    def mark_message_read(self, message_id: int, recipient_role: str, recipient: str, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE matching_messages SET read_at=? WHERE id=? AND recipient_role=? AND recipient=? AND read_at IS NULL",
            (now, message_id, recipient_role, recipient),
        )
        return int(cursor.rowcount)
