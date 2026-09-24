"""Procedure-level money and order totals. Legacy total-only orders stay readable."""
from decimal import Decimal, InvalidOperation
from math import isfinite
from uuid import uuid4

from fastapi import HTTPException

MAX_LINE_NAIRA = Decimal("100000000.00")


def money(value, label="Amount") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, f"{label} must be a positive monetary amount")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f"{label} must be a positive monetary amount") from None
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2 or not isfinite(float(amount)):
        raise HTTPException(422, f"{label} must be a positive monetary amount with at most two decimals")
    if amount > MAX_LINE_NAIRA:
        raise HTTPException(422, f"{label} exceeds the maximum allowed amount")
    try:
        normalized = amount.quantize(Decimal("0.01"))
        if Decimal(str(float(normalized))).quantize(Decimal("0.01")) != normalized:
            raise HTTPException(422, f"{label} cannot be stored without losing cents")
        return normalized
    except InvalidOperation:
        raise HTTPException(422, f"{label} is too large") from None


def medication_docs(medications):
    return [{**m, "lineId": str(uuid4())} for m in medications]


def price_lines(medications, submitted):
    if not medications or not all(m.get("lineId") and m.get("procedureCode") for m in medications):
        raise HTTPException(422, "Order lacks medication line IDs or procedure codes")
    if not isinstance(submitted, list) or len(submitted) != len(medications):
        raise HTTPException(422, "Provide one price for every medication")
    by_id = {}
    for row in submitted:
        if not isinstance(row, dict) or not isinstance(row.get("medicationLineId"), str):
            raise HTTPException(422, "Invalid medication price line")
        line_id = row["medicationLineId"]
        if line_id in by_id:
            raise HTTPException(422, "Duplicate medication price line")
        by_id[line_id] = row
    if set(by_id) != {m["lineId"] for m in medications}:
        raise HTTPException(422, "Medication price lines do not match this order")
    result = []
    for med in medications:
        row = by_id[med["lineId"]]
        if row.get("procedureCode") != med["procedureCode"]:
            raise HTTPException(422, "Procedure code does not match this order")
        result.append({"medicationLineId": med["lineId"], "procedureCode": med["procedureCode"],
                       "amount": float(money(row.get("amount"), "Procedure price"))})
    return result


def subtotal(lines):
    return float(sum((Decimal(str(row["amount"])) for row in lines), Decimal("0.00")))


def totals(lines, delivery_fee=None):
    sub = Decimal(str(subtotal(lines)))
    fee = Decimal("0.00") if delivery_fee is None else money(delivery_fee, "Delivery fee")
    return {"medicationSubtotal": float(sub), "overallTotal": float(sub + fee),
            "winnerTotalPrice": float(sub + fee)}
