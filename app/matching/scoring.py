from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.clock import from_storage

RISK_RANK = {"low": 1, "medium": 2, "high": 3}
RISK_LABEL = {"low": "低风险", "medium": "中风险", "high": "高风险"}

# 各评分因子权重，合计 100
WEIGHTS = {
    "capability": 25,
    "risk_headroom": 15,
    "ethics": 15,
    "region": 15,
    "population": 15,
    "open_window": 15,
}


@dataclass(slots=True)
class PairEvaluation:
    product_id: int
    site_id: int
    department_id: int | None
    eligible: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "site_id": self.site_id,
            "department_id": self.department_id,
            "eligible": self.eligible,
            "score": self.score,
            "reasons": self.reasons,
            "blockers": self.blockers,
        }


def _caps(row: Any, column: str = "capabilities_json") -> set[str]:
    try:
        return {str(item) for item in json.loads(row[column])}
    except (TypeError, ValueError, KeyError):
        return set()


def _profile_values(profile: dict[str, Any] | None) -> dict[str, set[str] | str]:
    if profile is None:
        return {
            "eligible": set(),
            "excluded": set(),
            "required": set(),
            "allowed_regions": set(),
            "ethics": "未提交",
            "open_from": None,
            "open_until": None,
        }
    load = lambda key: {str(item) for item in json.loads(profile.get(key) or "[]")}
    return {
        "eligible": load("eligible_populations_json"),
        "excluded": load("excluded_populations_json"),
        "required": load("required_capabilities_json"),
        "allowed_regions": load("allowed_regions_json"),
        "ethics": profile.get("ethics_review_status") or "未提交",
        "open_from": profile.get("open_from"),
        "open_until": profile.get("open_until"),
    }


def _region_affinity(site_region: str, requested: str) -> float:
    if not requested:
        return 0.6
    if site_region == requested:
        return 1.0
    if site_region.startswith(requested) or requested.startswith(site_region):
        return 0.7
    return 0.3


def _window_fraction(open_from: str | None, open_until: str | None, now: str) -> float | None:
    if not open_from or not open_until:
        return None
    start = from_storage(open_from)
    end = from_storage(open_until)
    current = from_storage(now)
    if start is None or end is None or current is None or end <= start:
        return None
    if current < start:
        return 0.0
    if current > end:
        return 0.0
    return max(0.0, min(1.0, (end - current) / (end - start)))


def evaluate_pair(
    product: dict[str, Any],
    site: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    departments: list[dict[str, Any]] | None = None,
    requested_region: str = "",
    requested_population: str = "",
    now: str,
) -> PairEvaluation:
    """结合产品类别、风险级别、适用人群、地区、场地/科室能力、伦理准备和开放时间给出评分与理由。"""
    values = _profile_values(profile)
    required: set[str] = values["required"]  # type: ignore[assignment]
    risk = str(product["risk_level"])
    risk_rank = RISK_RANK[risk]
    site_caps = _caps(site)
    active_departments = [dept for dept in (departments or []) if dept.get("active", 1)]

    blockers: list[str] = []
    reasons: list[str] = []

    if not product["active"]:
        blockers.append("产品已停用，不能进入试点匹配")
    if product["regulatory_status"] == "暂停":
        blockers.append("产品处于暂停状态，不能发起新预留")
    if site["status"] != "active":
        blockers.append(f"场地状态为 {site['status']}，当前不可承接新试点")

    # 能力与科室承接判断：场地能力是科室承接的前提，必备能力必须两边都覆盖
    site_missing = required - site_caps
    capable_departments = [dept for dept in active_departments if required <= _caps(dept) and not site_missing]
    risk_departments = [dept for dept in capable_departments if RISK_RANK[str(dept["max_risk_level"])] >= risk_rank]
    ethics_departments = [dept for dept in risk_departments if risk != "high" or int(dept["ethics_ready"]) == 1]
    chosen = ethics_departments[0] if ethics_departments else None
    if chosen is not None:
        chosen = max(
            ethics_departments,
            key=lambda dept: (int(dept["ethics_ready"]), RISK_RANK[str(dept["max_risk_level"])] - risk_rank, len(_caps(dept))),
        )

    if site_missing:
        blockers.append("场地已不具备必备能力：" + "、".join(sorted(site_missing)))
    if active_departments:
        if not capable_departments and not site_missing:
            missing = "、".join(sorted(required)) or "（未声明）"
            blockers.append(f"院内没有科室具备全部必备能力（{missing}）")
        elif capable_departments and not risk_departments:
            blockers.append(f"具备能力的科室最高只能承接低一档风险，无法承接{RISK_LABEL[risk]}产品")
        elif risk_departments and not ethics_departments:
            blockers.append("高风险产品要求承接科室完成伦理准备，当前科室伦理未就绪")
    else:
        if required and not required <= site_caps:
            blockers.append("场地缺少产品必备能力：" + "、".join(sorted(required - site_caps)))
        if risk != "low":
            blockers.append(f"{RISK_LABEL[risk]}产品必须由具备对应能力的科室承接，该场地尚未登记科室")

    if risk == "high" and values["ethics"] != "已通过":
        blockers.append(f"高风险产品的伦理审查状态为“{values['ethics']}”，须已通过方可匹配")

    allowed_regions: set[str] = values["allowed_regions"]  # type: ignore[assignment]
    if requested_region:
        affinity = _region_affinity(str(site["region"]), requested_region)
        if affinity < 0.5:
            blockers.append(f"场地位于{site['region']}，不在申请地区{requested_region}")
    elif allowed_regions and str(site["region"]) not in allowed_regions:
        blockers.append(f"产品限定开放地区为{'、'.join(sorted(allowed_regions))}，不含{site['region']}")

    eligible_pop: set[str] = values["eligible"]  # type: ignore[assignment]
    excluded_pop: set[str] = values["excluded"]  # type: ignore[assignment]
    if requested_population:
        if requested_population in excluded_pop:
            blockers.append(f"适用人群“{requested_population}”被产品明确排除")
        elif eligible_pop and requested_population not in eligible_pop:
            blockers.append(f"产品适用人群不含“{requested_population}”")

    open_from = values["open_from"]  # type: ignore[assignment]
    open_until = values["open_until"]  # type: ignore[assignment]
    if open_from and now < str(open_from):
        blockers.append(f"试点开放窗口尚未开始（{open_from}）")
    if open_until and now > str(open_until):
        blockers.append(f"试点开放窗口已经结束（{open_until}）")

    eligible = not blockers

    if eligible:
        capability_score = 1.0 if required else 0.7
        headroom = (
            min(1.0, 0.6 + 0.2 * (RISK_RANK[str(chosen["max_risk_level"])] - risk_rank))
            if chosen is not None
            else (0.7 if risk == "low" else 0.5)
        )
        ethics_ready = int(chosen["ethics_ready"]) == 1 if chosen is not None else False
        if values["ethics"] == "已通过" and ethics_ready:
            ethics_score = 1.0
        elif values["ethics"] == "已通过":
            ethics_score = 0.6
        else:
            ethics_score = 0.3
        region_score = _region_affinity(str(site["region"]), requested_region)
        if not requested_region and allowed_regions and str(site["region"]) in allowed_regions:
            region_score = 1.0
        if requested_population and requested_population in eligible_pop:
            population_score = 1.0
        elif not requested_population:
            population_score = 0.7
        else:
            population_score = 0.4
        fraction = _window_fraction(open_from, open_until, now)  # type: ignore[arg-type]
        window_score = 1.0 if fraction is None else fraction

        points = {
            "capability": WEIGHTS["capability"] * capability_score,
            "risk_headroom": WEIGHTS["risk_headroom"] * headroom,
            "ethics": WEIGHTS["ethics"] * ethics_score,
            "region": WEIGHTS["region"] * region_score,
            "population": WEIGHTS["population"] * population_score,
            "open_window": WEIGHTS["open_window"] * window_score,
        }
        score = round(sum(points.values()), 1)

        dept_text = f"，承接科室：{chosen['name']}" if chosen is not None else ""
        reasons.append(f"能力匹配：覆盖全部 {len(required)} 项必备能力{dept_text}（+{points['capability']:.1f}）")
        reasons.append(f"风险承接：{RISK_LABEL[risk]}产品对应风险余量充足（+{points['risk_headroom']:.1f}）")
        ethics_text = "伦理审查已通过且科室伦理就绪" if ethics_score == 1.0 else ("产品伦理已通过" if values["ethics"] == "已通过" else f"伦理状态“{values['ethics']}”，建议补强")
        reasons.append(f"伦理准备：{ethics_text}（+{points['ethics']:.1f}）")
        region_text = "与申请地区完全一致" if region_score == 1.0 else ("属于同一地区圈" if region_score >= 0.7 else "地区不完全一致")
        reasons.append(f"地区：{site['region']}，{region_text}（+{points['region']:.1f}）")
        pop_text = f"适用人群包含{requested_population}" if requested_population else "未限定申请人群"
        reasons.append(f"适用人群：{pop_text}（+{points['population']:.1f}）")
        window_text = "开放窗口内时间充裕" if window_score >= 0.5 else ("开放窗口内但剩余时间有限" if fraction is not None else "未限制开放窗口")
        reasons.append(f"开放时间：{window_text}（+{points['open_window']:.1f}）")
    else:
        score = 0.0
        reasons = [f"存在 {len(blockers)} 项硬性阻断，不能进入候选顺序"]

    return PairEvaluation(
        product_id=int(product["id"]),
        site_id=int(site["id"]),
        department_id=int(chosen["id"]) if chosen is not None else None,
        eligible=eligible,
        score=score,
        reasons=reasons,
        blockers=blockers,
    )
