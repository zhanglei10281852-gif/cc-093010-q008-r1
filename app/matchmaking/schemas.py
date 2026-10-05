from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RecommendQuery(BaseModel):
    product_code: str | None = Field(default=None, min_length=2, max_length=64)
    site_code: str | None = Field(default=None, min_length=2, max_length=64)
    category: str | None = Field(default=None, max_length=40)
    risk_level: Literal["low", "medium", "high"] | None = None
    region: str | None = Field(default=None, max_length=120)
    site_type: str | None = Field(default=None, max_length=40)
    slot_quantity: int = Field(default=1, ge=1, le=1000)
    window_start: str | None = None
    window_end: str | None = None
    include_inactive: bool = False


class ReserveRequest(BaseModel):
    product_code: str = Field(min_length=2, max_length=64)
    site_code: str = Field(min_length=2, max_length=64)
    slot_quantity: int = Field(default=1, ge=1, le=1000)
    ttl_seconds: int = Field(default=72 * 3600, ge=900, le=14 * 24 * 3600)
    hospital_contact: str = Field(default="", max_length=120)
    reason: str = Field(default="运营发起限时预留", max_length=1000)
    idempotency_key: str = Field(default="", max_length=160)


class HospitalResponse(BaseModel):
    decision: Literal["accept", "conditional", "reject"]
    actor: str = Field(default="hospital", min_length=1, max_length=120)
    condition: str = Field(default="", max_length=1000)
    note: str = Field(default="", max_length=1000)


class EnterpriseResponse(BaseModel):
    decision: Literal["confirm", "decline"] = "confirm"
    actor: str = Field(default="enterprise", min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)


class RevokeRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class MatchingListQuery(BaseModel):
    statuses: list[Literal[
        "reserved", "hospital_accepted", "hospital_conditional", "hospital_rejected",
        "confirmed", "enterprise_declined", "expired", "revoked",
    ]] = Field(default_factory=list)
    site_code: str | None = Field(default=None, max_length=64)
    product_code: str | None = Field(default=None, max_length=64)
    pending_only: bool = False
    limit: int = Field(default=100, ge=1, le=500)


class TimelineQuery(BaseModel):
    start: str = Field(min_length=10)
    end: str = Field(min_length=10)
    site_code: str | None = Field(default=None, max_length=64)
