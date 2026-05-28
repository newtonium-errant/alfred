#!/usr/bin/env bash
# Backwards-compat shim for the Algernon systemd-units installer.
#
# The real implementation lives at
# ``alfred.scripts.install_systemd_units`` (Python module). This shim
# preserves the bash-shape invocation path that systemd-ecosystem
# operators expect from "install script" patterns.
#
# Recommended invocation (post-package-hoist):
#   python -m alfred.scripts.install_systemd_units [--dry-run]
#
# Bash-shim invocation (this script):
#   scripts/install_systemd_units.sh [--dry-run]
#
# All argv passes through to the Python module unchanged. ``--dry-run``
# is the canonical pre-flight: prints the install plan + placeholder
# substitution without writing any files or calling systemctl.
#
# Sudo: the installer prompts for sudo to enable linger if not already
# on. The Python module handles the prompt + idempotency check; this
# shim is purely a wrapper.

set -eu

# Prefer the project venv's python if present (matches the canonical
# subprocess pattern in scripts/migrate_tier_phase1.py and instance_set).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
    # Fall back to whatever python is on PATH. Operator running the
    # installer outside the canonical venv layout — best effort.
    PYTHON="$(command -v python3 || command -v python)"
fi

if [[ -z "${PYTHON}" || ! -x "${PYTHON}" ]]; then
    echo "error: no python interpreter found. Activate the alfred venv" >&2
    echo "       or set PATH to include python3, then retry." >&2
    exit 2
fi

exec "${PYTHON}" -m alfred.scripts.install_systemd_units "$@"
