"""``alfred reconcile`` subcommand handlers.

Five verbs, each returning a process exit code:

``seed``     parse a payment-summary note into the ledger (idempotent)
``render``   regenerate the note text FROM the ledger (prints; writes nothing
             to any vault)
``report``   build the backlog bulk-review artifact (CSV + summary)
``status``   what is in the ledger, and where it lives
``correct``  record an operator ruling on one line's classification

**Every command states what it did, including when it did nothing.** A
seed that skipped rows prints the count AND the rows; a status against an
empty ledger says the ledger is empty rather than printing nothing; a
report with no findings says so. Silence is indistinguishable from broken,
and on a payment loop the broken case costs money.

**Nothing here writes to a vault.** ``render`` prints to stdout so the
operator can see the regenerated text and pipe it where he wants. Wiring
it into the batch pipeline's carried-record write path is the documented
seam in :mod:`alfred.reconcile.render`, deliberately not taken in P1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from .attention import (
    ALL_CLASSES,
    Correction,
    append_correction,
    class_counts,
    classify_all,
    load_corrections,
)
from .config import ReconcileConfig
from .invoices import load_snapshot
from .ledger import load_ledger, upsert
from .parser import parse_note
from .paths import corrections_path_in, ledger_path_in, reports_dir_in
from .render import render_note
from .report import build_report, report_stem, write_report

log = structlog.get_logger(__name__)


def _emit(payload: dict[str, Any], wants_json: bool, *lines: str) -> None:
    """Print JSON or human lines. One place, so the two never diverge."""
    if wants_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    for line in lines:
        print(line)


def _refuse_disabled(config: ReconcileConfig, wants_json: bool) -> int:
    """The one refusal every command shares: no resolvable store."""
    msg = (
        "reconcile is OFF for this config: no store directory could be "
        "resolved. Set logging.dir and telegram.instance.name, or set "
        "reconcile.store_dir explicitly. Nothing was read or written."
    )
    _emit({"ok": False, "error": "store_unresolved", "detail": msg}, wants_json, msg)
    log.warning("reconcile.cli.refused_disabled", instance=config.instance_name)
    return 1


def cmd_seed(
    config: ReconcileConfig,
    *,
    note_path: str,
    batch_id: str = "",
    session: str = "",
    capture_ref: str = "",
    dry_run: bool = False,
    wants_json: bool = False,
) -> int:
    """Parse a payment-summary note into the ledger. Idempotent on re-run."""
    if not config.enabled or not config.store_dir:
        return _refuse_disabled(config, wants_json)

    p = Path(note_path).expanduser()
    if not p.is_file():
        msg = f"note not found: {p}"
        _emit({"ok": False, "error": "note_not_found", "path": str(p)},
              wants_json, msg)
        return 1
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        msg = f"could not read {p}: {exc}"
        _emit({"ok": False, "error": "note_unreadable", "detail": str(exc)},
              wants_json, msg)
        return 1

    result = parse_note(
        text,
        source_note=str(p),
        batch_id=batch_id,
        session=session,
        capture_ref=capture_ref,
        date_order=config.date_order or None,
    )

    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "note": str(p),
        "statements": len(result.statements),
        "claim_lines": len(result.claim_lines),
        "subtotals": len(result.subtotals),
        "skipped": [s.to_dict() for s in result.skipped],
        "skipped_count": len(result.skipped),
        "collisions": result.collisions,
        "unmapped_headings": sorted(set(result.unmapped_headings)),
        "tables_seen": result.tables_seen,
        "tables_parsed": result.tables_parsed,
        "summary": result.summary(),
    }

    if not dry_run:
        ledger = ledger_path_in(config.store_dir)
        res = upsert(
            ledger,
            statements=result.statements,
            claim_lines=result.claim_lines,
            subtotals=result.subtotals,
        )
        payload["ledger_path"] = str(ledger)
        payload["inserted"] = res.inserted
        payload["updated"] = res.updated
        payload["unchanged"] = res.unchanged

    lines = [result.summary()]
    if dry_run:
        lines.append("(dry run — the ledger was not written)")
    else:
        lines.append(
            f"Ledger {payload['ledger_path']}: "
            f"{payload['inserted']} inserted, {payload['updated']} updated, "
            f"{payload['unchanged']} unchanged."
        )
        if payload["inserted"] == 0 and payload["updated"] == 0:
            lines.append(
                "Nothing changed — every row was already in the ledger. "
                "That is what an idempotent re-run looks like, not a failure."
            )
    if result.unmapped_headings:
        lines.append("")
        lines.append(
            "Columns present in the source that this parser has no field "
            "for (their data was NOT captured): "
            + ", ".join(sorted(set(result.unmapped_headings)))
        )
    if result.collisions:
        lines.append("")
        lines.append(
            f"{len(result.collisions)} claim line(s) shared a key with "
            f"another and were disambiguated by source order. This is "
            f"expected where the Claim # column carries text instead of a "
            f"number — noted so it is never silent."
        )
    if result.skipped:
        lines.append("")
        lines.append(
            f"{len(result.skipped)} row(s) were NOT parsed and are NOT in "
            f"the ledger:"
        )
        for s in result.skipped:
            lines.append(f"  line {s.line_no}: {s.reason}")
            lines.append(f"    {s.raw[:160]}")
    else:
        lines.append("")
        lines.append("Every row in the note parsed — nothing was skipped.")

    _emit(payload, wants_json, *lines)
    # A partial parse is a non-zero exit: the operator asked for the note to
    # be seeded, and some of it was not. Exiting 0 with a skipped list would
    # let a scripted caller treat a lossy seed as a success.
    return 0 if result.ok else 2


def cmd_render(config: ReconcileConfig, *, wants_json: bool = False) -> int:
    """Regenerate the note text from the ledger. Prints; writes no vault."""
    if not config.enabled or not config.store_dir:
        return _refuse_disabled(config, wants_json)
    contents = load_ledger(ledger_path_in(config.store_dir))
    text = render_note(contents)
    if wants_json:
        print(json.dumps({
            "ok": True,
            "statements": len(contents.statements),
            "claim_lines": len(contents.claim_lines),
            "body": text,
        }, indent=2))
    else:
        print(text)
    return 0


def cmd_report(config: ReconcileConfig, *, wants_json: bool = False) -> int:
    """Build the backlog bulk-review artifact and write it to the store."""
    if not config.enabled or not config.store_dir:
        return _refuse_disabled(config, wants_json)

    contents = load_ledger(ledger_path_in(config.store_dir))
    corrections = load_corrections(corrections_path_in(config.store_dir))

    # THE INVOICE SIDE, THREADED AT THE PRODUCTION CALL SITE. A snapshot
    # kwarg that only tests ever pass is the standing trap: every pin green,
    # the feature dead in the field, and the operator told "nothing is late"
    # by a report that never opened the export. When no path resolves, the
    # snapshot stays None and the report SAYS the invoice side is absent —
    # which is a different statement from "nothing is late".
    snapshot = (
        load_snapshot(config.invoices_path) if config.invoices_path else None
    )
    if snapshot is None:
        log.info(
            "reconcile.cli.no_invoices_path",
            detail="reconcile.invoices_path is empty (no logging.dir to "
                   "derive it from, and none set explicitly), so the report "
                   "covers the statement side only — no late detection, no "
                   "payment matching.",
        )

    report = build_report(
        contents,
        eob_map=config.eob_classes,
        corrections=corrections,
        snapshot=snapshot,
    )
    stem = report_stem()
    csv_path, summary_path = write_report(
        report, reports_dir_in(config.store_dir), stem=stem
    )

    payload = {
        "ok": True,
        "csv_path": str(csv_path),
        "summary_path": str(summary_path),
        "total_lines": report.total_lines,
        "flagged_lines": report.flagged_lines,
        "class_counts": report.class_counts,
        "has_discrepancies": report.has_discrepancies,
        "proposals": [p.to_dict() for p in report.proposals],
        "invoices_path": config.invoices_path,
        "invoice_side": snapshot is not None,
        "invoices_stale": bool(snapshot.is_stale) if snapshot else None,
        "unknown_statuses": dict(snapshot.unknown_statuses) if snapshot else {},
        "late": len(report.aging.late) if report.aging else 0,
        "match_proposals": len(report.match.proposals) if report.match else 0,
        "match_ambiguous": len(report.match.ambiguous) if report.match else 0,
    }
    lines = [
        f"Wrote {csv_path}",
        f"Wrote {summary_path}",
        "",
        f"{report.total_lines} claim line(s), {report.flagged_lines} needing "
        f"attention.",
    ]
    if not report.total_lines:
        lines.append(
            "The ledger is empty — the report says so rather than rendering "
            "blank sections. Run 'alfred reconcile seed --note <path>' first."
        )
    elif not report.class_counts:
        lines.append(
            "No line carries an attention class: everything was paid as "
            "billed with no unrecognised EOB code."
        )
    else:
        for cls, n in report.class_counts.items():
            lines.append(f"  {cls}: {n}")
    if report.has_discrepancies:
        lines.append("")
        lines.append(
            "Cross-foot findings present — our sums disagree with the "
            "provider's own totals somewhere. See the summary's "
            "'Cross-foot findings' section; treat them as parse findings "
            "first."
        )

    lines.append("")
    if snapshot is None:
        lines.append(
            "No invoice export was read, so this report covers only what the "
            "provider ANSWERED. Late detection and payment matching need "
            "reconcile.invoices_path to resolve to RRTS's export."
        )
    else:
        lines.append(f"Invoice side: {snapshot.summary()}")
        if snapshot.unknown_statuses:
            total = sum(snapshot.unknown_statuses.values())
            lines.append(
                f"  {total} invoice(s) carry an unrecognised status "
                f"({', '.join(sorted(snapshot.unknown_statuses))}) — neither "
                f"chased nor matched. The report's 'Invoice side' section is "
                f"the only place they surface."
            )
        if report.aging is not None:
            lines.append(f"  {report.aging.summary()}")
        if report.match is not None:
            lines.append(f"  {report.match.summary()}")
    _emit(payload, wants_json, *lines)
    return 0


def cmd_status(config: ReconcileConfig, *, wants_json: bool = False) -> int:
    """What is in the ledger, and where it lives."""
    if not config.enabled or not config.store_dir:
        return _refuse_disabled(config, wants_json)

    ledger = ledger_path_in(config.store_dir)
    contents = load_ledger(ledger)
    corrections = load_corrections(corrections_path_in(config.store_dir))
    results = classify_all(
        contents.claim_lines,
        eob_map=config.eob_classes,
        corrections=corrections,
    )
    counts = class_counts(results)
    flagged = sum(1 for r in results if r.needs_attention)

    payload = {
        "ok": True,
        "instance": config.instance_name,
        "store_dir": config.store_dir,
        "ledger_path": str(ledger),
        "ledger_exists": ledger.is_file(),
        "statements": len(contents.statements),
        "claim_lines": len(contents.claim_lines),
        "subtotals": len(contents.subtotals),
        "corrections": len(corrections),
        "flagged": flagged,
        "class_counts": counts,
        "eob_codes_mapped": len(config.eob_classes),
        "date_order": config.date_order or "(unset — slash dates refused)",
    }
    lines = [
        f"Instance: {config.instance_name or '(unnamed)'}",
        f"Store:    {config.store_dir}",
    ]
    if not ledger.is_file():
        lines.append("")
        lines.append(
            "No ledger file yet. This is the pre-seed state, not a failure — "
            "run 'alfred reconcile seed --note <path>' to create it."
        )
    else:
        lines.append("")
        lines.append(
            f"{len(contents.statements)} statement(s), "
            f"{len(contents.claim_lines)} claim line(s), "
            f"{len(contents.subtotals)} subtotal row(s)."
        )
        lines.append(f"{flagged} line(s) need attention.")
        if counts:
            for cls, n in counts.items():
                lines.append(f"  {cls}: {n}")
        elif contents.claim_lines:
            lines.append(
                "  no attention classes — everything paid as billed."
            )
        lines.append(f"{len(corrections)} operator correction(s) recorded.")
    if not config.eob_classes:
        lines.append("")
        lines.append(
            "No EOB codes are mapped (reconcile.eob_classes is empty), so "
            "every coded line surfaces as unknown_eob. That is the intended "
            "starting state: unknown codes fail OPEN, and the bulk-review "
            "report is where the mapping gets built."
        )
    _emit(payload, wants_json, *lines)
    return 0


def cmd_correct(
    config: ReconcileConfig,
    *,
    line_key: str,
    classes: list[str],
    operator: str,
    note: str = "",
    wants_json: bool = False,
) -> int:
    """Record an operator ruling on one line's classification."""
    if not config.enabled or not config.store_dir:
        return _refuse_disabled(config, wants_json)
    if not (line_key or "").strip():
        msg = "--line-key is required (copy it from the report's line_key column)"
        _emit({"ok": False, "error": "missing_line_key"}, wants_json, msg)
        return 1
    if not (operator or "").strip():
        msg = "--operator is required — a ruling with no author is not evidence"
        _emit({"ok": False, "error": "missing_operator"}, wants_json, msg)
        return 1

    unknown = [c for c in classes if c not in ALL_CLASSES]
    if unknown:
        msg = (
            f"unknown attention class(es): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(ALL_CLASSES))}. "
            f"Pass no classes to rule a line CLEAR."
        )
        _emit({"ok": False, "error": "unknown_class", "unknown": unknown},
              wants_json, msg)
        return 1

    contents = load_ledger(ledger_path_in(config.store_dir))
    match = next(
        (c for c in contents.claim_lines if c.key == line_key), None
    )
    if match is None:
        msg = (
            f"no claim line in the ledger has key {line_key!r}. The ruling "
            f"was NOT recorded — a correction against a line that does not "
            f"exist would sit in the file teaching nothing."
        )
        _emit({"ok": False, "error": "unknown_line_key"}, wants_json, msg)
        return 1

    from .attention import split_eob_codes

    correction = Correction(
        line_key=line_key,
        classes=sorted(set(classes)),
        operator=operator.strip(),
        note=note.strip(),
        eob_codes=split_eob_codes(match.eob_code),
    )
    append_correction(corrections_path_in(config.store_dir), correction)

    payload = {
        "ok": True,
        "line_key": line_key,
        "classes": correction.classes,
        "operator": correction.operator,
        "eob_codes": correction.eob_codes,
    }
    lines = [
        f"Recorded: {line_key}",
        f"  classes:  {', '.join(correction.classes) or '(clear — no attention)'}",
        f"  operator: {correction.operator}",
    ]
    if correction.eob_codes:
        lines.append(
            f"  EOB code(s) {', '.join(correction.eob_codes)} noted — repeated "
            f"rulings on the same code become a PROPOSED mapping in the "
            f"report. Nothing is applied automatically."
        )
    else:
        lines.append(
            "  the line carries no EOB code, so this ruling teaches about "
            "this line only."
        )
    _emit(payload, wants_json, *lines)
    return 0


__all__ = [
    "cmd_correct",
    "cmd_render",
    "cmd_report",
    "cmd_seed",
    "cmd_status",
]
