from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.clock import from_storage

# 产品类别对应的能力标识，用于和场地 capabilities 对齐
CATEGORY_CAPABILITY = {
    "康复设备": "rehab-device",
    "辅助诊断": "diagnostic-ai",
    "数字疗法": "digital-therapeutic",
    "慢病管理": "chronic-care",
    "数字中医": "tcm-digital",
    "健康消费": "consumer-health",
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
ETHICS_ORDER = {"none": 0, "basic": 1, "full": 2}
REQUIRED_ETHICS = {"low": "none", "medium": "basic", "high": "full"}


@dataclass(slots=True)
class ScoreCard:
    product_code: str
    site_code: str
    eligible: bool = True
    total_score: int = 0
    scores: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def add(self, dimension: str, score: int, reason: str) -> None:
        self.scores[dimension] = score
        self.total_score += score
        if reason:
            self.reasons.append(reason)

    def block(self, reason: str) -> None:
        self.eligible = False
        self.blockers.append(reason)
        self.reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "site_code": self.site_code,
            "eligible": self.eligible,
            "total_score": self.total_score,
            "scores": self.scores,
            "reasons": self.reasons,
            "blockers": self.blockers,
        }


def _window_overlap(window: dict[str, Any], start: Any, end: Any) -> bool:
    w_start = from_storage(str(window.get("starts_at", "")))
    w_end = from_storage(str(window.get("ends_at", "")))
    if w_start is None or w_end is None:
        return False
    return w_start <= start and end <= w_end


class MatchingRanker:
    """按产品类别、风险级别、适用人群、地区、场地能力、伦理准备和开放时间给出带理由的候选顺序。"""

    def rank(
        self,
        products: list[dict[str, Any]],
        sites: list[dict[str, Any]],
        *,
        now: Any,
        slot_quantity: int = 1,
        window_start: str | None = None,
        window_end: str | None = None,
        occupied: dict[int, int] | None = None,
    ) -> list[dict[str, Any]]:
        occupied = occupied or {}
        requested_start = from_storage(window_start) if window_start else None
        requested_end = from_storage(window_end) if window_end else None
        cards: list[ScoreCard] = []
        for product in products:
            for site in sites:
                cards.append(self._score_pair(product, site, now, slot_quantity, requested_start, requested_end, occupied.get(int(site["id"]), 0)))
        cards.sort(key=lambda card: (card.eligible, card.total_score), reverse=True)
        result: list[dict[str, Any]] = []
        for order, card in enumerate(cards, start=1):
            data = card.as_dict()
            data["rank"] = order if card.eligible else None
            result.append(data)
        return result

    def _score_pair(self, product: dict[str, Any], site: dict[str, Any], now: Any, slot_quantity: int, requested_start: Any, requested_end: Any, occupied_slots: int) -> ScoreCard:
        card = ScoreCard(product_code=str(product["code"]), site_code=str(site["code"]))
        product_pops = list(product.get("intended_populations") or [])
        site_pops = list(site.get("served_populations") or [])
        capabilities = list(site.get("capabilities") or [])
        windows = list(site.get("open_windows") or [])
        supported_risks = list(site.get("supported_risk_levels") or [])
        ethics = str(site.get("ethics_prepared_level") or "none")
        risk = str(product["risk_level"])

        # 硬门槛：产品与场地状态
        if not product.get("active"):
            card.block(f"产品 {product['code']} 已停用，不能进入候选")
        if product.get("regulatory_status") == "暂停":
            card.block(f"产品 {product['code']} 处于暂停状态，暂停期间不得安排新试点")
        if site.get("status") != "active":
            card.block(f"场地 {site['code']} 当前状态为 {site.get('status')}，不接收新名额")

        # 硬门槛：风险级别可承接
        if risk in supported_risks:
            card.add("risk_capacity", 10, f"场地声明可承接 {risk} 级风险产品")
        else:
            card.block(f"高风险错配：场地 {site['code']} 未声明承接 {risk} 级风险产品")

        # 硬门槛：伦理准备
        required_ethics = REQUIRED_ETHICS[risk]
        if ETHICS_ORDER[ethics] >= ETHICS_ORDER[required_ethics]:
            ethics_score = 10 if ethics == required_ethics else 12
            card.add("ethics", ethics_score, f"伦理准备 {ethics} 满足 {risk} 级产品最低要求 {required_ethics}")
        else:
            card.block(f"伦理准备不足：{risk} 级产品至少需要 {required_ethics}，场地仅为 {ethics}")

        # 硬门槛：类别能力
        required_capability = CATEGORY_CAPABILITY.get(str(product["category"]))
        if required_capability and required_capability in capabilities:
            card.add("capability", 20, f"场地具备 {product['category']} 所需能力 {required_capability}")
        else:
            card.block(f"能力不匹配：场地缺少 {product['category']} 所需能力 {required_capability}")

        # 适用人群：产品声明的人群需要场地方覆盖，覆盖越全得分越高
        if product_pops:
            if not site_pops:
                card.block(f"适用人群不匹配：产品面向 {'、'.join(product_pops)}，场地未登记对应服务人群")
            else:
                covered = [item for item in product_pops if item in site_pops]
                if not covered:
                    card.block(f"适用人群不匹配：场地服务人群 {'、'.join(site_pops)} 不覆盖产品目标人群 {'、'.join(product_pops)}")
                else:
                    ratio = len(covered) / len(product_pops)
                    score = max(6, round(15 * ratio))
                    card.add("population", score, f"覆盖 {len(covered)}/{len(product_pops)} 类目标人群：{'、'.join(covered)}")
        elif site_pops:
            card.add("population", 8, f"产品未限定人群，场地可服务 {'、'.join(site_pops[:3])}")
        else:
            card.add("population", 6, "双方均未限定人群，按通用人群处理")

        # 地区
        product_region = str(product.get("origin_country") or "")
        site_region = str(site.get("region") or "")
        if product_region and site_region and (product_region == site_region or site_region in product_region or product_region in site_region):
            card.add("region", 15, f"地区直接匹配：{site_region}")
        elif site_region:
            card.add("region", 6, f"地区不一致（产品来源 {product_region or '未登记'} / 场地 {site_region}），需跨区协调")
        else:
            card.add("region", 6, "场地未登记地区")

        # 开放时间
        self._score_windows(card, windows, now, requested_start, requested_end)

        # 剩余容量
        capacity = int(site.get("max_concurrent") or 0)
        remaining = capacity - occupied_slots
        if remaining < slot_quantity:
            card.block(f"场地容量不足：剩余 {max(0, remaining)}/{capacity} 个名额，本次需要 {slot_quantity} 个")
        else:
            ratio = remaining / capacity if capacity else 0
            card.add("capacity", max(4, round(10 * ratio)), f"剩余容量 {remaining}/{capacity}，可容纳本次 {slot_quantity} 个名额")

        return card

    @staticmethod
    def _score_windows(card: ScoreCard, windows: list[dict[str, Any]], now: Any, requested_start: Any, requested_end: Any) -> None:
        if not windows:
            card.add("open_window", 8, "场地未登记开放时间窗，按可协调处理")
            return
        if requested_start is not None and requested_end is not None:
            covering = [w for w in windows if _window_overlap(w, requested_start, requested_end)]
            if covering:
                card.add("open_window", 20, "存在完整覆盖计划时段的开放时间窗")
            else:
                card.block("开放时间不匹配：没有任何开放时间窗完整覆盖计划时段")
            return
        open_now = [w for w in windows if _window_overlap(w, now, now)]
        if open_now:
            card.add("open_window", 16, "场地当前处于开放时间窗内")
        else:
            card.add("open_window", 10, "场地当前不在开放窗内，但已登记未来开放时间，可预约")
