"""Strict actions: no client catalog authority or arbitrary lifecycle transitions."""
from uuid import UUID
from pydantic import Field, field_validator
from models.pa_preparation import StrictModel


class ProposalAction(StrictModel):
    requestId: UUID
    expectedHeadVersion: int = Field(ge=1, strict=True)
    expectedActiveVersion: int | None = Field(ge=1, strict=True)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def safe_reason(cls, value):
        if not value.strip() or any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError('Invalid review reason')
        return value.strip()


class ActivationAction(ProposalAction):
    replaceActive: bool = Field(default=False, strict=True)
