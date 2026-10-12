"""Bounded request contracts; actor and permissions are server-owned."""
from uuid import UUID
from pydantic import Field
from models.pa_preparation import StrictModel


class CreateReviewExport(StrictModel):
    expectedVersion: int = Field(ge=0, strict=True)
    after: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100, strict=True)
