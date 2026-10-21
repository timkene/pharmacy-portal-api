"""Source code-system namespaces. Assigned server-side only; never from clients."""

# Exact MediCloud procedure code verified against a trusted, unrevoked catalog snapshot.
MEDICLOUD_NAMESPACE = "SYSTEM_MEDICLOUD_PROCEDURE"
# Intake code that could not be verified: eligible for manual mapping review only.
INTAKE_REVIEW_NAMESPACE = "PHARMACY_INTAKE_CODE"
# System namespaces (including SYSTEM_DELIVERY) never enter manual mapping workflows.
RESERVED_NAMESPACE_PREFIX = "SYSTEM_"

VERIFIED_METHOD = "TRUSTED_CATALOG_EXACT_CODE"
UNVERIFIED_METHOD = "UNVERIFIED_INTAKE_CODE"
VERIFIED_MAPPING_SOURCE = "VERIFIED_MEDICLOUD_CATALOG"
SOURCE_FIELDS = ("sourceNamespaceId", "sourceCode", "sourceDescription", "sourceVerification")


def is_reserved_namespace(namespace):
    return isinstance(namespace, str) and namespace.strip().upper().startswith(RESERVED_NAMESPACE_PREFIX)
