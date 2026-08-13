"""The report's invoice side — the late list, the matches, the unknown count.

All fixture data is INVENTED. The shapes are the live ones; the names,
k-numbers, invoice numbers and figures are not.

The pin this file exists for is the LAST one: the whole chain driven through
``alfred.cli.main``, with a real export written at the path the RECEIVER
derives. A ``snapshot=`` kwarg that only tests ever pass is the standing
trap — every unit pin green, the feature dead in the field, and the operator
told "nothing is late" by a report that never opened the export. Per-layer
tests structurally cannot catch that; only the end-to-end one can.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import structlog
import yaml

from alfred.common.rrts_export import export_path_for
from alfred.reconcile import cli as rcli
from alfred.reconcile.config import ReconcileConfig, load_from_unified
from alfred.reconcile.invoices import InvoiceSnapshot, load_snapshot
from alfred.reconcile.ledger import ClaimLine, LedgerContents, Statement
from alfred.reconcile.report import build_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN_NOTE = FIXTURES / "remittance_note_synthetic.md"

_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _export_document(
    *,
    exported_at: str = "2026-08-13T02:30:00Z",
    invoices: list[dict] | None = None,
) -> dict:
    return {
        "exported_at": exported_at,
        "invoices": invoices if invoices is not None else [_export_invoice()],
    }


def _export_invoice(
    invoice_no: str = "INV-1",
    *,
    client_name: str = "Marisol Aldenshaw",
    status: str = "sent",
    date_sent: str = "2026-01-05",
    amount: str = "300.00",
    dos: str = "2026-06-11",
) -> dict:
    return {
        "invoice_no": invoice_no,
        "client_name": client_name,
        "knumber": "K1234567",
        "status": status,
        "date_sent": date_sent,
        "invoice_date": date_sent,
        "amount_excl_tax": amount,
        "line_items": [{"date_of_service": dos, "amount": amount}],
    }


def _snapshot_from(document: dict, tmp_path: Path) -> InvoiceSnapshot:
    p = tmp_path / "invoices.json"
    p.write_text(json.dumps(document), encoding="utf-8")
    return load_snapshot(p, now=_NOW)


def _contents(*lines: ClaimLine) -> LedgerContents:
    return LedgerContents(
        statements=[Statement(statement_date="2026-07-01", provider="BC")],
        claim_lines=list(lines),
    )


def _line(surname: str = "Aldenshaw", dos: str = "2026-06-11") -> ClaimLine:
    return ClaimLine(
        statement_date="2026-07-01",
        claim_no="C-1",
        dos=dos,
        surname=surname,
        first_name="Marisol",
        benefit_code="BC1",
        total_billed=Decimal("300.00"),
        amount_paid=Decimal("250.00"),
    )


# --- the absent invoice side is a STATEMENT, not a blank ----------------------


def test_no_snapshot_says_so_rather_than_implying_nothing_is_late() -> None:
    """"No invoice side configured" and "nothing is late" are different
    claims, and a report that silently made the second one would be telling
    the operator his chase list is clean when it was never computed."""
    with structlog.testing.capture_logs() as captured:
        report = build_report(_contents(_line()), snapshot=None)

    assert report.snapshot is None
    assert report.match is None
    assert report.aging is None
    text = report.summary_text
    assert "No invoice export was read for this report" in text
    assert "only what the provider answered" in text.lower()

    events = [
        c for c in captured
        if c.get("event") == "reconcile.report.no_invoice_side"
    ]
    assert len(events) == 1


def test_an_absent_export_on_disk_differs_from_no_export_configured(
    tmp_path,
) -> None:
    """Two states that need different actions: one is unconfigured, the
    other is a feed that stopped arriving."""
    absent = load_snapshot(tmp_path / "never-written.json", now=_NOW)
    report = build_report(_contents(_line()), snapshot=absent)

    assert report.snapshot is not None
    assert report.snapshot.absent is True
    text = report.summary_text
    assert "No invoice export on disk" in text
    assert "STALE" in text, "an absent export is stale by definition"


# --- the unknown-status count line -------------------------------------------


def test_unknown_statuses_are_rendered_as_a_count_not_only_logged(
    tmp_path,
) -> None:
    """The aggregation existed and nothing rendered it. A status that only
    ever appears in a log line stays unknown for a month, because nobody
    greps for a thing they do not know exists."""
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-1", status="partially_credited"),
        _export_invoice("INV-2", status="partially_credited"),
        _export_invoice("INV-3", status="awaiting_adjudication"),
        _export_invoice("INV-4", status="sent"),
    ]), tmp_path)
    assert snap.unknown_statuses == {
        "partially_credited": 2, "awaiting_adjudication": 1
    }

    text = build_report(_contents(_line()), snapshot=snap).summary_text
    assert "3 invoice(s) carry a status this build does not recognise" in text
    assert "`partially_credited`: 2" in text
    assert "`awaiting_adjudication`: 1" in text
    assert "neither chased nor matched" in text


def test_the_count_line_names_itself_as_the_only_surface_for_them(
    tmp_path,
) -> None:
    """Both gates fail closed on an unknown status, so an unknown-status
    invoice is matched by nothing and aged by nothing. This line is the
    safety valve for that pair of exclusions and must say so."""
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-1", status="mystery"),
    ]), tmp_path)
    report = build_report(_contents(_line()), snapshot=snap)

    assert report.aging is not None and not report.aging.late
    assert report.match is not None and not report.match.proposals
    assert "the only place they appear" in report.summary_text


def test_a_clean_status_set_says_the_check_ran(tmp_path) -> None:
    """POSITIVE CONTROL for the pins above: with no unknown statuses the
    section states that the check ran and found nothing, rather than going
    silent and looking identical to a check that never happened."""
    snap = _snapshot_from(_export_document(), tmp_path)
    text = build_report(_contents(_line()), snapshot=snap).summary_text
    assert "Every invoice status in the export is one this build recognises" \
        in text
    assert "does not recognise" not in text


# --- the late-invoice section ------------------------------------------------


def test_a_late_invoice_is_tabled_with_its_basis(tmp_path) -> None:
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-LATE", date_sent="2026-01-05"),
    ]), tmp_path)
    report = build_report(
        _contents(_line(surname="Nobody")), snapshot=snap, now=_NOW
    )

    assert [e.invoice_no for e in report.aging.late] == ["INV-LATE"]
    text = report.summary_text
    assert "## Late invoices" in text
    assert "INV-LATE" in text
    assert "date_sent" in text


def test_nothing_late_is_stated_explicitly(tmp_path) -> None:
    """The ILB line the watchdog exists to be able to say. An empty section
    here is indistinguishable from a watchdog that failed to run."""
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-1", date_sent="2026-08-10"),
    ]), tmp_path)
    report = build_report(
        _contents(_line(surname="Nobody")), snapshot=snap, now=_NOW
    )

    assert report.aging is not None
    assert report.aging.late == []
    assert report.aging.examined == 1
    text = report.summary_text
    assert "No invoice is late — the loop has nothing to chase" in text
    assert "not an empty section" in text


def test_no_chaseable_invoices_reads_differently_from_none_late(
    tmp_path,
) -> None:
    """"Nothing to age" and "nothing is late" are different findings — the
    first says the export held no chaseable invoice at all."""
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-1", status="void"),
    ]), tmp_path)
    text = build_report(
        _contents(_line(surname="Nobody")), snapshot=snap, now=_NOW
    ).summary_text
    assert "there is nothing to age" in text
    assert "No invoice is late" not in text


def test_a_weak_aging_basis_is_rendered_distinctly(tmp_path) -> None:
    """An invoice chased on invoice_date should not look like one chased on
    the date it was actually sent."""
    doc = _export_document(invoices=[_export_invoice("INV-W")])
    doc["invoices"][0]["date_sent"] = ""
    doc["invoices"][0]["invoice_date"] = "2026-01-05"
    snap = _snapshot_from(doc, tmp_path)

    text = build_report(
        _contents(_line(surname="Nobody")), snapshot=snap, now=_NOW
    ).summary_text
    assert "invoice_date (weaker)" in text
    assert "weaker" in text and "RRTS's own rider" in text


# --- placement: ABOVE the empty-ledger early return --------------------------


def test_the_invoice_side_renders_even_when_the_ledger_is_empty(
    tmp_path,
) -> None:
    """The trap this placement avoids, and it is a documented one: the
    surveyor's no_changed_clusters early-return silenced vault-state
    observability twice. The invoice feed's state does not depend on whether
    anything has been seeded — an unseeded instance with a stale export is
    exactly the case a post-return placement would silence.
    """
    stale = _snapshot_from(
        _export_document(exported_at="2026-08-01T02:30:00Z"), tmp_path
    )
    assert stale.is_stale

    report = build_report(LedgerContents(), snapshot=stale)
    text = report.summary_text

    assert "The ledger holds no claim lines" in text, "the ILB line survives"
    assert "## Invoice side" in text
    assert "The export is STALE" in text
    assert text.index("## Invoice side") < text.index(
        "The ledger holds no claim lines"
    ), "the invoice side must be emitted before the early return"


def test_a_stale_export_suppresses_the_late_and_match_sections(
    tmp_path,
) -> None:
    """The promise, applied where it bites. Figures computed from old data
    would be about a world that has moved."""
    stale = _snapshot_from(
        _export_document(exported_at="2026-08-01T02:30:00Z"), tmp_path
    )
    report = build_report(_contents(_line()), snapshot=stale, now=_NOW)

    assert report.aging.stale_snapshot is True
    assert report.match.stale_snapshot is True
    text = report.summary_text
    assert "The export is STALE" in text
    assert "## Late invoices" not in text
    assert "## Proposed payment matches" not in text

    # POSITIVE CONTROL: the same document, fresh, renders both sections.
    fresh = _snapshot_from(_export_document(), tmp_path)
    fresh_text = build_report(
        _contents(_line()), snapshot=fresh, now=_NOW
    ).summary_text
    assert "## Late invoices" in fresh_text
    assert "## Proposed payment matches" in fresh_text


# --- the matcher's section ---------------------------------------------------


def test_a_proposal_is_rendered_with_its_basis_in_words(tmp_path) -> None:
    snap = _snapshot_from(_export_document(), tmp_path)
    report = build_report(_contents(_line()), snapshot=snap, now=_NOW)

    assert len(report.match.proposals) == 1
    text = report.summary_text
    assert "## Proposed payment matches" in text
    assert "Proposals only" in text
    assert "INV-1" in text
    assert "Why each one, in the matcher's own words" in text
    assert "client surname and date of service both agree" in text


def test_a_matched_invoice_drops_off_the_chase_list_in_one_report(
    tmp_path,
) -> None:
    """The ordering contract inside build_report: the matcher runs first and
    its output feeds the watchdog. Two callers doing this by hand could run
    them in the wrong order and the report would look complete either way."""
    snap = _snapshot_from(_export_document(invoices=[
        _export_invoice("INV-MATCHED", date_sent="2026-01-05"),
        _export_invoice(
            "INV-ORPHAN", client_name="Bram Quillon", date_sent="2026-01-05",
        ),
    ]), tmp_path)

    report = build_report(_contents(_line()), snapshot=snap, now=_NOW)

    assert report.match.matched_invoice_nos == {"INV-MATCHED"}
    assert [e.invoice_no for e in report.aging.late] == ["INV-ORPHAN"], (
        "the proposed invoice must stop being chased; the unproposed one "
        "must stay on the list"
    )
    assert "INV-ORPHAN" in report.summary_text


def test_an_unmatched_group_is_surfaced(tmp_path) -> None:
    snap = _snapshot_from(_export_document(), tmp_path)
    report = build_report(
        _contents(_line(surname="Quillon")), snapshot=snap, now=_NOW
    )
    assert len(report.match.unmatched) == 1
    assert "have no candidate invoice at all" in report.summary_text


# --- the CLI, and the derivation both ends share -----------------------------


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Testbed"}},
    }), encoding="utf-8")
    return path


def _run(argv: list[str]) -> int:
    import sys

    from alfred.cli import main

    original = sys.argv
    sys.argv = ["alfred", *argv]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = original
    return 0


def test_the_reader_and_the_writer_resolve_the_same_file(tmp_path) -> None:
    """POSITIVE evidence of identity, not the absence of contradiction. The
    receiver writes through the shared helper and the reconcile config reads
    through it; this asserts the two land on one file rather than merely
    failing to prove they do not.
    """
    data_dir = str(tmp_path / "data")
    config = load_from_unified({
        "logging": {"dir": data_dir},
        "telegram": {"instance": {"name": "Testbed"}},
    })

    writer_target = Path(export_path_for(data_dir)).resolve()
    reader_source = Path(config.invoices_path).resolve()
    assert reader_source == writer_target, (
        "the writer and the reader derived different files — the silent, "
        "total failure the shared derivation exists to prevent"
    )


def test_an_explicit_invoices_path_wins(tmp_path) -> None:
    config = load_from_unified({
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Testbed"}},
        "reconcile": {"invoices_path": "/srv/elsewhere/invoices.json"},
    })
    assert config.invoices_path == "/srv/elsewhere/invoices.json"


def test_no_data_dir_yields_no_invoices_path_rather_than_a_guess() -> None:
    config = load_from_unified({"telegram": {"instance": {"name": "Testbed"}}})
    assert config.invoices_path == ""


def test_the_cli_says_so_when_no_invoice_side_resolves(tmp_path, capsys):
    """ILB at the command surface. An operator running the report with no
    export configured must not read a statement-only report as a complete
    one."""
    config = load_from_unified({
        "logging": {"dir": str(tmp_path)},
        "telegram": {"instance": {"name": "Testbed"}},
    })
    config.invoices_path = ""

    with structlog.testing.capture_logs() as captured:
        assert rcli.cmd_report(config, wants_json=False) == 0
    out = capsys.readouterr().out
    assert "No invoice export was read" in out

    events = [
        c for c in captured
        if c.get("event") == "reconcile.cli.no_invoices_path"
    ]
    assert len(events) == 1


def test_end_to_end_the_export_reaches_the_report(tmp_path):
    """THE THREADING PIN.

    An optional kwarg that gates a feature must be threaded at the
    production call site, and per-layer pins cannot see that it is not: they
    pass the snapshot themselves. This drives argparse -> config load ->
    handler -> load_snapshot -> matcher -> watchdog -> written file, with the
    export placed at the path the RECEIVER derives, and reads the artifact
    off disk.
    """
    config_path = _config_file(tmp_path)
    data_dir = str(tmp_path / "data")

    export = Path(export_path_for(data_dir))
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text(json.dumps(_export_document(
        exported_at=datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        invoices=[
            _export_invoice("INV-LATE-E2E", date_sent="2026-01-05"),
        ],
    )), encoding="utf-8")

    assert _run(["--config", str(config_path), "reconcile", "seed",
                 "--note", str(CLEAN_NOTE), "--json"]) == 0
    assert _run(["--config", str(config_path), "reconcile",
                 "report", "--json"]) == 0

    store = tmp_path / "data" / "remittance" / "testbed"
    summaries = list((store / "reports").glob("backlog-review-*.md"))
    assert len(summaries) == 1
    text = summaries[0].read_text(encoding="utf-8")

    assert "## Invoice side" in text
    assert "## Late invoices" in text
    assert "INV-LATE-E2E" in text, (
        "the export did not reach the report — the snapshot kwarg is not "
        "threaded at the production call site"
    )


def test_end_to_end_without_an_export_the_report_states_the_absence(tmp_path):
    """POSITIVE CONTROL for the pin above: the identical run with no export
    on disk produces a report that SAYS the invoice side is missing, rather
    than one that silently omits the sections."""
    config_path = _config_file(tmp_path)

    assert _run(["--config", str(config_path), "reconcile", "seed",
                 "--note", str(CLEAN_NOTE), "--json"]) == 0
    assert _run(["--config", str(config_path), "reconcile",
                 "report", "--json"]) == 0

    store = tmp_path / "data" / "remittance" / "testbed"
    text = list((store / "reports").glob("*.md"))[0].read_text(encoding="utf-8")
    assert "## Invoice side" in text
    assert "No invoice export on disk" in text
    assert "INV-LATE-E2E" not in text


def test_the_cli_payload_carries_the_invoice_side_figures(tmp_path, capsys):
    config = load_from_unified({
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Testbed"}},
    })
    export = Path(config.invoices_path)
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_text(json.dumps(_export_document(
        exported_at=datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        invoices=[
            _export_invoice("INV-1", status="mystery_status"),
            _export_invoice("INV-2", date_sent="2026-01-05"),
        ],
    )), encoding="utf-8")

    assert rcli.cmd_report(config, wants_json=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["invoice_side"] is True
    assert payload["invoices_path"] == str(export)
    assert payload["invoices_stale"] is False
    assert payload["unknown_statuses"] == {"mystery_status": 1}
    assert payload["late"] == 1
