from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from app.core.errors import ValidationError
from app.matching.schemas import (
    CancelReservation,
    DepartmentCreate,
    DepartmentUpdate,
    EligibilityProfile,
    EnterpriseConfirm,
    HospitalResponse,
    RecommendationRequest,
    ReserveRequest,
    ResumeProduct,
    SiteCapabilityChange,
    SuspendProduct,
)
from app.matching.service import MatchingService

router = APIRouter(prefix="/api/matching", tags=["健康创新产品-场地匹配与容量预留"])

RESERVATION_STATUSES = {"pending", "conditional", "accepted", "rejected", "confirmed", "expired", "cancelled"}


def service() -> MatchingService:
    return MatchingService()


@router.put("/products/{product_code}/eligibility")
def set_eligibility(product_code: str, payload: EligibilityProfile, actor: str = Query(..., min_length=1)):
    return service().set_product_eligibility(product_code, payload.model_dump(), actor)


@router.post("/sites/{site_code}/departments", status_code=201)
def add_department(site_code: str, payload: DepartmentCreate, actor: str = Query(..., min_length=1)):
    return service().add_site_department(site_code, payload.model_dump(), actor)


@router.patch("/sites/{site_code}/departments/{department_id}")
def update_department(site_code: str, department_id: int, payload: DepartmentUpdate, actor: str = Query(..., min_length=1)):
    return service().update_site_department(site_code, department_id, payload.model_dump(), actor)


@router.post("/recommendations", status_code=201)
def recommend(payload: RecommendationRequest):
    return service().recommend(payload.model_dump())


@router.post("/reservations", status_code=202)
def reserve(payload: ReserveRequest, actor: str = Query(..., min_length=1)):
    return service().reserve(payload.model_dump(), actor)


@router.get("/reservations")
def list_reservations(
    status: list[str] | None = Query(default=None),
    site_code: str | None = None,
    product_code: str | None = None,
    requested_by: str | None = None,
):
    if status:
        invalid = set(status) - RESERVATION_STATUSES
        if invalid:
            raise ValidationError(f"不支持的预留状态：{'、'.join(sorted(invalid))}")
    return {
        "items": service().search_reservations(
            statuses=status, site_code=site_code, product_code=product_code, requested_by=requested_by,
        )
    }


@router.get("/reservations/{code}")
def get_reservation(code: str):
    return service().get_reservation(code)


@router.post("/reservations/{code}/hospital-response")
def hospital_respond(code: str, payload: HospitalResponse):
    return service().hospital_respond(code, payload.model_dump())


@router.post("/reservations/{code}/enterprise-confirm")
def enterprise_confirm(code: str, payload: EnterpriseConfirm):
    return service().enterprise_confirm(code, payload.confirmed_by, payload.accept_conditions)


@router.post("/reservations/{code}/cancel")
def cancel_reservation(code: str, payload: CancelReservation):
    return service().cancel(code, payload.actor, payload.reason)


@router.post("/sweep-expired")
def sweep_expired():
    return service().sweep_expired()


@router.get("/dashboard")
def dashboard(within_seconds: int = Query(default=3600, ge=60, le=604800)):
    return service().dashboard(within_seconds=within_seconds)


@router.get("/capacity-timeline")
def capacity_timeline(start: str | None = None, end: str | None = None, site_code: str | None = None):
    return service().capacity_timeline(start, end, site_code)


@router.get("/messages")
def list_messages(
    recipient_role: Literal["operator", "hospital", "enterprise"] = Query(...),
    recipient: str = Query(..., min_length=1),
    unread_only: bool = False,
):
    return service().messages(recipient_role, recipient, unread_only)


@router.post("/messages/{message_id}/read")
def mark_read(
    message_id: int,
    recipient_role: Literal["operator", "hospital", "enterprise"],
    recipient: str = Query(..., min_length=1),
):
    return service().mark_read(message_id, recipient_role, recipient)


@router.post("/products/{product_code}/suspend")
def suspend_product(product_code: str, payload: SuspendProduct):
    return service().suspend_product(product_code, payload.actor, payload.reason)


@router.post("/products/{product_code}/resume")
def resume_product(product_code: str, payload: ResumeProduct):
    return service().resume_product(product_code, payload.actor)


@router.patch("/sites/{site_code}/capabilities")
def change_site_capabilities(site_code: str, payload: SiteCapabilityChange):
    return service().change_site_capabilities(site_code, payload.capabilities, payload.actor, payload.reason)
