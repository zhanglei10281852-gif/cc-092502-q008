from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Part = Literal["rim", "body", "base", "handle", "other"]


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    config: dict = Field(default_factory=dict)


class SherdCreate(BaseModel):
    sherd_code: str = Field(min_length=1, max_length=80)
    part: Part
    fabric_color: str = Field(min_length=1, max_length=40)
    thickness_min: float = Field(ge=0, le=2000)
    thickness_max: float = Field(ge=0, le=2000)
    decoration_codes: list[str] = Field(default_factory=list, max_length=20)
    diameter_mm: float | None = Field(default=None, ge=0, le=5000)
    length_mm: float | None = Field(default=None, ge=0, le=5000)
    width_mm: float | None = Field(default=None, ge=0, le=5000)
    context_code: str = Field(min_length=1, max_length=80)
    context_layer: str = Field(default="", max_length=80)
    typology_code: str = Field(default="", max_length=40)
    rim_percent: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def check_thickness(self):
        if self.thickness_min > self.thickness_max:
            raise ValueError("厚度区间下界不能大于上界")
        return self


class SherdUpdate(BaseModel):
    part: Part | None = None
    fabric_color: str | None = Field(default=None, min_length=1, max_length=40)
    thickness_min: float | None = Field(default=None, ge=0, le=2000)
    thickness_max: float | None = Field(default=None, ge=0, le=2000)
    decoration_codes: list[str] | None = Field(default=None, max_length=20)
    diameter_mm: float | None = Field(default=None, ge=0, le=5000)
    length_mm: float | None = Field(default=None, ge=0, le=5000)
    width_mm: float | None = Field(default=None, ge=0, le=5000)
    context_code: str | None = Field(default=None, min_length=1, max_length=80)
    context_layer: str | None = Field(default=None, max_length=80)
    typology_code: str | None = Field(default=None, max_length=40)
    rim_percent: float | None = Field(default=None, ge=0, le=100)


class ReviewAction(BaseModel):
    rationale: str = Field(min_length=1, max_length=500)

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("复核依据不能为空")
        return value


class ConfirmAction(ReviewAction):
    vessel_code: str | None = Field(default=None, max_length=80)


class MergeAction(ReviewAction):
    vessel_ids: list[int] = Field(min_length=2, max_length=50)
    vessel_code: str | None = Field(default=None, max_length=80)


class PlanRun(BaseModel):
    plan_code: str = Field(min_length=1, max_length=80)
    contexts: list[str] | None = Field(default=None, max_length=200)
    parts: list[Part] | None = Field(default=None, max_length=10)


class ImportBundle(BaseModel):
    rule_set: RuleSetCreate | None = None
    sherds: list[SherdCreate] = Field(default_factory=list, max_length=5000)
    plan: PlanRun | None = None
    run_candidates: bool = True
    run_stats: bool = True
