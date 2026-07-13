"""Fail-closed pins for the sovereign scribe encounter-identity primitive.

``compute_encounter_id`` (scribe/identity.py) is THE PHI-safety chokepoint: a
low-entropy patient label salted with a weak/absent/UNRESOLVED salt yields a
dictionary-reversible id. These pins hold the guard closed — especially the
#67 F2 compensating control (an UNRESOLVED ``${SCRIBE_ENCOUNTER_SALT}`` placeholder
must be rejected, because the STAY-C unit's EnvironmentFile is now OPTIONAL and a
missing env-file leaves the salt as that literal).
"""

from __future__ import annotations

import pytest

from alfred.scribe.identity import (
    ENCOUNTER_ID_PREFIX,
    EncounterIdentityError,
    compute_encounter_id,
)

_SALT = "a-real-per-instance-secret-salt-value"


# ---------------------------------------------------------------------------
# Happy path + stability
# ---------------------------------------------------------------------------


def test_returns_opaque_prefixed_id_not_the_label() -> None:
    eid = compute_encounter_id("patient-jane-doe", salt=_SALT)
    assert eid.startswith(ENCOUNTER_ID_PREFIX)
    assert "patient-jane-doe" not in eid  # the label never appears in the id


def test_deterministic_and_salt_sensitive() -> None:
    a = compute_encounter_id("enc-label", salt=_SALT)
    assert a == compute_encounter_id("enc-label", salt=_SALT)  # stable per encounter
    assert a != compute_encounter_id("enc-label", salt="DIFFERENT_SALT")


def test_hmac_consumes_the_original_unstripped_salt() -> None:
    """Regression guard: the guard checks the STRIPPED salt but the HMAC must
    still consume the ORIGINAL salt — otherwise switching to the stripped value
    would SHIFT every existing encounter_id. A trailing-space salt must therefore
    produce a DIFFERENT id than its stripped form."""
    assert compute_encounter_id("x", salt=_SALT + " ") != compute_encounter_id("x", salt=_SALT)


# ---------------------------------------------------------------------------
# Fail-closed: empty / whitespace salt (existing behaviour — kept green)
# ---------------------------------------------------------------------------


def test_empty_salt_fails_loud() -> None:
    with pytest.raises(EncounterIdentityError, match="empty"):
        compute_encounter_id("label", salt="")


def test_whitespace_only_salt_fails_loud() -> None:
    with pytest.raises(EncounterIdentityError, match="empty"):
        compute_encounter_id("label", salt="   \t ")


def test_non_str_salt_fails_loud() -> None:
    with pytest.raises(EncounterIdentityError):
        compute_encounter_id("label", salt=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fail-closed: UNRESOLVED ${VAR} env-placeholder salt (#67 F2 compensating guard)
# ---------------------------------------------------------------------------


def test_unresolved_env_placeholder_salt_fails_loud() -> None:
    """#67 F2: a MISSING EnvironmentFile leaves scribe.encounter_salt as the
    literal ``${SCRIBE_ENCOUNTER_SALT}`` (resolve_env_placeholders keeps
    unresolved placeholders verbatim). That literal is PUBLIC + low-entropy — it
    MUST be rejected so no reversible id is ever minted."""
    with pytest.raises(EncounterIdentityError, match="UNRESOLVED"):
        compute_encounter_id("label", salt="${SCRIBE_ENCOUNTER_SALT}")


def test_unresolved_placeholder_with_surrounding_whitespace_fails_loud() -> None:
    """Strip-then-check: a placeholder padded with whitespace is still rejected."""
    with pytest.raises(EncounterIdentityError, match="UNRESOLVED"):
        compute_encounter_id("label", salt="  ${SCRIBE_ENCOUNTER_SALT}  ")


def test_real_secret_not_starting_with_placeholder_is_accepted() -> None:
    """A legitimate resolved secret (does not start with ``${``) is accepted —
    the guard rejects ONLY the unresolved-placeholder shape, not real salts."""
    eid = compute_encounter_id("label", salt="s3cr3t-value-not-a-placeholder")
    assert eid.startswith(ENCOUNTER_ID_PREFIX)


# ---------------------------------------------------------------------------
# Fail-closed: empty raw label
# ---------------------------------------------------------------------------


def test_empty_raw_label_fails_loud() -> None:
    with pytest.raises(EncounterIdentityError, match="raw_label"):
        compute_encounter_id("   ", salt=_SALT)
