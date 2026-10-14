"""Authorized offline workstation command; never runs on backend startup.

Run as: python -m scripts.extract_pa_catalog --secrets-file PATH
        --private-key-file OUTSIDE_REPOSITORY --output-file PATH
No key generation, SQL writes, member access or credential output.
"""
import argparse
import base64
import json
import os
import subprocess
from pathlib import Path
import tomllib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from core.pa_catalog_provenance import (canonical_json, catalog_hash, configured_authority, key_fingerprint,
    validate_entries, verify_artifact)
from core.pa_preparation_store import DomainConflict, now_utc
from models.pa_catalog import CatalogArtifact, MAX_ARTIFACT_BYTES, MAX_CATALOG_ENTRIES

IDENTITY_SQL = "SELECT CAST(SERVERPROPERTY('ServerName') AS nvarchar(128)), DB_NAME(), OBJECT_ID(N'dbo.proceduredata', N'U')"
COUNT_SQL = 'SELECT COUNT_BIG(*) FROM dbo.proceduredata WITH (HOLDLOCK, TABLOCK)'
CATALOG_SQL = 'SELECT procedurecode, proceduredesc FROM dbo.proceduredata WITH (HOLDLOCK, TABLOCK) ORDER BY procedurecode, proceduredesc'


def extract_catalog(connection, authority, signing_key):
    if not isinstance(signing_key, Ed25519PrivateKey) or key_fingerprint(signing_key.public_key()) != key_fingerprint(authority.public_key):
        raise DomainConflict('EXTRACTOR_SIGNING_AUTHORITY_MISMATCH')
    if connection.autocommit is not False: raise DomainConflict('EXTRACTOR_READ_TRANSACTION_REQUIRED')
    cursor = connection.cursor()
    try:
        identity = cursor.execute(IDENTITY_SQL).fetchone()
        if (not identity or identity[0] != authority.sql_server_identity or identity[1] != authority.sql_database_identity or not identity[2]):
            raise DomainConflict('EXTRACTOR_SOURCE_IDENTITY_UNVERIFIED')
        expected = cursor.execute(COUNT_SQL).fetchone()[0]
        if type(expected) is not int or not 0 < expected <= MAX_CATALOG_ENTRIES:
            raise DomainConflict('EXTRACTOR_CATALOG_COUNT_INVALID')
        cursor.execute(CATALOG_SQL)
        entries = []
        while True:
            chunk = cursor.fetchmany(1000)
            if not chunk: break
            for row in chunk:
                if len(row) != 2: raise DomainConflict('EXTRACTOR_SCHEMA_INVALID')
                entries.append({'procedureCode': row[0], 'procedureDescription': row[1]})
            if len(entries) > MAX_CATALOG_ENTRIES: raise DomainConflict('EXTRACTOR_CATALOG_TOO_LARGE')
        final = cursor.execute(COUNT_SQL).fetchone()[0]
        if not (len(entries) == expected == final): raise DomainConflict('EXTRACTOR_INCOMPLETE')
        validate_entries(entries)
        entries.sort(key=lambda row: (row['procedureCode'], row['procedureDescription']))
        extracted = now_utc()
        extracted = extracted.replace(microsecond=(extracted.microsecond // 1000) * 1000)
        manifest = {'artifactSchemaVersion': 1, 'source': 'MEDICLOUD_PROCEDUREDATA', 'authorityId': authority.authority_id,
            'sqlServerIdentity': identity[0], 'sqlDatabaseIdentity': identity[1], 'sourceTable': 'dbo.proceduredata',
            'sourceColumns': 'procedurecode,proceduredesc', 'sourceExtractedAt': extracted.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'contentHash': catalog_hash(entries), 'recordCount': len(entries), 'expectedRowCount': expected, 'finalRowCount': final,
            'extractionComplete': True, 'consistencyMethod': 'HOLDLOCK_TABLE_READ_TRANSACTION',
            'signingKeyFingerprint': key_fingerprint(signing_key.public_key())}
        artifact = CatalogArtifact.model_validate({'manifest': manifest,
            'signature': base64.b64encode(signing_key.sign(canonical_json(manifest))).decode('ascii'), 'entries': entries})
        verify_artifact(artifact, authority)
        data = canonical_json(artifact.model_dump())
        if len(data) > MAX_ARTIFACT_BYTES: raise DomainConflict('EXTRACTOR_ARTIFACT_TOO_LARGE')
        return data
    finally:
        # Release shared table locks. There are no data-changing statements to commit.
        connection.rollback()
        cursor.close()


def odbc_value(value):
    return '{' + str(value).replace('}', '}}') + '}'


def require_external_private_key(key_path, root):
    # Account for the primary checkout and all linked worktrees of this repo.
    common = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        text=True, stderr=subprocess.DEVNULL).strip()
    worktrees = subprocess.check_output(['git', '-C', str(root), 'worktree', 'list', '--porcelain'],
        text=True, stderr=subprocess.DEVNULL)
    roots = [root, Path(common).resolve().parent]
    roots.extend(Path(line[9:]).resolve() for line in worktrees.splitlines() if line.startswith('worktree '))
    if any(key_path.is_relative_to(repository) for repository in roots):
        raise ValueError('Private signing key must remain outside repository')


def main():
    parser = argparse.ArgumentParser(description='Controlled read-only MediCloud procedure catalog extraction')
    parser.add_argument('--secrets-file', type=Path, required=True)
    parser.add_argument('--private-key-file', type=Path, required=True)
    parser.add_argument('--output-file', type=Path, required=True)
    args = parser.parse_args()
    connection = None
    try:
        root = Path(__file__).resolve().parents[1]
        key_path = args.private_key_file.resolve()
        require_external_private_key(key_path, root)
        if args.output_file.resolve() == key_path:
            raise ValueError('Private signing key must remain outside artifact')
        authority = configured_authority()
        # Existing private key only; optional passphrase is never echoed or persisted.
        passphrase = os.environ.get('PA_CATALOG_SIGNING_KEY_PASSPHRASE')
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=passphrase.encode() if passphrase else None)
        with args.secrets_file.open('rb') as handle:
            credentials = tomllib.load(handle)['credentials']
        required = ('server', 'database', 'username', 'password', 'port')
        if any(not str(credentials.get(name, '')).strip() for name in required): raise ValueError()
        import pyodbc  # Offline workstation dependency only; no import-time connection.
        server = f"{credentials['server']},{int(credentials['port'])}"
        connection_string = ('DRIVER={ODBC Driver 18 for SQL Server};Encrypt=yes;TrustServerCertificate=no;'
            + 'SERVER=' + odbc_value(server) + ';DATABASE=' + odbc_value(credentials['database'])
            + ';UID=' + odbc_value(credentials['username']) + ';PWD=' + odbc_value(credentials['password']) + ';')
        connection = pyodbc.connect(connection_string, autocommit=False, timeout=15)
        artifact = extract_catalog(connection, authority, key)
        # Never overwrite existing files; the artifact contains public provenance/catalog only.
        with args.output_file.open('xb') as handle: handle.write(artifact)
        print('Catalog artifact created; no secrets included.')
    except Exception:
        raise SystemExit('Catalog extraction failed; no artifact authorized.') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                raise SystemExit("Catalog connection cleanup failed.") from None


if __name__ == '__main__':
    main()
