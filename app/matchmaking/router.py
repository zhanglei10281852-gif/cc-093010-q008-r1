from __future__ import annotations

from fastapi import APIRouter, Query

from app.matchmaking.schemas import EnterpriseResponse, HospitalResponse, RecommendQuery, ReserveRequest, RevokeRequest
from app.matchmaking.service import MatchmakingService

router = APIRouter(prefix="/api/matchings", tags=["试点名额推荐预留与确认"])

MATCHING_STATUSES = (
    "reserved", "hospital_accepted", "hospital_conditional", "hospital_rejected",
    "confirmed", "enterprise_declined", "expired", "revoked",
)


def service() -> MatchmakingService:
    return MatchmakingService()


@router.post("/recommend")
def recommend(payload: RecommendQuery):
    return service().recommend(payload.model_dump(exclude_none=True))


@router.post("/reservations", status_code=201)
def reserve(payload: ReserveRequest, actor: str = Query(..., min_length=1, max_length=120)):
    return service().reserve(payload.model_dump(), actor)


@router.post("/reservations/{matching_code}/hospital-response")
def hospital_respond(matching_code: str, payload: HospitalResponse):
    return service().hospital_respond(matching_code, payload.model_dump())


@router.post("/reservations/{matching_code}/enterprise-response")
def enterprise_respond(matching_code: str, payload: EnterpriseResponse):
    return service().enterprise_confirm(matching_code, payload.model_dump())


@router.post("/reservations/{matching_code}/revoke")
def revoke(matching_code: str, payload: RevokeRequest):
    return service().revoke(matching_code, payload.actor, payload.reason)


@router.post("/expire-due")
def expire_due(actor: str = Query(default="system-time-rule", min_length=1, max_length=120)):
    return service().expire_due(actor)


@router.get("")
def list_matchings(
    status: list[str] = Query(default_factory=list),
    site_code: str | None = None,
    product_code: str | None = None,
    pending_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
):
    statuses = [item for item in status if item in MATCHING_STATUSES]
    return service().list_matchings({
        "statuses": statuses, "site_code": site_code, "product_code": product_code,
        "pending_only": pending_only, "limit": limit,
    })


@router.get("/inbox")
def pending_inbox():
    return service().pending_inbox()


@router.get("/timeline")
def capacity_timeline(
    start: str = Query(..., min_length=10),
    end: str = Query(..., min_length=10),
    site_code: str | None = Query(default=None, max_length=64),
):
    return service().capacity_timeline({"start": start, "end": end, "site_code": site_code})


@router.get("/notifications")
def notifications(recipient_role: str | None = Query(default=None, pattern="^(hospital|enterprise|operator)$"), limit: int = Query(default=50, ge=1, le=500)):
    return service().notifications(recipient_role, limit)


@router.post("/notifications/{notification_id}/delivered")
def mark_delivered(notification_id: int):
    return service().mark_delivered(notification_id)


@router.get("/{matching_code}")
def get_matching(matching_code: str):
    return service().get_matching(matching_code)
