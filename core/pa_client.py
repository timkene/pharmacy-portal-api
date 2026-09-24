"""One legacy IssuePa request per procedure. Only this server reads PA credentials."""
import os
import re
from datetime import date

import httpx


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
    username, password = _credentials()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{_base()}/member", params={"hmonumber": enrollee_id},
                                    headers={"username": username, "password": password})
        response.raise_for_status()
        data = response.json()
    member = data[0] if isinstance(data, list) and data else data
    if not isinstance(member, dict):
        raise ValueError("Member lookup returned invalid data")
    result = {
        "group_id": str(member.get("GroupId") or member.get("groupId") or ""),
        "division_id": str(member.get("DivisionID") or member.get("divisionId") or ""),
        "dependant_number": str(member.get("DependantNumber") or member.get("dependantNumber") or "0"),
    }
    if not result["group_id"] or not result["division_id"]:
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
