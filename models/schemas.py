from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr


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
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    name: str
    email: str


class AuthResponse(BaseModel):
    success: bool
    user: Optional[UserResponse] = None
    session: Optional[str] = None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

class Enrollee(BaseModel):
    enrolleeId: str
    fullName: str


class Provider(BaseModel):
    providerId: str
    providerName: str


class Medication(BaseModel):
    procedureCode: Optional[str] = None
    name: str
    dosage: str
    quantity: int
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
    submittedAt: datetime


class OrderSummary(BaseModel):
    id: str
    intakeId: str
    enrolleeFullName: str
    diagnosis: str
    status: str
    biddingEndsAt: datetime
    createdAt: datetime
    bidCount: int


class OrderDetail(BaseModel):
    id: str
    intakeId: str
    enrollee: Enrollee
    provider: Optional[Provider] = None
    medications: List[Medication]
    biddingEndsAt: datetime
    status: str
    winnerId: Optional[str] = None
    winnerName: Optional[str] = None
    winnerTotalPrice: Optional[float] = None
    collectionCode: Optional[str] = None
    approvalCode: Optional[str] = None
    createdAt: datetime
    createdBy: str
    bids: List[BidOut] = []


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
    unitPrice: float
    totalPrice: float


# ---------------------------------------------------------------------------
# Aggregator dashboard
# ---------------------------------------------------------------------------

class AggregatorDashboardResponse(BaseModel):
    openSessions: list
    wonOrders: list
    completedOrders: list


# ---------------------------------------------------------------------------
# Collection verification
# ---------------------------------------------------------------------------

class VerifyCollectionRequest(BaseModel):
    code: str


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

class ApprovalResponse(BaseModel):
    success: bool
    approvalCode: str
