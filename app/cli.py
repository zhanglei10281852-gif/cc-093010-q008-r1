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
            product = client.post("/api/catalog/products", json={
                "code": "dtx-cognition",
                "name": "认知康复数字疗法",
                "organization": "示例数字疗法企业",
                "origin_country": "中国浙江",
                "category": "数字疗法",
                "intended_use": "用于卒中后轻度认知障碍人群的院内训练与随访，需要医院伦理备案",
                "intended_populations": ["卒中后患者"],
                "risk_level": "high",
                "regulatory_status": "研究",
            })
            capable_site = client.post("/api/catalog/sites", json={
                "code": "neuro-hospital",
                "name": "市一医院神经内科",
                "site_type": "医院",
                "region": "浙江",
                "capabilities": ["digital-therapeutic"],
                "served_populations": ["卒中后患者"],
                "max_concurrent": 2,
                "ethics_prepared_level": "full",
                "supported_risk_levels": ["medium", "high"],
                "open_windows": [{"starts_at": "2026-10-01T00:00:00+00:00", "ends_at": "2026-12-31T00:00:00+00:00"}],
            })
            weak_site = client.post("/api/catalog/sites", json={
                "code": "community-center",
                "name": "社区卫生服务中心",
                "site_type": "医院",
                "region": "浙江",
                "capabilities": ["consumer-health"],
                "served_populations": ["健康人群"],
                "max_concurrent": 5,
                "ethics_prepared_level": "none",
                "supported_risk_levels": ["low"],
            })
            recommend = client.post("/api/matchings/recommend", json={"product_code": "dtx-cognition", "region": "浙江"})
            reserve = client.post(
                "/api/matchings/reservations?actor=eco-lead",
                json={"product_code": "dtx-cognition", "site_code": "neuro-hospital", "ttl_seconds": 86400,
                      "idempotency_key": "matching-demo-001", "hospital_contact": "周主任"},
            )
            code = reserve.json().get("matching_code", "")
            accepted = client.post(f"/api/matchings/reservations/{code}/hospital-response",
                                   json={"decision": "conditional", "condition": "需安排每两周一次认知量表复评"})
            confirmed = client.post(f"/api/matchings/reservations/{code}/enterprise-response",
                                    json={"decision": "confirm", "actor": "vendor-dtx"})
            values = [product, capable_site, weak_site, recommend, reserve, accepted, confirmed]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            ranking = [(item["rank"], item["site_code"], item["eligible"], item["blockers"])
                       for item in recommend.json()["candidates"]]
            _print({
                "recommendation": ranking,
                "matching_code": code,
                "lifecycle": [reserve.json()["status"], accepted.json()["status"], confirmed.json()["status"]],
                "timeline_events": [event["event_type"] for event in confirmed.json()["events"]],
            })
        close_connection()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全球健康创新试点运营服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="初始化 SQLite 数据库")
    sub.add_parser("check-db", help="检查数据库完整性")
    sub.add_parser("smoke", help="进程内检查根路径和健康接口")
    sub.add_parser("pilot-demo", help="运行产品、场地、方案和场次演示")
    sub.add_parser("matching-demo", help="运行候选推荐、限时预留和双方确认演示")
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

