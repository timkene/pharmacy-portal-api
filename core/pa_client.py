"""One legacy IssuePa request per procedure. Only this server reads PA credentials."""
import logging
import os
import re
from datetime import date
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

_GROUP_ID_ALIASES = ("groupID", "GroupId", "groupId", "group_id")
_DIVISION_ID_ALIASES = ("divisionID", "DivisionID", "divisionId", "division_id")
_DEPENDANT_NUMBER_ALIASES = (
    "dependentNumber", "DependantNumber", "dependantNumber", "dependant_number",
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


def _base():
    base = os.getenv("MEDICLOUD_LEGACY_URL", "").strip().rstrip("/")
    if not base or not (base.startswith("https://") or base.startswith("http://localhost:") or base.startswith("http://127.0.0.1:")):
        raise RuntimeError("PA intermediary URL is not configured")
    return base


def _credentials():
    username = os.getenv("MEDICLOUD_LEGACY_USER", os.getenv("CLEARLINE_APP_USER", ""))
    password = os.getenv("MEDICLOUD_LEGACY_PASS", os.getenv("CLEARLINE_APP_PASS", ""))
    if not username or not password:
        raise RuntimeError("PA service credentials are not configured")
    return username, password


async def get_member_info(enrollee_id: str):
    try:
        base = _base()
        credentials = _credentials()
    except RuntimeError as exc:
        _member_failure("configuration", exc=exc)
        raise
    except Exception as exc:
        _member_failure("unexpected", exc=exc)
        raise
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{base}/member", params={"hmonumber": enrollee_id},
                                        headers={"username": credentials[0], "password": credentials[1]})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _member_failure("http_status", base=base, exc=exc,
                        upstream_status=exc.response.status_code)
        raise
    except httpx.TimeoutException as exc:
        _member_failure("transport", base=base, exc=exc, httpx_classification="timeout")
        raise
    except httpx.TransportError as exc:
        _member_failure("transport", base=base, exc=exc, httpx_classification="transport")
        raise
    except Exception as exc:
        _member_failure("unexpected", base=base, exc=exc)
        raise
    try:
        data = response.json()
    except ValueError as exc:
        _member_failure("json_decode", base=base, exc=exc)
        raise
    if isinstance(data, list) and not data:
        _member_failure("empty_list", base=base, json_top_level_type="list")
        raise ValueError("Member lookup returned invalid data")
    member = data[0] if isinstance(data, list) and data else data
    if not isinstance(member, dict):
        _member_failure("unexpected_response_type", base=base,
                        json_top_level_type=type(data).__name__)
        raise ValueError("Member lookup returned invalid data")
    group_id = _first_member_value(member, _GROUP_ID_ALIASES)
    division_id = _first_member_value(member, _DIVISION_ID_ALIASES)
    dependant_number = _first_member_value(member, _DEPENDANT_NUMBER_ALIASES, "0")
    result = {
        "group_id": str(group_id),
        "division_id": str(division_id),
        "dependant_number": str(dependant_number),
    }
    group_present = bool(result["group_id"])
    division_present = bool(result["division_id"])
    if not result["group_id"] or not result["division_id"]:
        _member_failure("missing_required_fields", base=base,
                        group_id_present=group_present, division_id_present=division_present)
        raise ValueError("Member lookup did not return required PA fields")
    return result


def build_payload(enrollee_id, member, procedure_code, diagnosis_code, quantity, amount, provider_id,
                  service_date: date):
    """Preserve the field names and defaults of Klaire's working CDR pa_utils.generate_pa."""
    today = service_date.isoformat()
    return {
        "GroupId": member["group_id"], "IID": enrollee_id, "DiagnosisCode": diagnosis_code,
        "IsNewBorn": False, "UserID": "admin", "ProviderTIN": provider_id,
        "ProcedureCode": procedure_code, "InsureID": enrollee_id, "Referral": "none",
        "DivisionID": member["division_id"], "IsNHIS": False, "ProviderId": provider_id,
        "Quantity": quantity, "AmountRequested": amount,
        "DependantNumber": member["dependant_number"], "ServiceType": "Out-Patient",
        "IsNewborn": False, "CompletionDate": today, "Comments": "", "UserId": "admin",
        "OtherProv": "", "ProvReason": "", "PAStatusDate": today,
        "PAStatus": "AUTHORIZED", "NoAmount": "0", "AdmissionCode": "0",
        "Proceduredate": today, "bookingreference": "", "Isbooked": False,
        "isExclusion": False, "AdditionalServices": [],
    }


async def issue_pa(payload):
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{_base()}/IssuePa", json=payload, auth=_credentials())
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("IssuePa returned an invalid response")
    number = (data.get("PANumber") or data.get("PaNumber") or data.get("panumber")
              or data.get("pa_number") or data.get("AdmissionCode") or data.get("admissionCode")
              or str(data.get("ID") or data.get("Id") or ""))
    if not isinstance(number, (str, int)) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]{2,63}", str(number).strip()) or str(number).strip() == "0":
        raise ValueError("IssuePa returned no PA reference")
    return str(number).strip()
