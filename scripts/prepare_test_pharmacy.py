"""Prepare the one existing Test Pharmacy sandbox account.

This script is intentionally explicit and never creates accounts or touches orders.
It is not imported or run by the API.
"""
import argparse
import os
import re

from bson import ObjectId
from pymongo import MongoClient

from core.security import hash_password


AGGREGATOR_ID = ObjectId("6aaaefd5cdc74f538ab22345")
ALLOWED_COMPANY_NAMES = {"Test pharmacy 1", "Test Pharmacy"}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def prepare(collection, *, dry_run=False, password=None):
    password = password if password is not None else os.getenv("TEST_PHARMACY_TEMP_PASSWORD")
    if not password:
        raise RuntimeError("TEST_PHARMACY_TEMP_PASSWORD is required")
    record = collection.find_one({"_id": AGGREGATOR_ID})
    if not record:
        raise RuntimeError("Test pharmacy aggregator was not found")
    if record.get("companyName") not in ALLOWED_COMPANY_NAMES:
        raise RuntimeError("Refusing: companyName is not Test pharmacy 1 or Test Pharmacy")

    fields = {
        "companyName": "Test Pharmacy",
        "username": "test pharmacy",
        "usernameNormalized": normalize("test pharmacy"),
        "mustChangePassword": True,
        "isTestAggregator": True,
        "password_hash": hash_password(password),
    }
    update = {"$set": fields, "$unset": {"medicloudProviderId": "", "providerId": ""}}
    if not dry_run:
        result = collection.update_one({"_id": AGGREGATOR_ID}, update)
        if result.matched_count != 1:
            raise RuntimeError("Test pharmacy changed during update")
    return {"aggregatorId": str(AGGREGATOR_ID), "dryRun": dry_run, "updated": not dry_run}


def run(*, dry_run=False):
    uri = os.getenv("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI is required")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[os.getenv("DB_NAME", "pharmacy_dispatch")].aggregator_users
        return prepare(collection, dry_run=dry_run)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare the existing Test Pharmacy sandbox aggregator")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(run(dry_run=args.dry_run))
