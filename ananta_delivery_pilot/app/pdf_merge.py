from __future__ import annotations

from io import BytesIO
from typing import Iterable


class PdfMergeError(RuntimeError):
    pass


def merge_pdfs(pdf_documents: Iterable[bytes]) -> bytes:
    try:
        from pypdf import PdfReader, PdfWriter
    except ModuleNotFoundError as error:
        raise PdfMergeError(
            "PDF merging requires the `pypdf` package. Install it with: python3 -m pip install -r requirements.txt"
        ) from error

    writer = PdfWriter()
    doc_count = 0
    for pdf_bytes in pdf_documents:
        if not pdf_bytes:
            continue
        doc_count += 1
        reader = PdfReader(BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)

    if doc_count == 0:
        raise PdfMergeError("No PDF parts provided to merge.")

    output = BytesIO()
    writer.write(output)
    return output.getvalue()

