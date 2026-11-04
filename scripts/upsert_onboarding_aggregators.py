"""Idempotently provision the five approved aggregator onboarding accounts.

This is intentionally a manual script; it is never called during API startup.
"""
import argparse
import os
import re
from datetime import datetime, timezone

from pymongo import MongoClient

from core.security import hash_password


ACCOUNTS = [
    ("Benson Pharmacy", "benson pharmacy", "22342"),
    ("Advantage Health Africa", "advantage health africa", "5770"),
    ("Famasi Africa", "famasi africa", "5505"),
    ("One Health", "one health", "21483"),
    ("Medcourt Pharmacy", "medcourt pharmacy", "5361"),
]


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _find_existing(collection, company: str, username: str):
    normalized = normalize(username)
    by_username = collection.find_one({"usernameNormalized": normalized})
    by_company = collection.find_one({"companyName": {"$regex": f"^{re.escape(company)}$", "$options": "i"}})
    if by_username and by_company and by_username.get("_id") != by_company.get("_id"):
        raise RuntimeError(
            f"Refusing {company}: username and company match different existing accounts"
        )
    return by_username or by_company


def run(dry_run: bool = False, reset: bool = False) -> None:
    temporary = os.getenv("AGGREGATOR_TEMP_PASSWORD")
    if not temporary:
        raise SystemExit("AGGREGATOR_TEMP_PASSWORD is required")
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    collection = client[os.getenv("DB_NAME", "pharmacy_dispatch")].aggregator_users
    now = datetime.now(timezone.utc)
    try:
        resolved = []
        for company, username, provider_id in ACCOUNTS:
            normalized = normalize(username)
            existing = _find_existing(collection, company, username)
            resolved.append((company, username, provider_id, normalized, existing))

        for company, username, provider_id, normalized, existing in resolved:
            set_fields = {
                "companyName": company, "username": username, "usernameNormalized": normalized,
                "medicloudProviderId": provider_id, "providerId": provider_id,
            }
            if not existing:
                set_fields.update({"password_hash": hash_password(temporary), "mustChangePassword": True,
                                   "emailVerified": False, "created_at": now})
            elif reset:
                set_fields.update({"password_hash": hash_password(temporary), "mustChangePassword": True})
            if dry_run:
                print(f"would {'update' if existing else 'create'} {company}")
            elif existing:
                collection.update_one({"_id": existing["_id"]}, {"$set": set_fields})
            else:
                collection.insert_one(set_fields)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset", action="store_true", help="Reset existing onboarding passwords")
    args = parser.parse_args()
    run(args.dry_run, args.reset)
