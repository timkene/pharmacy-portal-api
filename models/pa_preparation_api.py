"""Allowlisted public preparation contracts: no member evidence or privilege input."""
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBatch(PublicModel):
    businessDate: date


class BatchVersion(PublicModel):
    expectedVersion: int = Field(ge=0, strict=True)


class ProviderView(PublicModel):
    referringProviderId: str | None
    referringProviderDisplay: str | None
    aggregatorId: str | None
    aggregatorDisplay: str | None
    issuePaProviderId: str | None


class MedicationView(PublicModel):
    sourceCode: str | None
    sourceDescription: str
    quantity: int | None
    dosage: str | None


class PricingView(PublicModel):
    approvedLineAmount: str | None
    currency: Literal["NGN"]
    priceSource: Literal["finalProcedurePrices", "approvedProcedurePrices", "deliveryFee"]


class MappingView(PublicModel):
    sourceNamespaceId: str | None
    mappingStatus: Literal["ACTIVE", "PROPOSED", "VALIDATED", "REJECTED", "RETIRED", "UNMAPPED"]
    mappedMediCloudCode: str | None
    mappedMediCloudDescription: str | None


class ValidationView(PublicModel):
    member: Literal["VALID", "INVALID", "UNAVAILABLE"]
    memberFreshAsOf: datetime | None
    memberFreshUntil: datetime | None
    price: Literal["VALID", "INVALID"]
    provider: Literal["VALID", "INVALID"]
    mapping: Literal["VALID", "REVIEW_REQUIRED"]


class TransmissionView(PublicModel):
    state: Literal["pending", "submitting", "generated", "verification_required", "failed_rejected", "failed_retryable", "not_generated", "UNKNOWN"]
    verificationRequired: bool
    confirmedPaReference: str | None


class TeamLine(PublicModel):
    batchId: str
    preparationLineId: str
    preparationRevision: int
    orderId: str
    orderLineId: str
    calculatedAt: datetime
    provider: ProviderView
    medication: MedicationView
    pricing: PricingView
    mapping: MappingView
    validation: ValidationView
    preparationStatus: Literal["READY", "REVIEW_REQUIRED", "BLOCKED"]
    reasonCodes: list[str]
    isStale: bool
    submissionEligible: bool
    transmission: TransmissionView
