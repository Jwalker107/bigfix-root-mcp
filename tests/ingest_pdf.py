"""Build tiny, valid PDFs for the ingest tests (one line of text per page)."""


def make_pdf(pages: list[str]) -> bytes:
    """A PDF whose page N shows pages[N-1] as a single line. An empty string is a blank page."""
    count = len(pages)
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(count))
    objects: dict[int, bytes] = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: f"<</Type/Pages/Kids[{kids}]/Count {count}>>".encode(),
        3: b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    }
    for index, text in enumerate(pages):
        page_number, content_number = 4 + 2 * index, 5 + 2 * index
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode()
        objects[page_number] = (
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents {content_number} 0 R"
            "/Resources<</Font<</F1 3 0 R>>>>>>"
        ).encode()
        objects[content_number] = (
            f"<</Length {len(stream)}>>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {size}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)
