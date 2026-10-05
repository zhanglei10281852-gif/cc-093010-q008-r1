from __future__ import annotations

import sqlite3

from app.catalog.repository import CatalogRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.matchmaking.service import MatchmakingService


class CatalogService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CatalogRepository(self.connection)

    def create_product(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.product_by_code(code):
            raise ConflictError("产品编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "organization": data["organization"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_product(payload, to_storage(self.clock.now()))

    def update_product(self, code: str, changes: dict) -> dict:
        product = self.repository.product_by_code(code)
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的产品字段")
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            updated = catalog.update_product(product["id"], values, to_storage(self.clock.now()))
            # 产品暂停或停用：只撤销尚未生效的名额，已确认的名额不受影响
            becomes_unavailable = values.get("regulatory_status") == "暂停" or values.get("active") is False
            if becomes_unavailable:
                reason = f"产品 {product['code']} " + ("被暂停" if values.get("regulatory_status") == "暂停" else "被停用")
                MatchmakingService(connection, self.clock).release_pending_for_product(
                    connection, int(product["id"]), reason, "catalog-maintainer", to_storage(self.clock.now()),
                )
            return updated

    def list_products(self, category: str | None, status: str | None, active_only: bool, limit: int) -> list[dict]:
        return self.repository.list_products(category=category, status=status, active_only=active_only, limit=limit)

    def create_site(self, data: dict) -> dict:
        code = data["code"].strip().lower()
        if self.repository.site_by_code(code):
            raise ConflictError("场地编码已存在")
        payload = {**data, "code": code, "name": data["name"].strip(), "region": data["region"].strip()}
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_site(payload, to_storage(self.clock.now()))

    def update_site(self, code: str, changes: dict) -> dict:
        site = self.repository.site_by_code(code)
        if site is None:
            raise NotFoundError("试点场地不存在")
        values = {key: value for key, value in changes.items() if value is not None}
        if not values:
            raise ValidationError("没有可更新的场地字段")
        with transaction(immediate=True) as connection:
            catalog = CatalogRepository(connection)
            now = to_storage(self.clock.now())
            updated = catalog.update_site(site["id"], values, now)
            # 场地能力/容量/开放状态变化：重新校验并只撤销不再适配或超出新容量的未决名额
            affects_matching = {
                "capabilities", "served_populations", "max_concurrent", "ethics_prepared_level",
                "supported_risk_levels", "open_windows", "status",
            }
            swept: list[dict] = []
            if affects_matching.intersection(values):
                swept = MatchmakingService(connection, self.clock).sweep_pending_for_site(
                    connection, int(site["id"]), "catalog-maintainer", now,
                )
            updated["auto_revoked_matchings"] = swept
            return updated

    def list_sites(self, status: str | None, site_type: str | None, capability: str | None) -> list[dict]:
        return self.repository.list_sites(status=status, site_type=site_type, capability=capability)

    def submit_evidence(self, data: dict) -> dict:
        product = self.repository.product_by_code(data["product_code"])
        if product is None:
            raise NotFoundError("健康创新产品不存在")
        duplicate = self.repository.evidence_duplicate(product["id"], data["evidence_type"], data["version"], data["content_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_evidence(product["id"], data, to_storage(self.clock.now()))

    def review_evidence(self, evidence_id: int, reviewer: str, decision: str, note: str) -> dict:
        evidence = self.repository.evidence_by_id(evidence_id)
        if evidence is None:
            raise NotFoundError("证据材料不存在")
        if evidence["status"] != "submitted":
            raise ConflictError("只有待审阅材料可以作出决定")
        if decision == "rejected" and len(note.strip()) < 4:
            raise ValidationError("驳回时需要说明可执行的原因")
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).review_evidence(evidence_id, reviewer.strip(), decision, to_storage(self.clock.now()))

    def list_evidence(self, product_code: str | None, status: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.list_evidence(product_id=product_id, status=status)

    def submit_feedback(self, data: dict) -> dict:
        product = self.repository.product_by_code(data["product_code"])
        if product is None or not product["active"]:
            raise NotFoundError("可体验的健康创新产品不存在")
        site = self.repository.site_by_code(data["site_code"])
        if site is None or site["status"] != "active":
            raise NotFoundError("可用的试点场地不存在")
        duplicate = self.repository.feedback_duplicate(site["id"], data["session_reference"], data["audience_type"], data["contact_digest"])
        if duplicate:
            return duplicate
        with transaction(immediate=True) as connection:
            return CatalogRepository(connection).create_feedback(product["id"], site["id"], data, to_storage(self.clock.now()))

    def feedback_summary(self, product_code: str | None) -> list[dict]:
        product_id = None
        if product_code:
            product = self.repository.product_by_code(product_code)
            if product is None:
                raise NotFoundError("健康创新产品不存在")
            product_id = product["id"]
        return self.repository.feedback_summary(product_id)

