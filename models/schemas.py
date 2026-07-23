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
    isCheapest: bool = False
    submittedAt: datetime


class OrderSummary(BaseModel):
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


class OrderDetail(BaseModel):
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


class FulfillOrderRequest(BaseModel):
    fulfillmentType: str  # 'delivered' or 'picked_up'
    deliveryFee: Optional[float] = None


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
