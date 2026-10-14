"""Versioned offline catalog envelope. No client public key or trust flag."""
from typing import Literal
from pydantic import ConfigDict, Field, model_validator
from models.pa_preparation import StrictModel, CatalogEntry

MAX_CATALOG_ENTRIES = 100000
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class SignedManifest(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    artifactSchemaVersion: Literal[1]
    source: Literal['MEDICLOUD_PROCEDUREDATA']
    authorityId: str = Field(min_length=1, max_length=200)
    sqlServerIdentity: str = Field(min_length=1, max_length=200)
    sqlDatabaseIdentity: str = Field(min_length=1, max_length=200)
    sourceTable: Literal['dbo.proceduredata']
    sourceColumns: Literal['procedurecode,proceduredesc']
    sourceExtractedAt: str = Field(min_length=24, max_length=24)
    contentHash: str = Field(pattern=r'^[a-f0-9]{64}$')
    recordCount: int = Field(gt=0, le=MAX_CATALOG_ENTRIES)
    expectedRowCount: int = Field(gt=0, le=MAX_CATALOG_ENTRIES)
    finalRowCount: int = Field(gt=0, le=MAX_CATALOG_ENTRIES)
    extractionComplete: Literal[True]
    consistencyMethod: Literal['HOLDLOCK_TABLE_READ_TRANSACTION']
    signingKeyFingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='before')
    @classmethod
    def exact_scalar_types(cls, value):
        if not isinstance(value, dict): raise ValueError('Manifest object required')
        for key in ('artifactSchemaVersion', 'recordCount', 'expectedRowCount', 'finalRowCount'):
            if type(value.get(key)) is not int: raise ValueError('Exact integer required')
        if value.get('extractionComplete') is not True: raise ValueError('Explicit completeness required')
        return value



class CatalogArtifact(StrictModel):
    manifest: SignedManifest
    signature: str = Field(min_length=88, max_length=88)
    entries: list[CatalogEntry] = Field(min_length=1, max_length=MAX_CATALOG_ENTRIES)
