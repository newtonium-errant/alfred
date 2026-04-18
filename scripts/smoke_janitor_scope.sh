#!/usr/bin/env bash
# Smoke tests for the janitor scope lock (Option E commit 5).
#
# Exercises the field_allowlist branch of check_scope() end-to-end via
# the `alfred vault` CLI. Covers:
#
#   1. janitor scope rejects out-of-allowlist fields (alfred_tags)
#   2. janitor scope accepts in-allowlist field (janitor_note)
#   3. janitor_enrich scope accepts in-allowlist field (description)
#   4. Post-test cleanup — reverts the two fields it touched and
#      diffs against a pre-test backup so the vault stays clean.
#
# Executable documentation of the scope contract. If these four cases
# stop behaving, the lock is either broken or the allowlist has
# drifted from the plan. No pytest machinery — just bash + alfred.
#
# Usage:
#   scripts/smoke_janitor_scope.sh
#
# Requires:
#   - .venv/ active or `alfred` on PATH
#   - A vault at ./vault with person/Andrew Newton.md present

set -u

# --- configuration -----------------------------------------------------------

ALFRED_CMD="${ALFRED_CMD:-.venv/bin/alfred}"
if ! command -v "${ALFRED_CMD}" >/dev/null 2>&1 && [[ ! -x "${ALFRED_CMD}" ]]; then
    ALFRED_CMD="alfred"
fi

VAULT_PATH="${ALFRED_VAULT_PATH:-$(pwd)/vault}"
export ALFRED_VAULT_PATH="${VAULT_PATH}"

TARGET_REL="person/Andrew Newton.md"
TARGET_ABS="${VAULT_PATH}/${TARGET_REL}"
BACKUP="$(mktemp -t smoke_scope_backup.XXXXXX.md)"

if [[ ! -f "${TARGET_ABS}" ]]; then
    echo "FAIL: target file missing: ${TARGET_ABS}"
    exit 1
fi

# Snapshot so we can diff at the end. Stores the two fields' original
# values so the cleanup phase can restore them exactly.
cp "${TARGET_ABS}" "${BACKUP}"

PASS_COUNT=0
FAIL_COUNT=0

pass() {
    echo "PASS: $1"
    PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
    echo "FAIL: $1"
    FAIL_COUNT=$((FAIL_COUNT + 1))
}

# Extract original values so cleanup writes back exactly what was there.
ORIG_DESCRIPTION="$(
    ALFRED_VAULT_SCOPE="" "${ALFRED_CMD}" vault read "${TARGET_REL}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["frontmatter"].get("description",""))'
)"
ORIG_JANITOR_NOTE="$(
    ALFRED_VAULT_SCOPE="" "${ALFRED_CMD}" vault read "${TARGET_REL}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["frontmatter"].get("janitor_note",""))'
)"

# --- tests -------------------------------------------------------------------

echo "=== Smoke 1: janitor scope rejects alfred_tags ==="
OUT=$(ALFRED_VAULT_SCOPE=janitor "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set 'alfred_tags=["smoke-test"]' 2>&1)
RC=$?
if [[ ${RC} -ne 0 ]] && echo "${OUT}" | grep -q "Scope 'janitor'"; then
    pass "out-of-allowlist field rejected with scope error (exit=${RC})"
else
    fail "expected non-zero exit and 'Scope janitor' in output; got exit=${RC}, out=${OUT}"
fi

echo "=== Smoke 2: janitor scope accepts janitor_note ==="
OUT=$(ALFRED_VAULT_SCOPE=janitor "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set 'janitor_note="SMOKE -- commit 6 test"' 2>&1)
RC=$?
if [[ ${RC} -eq 0 ]] && echo "${OUT}" | grep -q "janitor_note"; then
    pass "in-allowlist field accepted (exit=${RC})"
else
    fail "expected exit 0 with janitor_note in output; got exit=${RC}, out=${OUT}"
fi

echo "=== Smoke 3: janitor_enrich scope accepts description ==="
OUT=$(ALFRED_VAULT_SCOPE=janitor_enrich "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set 'description="SMOKE -- commit 6 test"' 2>&1)
RC=$?
if [[ ${RC} -eq 0 ]] && echo "${OUT}" | grep -q "description"; then
    pass "janitor_enrich allows description (exit=${RC})"
else
    fail "expected exit 0 with description in output; got exit=${RC}, out=${OUT}"
fi

echo "=== Smoke 4: janitor_enrich scope rejects out-of-allowlist field ==="
OUT=$(ALFRED_VAULT_SCOPE=janitor_enrich "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set 'alfred_tags=["smoke-test"]' 2>&1)
RC=$?
if [[ ${RC} -ne 0 ]] && echo "${OUT}" | grep -q "Scope 'janitor_enrich'"; then
    pass "janitor_enrich rejects out-of-allowlist (exit=${RC})"
else
    fail "expected non-zero exit and 'Scope janitor_enrich' in output; got exit=${RC}, out=${OUT}"
fi

# --- cleanup -----------------------------------------------------------------

echo "=== Cleanup: restore touched fields ==="
ALFRED_VAULT_SCOPE=janitor_enrich "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set "description=${ORIG_DESCRIPTION}" >/dev/null 2>&1 || true
ALFRED_VAULT_SCOPE=janitor "${ALFRED_CMD}" vault edit "${TARGET_REL}" \
    --set "janitor_note=${ORIG_JANITOR_NOTE}" >/dev/null 2>&1 || true

if diff -q "${TARGET_ABS}" "${BACKUP}" >/dev/null 2>&1; then
    pass "vault restored to pre-test state (diff is empty)"
else
    echo "  diff:"
    diff "${TARGET_ABS}" "${BACKUP}" || true
    fail "vault not fully restored -- inspect ${TARGET_ABS} vs ${BACKUP}"
fi

rm -f "${BACKUP}"

# --- summary -----------------------------------------------------------------

echo
echo "--- summary: ${PASS_COUNT} pass, ${FAIL_COUNT} fail ---"
if [[ ${FAIL_COUNT} -gt 0 ]]; then
    exit 1
fi
exit 0
