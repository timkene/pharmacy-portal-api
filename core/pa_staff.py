"""Current staff permissions; sessions and client claims never grant PA capabilities."""
PA_PERMISSIONS = frozenset({
    'PA_PREPARATION_READ', 'PA_PREPARATION_WRITE', 'PA_MAPPING_EXPORT', 'PA_MAPPING_IMPORT',
    'PA_MAPPING_REVIEW', 'PA_MAPPING_APPROVE', 'PA_MAPPING_ACTIVATE',
    'PA_CATALOG_READ', 'PA_CATALOG_INGEST', 'PA_CATALOG_VALIDATE', 'PA_CATALOG_TRUST',
})


def staff_enabled(record):
    # Legacy records omit these flags. When present they must be explicit booleans.
    return (isinstance(record, dict) and record.get('_id') is not None
        and ('disabled' not in record or record['disabled'] is False)
        and ('isActive' not in record or record['isActive'] is True))


def pa_permissions(record):
    values = record.get('permissions') if staff_enabled(record) else None
    return sorted({value for value in values if isinstance(value, str) and value in PA_PERMISSIONS}) if isinstance(values, list) else []
