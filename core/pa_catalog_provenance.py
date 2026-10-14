"""Offline artifact cryptography and configured authority; no SQL/HTTP capability."""
import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.pa_preparation_store import DomainConflict, now_utc
from models.pa_catalog import CatalogArtifact, MAX_ARTIFACT_BYTES

CATALOG_MAX_AGE = timedelta(hours=24)
_VERIFIED_SNAPSHOTS = ContextVar('request_verified_catalogs', default=None)


@asynccontextmanager
async def catalog_verification_scope():
    if _VERIFIED_SNAPSHOTS.get() is not None:
        yield
        return
    token = _VERIFIED_SNAPSHOTS.set({})
    try:
        yield
    finally:
        _VERIFIED_SNAPSHOTS.reset(token)



@dataclass(frozen=True)
class CatalogAuthority:
    public_key: Ed25519PublicKey
    authority_id: str
    sql_server_identity: str
    sql_database_identity: str


class CatalogConfigurationError(RuntimeError):
    pass


def configured_authority():
    try:
        values = [os.environ[name].strip() for name in ('PA_CATALOG_PUBLIC_KEY_PATH', 'PA_CATALOG_AUTHORITY_ID',
            'PA_CATALOG_SQL_SERVER_IDENTITY', 'PA_CATALOG_SQL_DATABASE_IDENTITY')]
        if not all(values): raise ValueError()
        key = serialization.load_pem_public_key(Path(values[0]).read_bytes())
        if not isinstance(key, Ed25519PublicKey): raise ValueError()
        return CatalogAuthority(key, *values[1:])
    except (OSError, KeyError, ValueError, TypeError, UnsupportedAlgorithm):
        raise CatalogConfigurationError('CATALOG_AUTHORITY_NOT_CONFIGURED') from None


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def key_fingerprint(key):
    return hashlib.sha256(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).hexdigest()


def extracted_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.microsecond % 1000:
            raise ValueError()
        if parsed.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z') != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise DomainConflict('CATALOG_TIMESTAMP_INVALID') from None


def require_fresh(value, now=None):
    now = now or now_utc()
    if value > now: raise DomainConflict('CATALOG_FUTURE_DATED')
    if now - value >= CATALOG_MAX_AGE: raise DomainConflict('CATALOG_EXPIRED')


def catalog_hash(entries):
    pairs = sorted((row['procedureCode'], row['procedureDescription']) for row in entries)
    # Same catalog wire hash as Phase 1; signature additionally binds provenance.
    return hashlib.sha256(json.dumps(pairs, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def validate_entries(entries):
    seen = set()
    for row in entries:
        if set(row) != {'procedureCode', 'procedureDescription'}:
            raise DomainConflict('CATALOG_SCHEMA_INVALID')
        code, description = row['procedureCode'], row['procedureDescription']
        if (not isinstance(code, str) or not code.strip() or code != code.strip() or len(code) > 200
                or not isinstance(description, str) or not description.strip() or len(description) > 1000
                or description.strip().upper() == code.upper()
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in code + description)):
            raise DomainConflict('CATALOG_ENTRY_INVALID')
        normalized = code.upper()
        if normalized in seen: raise DomainConflict('DUPLICATE_NORMALIZED_CODE')
        seen.add(normalized)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError()
        result[key] = value
    return result


def parse_artifact(data):
    if not data or len(data) > MAX_ARTIFACT_BYTES:
        raise DomainConflict('CATALOG_ARTIFACT_SIZE_INVALID')
    try:
        raw = json.loads(data.decode('utf-8'), object_pairs_hook=no_duplicate_keys,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        # Strict strings/integers: do not coerce arbitrary uploaded catalog data.
        return CatalogArtifact.model_validate(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DomainConflict('CATALOG_ARTIFACT_INVALID') from None


def verify_artifact(artifact, authority=None, *, authorization=True):
    authority = authority or configured_authority()
    manifest = artifact.manifest.model_dump()
    if (manifest['authorityId'] != authority.authority_id
            or manifest['sqlServerIdentity'] != authority.sql_server_identity
            or manifest['sqlDatabaseIdentity'] != authority.sql_database_identity
            or manifest['signingKeyFingerprint'] != key_fingerprint(authority.public_key)):
        raise DomainConflict('CATALOG_AUTHORITY_MISMATCH')
    try:
        signature = base64.b64decode(artifact.signature, validate=True)
        authority.public_key.verify(signature, canonical_json(manifest))
    except (InvalidSignature, ValueError, TypeError):
        raise DomainConflict('CATALOG_SIGNATURE_INVALID') from None
    entries = [row.model_dump() for row in artifact.entries]
    validate_entries(entries)
    if entries != sorted(entries, key=lambda row: (row['procedureCode'], row['procedureDescription'])):
        raise DomainConflict('CATALOG_ORDER_INVALID')
    count = len(entries)
    if not count or not (count == manifest['recordCount'] == manifest['expectedRowCount'] == manifest['finalRowCount']):
        raise DomainConflict('CATALOG_INCOMPLETE')
    if catalog_hash(entries) != manifest['contentHash']:
        raise DomainConflict('CATALOG_DIGEST_MISMATCH')
    timestamp = extracted_timestamp(manifest['sourceExtractedAt'])
    if timestamp > now_utc(): raise DomainConflict('CATALOG_FUTURE_DATED')
    if authorization: require_fresh(timestamp)
    return {'manifest': manifest, 'signature': artifact.signature,
        'manifestDigest': hashlib.sha256(canonical_json(manifest)).hexdigest(),
        'signingKeyFingerprint': manifest['signingKeyFingerprint']}, timestamp


async def verify_snapshot(db, snapshot, session=None, *, authorization=True, authority=None, entry_bindings=None):
    # Verify persisted immutable provenance against CURRENT server authority.
    from models.pa_catalog import MAX_CATALOG_ENTRIES
    provenance = snapshot.get('signedProvenance')
    if not isinstance(provenance, dict): raise DomainConflict('SIGNED_CATALOG_PROVENANCE_REQUIRED')
    records = await db.pa_procedure_catalog_entries.find({'snapshotId': snapshot['_id']}, session=session).to_list(MAX_CATALOG_ENTRIES + 1)
    if len(records) > MAX_CATALOG_ENTRIES: raise DomainConflict('CATALOG_INCOMPLETE')
    entries = [{'procedureCode': row['procedureCode'], 'procedureDescription': row['procedureDescription']} for row in records]
    entries.sort(key=lambda row: (row['procedureCode'], row['procedureDescription']))
    try:
        artifact = CatalogArtifact.model_validate({'manifest': provenance['manifest'], 'signature': provenance['signature'], 'entries': entries})
    except (KeyError, ValueError, TypeError):
        raise DomainConflict('CATALOG_STORED_EVIDENCE_INVALID') from None
    verified, timestamp = verify_artifact(artifact, authority, authorization=authorization)
    if verified != provenance: raise DomainConflict('CATALOG_PROVENANCE_MISMATCH')
    manifest = verified['manifest']
    if (snapshot.get('ingestedCount') != len(entries) or snapshot.get('recordCount') != len(entries)
            or snapshot.get('contentHash') != manifest['contentHash'] or snapshot.get('source') != manifest['source']
            or snapshot.get('schemaVersion') != manifest['artifactSchemaVersion'] or snapshot.get('sourceExtractedAt') != timestamp
            or any(row.get('normalizedCode') != row['procedureCode'].upper() for row in records)):
        raise DomainConflict('CATALOG_STORED_EVIDENCE_INVALID')
    if entry_bindings is not None:
        entry_bindings.update({row['normalizedCode']: (row['procedureCode'], row['procedureDescription']) for row in records})
    return verified


async def verified_lookup_context(db, snapshot, validation, session, *, authorization):
    authority = configured_authority()  # Re-read current key/source policy each use.
    def timestamp(value):
        if isinstance(value, datetime): return value.isoformat()
        raise TypeError('Invalid stored evidence')
    # Header/evidence mutation, trust/revocation changes and key rotation cannot reuse verification.
    binding = hashlib.sha256(json.dumps({'snapshot': snapshot, 'validation': validation,
        'authority': (key_fingerprint(authority.public_key), authority.authority_id,
                      authority.sql_server_identity, authority.sql_database_identity)},
        sort_keys=True, separators=(',', ':'), default=timestamp).encode()).hexdigest()
    key = (id(db), id(session), binding)
    cache = _VERIFIED_SNAPSHOTS.get()
    cached = cache.get(key) if cache is not None else None
    if cached is None:
        entries = {}
        verified = await verify_snapshot(db, snapshot, session, authorization=False,
                                         authority=authority, entry_bindings=entries)
        # Hold strong references to avoid object-id reuse inside a long request.
        cached = {'provenance': verified, 'entries': entries, 'db': db, 'session': session}
        if cache is not None: cache[key] = cached
    timestamp_value = extracted_timestamp(cached['provenance']['manifest']['sourceExtractedAt'])
    if timestamp_value > now_utc(): raise DomainConflict('CATALOG_FUTURE_DATED')
    if authorization: require_fresh(timestamp_value)
    return cached
