from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

ACTIVE_STATUSES = ("reserved", "hospital_accepted", "hospital_conditional", "confirmed")
PENDING_STATUSES = ("reserved", "hospital_accepted", "hospital_conditional")
TERMINAL_STATUSES = ("hospital_rejected", "enterprise_declined", "expired", "revoked", "confirmed")


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, fallback in (
        ("capabilities_json", []),
        ("served_populations_json", []),
        ("supported_risk_levels_json", []),
        ("open_windows_json", []),
        ("intended_populations_json", []),
    ):
        if key in data:
            try:
                data[key.removesuffix("_json")] = json.loads(data.pop(key) or "[]")
            except (TypeError, ValueError):
                data[key.removesuffix("_json")] = fallback
    return data


class MatchmakingRepository:
    """封装试点名额推荐、预留、确认与释放的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---------- 目录读取 ----------

    def product_by_id(self, product_id: int) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM health_products WHERE id=?", (product_id,)).fetchone())

    def product_by_code(self, code: str) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM health_products WHERE code=?", (code.strip().lower(),)).fetchone())

    def candidate_products(self, *, category: str | None, risk_level: str | None, include_inactive: bool) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if not include_inactive:
            clauses.append("active=1 AND regulatory_status<>'暂停'")
        if category:
            clauses.append("category=?")
            params.append(category)
        if risk_level:
            clauses.append("risk_level=?")
            params.append(risk_level)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("SELECT * FROM health_products" + where + " ORDER BY category,code", tuple(params)).fetchall()
        return [dict(_decode(row)) for row in rows]

    def site_by_id(self, site_id: int) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM pilot_sites WHERE id=?", (site_id,)).fetchone())

    def site_by_code(self, code: str) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM pilot_sites WHERE code=?", (code.strip().lower(),)).fetchone())

    def candidate_sites(self, *, region: str | None, site_type: str | None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if region:
            clauses.append("region=?")
            params.append(region)
        if site_type:
            clauses.append("site_type=?")
            params.append(site_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("SELECT * FROM pilot_sites" + where + " ORDER BY region,code", tuple(params)).fetchall()
        return [dict(_decode(row)) for row in rows]

    # ---------- 容量与占用 ----------

    def occupied_slots(self, site_id: int) -> int:
        """未决预留与已确认名额都占用场地容量；已释放的不再占用。"""
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.connection.execute(
            f"SELECT COALESCE(SUM(slot_quantity),0) FROM pilot_matchings WHERE site_id=? AND status IN ({placeholders})",
            (site_id, *ACTIVE_STATUSES),
        ).fetchone()
        return int(row[0])

    def active_matching(self, product_id: int, site_id: int) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        return _decode(self.connection.execute(
            f"SELECT * FROM pilot_matchings WHERE product_id=? AND site_id=? AND status IN ({placeholders})",
            (product_id, site_id, *ACTIVE_STATUSES),
        ).fetchone())

    def pending_matching(self, product_id: int, site_id: int) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        return _decode(self.connection.execute(
            f"SELECT * FROM pilot_matchings WHERE product_id=? AND site_id=? AND status IN ({placeholders})",
            (product_id, site_id, *PENDING_STATUSES),
        ).fetchone())

    def matching_by_code(self, matching_code: str) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM pilot_matchings WHERE matching_code=?", (matching_code,)).fetchone())

    def matching_by_id(self, matching_id: int) -> dict[str, Any] | None:
        return _decode(self.connection.execute("SELECT * FROM pilot_matchings WHERE id=?", (matching_id,)).fetchone())

    def detail(self, matching_id: int) -> dict[str, Any] | None:
        matching = self.matching_by_id(matching_id)
        if matching is None:
            return None
        product = self.connection.execute("SELECT code,name,organization,category,risk_level,regulatory_status FROM health_products WHERE id=?", (matching["product_id"],)).fetchone()
        site = self.connection.execute("SELECT code,name,site_type,region FROM pilot_sites WHERE id=?", (matching["site_id"],)).fetchone()
        matching["product"] = dict(product) if product else None
        matching["site"] = dict(site) if site else None
        matching["events"] = self.events(matching_id)
        matching["notifications"] = self.notifications(matching_id)
        return matching

    def insert_matching(self, *, matching_code: str, product_id: int, site_id: int, slot_quantity: int, requested_by: str, idempotency_key: str, hospital_contact: str, expires_at: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO pilot_matchings(matching_code,product_id,site_id,slot_quantity,status,requested_by,idempotency_key,hospital_contact,expires_at,last_actor,created_at,updated_at) "
            "VALUES(?,?,?,?,'reserved',?,?,?,?,'operator',?,?)",
            (matching_code, product_id, site_id, slot_quantity, requested_by, idempotency_key, hospital_contact, expires_at, now, now),
        )
        return self.matching_by_id(int(cursor.lastrowid)) or {}

    def transition(self, matching_id: int, *, expected: Iterable[str], updates: dict[str, Any], actor: str, now: str) -> int:
        """条件更新：仅当当前状态属于 expected 时生效，返回受影响行数（0 表示已被并发方抢先改变）。"""
        assignments = [f"{key}=?" for key in updates] + ["version=version+1", "updated_at=?", "last_actor=?"]
        expected = tuple(expected)
        placeholders = ",".join("?" for _ in expected)
        params: list[Any] = [*updates.values(), now, actor, matching_id, *expected]
        cursor = self.connection.execute(
            f"UPDATE pilot_matchings SET {','.join(assignments)} WHERE id=? AND status IN ({placeholders})",
            tuple(params),
        )
        return cursor.rowcount

    def list_matchings(self, *, statuses: tuple[str, ...] | None, site_code: str | None, product_code: str | None, pending_only: bool, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"m.status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if pending_only:
            clauses.append(f"m.status IN ({','.join('?' for _ in PENDING_STATUSES)})")
            params.extend(PENDING_STATUSES)
        if site_code:
            clauses.append("s.code=?")
            params.append(site_code.strip().lower())
        if product_code:
            clauses.append("p.code=?")
            params.append(product_code.strip().lower())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            "SELECT m.*,p.code AS product_code,p.name AS product_name,s.code AS site_code,s.name AS site_name,s.region AS site_region "
            "FROM pilot_matchings m JOIN health_products p ON p.id=m.product_id JOIN pilot_sites s ON s.id=m.site_id"
            + where + " ORDER BY m.expires_at ASC,m.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def due_expirations(self, now: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        rows = self.connection.execute(
            f"SELECT * FROM pilot_matchings WHERE status IN ({placeholders}) AND expires_at<=? ORDER BY expires_at,id",
            (*PENDING_STATUSES, now),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 事件时间线与通知 ----------

    def add_event(self, matching_id: int, event_type: str, actor: str, from_status: str, to_status: str, reason: str, detail: dict[str, Any], now: str) -> None:
        seq = int(self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM pilot_matching_events WHERE matching_id=?", (matching_id,)).fetchone()[0])
        self.connection.execute(
            "INSERT INTO pilot_matching_events(matching_id,seq,event_type,actor,from_status,to_status,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (matching_id, seq, event_type, actor, from_status, to_status, reason[:1000], json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, matching_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM pilot_matching_events WHERE matching_id=? ORDER BY seq", (matching_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
            result.append(item)
        return result

    def events_in_window(self, start: str, end: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT e.*,m.matching_code FROM pilot_matching_events e JOIN pilot_matchings m ON m.id=e.matching_id "
            "WHERE e.created_at>=? AND e.created_at<? ORDER BY e.created_at,e.id",
            (start, end),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
            result.append(item)
        return result

    def matching_codes_for_site(self, site_id: int) -> set[str]:
        rows = self.connection.execute("SELECT matching_code FROM pilot_matchings WHERE site_id=?", (site_id,)).fetchall()
        return {str(row[0]) for row in rows}

    def add_notification(self, matching_id: int, recipient_role: str, event_type: str, title: str, reason: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO pilot_matching_notifications(matching_id,recipient_role,event_type,title,reason,created_at) VALUES(?,?,?,?,?,?)",
            (matching_id, recipient_role, event_type, title[:200], reason[:1000], now),
        )

    def notifications(self, matching_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT id,recipient_role,event_type,title,reason,delivered_at,created_at FROM pilot_matching_notifications WHERE matching_id=? ORDER BY id",
            (matching_id,),
        ).fetchall()]

    def pending_notifications(self, recipient_role: str | None, limit: int) -> list[dict[str, Any]]:
        clauses = ["delivered_at=''"]
        params: list[Any] = []
        if recipient_role:
            clauses.append("recipient_role=?")
            params.append(recipient_role)
        params.append(limit)
        return [dict(row) for row in self.connection.execute(
            "SELECT n.*,m.matching_code FROM pilot_matching_notifications n JOIN pilot_matchings m ON m.id=n.matching_id "
            "WHERE " + " AND ".join(clauses) + " ORDER BY n.id LIMIT ?",
            tuple(params),
        ).fetchall()]

    def mark_notification_delivered(self, notification_id: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE pilot_matching_notifications SET delivered_at=? WHERE id=? AND delivered_at=''",
            (now, notification_id),
        )
        return cursor.rowcount
