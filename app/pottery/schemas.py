from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


def _check_rationale(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("复核依据不能为空")
    return text


class ContextCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    kind: str = Field(default="灰坑", max_length=40)
    description: str = Field(default="", max_length=200)


class ExclusionCreate(BaseModel):
    context_a: str = Field(..., min_length=1, max_length=60)
    context_b: str = Field(..., min_length=1, max_length=60)
    reason: str = Field(..., min_length=1, max_length=200)


class SherdCreate(BaseModel):
    sherd_code: str = Field(..., min_length=1, max_length=80)
    part: str = Field(..., pattern="^(rim|body|base|handle|other)$")
    fabric_color: str = Field(..., min_length=1, max_length=40)
    thickness_min_mm: float = Field(..., gt=0, le=1000)
    thickness_max_mm: float = Field(..., gt=0, le=1000)
    decoration_code: str = Field(default="", max_length=60)
    length_mm: float | None = Field(default=None, gt=0, le=10000)
    width_mm: float | None = Field(default=None, gt=0, le=10000)
    rim_arc_percent: float = Field(default=0, ge=0, le=100)
    vessel_type: str = Field(default="", max_length=40)
    context_code: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def check_thickness(self):
        if self.thickness_min_mm > self.thickness_max_mm:
            raise ValueError("厚度区间下界不能大于上界")
        return self


class RuleSetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    rules: dict


class ConfirmIn(BaseModel):
    rationale: str = Field(..., min_length=1, max_length=500)
    expected_rule_set_id: int | None = None

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        return _check_rationale(value)


class RejectIn(BaseModel):
    rationale: str = Field(..., min_length=1, max_length=500)

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        return _check_rationale(value)


class MergeIn(BaseModel):
    vessel_ids: list[int] = Field(..., min_length=2)
    rationale: str = Field(..., min_length=1, max_length=500)

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        return _check_rationale(value)


class SplitIn(BaseModel):
    groups: list[list[str]] = Field(..., min_length=2)
    rationale: str = Field(..., min_length=1, max_length=500)

    @field_validator("rationale")
    @classmethod
    def rationale_not_blank(cls, value: str) -> str:
        return _check_rationale(value)


class PlanCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)


class ImportIn(BaseModel):
    batch_key: str = Field(..., min_length=1, max_length=120)
    file_name: str = Field(default="", max_length=200)
    contexts: list[ContextCreate] = Field(default_factory=list)
    exclusions: list[ExclusionCreate] = Field(default_factory=list)
    sherds: list[SherdCreate] = Field(default_factory=list)
