"""Normalized enrollee lookup and one conservative IssuePa submission per PA group."""
import logging
import os
import re
from datetime import date
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

_DEPENDANT_NUMBER_ALIASES = (
    "dependantnumber", "dependentNumber", "DependantNumber", "dependantNumber", "dependant_number",
)


def _first_member_value(member, aliases, default=""):
    """Return the first non-empty alias in the declared precedence order."""
    for alias in aliases:
        value = member.get(alias)
        if value is not None and value != "":
            return value
    return default


def _member_failure(stage, *, base=None, exc=None, **details):
    safe = {"stage": stage, **details}
    if base:
        parsed = urlsplit(base)
        safe["upstream_host"] = parsed.hostname or "unknown"
        safe["upstream_path"] = f"{parsed.path.rstrip('/')}/member" or "/member"
    if exc:
        safe["exception_class"] = type(exc).__name__
    logger.warning("PA member lookup failed %s", " ".join(f"{key}={value}" for key, value in safe.items()))


# Office-confirmed IssuePa host. Override with MEDICLOUD_LEGACY_URL for tests.
_ISSUE_PA_BASE = "https://medicloud.dev/interm"


def _base():
    base = os.getenv("MEDICLOUD_LEGACY_URL", _ISSUE_PA_BASE).strip().rstrip("/")
    if not base or not (base.startswith("https://") or base.startswith("http://localhost:") or base.startswith("http://127.0.0.1:")):
        raise RuntimeError("PA intermediary URL is not configured")
    return base


def _credentials():
    username = os.getenv("MEDICLOUD_LEGACY_USER", os.getenv("CLEARLINE_APP_USER", ""))
    password = os.getenv("MEDICLOUD_LEGACY_PASS", os.getenv("CLEARLINE_APP_PASS", ""))
    if not username or not password:
        raise RuntimeError("PA service credentials are not configured")
    return username, password


def member_flag(value):
    if value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true"}):
        return True
    if value is False or value == 0 or (isinstance(value, str) and value.strip().lower() in {"0", "false"}):
        return False
    return None


def dependant_number(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    return str(int(text)) if re.fullmatch(r"[0-9]+", text) else None


async def get_member_info(enrollee_id: str):
    """Consume the normalized lookup, never infer a legacy ID from raw IDs."""
    base = os.getenv("MEDICLOUD_API_URL", "").strip().rstrip("/")
    key = os.getenv("MEDICLOUD_API_KEY", "")
    if not base.startswith("https://") or not key:
        _member_failure("configuration")
        raise RuntimeError("Normalized enrollee lookup URL/API key is not configured")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{base}/enrollees/lookup",
                                        params={"enrollee_id": enrollee_id},
                                        headers={"X-API-Key": key})
            response.raise_for_status()
            data = response.json()
        rows = data.get("results") if isinstance(data, dict) else None
        if (not isinstance(rows, list) or len(rows) != 1 or data.get("truncated")
                or data.get("found") is not True or data.get("count") != 1
                or not isinstance(rows[0], dict)):
            raise ValueError("Lookup must return exactly one unambiguous member")
        member = rows[0]
        if str(member.get("legacycode", "")).strip().casefold() != enrollee_id.strip().casefold():
            raise ValueError("Lookup member identity does not match")
        legacy = member.get("group_legacy_id")
        if not isinstance(legacy, str) or not legacy.strip():
            raise ValueError("Member Group LegacyID is missing; update normalized enrollee lookup")
        if member_flag(member.get("isterminated")) is not False or member_flag(member.get("isdeleted")) is not False:
            raise ValueError("Member active/deleted status is missing, ambiguous or ineligible")
        member_id = member.get("memberid")
        if isinstance(member_id, bool) or not str(member_id or "").isdigit() or int(member_id) <= 0:
            raise ValueError("Authoritative member ID is missing")
        principal = member_flag(member.get("isprincipal"))
        if principal is None:
            raise ValueError("Authoritative principal/dependant identity is missing or ambiguous")
        raw_id = member.get("dependantid")
        number = dependant_number(raw_id) if raw_id is not None else None
        if raw_id is not None and number is None:
            raise ValueError("Dependant ID is invalid")
        if principal is True:
            if number not in (None, "0"):
                raise ValueError("Principal identity conflicts with dependant ID")
            dependant = "0"
        else:
            if number is None or int(number) <= 0:
                raise ValueError("Dependant requires a positive dependant ID")
            dependant = number
        # Older lookup clients may carry aliases; never use them as a source.
        for alias in _DEPENDANT_NUMBER_ALIASES:
            if alias in member and member[alias] not in (None, ""):
                if dependant_number(member[alias]) != dependant:
                    raise ValueError("Conflicting dependant identity")
        return {"group_legacy_id": legacy.strip(), "dependant_number": dependant, "member_id": str(member_id)}
    except Exception as exc:
        _member_failure("lookup", exc=exc)
        raise


def build_payload(enrollee_id, member, procedure_code, diagnosis_code, quantity, amount, provider_id,
                  service_date: date, additional_lines=None):
    """Build the confirmed single/grouped IssuePa contract without exposing credentials."""
    today = service_date.isoformat()
    legacy = member.get("group_legacy_id")
    if not isinstance(legacy, str) or not legacy.strip():
        raise ValueError("Member Group LegacyID is missing")
    payload = {
        "GroupId": legacy.strip(), "IID": enrollee_id, "DiagnosisCode": diagnosis_code,
        "IsNewBorn": False, "UserID": "admin", "ProviderTIN": provider_id,
        "ProcedureCode": procedure_code, "InsureID": enrollee_id, "Referral": "none",
        "DivisionID": legacy.strip(), "IsNHIS": False, "ProviderId": provider_id,
        "Quantity": quantity, "AmountRequested": amount,
        "DependantNumber": member["dependant_number"], "ServiceType": "Out-Patient",
        "IsNewborn": False, "CompletionDate": today, "Comments": "",
        "UserId": "admin",
        "OtherProv": "", "ProvReason": "", "PAStatusDate": today,
        "PAStatus": "AUTHORIZED", "NoAmount": "0", "AdmissionCode": "0",
        "Proceduredate": today, "bookingreference": "", "Isbooked": False,
        "isExclusion": False,
    }

    if additional_lines:
        payload["AdditionalServices"] = [{
            "ProcedureCode": line["procedureCode"], "DiagnosisCode": line["diagnosisCode"],
            "ProcedureDesc": line.get("description", ""),
            "DiagnosisDesc": line.get("diagnosisDescription", ""),
            "Quantity": line["quantity"], "AmountRequested": line["amount"],
            "AmountGranted": line["amount"],
        } for line in additional_lines]
    return payload


def response_reference(data):
    if not isinstance(data, dict):
        return None
    number = (data.get("PANumber") or data.get("PaNumber") or data.get("panumber")
              or data.get("pa_number") )
    if isinstance(number, (str, int)) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]{2,63}", str(number).strip()) and str(number).strip() != "0":
        return str(number).strip()
    return None


def reconcile_response(data, lines):
    """Recognize only explicit, uniquely mapped service evidence; unknown schemas stay uncertain.

    These keys are defensive adapters, not a claimed MediCloud response contract.
    A reference or an order-level status alone never approves services.
    """
    ref = response_reference(data)
    candidates = []
    if isinstance(data, dict):
        for key in ("Services", "AdditionalServices"):
            if isinstance(data.get(key), list):
                candidates.extend(x for x in data[key] if isinstance(x, dict))
    results = {}
    for line in lines:
        matches = [x for x in candidates if x.get("ProcedureCode") == line["procedureCode"]]
        # Procedure-only evidence cannot disambiguate repeated procedures.
        if sum(x["procedureCode"] == line["procedureCode"] for x in lines) != 1 or len(matches) != 1:
            results[line["lineId"]] = ("verification_required", None)
            continue
        row = matches[0]
        if (("DiagnosisCode" in row and row["DiagnosisCode"] != line["diagnosisCode"])
                or ("Quantity" in row and row["Quantity"] != line["quantity"])
                or ("AmountRequested" in row and row["AmountRequested"] != line["amount"])):
            results[line["lineId"]] = ("verification_required", None)
            continue
        statuses = [str(row[k]).strip().upper() for k in ("Status", "PAStatus") if k in row]
        status = statuses[0] if statuses and len(set(statuses)) == 1 else None
        if "AmountGranted" in row:
            from decimal import Decimal, InvalidOperation
            try:
                granted = Decimal(str(row["AmountGranted"]))
                requested = Decimal(str(line["amount"]))
                if isinstance(row["AmountGranted"], bool) or not granted.is_finite() or granted != requested:
                    results[line["lineId"]] = ("verification_required", None)
                    continue
            except (InvalidOperation, ValueError):
                results[line["lineId"]] = ("verification_required", None)
                continue
        if status in {"ACCEPTED", "APPROVED", "AUTHORIZED"} and ref:
            results[line["lineId"]] = ("generated", status)
        elif status in {"REJECTED", "DENIED", "FAILED"}:
            results[line["lineId"]] = ("failed_rejected", status)
        else:
            results[line["lineId"]] = ("verification_required", None)
    return ref, results


def response_evidence(response):
    """Bounded raw evidence, redacted with configured secrets, kept out of public state/logs."""
    import hashlib
    raw = response.text
    import base64
    try:
        username, password = _credentials()
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        raw = raw.replace(encoded, "<redacted>")
    except RuntimeError:
        pass
    for name in ("MEDICLOUD_LEGACY_USER", "MEDICLOUD_LEGACY_PASS", "CLEARLINE_APP_USER",
                 "CLEARLINE_APP_PASS", "MEDICLOUD_API_KEY", "SESSION_SECRET"):
        secret = os.getenv(name, "")
        if secret:
            raw = raw.replace(secret, "<redacted>")
    return {"httpStatus": response.status_code, "bodySha256": hashlib.sha256(response.content).hexdigest(),
            "body": raw[:8192], "truncated": len(raw) > 8192}


async def issue_pa(payload):
    async with httpx.AsyncClient(timeout=20) as client:
        username, password = _credentials()
        response = await client.post(f"{_base()}/IssuePa", json=payload,
                                     headers={"username": username, "password": password})
        evidence = response_evidence(response)
        try:
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            exc.pa_evidence = evidence
            try:
                exc.pa_reference = response_reference(response.json())
            except ValueError:
                exc.pa_reference = None
            raise
    return {"data": data, "evidence": evidence}
