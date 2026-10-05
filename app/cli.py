from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from app.database import close_connection, database_path, get_connection, init_db
from app.main import app


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def init_database() -> int:
    init_db()
    _print({"database": str(database_path()), "initialized": True})
    return 0


def check_database() -> int:
    init_db()
    connection = get_connection()
    _print({
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
    })
    return 0


def smoke() -> int:
    with tempfile.TemporaryDirectory(prefix="health-smoke-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "smoke.db")
        close_connection()
        with TestClient(app) as client:
            root = client.get("/")
            health = client.get("/api/system/health")
            if root.status_code != 200 or health.status_code != 200:
                _print({"root": root.text, "health": health.text})
                return 1
            _print({"root": root.json(), "health": health.json()})
        close_connection()
    return 0


def pilot_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-demo-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "demo.db")
        close_connection()
        with TestClient(app) as client:
            product = client.post("/api/catalog/products", json={
                "code": "exoskeleton-a",
                "name": "轻量助力外骨骼",
                "organization": "示例康复科技",
                "origin_country": "中国",
                "category": "康复设备",
                "intended_use": "用于展会和康复机构的步态助力体验与运行数据观察",
                "risk_level": "medium",
                "regulatory_status": "展示",
            })
            site = client.post("/api/catalog/sites", json={
                "code": "expo-hall-a",
                "name": "数智医疗体验点",
                "site_type": "展会体验点",
                "region": "杭州",
                "capabilities": ["gait-assist"],
                "max_concurrent": 2,
            })
            protocol = client.post("/api/pilots/protocols?actor=demo", json={
                "code": "gait-assist",
                "name": "外骨骼步态体验方案",
                "capability": "gait-assist",
                "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
                "default_parameters": {},
                "max_runtime_seconds": 1800,
                "max_attempts": 2,
            })
            submitted = client.post("/api/pilots/sessions", json={
                "protocol_code": "gait-assist",
                "project_code": "expo-2026",
                "requested_by": "operator-demo",
                "parameters": {"minutes": 8},
                "priority": 70,
                "idempotency_key": "demo-session-001",
            })
            claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "expo-hall-a", "capabilities": ["gait-assist"], "lease_seconds": 60})
            values = [product, site, protocol, submitted, claimed]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            _print({"product": product.json()["code"], "site": site.json()["code"], "session": claimed.json()["session"]})
        close_connection()
    return 0


def matching_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-matching-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "matching.db")
        close_connection()
        with TestClient(app) as client:
            steps: list[dict] = []
            product = client.post("/api/catalog/products", json={
                "code": "dt-stroke", "name": "卒中康复数字疗法", "organization": "神境数字医疗",
                "origin_country": "中国", "category": "数字疗法",
                "intended_use": "用于卒中后上肢与认知功能康复的处方数字疗法，需要临床监测",
                "risk_level": "high", "regulatory_status": "研究",
            })
            site = client.post("/api/catalog/sites", json={
                "code": "hospital-a", "name": "市第一人民医院", "site_type": "医院",
                "region": "浙江杭州", "capabilities": ["digital-therapy"], "max_concurrent": 2,
            })
            client.put("/api/matching/products/dt-stroke/eligibility?actor=eco-lead", json={
                "eligible_populations": ["卒中恢复期成人"], "required_capabilities": ["digital-therapy"],
                "allowed_regions": ["浙江杭州"], "ethics_review_status": "已通过",
                "ethics_committee": "市一医院伦理委员会",
            })
            client.post("/api/matching/sites/hospital-a/departments?actor=eco-lead", json={
                "code": "rehab", "name": "康复医学科", "capabilities": ["digital-therapy"],
                "max_risk_level": "high", "ethics_ready": True,
            })
            reco = client.post("/api/matching/recommendations", json={
                "product_codes": ["dt-stroke"], "requested_region": "浙江杭州",
                "requested_population": "卒中恢复期成人", "requested_by": "eco-lead",
            })
            recommendation = reco.json()["items"][0]
            steps.append({"rank_1_eligible": recommendation["eligible"], "score": recommendation["score"], "reasons": recommendation["reasons"][:2]})
            reserved = client.post("/api/matching/reservations?actor=operator-li", json={
                "recommendation_id": recommendation["id"], "idempotency_key": "matching-demo-001",
                "hospital_response_seconds": 172800,
            })
            code = reserved.json()["reservation_code"]
            client.post(f"/api/matching/reservations/{code}/hospital-response", json={
                "decision": "conditional", "responded_by": "hospital-a-office",
                "conditions": ["首两周安排责任医师每日在场"],
            })
            confirmed = client.post(f"/api/matching/reservations/{code}/enterprise-confirm", json={
                "confirmed_by": "shenjing-rep", "accept_conditions": True,
            })
            timeline = client.get("/api/matching/capacity-timeline?site_code=hospital-a")
            steps.append({
                "reservation": code, "final_status": confirmed.json()["status"],
                "capacity_events": [item["event_type"] for item in timeline.json()["items"]],
            })
            dashboard = client.get("/api/matching/dashboard").json()
            steps.append({"awaiting_hospital_reply": dashboard["awaiting_hospital_reply"], "awaiting_enterprise_confirm": dashboard["awaiting_enterprise_confirm"]})
            values = [product, site, reco, reserved, confirmed]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            _print({"steps": steps})
        close_connection()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全球健康创新试点运营服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="初始化 SQLite 数据库")
    sub.add_parser("check-db", help="检查数据库完整性")
    sub.add_parser("smoke", help="进程内检查根路径和健康接口")
    sub.add_parser("pilot-demo", help="运行产品、场地、方案和场次演示")
    sub.add_parser("matching-demo", help="运行匹配推荐、限时预留和双方确认演示")
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    actions = {"init-db": init_database, "check-db": check_database, "smoke": smoke, "pilot-demo": pilot_demo, "matching-demo": matching_demo}
    try:
        return actions[command]()
    finally:
        close_connection()


if __name__ == "__main__":
    sys.exit(main())

