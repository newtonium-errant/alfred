"""Real, minimal PDFs for the #57 ingest tests.

GENERATED rather than committed as binaries, and that is a deliberate choice:
a checked-in PDF is an opaque blob a reviewer cannot read, cannot diff, and
cannot adjust — "trust me, this one is encrypted". These builders emit genuine
PDF bytes (pypdf parses them, and the extraction path under test is the real
one), while the *reason each fixture is the shape it is* stays legible in
source.

Every shape here was measured against pypdf 6.13.0 during the #57 recon; the
observed behaviour is recorded beside each builder so a future pypdf bump that
changes one fails a test with the expectation written down next to it.
"""

from __future__ import annotations

import io


def build_pdf(objects: list[bytes], root_num: int = 1) -> bytes:
    """Assemble numbered PDF objects into a file with a correct xref table.

    Hand-rolled because the alternative is a reportlab-sized dependency for a
    handful of test files. Offsets are computed rather than hardcoded, so
    editing a builder above cannot silently produce a file with a broken xref
    that pypdf then "repairs" — which would make a corrupt-input test pass for
    the wrong reason.
    """
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root {root_num} 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def text_layer_pdf(lines: list[str] | None = None) -> bytes:
    """A PDF with a real text layer — the path that SUCCEEDS.

    Measured: ``extract_text()`` returns the lines joined by newlines.
    """
    lines = lines if lines is not None else [
        "Statement of Account", "Opening balance 1,204.55",
        "Direct debit  ACME UTILITIES  84.20",
    ]
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        ops.append(f"({esc}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode()
    return build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def scanned_pdf() -> bytes:
    """A page with graphics but NO text operators — the SCANNED shape.

    This is what a photographed or scanned document looks like to a text
    extractor: the page parses fine, and ``extract_text()`` returns ''. Drawing
    a filled rectangle rather than embedding an actual raster keeps the fixture
    small while producing the identical extraction outcome — the code under
    test branches on "no text came out", not on what the page contained.
    """
    stream = b"0.5 0.5 0.5 rg\n100 100 400 300 re\nf\n"
    return build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ])


def encrypted_pdf(password: str = "secret") -> bytes:
    """A password-protected PDF.

    Measured: ``PdfReader`` constructs fine and ``is_encrypted`` is True, but
    the first page access raises ``FileNotDecryptedError`` — which is where the
    extractor classifies it. Built by re-writing the text-layer fixture through
    ``PdfWriter.encrypt`` so it is genuinely encrypted, not merely flagged.
    """
    import pypdf

    writer = pypdf.PdfWriter(clone_from=io.BytesIO(text_layer_pdf()))
    writer.encrypt(password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def corrupt_pdf() -> bytes:
    """A valid PDF truncated mid-file. Measured: ``PdfStreamError``."""
    whole = text_layer_pdf()
    return whole[: len(whole) // 2]


def not_a_pdf() -> bytes:
    """Bytes that were never a PDF — the mis-picked-file case."""
    return b"Dear Andrew,\n\nthis is an email body, not a PDF.\n" * 3


def empty_pdf() -> bytes:
    """Zero bytes. Measured: ``EmptyFileError``."""
    return b""


def oversize_text_pdf(lines: int = 400) -> bytes:
    """A text-layer PDF UNDER the byte cap but OVER the character cap.

    That combination is the point — it proves the two limits are separate axes
    rather than one number wearing two hats, and it is what lets the two
    refusals be told apart.

    Note the direction, because the obvious guess is wrong: extraction SHRINKS
    a PDF rather than growing it (measured at ``lines=400``: 22,980 bytes in,
    19,199 characters out — the file carries structure the text does not). So
    this fixture clears the byte cap comfortably while still exceeding the much
    smaller per-test character cap the route is configured with.
    """
    return text_layer_pdf(
        [f"Line {n:04d} of a long statement with padding text" for n in range(lines)]
    )
