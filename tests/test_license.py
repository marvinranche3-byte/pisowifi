"""
Property-based tests for the PotsWorks PisoWiFi License Key System.

Covers:
  - Property 9: License Key Validation Round Trip  (Validates: Requirements 22.6)
"""

import os
import sys

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

_TESTS_DIR = os.path.dirname(__file__)
_BACKEND_PATH = os.path.abspath(
    os.path.join(_TESTS_DIR, '..', 'overlay', 'opt', 'pisowifi', 'backend')
)
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)


# ===========================================================================
# Property 9: License Key Validation Round Trip
# Validates: Requirements 22.6
# ===========================================================================

@given(st.from_regex(r'OPI-[0-9A-F]{12}', fullmatch=True))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_license_key_validation_round_trip(hardware_id):
    """
    **Validates: Requirements 22.6**

    Property 9: For any hardware ID, generating a license key and then
    validating it must always return True.
    """
    from license import generate_license_key, validate_license_key

    # Generate a key for this hardware ID
    key = generate_license_key(hardware_id)

    # Validate it — must always succeed
    result = validate_license_key(key, hardware_id)
    assert result is True, (
        f"validate_license_key('{key}', '{hardware_id}') returned False, "
        f"expected True"
    )


@given(st.from_regex(r'OPI-[0-9A-F]{12}', fullmatch=True))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_wrong_key_fails_validation(hardware_id):
    """
    A key generated for one hardware ID must NOT validate for a different hardware ID.
    """
    from license import generate_license_key, validate_license_key

    key = generate_license_key(hardware_id)

    # Modify the hardware ID slightly — should fail
    wrong_id = 'OPI-' + 'FF' * 6  # All-FF MAC
    if wrong_id == hardware_id:
        wrong_id = 'OPI-' + '00' * 6  # All-00 MAC

    result = validate_license_key(key, wrong_id)
    assert result is False, (
        f"Key for '{hardware_id}' should NOT validate for '{wrong_id}'"
    )


@given(st.from_regex(r'OPI-[0-9A-F]{12}', fullmatch=True))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_key_format_is_correct(hardware_id):
    """License key must be in XXXXX-XXXXX-XXXXX-XXXXX format (20 hex chars, 4 groups of 5)."""
    from license import generate_license_key

    key = generate_license_key(hardware_id)
    parts = key.split('-')

    assert len(parts) == 4, f"Key should have 4 parts, got {len(parts)}: {key}"
    for part in parts:
        assert len(part) == 5, f"Each part should be 5 chars, got '{part}' in {key}"
        assert all(c in '0123456789ABCDEF' for c in part), (
            f"Key part '{part}' contains non-hex characters"
        )
