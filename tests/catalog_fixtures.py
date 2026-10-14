"""Ephemeral TEST ONLY signing authority; never imported by production code."""
import base64
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from core.pa_catalog_provenance import canonical_json, catalog_hash, key_fingerprint

TEST_KEY = Ed25519PrivateKey.generate()


def configure(monkeypatch, tmp_path):
    path = tmp_path / 'test-only-catalog-public.pem'
    path.write_bytes(TEST_KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv('PA_CATALOG_PUBLIC_KEY_PATH', str(path))
    monkeypatch.setenv('PA_CATALOG_AUTHORITY_ID', 'TEST-MEDICLOUD')
    monkeypatch.setenv('PA_CATALOG_SQL_SERVER_IDENTITY', 'test-sql')
    monkeypatch.setenv('PA_CATALOG_SQL_DATABASE_IDENTITY', 'test-db')


def signed_artifact(entries=None, extracted=None, key=TEST_KEY, **changes):
    entries = entries if entries is not None else [{'procedureCode': 'DRG-CANON', 'procedureDescription': 'Canonical medication A'}]
    entries = sorted(entries, key=lambda row: (row['procedureCode'], row['procedureDescription']))
    extracted = extracted or datetime.now(timezone.utc) - timedelta(minutes=1)
    extracted = extracted.replace(microsecond=(extracted.microsecond // 1000) * 1000)
    manifest = {'artifactSchemaVersion': 1, 'source': 'MEDICLOUD_PROCEDUREDATA', 'authorityId': 'TEST-MEDICLOUD',
        'sqlServerIdentity': 'test-sql', 'sqlDatabaseIdentity': 'test-db', 'sourceTable': 'dbo.proceduredata',
        'sourceColumns': 'procedurecode,proceduredesc', 'sourceExtractedAt': extracted.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
        'contentHash': catalog_hash(entries), 'recordCount': len(entries), 'expectedRowCount': len(entries), 'finalRowCount': len(entries),
        'extractionComplete': True, 'consistencyMethod': 'HOLDLOCK_TABLE_READ_TRANSACTION', 'signingKeyFingerprint': key_fingerprint(key.public_key())}
    manifest.update(changes)
    return {'manifest': manifest, 'signature': base64.b64encode(key.sign(canonical_json(manifest))).decode(), 'entries': entries}
