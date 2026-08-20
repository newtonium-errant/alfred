"""Pins for the ingest-provenance lock — "body locked, frontmatter open".

Operator ruling (ratified 2026-08-18, EDIT-SURFACE lane): web-ingested
verbatim records — the ingest pipeline stamps ``ingested_via`` (+ the
other provenance fields) at create time — are PROVENANCE-LOCKED against
agent curation:

  * Chat agents may CURATE FRONTMATTER (retitle, related links, tags)
    — EXCEPT the provenance fields, which lock too: ``ingested_at``,
    ``ingested_by``, ``ingested_via``, ``ingest_correlation_id``,
    ``source``.
  * Chat agents may NEVER alter the BODY of such records.
  * OWN-AUTHORED documents are EXEMPT: no ``ingested_via`` marker means
    fully editable (the discriminator is the marker's ABSENCE).
  * Delete stays refused as-is (no change to delete rules).

Evidence base (the driven hole): at 8b16e035 a tampered body edit landed
ON DISK identically under vera / talker / kalle scopes (body_append),
and under hypatia additionally via body_insert_at + body_replace
(``document`` is in her per-type allowlists); provenance frontmatter
edits landed under every chat scope. Every pin below that asserts a
refusal reproduced that drive at 8b16e035 first (LANDED) and now
asserts REFUSED + disk untouched.

The gate is ``scope._check_ingest_provenance_lock`` (see its matrix).
Every refusal pin here asserts the MESSAGE (which rule fired, which
surface/field, the propose-to-operator alternative) — a refusal for an
unrelated cause must not satisfy these pins — and every refusal family
carries its nearest admissible neighbour accepted (the marker-absent
positive control), so the pins fail against a build that locks
everything just as they fail against one that locks nothing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import structlog

from alfred.vault.ops import VaultError, vault_create, vault_edit, vault_read
from alfred.vault.scope import (
    INGEST_PROVENANCE_LOCKED_FIELDS,
    INGEST_PROVENANCE_MARKER_FIELD,
    ScopeError,
    check_scope,
)


# The ruling's chat-agent scopes — the four instances whose chat
# surface routes through ``telegram/conversation.py`` → ``vault_edit``.
CHAT_SCOPES = ["talker", "kalle", "hypatia", "vera"]

VERBATIM_BODY = (
    "The operator's verbatim artifact body.\n"
    "Line two stays as filed.\n"
)
TAMPER = "TAMPERED-BY-AGENT"


def _mint_ingested(
    tmp_vault: Path, title: str, record_type: str = "document",
) -> str:
    """Mint a web-ingested verbatim record through the PRODUCTION shape:
    ``vault_create`` under scope ``web_ingest`` with the provenance
    frontmatter ``transport/routes_ingest.py`` composes."""
    result = vault_create(
        tmp_vault, record_type, title,
        set_fields={
            "ingested_via": "web",
            "source": "https://example.test/artifact",
            "ingested_by": "andrew",
            "ingested_at": "2026-08-19T00:00:00+00:00",
            "ingest_correlation_id": "corr-123",
        },
        body=VERBATIM_BODY,
        scope="web_ingest",
    )
    return result["path"]


def _mint_authored(
    tmp_vault: Path, title: str, record_type: str = "document",
) -> str:
    """Mint an OWN-AUTHORED record (no marker) through the production
    shape: Hypatia's create scope. The exemption's discriminator is the
    marker's absence — nothing else differs from the ingested twin."""
    result = vault_create(
        tmp_vault, record_type, title,
        body=VERBATIM_BODY,
        scope="hypatia",
    )
    return result["path"]


def _body_on_disk(tmp_vault: Path, rel: str) -> str:
    return (tmp_vault / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The driven hole — body tamper refused, disk untouched, per chat scope
# ---------------------------------------------------------------------------


class TestBodyLock:
    @pytest.mark.parametrize("scope", CHAT_SCOPES)
    def test_body_append_tamper_refused_and_disk_untouched(
        self, tmp_vault: Path, scope: str,
    ) -> None:
        """The reviewer's exact drive: body_append under each chat scope
        landed on disk at 8b16e035; now refuses with the provenance
        message and the file stays byte-identical on the body."""
        rel = _mint_ingested(tmp_vault, f"Ingested {scope} Append")
        before = _body_on_disk(tmp_vault, rel)
        with pytest.raises(ScopeError) as exc:
            vault_edit(tmp_vault, rel, body_append=TAMPER, scope=scope)
        msg = str(exc.value)
        # WHY-pins: the rule, the marker, the alternative.
        assert "may not alter the body" in msg
        assert "ingested_via: web" in msg
        assert "propose the change to the operator" in msg
        after = _body_on_disk(tmp_vault, rel)
        assert TAMPER not in after
        assert after == before

        # Nearest admissible neighbour: the SAME scope, the SAME edit,
        # on an AUTHORED (marker-absent) record — accepted and lands.
        authored = _mint_authored(tmp_vault, f"Authored {scope} Append")
        vault_edit(tmp_vault, authored, body_append=TAMPER, scope=scope)
        assert TAMPER in _body_on_disk(tmp_vault, authored)

    @pytest.mark.parametrize("op_kwargs", [
        {"body_replace": "REWRITTEN WHOLESALE"},
        {"body_insert_at": {
            "marker": "Line two stays as filed.",
            "position": "before",
            "content": "INSERTED-TAMPER",
        }},
    ], ids=["body_replace", "body_insert_at"])
    def test_hypatia_body_tools_refused_on_ingested_document(
        self, tmp_vault: Path, op_kwargs: dict,
    ) -> None:
        """``document`` sits in hypatia's per-type allowlists, so before
        the lock these landed. The provenance lock refuses BOTH — and
        the allowlist cells keep governing her AUTHORED documents (the
        ruling's explicit exempt positive control: without it, these
        pins would pass against a build that locked every document)."""
        rel = _mint_ingested(tmp_vault, "Ingested Hypatia Tool")
        before = _body_on_disk(tmp_vault, rel)
        with pytest.raises(ScopeError) as exc:
            vault_edit(tmp_vault, rel, scope="hypatia", **op_kwargs)
        msg = str(exc.value)
        assert "may not alter the body" in msg
        assert "provenance lock" in msg
        assert _body_on_disk(tmp_vault, rel) == before

        # The authored twin SUCCEEDS through the very same allowlist.
        authored = _mint_authored(tmp_vault, "Authored Hypatia Tool")
        vault_edit(tmp_vault, authored, scope="hypatia", **op_kwargs)
        after = _body_on_disk(tmp_vault, authored)
        assert ("REWRITTEN WHOLESALE" in after) or ("INSERTED-TAMPER" in after)

    def test_body_rewriter_refused_scoped(self, tmp_vault: Path) -> None:
        """The fourth body surface — ``body_rewriter`` rides the same
        ``body_write`` flag through the edit gate."""
        rel = _mint_ingested(tmp_vault, "Ingested Rewriter")
        before = _body_on_disk(tmp_vault, rel)
        with pytest.raises(ScopeError, match="may not alter the body"):
            vault_edit(
                tmp_vault, rel,
                body_rewriter=lambda body: body + TAMPER,
                scope="talker",
            )
        assert _body_on_disk(tmp_vault, rel) == before

    def test_unscoped_operator_path_unchanged(self, tmp_vault: Path) -> None:
        """scope=None (operator CLI / pipeline internals) stays
        unrestricted — ``check_scope`` returns early on no scope. The
        lock binds AGENT curation only; this is the over-trigger
        control for the whole family."""
        rel = _mint_ingested(tmp_vault, "Ingested Operator Edit")
        vault_edit(tmp_vault, rel, body_append="operator amendment")
        assert "operator amendment" in _body_on_disk(tmp_vault, rel)

    @pytest.mark.parametrize("scope", ["curator", "janitor", "distiller"])
    def test_pipeline_scopes_also_locked_with_specific_message(
        self, tmp_vault: Path, scope: str,
    ) -> None:
        """The lock binds every SCOPED caller, not just chat scopes
        (verbatim bodies are sacrosanct against agent curation), and it
        fires BEFORE the generic ``allow_body_writes`` / permission
        gates — so even a scope that would refuse anyway (janitor's Q3
        body-write deny) names the SPECIFIC rule. Ordering pin: the
        message is the provenance one, not the generic one."""
        rel = _mint_ingested(tmp_vault, f"Ingested Pipeline {scope}")
        with pytest.raises(ScopeError) as exc:
            vault_edit(tmp_vault, rel, body_append=TAMPER, scope=scope)
        assert "may not alter the body" in str(exc.value)
        assert "provenance lock" in str(exc.value)

    def test_empty_marker_is_absent(self, tmp_vault: Path) -> None:
        """A whitespace/empty ``ingested_via`` value is treated as
        marker-ABSENT (authored): the lock keys on a truthy marker, and
        an empty string must not half-lock a record."""
        vault_create(
            tmp_vault, "note", "Empty Marker Note",
            set_fields={"ingested_via": ""},
            body=VERBATIM_BODY,
        )
        vault_edit(
            tmp_vault, "note/Empty Marker Note.md",
            body_append="fine", scope="talker",
        )
        assert "fine" in _body_on_disk(tmp_vault, "note/Empty Marker Note.md")


# ---------------------------------------------------------------------------
# Frontmatter stays open — the ruling's allowed curation surface
# ---------------------------------------------------------------------------


class TestFrontmatterOpen:
    @pytest.mark.parametrize("scope", CHAT_SCOPES)
    def test_curation_edits_succeed_on_locked_record(
        self, tmp_vault: Path, scope: str,
    ) -> None:
        """Retitle / related links / tags keep working on the SAME
        records the body lock protects — the live-path positive control
        for every refusal pin in this file."""
        rel = _mint_ingested(tmp_vault, f"Ingested Curation {scope}")
        vault_edit(
            tmp_vault, rel,
            set_fields={"title": "Renamed Artifact"},
            scope=scope,
        )
        vault_edit(
            tmp_vault, rel,
            append_fields={"related": "[[note/Some Note]]", "tags": "curated"},
            scope=scope,
        )
        post = vault_read(tmp_vault, rel)
        assert post["frontmatter"]["title"] == "Renamed Artifact"
        assert "[[note/Some Note]]" in post["frontmatter"]["related"]
        assert "curated" in post["frontmatter"]["tags"]
        # The body stayed verbatim through all of it.
        assert VERBATIM_BODY.strip() in _body_on_disk(tmp_vault, rel)


# ---------------------------------------------------------------------------
# Provenance fields locked — the ruling's five
# ---------------------------------------------------------------------------


class TestProvenanceFieldsLocked:
    @pytest.mark.parametrize("field,value", [
        ("ingested_at", "2001-01-01T00:00:00+00:00"),
        ("ingested_by", "someone-else"),
        ("ingested_via", "carrier-pigeon"),
        ("ingest_correlation_id", "forged"),
        ("source", "https://forged.test"),
    ])
    def test_set_provenance_field_refused(
        self, tmp_vault: Path, field: str, value: str,
    ) -> None:
        rel = _mint_ingested(tmp_vault, f"Ingested Set {field}")
        with pytest.raises(ScopeError) as exc:
            vault_edit(
                tmp_vault, rel, set_fields={field: value}, scope="talker",
            )
        msg = str(exc.value)
        assert "may not edit provenance field" in msg
        assert field in msg
        assert "propose the change to the operator" in msg
        # On-disk provenance untouched.
        post = vault_read(tmp_vault, rel)
        assert post["frontmatter"][field] != value

    def test_unset_marker_refused(self, tmp_vault: Path) -> None:
        """Stripping the marker would unlock the record — unset rides
        the same fields list and refuses."""
        rel = _mint_ingested(tmp_vault, "Ingested Unset Marker")
        with pytest.raises(ScopeError, match="may not edit provenance field"):
            vault_edit(
                tmp_vault, rel,
                unset_fields=["ingested_via"], scope="kalle",
            )
        post = vault_read(tmp_vault, rel)
        assert post["frontmatter"]["ingested_via"] == "web"

    def test_append_to_source_refused(self, tmp_vault: Path) -> None:
        rel = _mint_ingested(tmp_vault, "Ingested Append Source")
        with pytest.raises(ScopeError, match="may not edit provenance field"):
            vault_edit(
                tmp_vault, rel,
                append_fields={"source": "https://second.test"},
                scope="vera",
            )

    def test_mixed_edit_refused_whole(self, tmp_vault: Path) -> None:
        """An edit combining an ALLOWED field with a LOCKED one refuses
        as a whole — no partial landing."""
        rel = _mint_ingested(tmp_vault, "Ingested Mixed Edit")
        with pytest.raises(ScopeError, match="may not edit provenance field"):
            vault_edit(
                tmp_vault, rel,
                set_fields={"title": "New Title", "ingested_by": "x"},
                scope="talker",
            )
        post = vault_read(tmp_vault, rel)
        assert post["frontmatter"].get("title") != "New Title"
        assert post["frontmatter"]["ingested_by"] == "andrew"


# ---------------------------------------------------------------------------
# Forge guard — the marker is pipeline-stamped, never agent-written
# ---------------------------------------------------------------------------


class TestForgeGuard:
    def test_agent_may_not_stamp_marker_on_unmarked_record(
        self, tmp_vault: Path,
    ) -> None:
        """Writing ``ingested_via`` onto an UNMARKED record via a scoped
        edit refuses — stamping it would forge provenance, or lock an
        authored record out of its own workflow."""
        vault_create(tmp_vault, "note", "Plain Note", body="Just a note.\n")
        with pytest.raises(ScopeError) as exc:
            vault_edit(
                tmp_vault, "note/Plain Note.md",
                set_fields={"ingested_via": "web"},
                scope="talker",
            )
        msg = str(exc.value)
        assert "stamped by the ingest pipeline" in msg
        assert "propose the change to the operator" in msg

        # Nearest neighbours accepted: the same scoped edit on a
        # NON-provenance field, and the same marker write UNSCOPED
        # (operator path).
        vault_edit(
            tmp_vault, "note/Plain Note.md",
            set_fields={"summary": "fine"}, scope="talker",
        )
        vault_edit(
            tmp_vault, "note/Plain Note.md",
            set_fields={"ingested_via": "web"},
        )
        post = vault_read(tmp_vault, "note/Plain Note.md")
        assert post["frontmatter"]["ingested_via"] == "web"


# ---------------------------------------------------------------------------
# Marker beyond the ruling's named types — derived-conservative, FLAGGED
# ---------------------------------------------------------------------------


class TestMarkerBeyondRuledTypes:
    def test_web_ingested_note_body_locked(self, tmp_vault: Path) -> None:
        """DERIVED DECISION (flagged for the gate): the ruling names
        ``document``/``source``, but ``WEB_INGEST_CREATE_TYPES`` also
        mints verbatim ``note`` records with the same marker, and a
        type-keyed lock would be escapable by retype-via-edit. The lock
        keys on the MARKER, so a web-ingested note's body refuses
        identically. Conservative per the ruling's principle
        (provenance-marked verbatim bodies are sacrosanct against agent
        curation); the batch-carried note keeps its pipeline rule via
        the vera_batch exemption pinned below."""
        rel = _mint_ingested(tmp_vault, "Ingested Note", record_type="note")
        with pytest.raises(ScopeError, match="may not alter the body"):
            vault_edit(tmp_vault, rel, body_append=TAMPER, scope="kalle")
        assert TAMPER not in _body_on_disk(tmp_vault, rel)

        # Positive control: an ordinary (unmarked) note stays editable
        # under the same scope — ``note`` is in kalle's universe.
        vault_create(tmp_vault, "note", "Kalle Plain", body="Note body.\n")
        vault_edit(
            tmp_vault, "note/Kalle Plain.md",
            body_append="addendum", scope="kalle",
        )
        assert "addendum" in _body_on_disk(tmp_vault, "note/Kalle Plain.md")


# ---------------------------------------------------------------------------
# vera_batch exemption — the pipeline rule the lock must NOT break
# ---------------------------------------------------------------------------


class TestVeraBatchExemption:
    def _mint_batch_note(self, tmp_vault: Path, title: str) -> str:
        """Mirror ``routes_batch.py``'s mint exactly — the batch-carried
        note is a machine-regenerated RENDER that happens to carry the
        marker."""
        result = vault_create(
            tmp_vault, "note", title,
            set_fields={
                "batch_id": "batch-20260819-abc",
                "batch_status": "open",
                "batch_items_total": 3,
                "batch_items_done": 0,
                "batch_items_failed": 0,
                "batch_created_at": "2026-08-19T00:00:00+00:00",
                "ingested_via": "web",
                "ingested_by": "andrew",
                "source": "batch upload — 3 scans",
            },
            body="# Batch\n\n(rendering)\n",
            scope="vera_batch",
        )
        return result["path"]

    def test_worker_shape_body_replace_still_lands(
        self, tmp_vault: Path,
    ) -> None:
        """The batch worker's exact call shape (``batch/worker.py``:
        body_replace + progress counters under scope vera_batch) keeps
        working on its own marker-carrying record — the lock's
        over-trigger detector on the pipeline side."""
        rel = self._mint_batch_note(tmp_vault, "Batch Carried Note")
        vault_edit(
            tmp_vault, rel,
            body_replace="# Batch\n\nitem 1 done\n",
            set_fields={
                "batch_items_done": 1,
                "batch_items_total": 3,
                "batch_items_failed": 0,
                "batch_updated_at": "2026-08-19T01:00:00+00:00",
            },
            scope="vera_batch",
        )
        assert "item 1 done" in _body_on_disk(tmp_vault, rel)

    def test_exemption_does_not_reach_web_ingested_records(
        self, tmp_vault: Path,
    ) -> None:
        """Composition pin: the exemption only SKIPS the provenance lock
        for vera_batch body_replace — the OWNERSHIP gate behind it still
        refuses a record with no ``batch_id``, so a web-ingested note
        (let alone a document) stays out of the batch worker's reach."""
        rel = _mint_ingested(
            tmp_vault, "Ingested Not Batch", record_type="note",
        )
        with pytest.raises(ScopeError) as exc:
            vault_edit(
                tmp_vault, rel,
                body_replace="flattened", scope="vera_batch",
            )
        msg = str(exc.value)
        assert "carries no 'batch_id'" in msg
        # And it was the ownership gate, not the provenance lock — the
        # exemption held; the next gate caught it.
        assert "may not alter the body" not in msg
        assert "flattened" not in _body_on_disk(tmp_vault, rel)

    def test_exemption_is_body_replace_only(self, tmp_vault: Path) -> None:
        """vera_batch's exemption is exactly the worker's surface —
        body_append on its own marked record still refuses (the worker
        renders wholesale; nothing appends)."""
        rel = self._mint_batch_note(tmp_vault, "Batch Append Refused")
        with pytest.raises(ScopeError, match="may not alter the body"):
            vault_edit(
                tmp_vault, rel, body_append=TAMPER, scope="vera_batch",
            )


# ---------------------------------------------------------------------------
# Second altitude — direct check_scope drives (not through vault_edit)
# ---------------------------------------------------------------------------


class TestDirectCheckScopeDrives:
    _MARKED_FM = {
        "type": "document",
        "ingested_via": "web",
        "source": "https://example.test",
    }

    @pytest.mark.parametrize("op", ["body_insert_at", "body_replace"])
    def test_direct_body_op_refused(self, op: str) -> None:
        """A direct ``check_scope(<body op>)`` drive — bypassing
        vault_edit — hits the same lock inside
        ``_check_body_mutation_allowed``."""
        with pytest.raises(ScopeError, match="may not alter the body"):
            check_scope(
                "hypatia", op,
                rel_path="document/Direct.md",
                record_type="document",
                existing_frontmatter=dict(self._MARKED_FM),
            )

    @pytest.mark.parametrize("op", ["body_insert_at", "body_replace"])
    def test_direct_body_op_unmarked_passes(self, op: str) -> None:
        """The marker-absent twin clears the lock and the per-type
        allowlist (document is in hypatia's cells) — proving the direct
        drive above refused on PROVENANCE, not on a dead path."""
        check_scope(
            "hypatia", op,
            rel_path="document/Direct.md",
            record_type="document",
            existing_frontmatter={"type": "document"},
        )


# ---------------------------------------------------------------------------
# CLI path — the agent path threads the on-disk frontmatter
# ---------------------------------------------------------------------------


class TestCliPathThreaded:
    """``alfred vault edit`` scope-checks at the CLI layer and calls
    ``vault_edit`` UNSCOPED — so the lock only reaches this path because
    ``cmd_edit`` now threads the target's on-disk frontmatter into
    ``check_scope``. These pins drive the production entry point; the
    per-layer pins above cannot catch a dead CLI thread."""

    def _args(self, path: str, **over: object) -> argparse.Namespace:
        base: dict = {
            "path": path,
            "set": None,
            "append": None,
            "unset": None,
            "body_append": None,
            "body_stdin": False,
        }
        base.update(over)
        return argparse.Namespace(**base)

    @pytest.fixture(autouse=True)
    def _cli_env(
        self, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("ALFRED_VAULT_SCOPE", "talker")
        # Dispatcher env-var test-hygiene contract (CLAUDE.md): clear
        # the bleed-prone vars so this test never inherits another
        # test's session/audit context.
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

    def test_cli_body_append_refused(
        self,
        tmp_vault: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.vault.cli import cmd_edit

        rel = _mint_ingested(tmp_vault, "CLI Ingested")
        before = _body_on_disk(tmp_vault, rel)
        with structlog.testing.capture_logs():
            with pytest.raises(SystemExit) as exc_info:
                cmd_edit(self._args(rel, body_append=TAMPER))
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "may not alter the body" in payload["error"]
        assert "propose the change to the operator" in payload["error"]
        assert _body_on_disk(tmp_vault, rel) == before

    def test_cli_provenance_field_refused(
        self,
        tmp_vault: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from alfred.vault.cli import cmd_edit

        rel = _mint_ingested(tmp_vault, "CLI Ingested Fields")
        with structlog.testing.capture_logs():
            with pytest.raises(SystemExit) as exc_info:
                cmd_edit(self._args(rel, set=["ingested_by=someone-else"]))
        assert exc_info.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert "may not edit provenance field" in payload["error"]

    def test_cli_curation_still_lands(
        self,
        tmp_vault: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The CLI positive control: retitle through the same entry
        point succeeds on the same locked record."""
        from alfred.vault.cli import cmd_edit

        rel = _mint_ingested(tmp_vault, "CLI Ingested Curation")
        with structlog.testing.capture_logs():
            cmd_edit(self._args(rel, set=["title=Renamed Via CLI"]))
        payload = json.loads(capsys.readouterr().out)
        assert "title" in payload["fields_changed"]
        post = vault_read(tmp_vault, rel)
        assert post["frontmatter"]["title"] == "Renamed Via CLI"

    def test_cli_missing_target_fails_on_real_error(
        self,
        tmp_vault: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A missing target must surface vault_edit's own fail-loud
        (File not found), not a phantom refusal from the best-effort
        frontmatter read."""
        from alfred.vault.cli import cmd_edit

        with structlog.testing.capture_logs():
            with pytest.raises(SystemExit):
                cmd_edit(self._args(
                    "document/Nope.md", body_append="anything",
                ))
        payload = json.loads(capsys.readouterr().out)
        assert "File not found" in payload["error"]


# ---------------------------------------------------------------------------
# Delete + read — untouched by the ruling
# ---------------------------------------------------------------------------


class TestUntouchedSurfaces:
    def test_chat_delete_stays_refused_as_is(self, tmp_vault: Path) -> None:
        """Delete stays refused AS-IS: the existing generic scope denial
        fires, not a provenance message — the ruling changed nothing
        here."""
        rel = _mint_ingested(tmp_vault, "Ingested Delete")
        with pytest.raises(ScopeError) as exc:
            check_scope("vera", "delete", rel_path=rel)
        msg = str(exc.value)
        assert "denied for scope 'vera'" in msg
        assert "provenance" not in msg

    def test_read_stays_open(self, tmp_vault: Path) -> None:
        rel = _mint_ingested(tmp_vault, "Ingested Read")
        check_scope("vera", "read", rel_path=rel)  # no raise
        result = vault_read(tmp_vault, rel)
        assert "verbatim artifact" in result["body"]


# ---------------------------------------------------------------------------
# Contract pins — lockstep with the ruling
# ---------------------------------------------------------------------------


class TestContractPins:
    def test_locked_fields_are_the_rulings_five(self) -> None:
        """The ruling (2026-08-18) locks EXACTLY these five. Widening or
        narrowing ``INGEST_PROVENANCE_LOCKED_FIELDS`` is a deliberate
        act that updates this pin in the same commit."""
        assert INGEST_PROVENANCE_LOCKED_FIELDS == frozenset({
            "ingested_at",
            "ingested_by",
            "ingested_via",
            "ingest_correlation_id",
            "source",
        })

    def test_marker_field_name(self) -> None:
        assert INGEST_PROVENANCE_MARKER_FIELD == "ingested_via"
        assert INGEST_PROVENANCE_MARKER_FIELD in (
            INGEST_PROVENANCE_LOCKED_FIELDS
        )
