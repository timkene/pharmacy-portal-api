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


def run(dry_run: bool = False) -> None:
    temporary = os.getenv("AGGREGATOR_TEMP_PASSWORD")
    if not temporary:
        raise SystemExit("AGGREGATOR_TEMP_PASSWORD is required")
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    collection = client[os.getenv("DB_NAME", "pharmacy_dispatch")].aggregator_users
    now = datetime.now(timezone.utc)
    for company, username, provider_id in ACCOUNTS:
        normalized = normalize(username)
        existing = collection.find_one({"usernameNormalized": normalized})
        if not existing:
            existing = collection.find_one({"companyName": {"$regex": f"^{re.escape(company)}$", "$options": "i"}})
        set_fields = {
            "companyName": company, "username": username, "usernameNormalized": normalized,
            "medicloudProviderId": provider_id, "providerId": provider_id,
        }
        if not existing:
            set_fields.update({"password_hash": hash_password(temporary), "mustChangePassword": True,
                               "emailVerified": False, "created_at": now})
        elif existing.get("mustChangePassword") or existing.get("emailVerified") is not True:
            set_fields.update({"password_hash": hash_password(temporary), "mustChangePassword": True,
                               "emailVerified": bool(existing.get("emailVerified", False))})
        if dry_run:
            print(f"would {'update' if existing else 'create'} {company}")
        else:
            if existing:
                collection.update_one({"_id": existing["_id"]}, {"$set": set_fields})
            else:
                collection.insert_one(set_fields)
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    run(parser.parse_args().dry_run)
