"""Internal preparation inputs. No transport or public administrator API."""
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreparationStatus(str, Enum):
    READY = "READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    BLOCKED = "BLOCKED"


class MappingStatus(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class Actor(StrictModel):
    # Construct from authenticated server context, never an uploaded row.
    actorId: str = Field(min_length=1, max_length=200)
    canPrepare: bool = Field(default=False, strict=True)
    canProposeMappings: bool = Field(default=False, strict=True)
    canIngestCatalog: bool = Field(default=False, strict=True)
    canValidateCatalog: bool = Field(default=False, strict=True)
    canTrustCatalog: bool = Field(default=False, strict=True)
    canReviewMappings: bool = Field(default=False, strict=True)


class MappingIdentity(StrictModel):
    sourceNamespaceId: str = Field(min_length=1, max_length=200)
    sourceIdentityType: Literal["CODE", "DESCRIPTION"]
    sourceIdentityKey: str = Field(min_length=1, max_length=500)

    @field_validator("sourceNamespaceId", "sourceIdentityKey")
    @classmethod
    def no_blank(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError("Identity must be nonblank and trimmed")
        return value


class MappingProposal(StrictModel):
    identity: MappingIdentity
    sourceCode: str | None = Field(default=None, max_length=200)
    sourceDescription: str = Field(min_length=1, max_length=1000)
    proposedMedicloudCode: str = Field(min_length=1, max_length=200)
    mappingSource: Literal["MANUAL", "CURSOR", "AI", "FUZZY"]
    candidateConfidence: float | None = Field(default=None, ge=0, le=1)


class ReviewEvidence(StrictModel):
    reviewedBy: str = Field(min_length=1, max_length=200)
    reviewedAt: datetime
    evidenceReference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reviewedAt")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Review timestamp must include timezone")
        return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)

    @field_validator("evidenceReference", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Review evidence must be nonblank")
        return value


class CatalogEntry(StrictModel):
    procedureCode: str = Field(min_length=1, max_length=200)
    procedureDescription: str = Field(min_length=1, max_length=1000)


class CatalogEvidence(StrictModel):
    source: Literal["MEDICLOUD_PROCEDUREDATA"] = "MEDICLOUD_PROCEDUREDATA"
    sourceExtractedAt: datetime
    recordCount: int = Field(gt=0)
    contentHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidenceReference: str = Field(min_length=1, max_length=200)
    schemaVersion: Literal[1] = 1

    @field_validator("sourceExtractedAt")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Extraction timestamp must include timezone")
        return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)


MEMBER_EVIDENCE_MAX_AGE = timedelta(hours=1)


class CatalogValidationEvidence(CatalogEvidence):
    snapshotId: str
    validationMethod: Literal["SERVER_CONTROLLED_MEDICLOUD_EXTRACTION"]
    validatedAt: datetime
    validatedBy: str = Field(min_length=1)
    validationEvidenceId: str = Field(min_length=1)

    @field_validator("validatedAt")
    @classmethod
    def validation_aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Validation timestamp must include timezone")
        return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)


class MemberEvidence(StrictModel):
    evidenceId: str = Field(min_length=1, max_length=200)
    validatedAt: datetime
    expiresAt: datetime
    orderId: str
    orderVersion: int = Field(ge=0)
    # Restricted integrity binding only; a hash is not anonymization.
    enrolleeFingerprint: str
    memberId: str
    groupId: str | None = None
    groupLegacyId: str
    isPrincipal: bool
    dependantId: str | None = None
    isTerminated: bool
    isDeleted: bool

    @field_validator("isPrincipal", "isTerminated", "isDeleted", mode="before")
    @classmethod
    def explicit_flags(cls, value):
        if type(value) is not bool:
            raise ValueError("Member flags must be explicit booleans")
        return value

    @field_validator("validatedAt", "expiresAt")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Member evidence timestamp must include timezone")
        return value.astimezone(timezone.utc).replace(microsecond=(value.microsecond // 1000) * 1000)

    @model_validator(mode="after")
    def bounded_freshness(self):
        if not self.validatedAt < self.expiresAt <= self.validatedAt + MEMBER_EVIDENCE_MAX_AGE:
            raise ValueError("Member evidence exceeds maximum validity")
        return self


class BatchRecord(StrictModel):
    businessDate: str
    timezone: Literal["Africa/Lagos"] = "Africa/Lagos"
    status: Literal["OPEN", "AUDITING", "CLOSED"] = "OPEN"
    version: int = Field(ge=0)
    createdAt: datetime
    createdBy: str
    totalOrders: int = Field(default=0, ge=0)
    totalLines: int = Field(default=0, ge=0)
    readyLines: int = Field(default=0, ge=0)
    reviewRequiredLines: int = Field(default=0, ge=0)
    blockedLines: int = Field(default=0, ge=0)


class PreparationLineEvidence(StrictModel):
    """Allowlisted, immutable evidence. Storage adds ID/revision/current metadata."""
    orderId: str
    orderLineId: str
    orderVersion: int
    assignmentVersion: int
    referringProviderId: str | None
    aggregatorId: str | None
    issuePaProviderId: str | None
    sourceNamespaceId: str | None
    sourceIdentityType: Literal["CODE", "DESCRIPTION"]
    sourceIdentityKey: str
    sourceCode: str | None
    sourceDescription: str
    diagnosisCode: str | None
    quantity: int | None = Field(strict=True)
    serviceInput: dict
    lineAmount: str | None
    currency: Literal["NGN"]
    priceSource: Literal["finalProcedurePrices", "approvedProcedurePrices", "deliveryFee"]
    memberSnapshot: MemberEvidence | None
    mappingId: str | None
    mappingVersion: int | None
    mappingSource: str | None
    candidateConfidence: float | None
    catalogSnapshotId: str | None
    medicloudCode: str | None
    medicloudDescription: str | None
    memberValidation: Literal["VALID", "INVALID"]
    pricingValidation: Literal["VALID", "INVALID"]
    providerValidation: Literal["VALID", "INVALID"]
    mappingValidation: Literal["VALID", "REVIEW_REQUIRED"]
    providerTariffValidation: Literal["NOT_IMPLEMENTED"]
    tariffPriceValidation: Literal["NOT_IMPLEMENTED"]
    preparationStatus: PreparationStatus
    blockingReasons: list[str]
    reviewReasons: list[str]
    submissionEligible: bool
    submissionEligibilityReasons: list[str]
    inputFingerprint: str
