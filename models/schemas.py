from __future__ import annotations

from datetime import datetime
from typing import Annotated, List, Optional, Literal

from pydantic import BaseModel, EmailStr, StringConstraints, Field


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class StaffLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AggregatorSignupRequest(BaseModel):
    companyName: str
    contactName: str
    email: EmailStr
    phone: str
    password: str


class AggregatorLoginRequest(BaseModel):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: str

    def model_post_init(self, __context):
        if not (self.username or self.email):
            raise ValueError("username or email is required")


class UserResponse(BaseModel):
    name: str
    email: Optional[str] = None


class AggregatorIdentityResponse(BaseModel):
    name: str
    email: Optional[str] = None
    userId: str
    mustChangePassword: Optional[bool] = None
    emailVerified: Optional[bool] = None
    hasEmail: Optional[bool] = None
    medicloudProviderId: Optional[str] = None
    companyName: Optional[str] = None


class PasswordChangeRequest(BaseModel):
    currentPassword: str
    newPassword: str


class AggregatorEmailRequest(BaseModel):
    email: EmailStr


class TokenRequest(BaseModel):
    token: Optional[str] = None
    code: Optional[str] = None

    def model_post_init(self, __context):
        if not (self.token or self.code):
            raise ValueError("token or code is required")


class ForgotPasswordRequest(BaseModel):
    identifier: Optional[str] = None
    username: Optional[str] = None
    email: Optional[EmailStr] = None

    def model_post_init(self, __context):
        value = self.identifier or self.username or (str(self.email) if self.email else None)
        if not value or not value.strip():
            raise ValueError("identifier, username, or email is required")
        self.identifier = value.strip()


class ResetPasswordRequest(BaseModel):
    token: str
    newPassword: str


class StaffIdentityResponse(BaseModel):
    name: str
    email: Optional[str] = None
    userId: str


class AuthResponse(BaseModel):
    success: bool
    user: Optional[UserResponse | AggregatorIdentityResponse] = None
    session: Optional[str] = None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

class Enrollee(BaseModel):
    enrolleeId: str
    fullName: str
    phone: Optional[str] = None
    address: Optional[str] = None
    title: Optional[str] = None
    gender: Optional[str] = None
    dateOfBirth: Optional[str] = None
    planType: Optional[str] = None
    groupName: Optional[str] = None
    email: Optional[str] = None
    effectiveDate: Optional[str] = None
    terminationDate: Optional[str] = None
    isterminated: Optional[bool] = None


class Provider(BaseModel):
    providerId: str
    providerName: str


class Medication(BaseModel):
    lineId: Optional[str] = None
    procedureCode: Optional[str] = None
    name: str
    dosage: str
    quantity: int
    tablets: int = 1
    frequency: Optional[str] = None
    durationDays: Optional[int] = None
    diagnosisCode: Optional[str] = None
    diagnosis: str


class CreateOrderRequest(BaseModel):
    enrollee: Enrollee
    provider: Provider
    medications: List[Medication]


class BidOut(BaseModel):
    id: str
    orderId: str
    aggregatorId: str
    aggregatorName: str
    unitPrice: float
    totalPrice: float
    procedurePrices: Optional[list[dict]] = None
    isCheapest: bool = False
    submittedAt: datetime


class RejectOrderRequest(BaseModel):
    comment: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class AcceptOrderRequest(BaseModel):
    expectedVersion: Optional[int] = Field(default=None, ge=0)


class VersionRequest(BaseModel):
    expectedVersion: int = Field(ge=0)


class ReasonRequest(VersionRequest):
    reason: Reason


class DirectQuoteRequest(VersionRequest):
    totalPrice: Optional[Price] = None
    procedurePrices: Optional[list[dict]] = None


class DirectApproveRequest(VersionRequest):
    adjusted_price: Optional[Price] = None
    procedurePrices: Optional[list[dict]] = None
    reason: Optional[Reason] = None


class PriceAdjustmentRequest(ReasonRequest):
    totalPrice: Optional[Price] = None
    procedurePrices: Optional[list[dict]] = None


class AssignOrderRequest(BaseModel):
    aggregatorId: str
    expectedVersion: Optional[int] = Field(default=None, ge=0)


class LifecycleFields(BaseModel):
    version: int = 0
    assignmentVersion: int = 0
    directQuote: Optional[dict] = None
    priceApprovedAt: Optional[datetime] = None
    fulfilledAt: Optional[datetime] = None
    acceptedAt: Optional[datetime] = None
    cancelledAt: Optional[datetime] = None
    recalledAt: Optional[datetime] = None
    paGeneration: dict = Field(default_factory=lambda: {"available": False, "status": "not_configured"})
    quotedProcedurePrices: Optional[list[dict]] = None
    approvedProcedurePrices: Optional[list[dict]] = None
    finalProcedurePrices: Optional[list[dict]] = None
    medicationSubtotal: Optional[float] = None
    overallTotal: Optional[float] = None


class OrderSummary(LifecycleFields):
    reviewFlags: Optional[dict] = None
    id: str
    intakeId: str
    enrollee: Enrollee
    medications: List[Medication] = []
    diagnosis: Optional[str] = None
    status: str
    biddingEndsAt: Optional[datetime] = None
    createdAt: datetime
    completedAt: Optional[datetime] = None
    bidCount: int
    winnerName: Optional[str] = None
    winnerTotalPrice: Optional[float] = None
    fulfillmentType: Optional[str] = None
    deliveryFee: Optional[float] = None
    assignmentType: Optional[str] = None
    denialComment: Optional[str] = None


class OrderDetail(LifecycleFields):
    history: list[dict] = Field(default_factory=list)
    completedAt: Optional[datetime] = None
    reviewFlags: Optional[dict] = None
    id: str
    intakeId: str
    enrollee: Enrollee
    provider: Optional[Provider] = None
    medications: List[Medication]
    biddingEndsAt: Optional[datetime] = None
    status: str
    winnerId: Optional[str] = None
    winnerName: Optional[str] = None
    winnerTotalPrice: Optional[float] = None
    fulfillmentType: Optional[str] = None
    deliveryFee: Optional[float] = None
    createdAt: datetime
    createdBy: str
    bids: List[BidOut] = []
    assignmentType: Optional[str] = None
    denialComment: Optional[str] = None
    deniedBy: Optional[dict] = None
    deniedAt: Optional[datetime] = None


class OrderListResponse(BaseModel):
    orders: List[OrderSummary]
    total: int
    page: int


class CreateOrderResponse(BaseModel):
    success: bool
    orderId: str


# ---------------------------------------------------------------------------
# Bids
# ---------------------------------------------------------------------------

class PlaceBidRequest(BaseModel):
    unitPrice: Optional[Price] = None
    totalPrice: Optional[Price] = None
    procedurePrices: Optional[list[dict]] = None


class FulfillOrderRequest(BaseModel):
    fulfillmentType: Literal["delivered", "picked_up"]
    deliveryFee: Optional[Price] = None
    expectedVersion: Optional[int] = Field(default=None, ge=0)


class UpdateOrderRequest(BaseModel):
    enrollee: Optional[Enrollee] = None
    provider: Optional[Provider] = None
    medications: Optional[List[Medication]] = None


# ---------------------------------------------------------------------------
# Aggregator dashboard
# ---------------------------------------------------------------------------

class AggregatorDashboardResponse(BaseModel):
    openSessions: list
    wonOrders: list
    completedOrders: list


# ---------------------------------------------------------------------------
# Klaire callback
# ---------------------------------------------------------------------------

class KlaireCallbackRequest(BaseModel):
    received: bool


class ClearlineApproveRequest(BaseModel):
    adjusted_price: Optional[Price] = None
    procedurePrices: Optional[list[dict]] = None
    reason: Optional[Reason] = None
