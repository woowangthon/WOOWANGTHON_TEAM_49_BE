"""Unchanged behavior tests retained from the copied backend suite.

The archived files remain explicitly runnable. Tests for the retired internal
HTTP/mock pipeline are superseded by real-agent integration tests, not skipped
and counted as passing. See docs/VERIFICATION.md for the original run results.
"""
from tests.backend.legacy_contracts.original_ai import test_invalid_known_field_types_rejected
from tests.backend.legacy_contracts.original_e2e import (
    test_auth_refresh_logout_account_deletion,
    test_concurrent_allocations_do_not_exceed_transaction,
    test_consultation_nationwide_fallback,
    test_cors_and_openapi,
    test_invalid_uploads_and_values,
    test_ownership_scope_and_deletion,
)
from tests.backend.legacy_contracts.original_recovery import (
    test_deletion_status_uses_limited_receipt_cookie,
    test_existing_database_migration_preserves_relations_and_historical_ids,
    test_result_reads_use_consistent_database_snapshot,
)

__all__ = [
    'test_invalid_known_field_types_rejected',
    'test_auth_refresh_logout_account_deletion',
    'test_concurrent_allocations_do_not_exceed_transaction',
    'test_consultation_nationwide_fallback',
    'test_cors_and_openapi',
    'test_invalid_uploads_and_values',
    'test_ownership_scope_and_deletion',
    'test_deletion_status_uses_limited_receipt_cookie',
    'test_existing_database_migration_preserves_relations_and_historical_ids',
    'test_result_reads_use_consistent_database_snapshot',
]
