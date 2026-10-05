from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class EligibilityProfile(BaseModel):
    eligible_populations: list[str] = Field(default_factory=list, max_length=50)
    excluded_populations: list[str] = Field(default_factory=list, max_length=50)
    required_capabilities: list[str] = Field(default_factory=list, max_length=100)
    allowed_regions: list[str] = Field(default_factory=list, max_length=100)
    ethics_review_status: Literal["未提交", "审查中", "已通过", "未通过"] = "未提交"
    ethics_approved_at: str | None = Field(default=None, max_length=40)
    ethics_committee: str = Field(default="", max_length=160)
    open_from: str | None = Field(default=None, max_length=40)
    open_until: str | None = Field(default=None, max_length=40)


class DepartmentCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    max_risk_level: Literal["low", "medium", "high"] = "low"
    ethics_ready: bool = False


class DepartmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    capabilities: list[str] | None = Field(default=None, max_length=100)
    max_risk_level: Literal["low", "medium", "high"] | None = None
    ethics_ready: bool | None = None
    active: bool | None = None


class RecommendationRequest(BaseModel):
    product_codes: list[str] | None = Field(default=None, max_length=100)
    site_codes: list[str] | None = Field(default=None, max_length=100)
    requested_region: str = Field(default="", max_length=120)
    requested_population: str = Field(default="", max_length=120)
    requested_by: str = Field(default="", max_length=120)
    batch_code: str | None = Field(default=None, max_length=80)


class ReserveRequest(BaseModel):
    recommendation_id: int | None = None
    product_code: str | None = Field(default=None, max_length=64)
    site_code: str | None = Field(default=None, max_length=64)
    quantity: int = Field(default=1, ge=1, le=100)
    idempotency_key: str = Field(min_length=6, max_length=160)
    hospital_response_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    enterprise_confirm_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def require_target(self) -> "ReserveRequest":
        if self.recommendation_id is None and not (self.product_code and self.site_code):
            raise ValueError("必须提供 recommendation_id 或同时提供 product_code 与 site_code")
        return self


class HospitalResponse(BaseModel):
    decision: Literal["accepted", "rejected", "conditional"]
    responded_by: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)
    conditions: list[str] = Field(default_factory=list, max_length=20)


class EnterpriseConfirm(BaseModel):
    confirmed_by: str = Field(min_length=1, max_length=120)
    accept_conditions: bool = True


class CancelReservation(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class SuspendProduct(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class ResumeProduct(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class SiteCapabilityChange(BaseModel):
    capabilities: list[str] = Field(max_length=100)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)
