"""Controlled one-record provider mapping. Never run implicitly at startup."""
import argparse
import os

from pymongo import MongoClient
from bson import ObjectId


COMPANY = "Test pharmacy 1"
PROVIDER_ID = "4304"


def update_record(collection, aggregator_id, apply=False):
    if not ObjectId.is_valid(aggregator_id):
        raise ValueError("A valid --aggregator-id ObjectId is required")
    matches = list(collection.find({"_id": ObjectId(aggregator_id)}, {"providerId": 1, "companyName": 1}).limit(2))
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one aggregator with the specified ID")
    record = matches[0]
    if record.get("companyName") != COMPANY:
        raise RuntimeError("Aggregator companyName does not match Test pharmacy 1")
    current = record.get("providerId")
    if current not in (None, "", PROVIDER_ID):
        raise RuntimeError("Aggregator has a different providerId; manual review required")
    if apply and current != PROVIDER_ID:
        result = collection.update_one({"_id": record["_id"], "providerId": current},
                                       {"$set": {"providerId": PROVIDER_ID}})
        if result.matched_count != 1:
            raise RuntimeError("Aggregator changed during update; no mapping confirmed")
    return {"aggregatorId": str(record["_id"]), "currentProviderId": current, "providerId": PROVIDER_ID,
            "changed": bool(apply and current != PROVIDER_ID)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Map Test pharmacy 1 to providerId 4304")
    parser.add_argument("--apply", action="store_true", help="Actually update the one matched record")
    parser.add_argument("--db-name", required=True, help="Explicit target database name")
    parser.add_argument("--aggregator-id", required=True, help="Verified Test pharmacy 1 ObjectId")
    args = parser.parse_args()
    uri = os.getenv("MONGO_URI")
    if not uri:
        parser.error("MONGO_URI is required")
    with MongoClient(uri, serverSelectionTimeoutMS=5000) as client:
        result = update_record(client[args.db_name].aggregator_users, args.aggregator_id, apply=args.apply)
    print(result)
