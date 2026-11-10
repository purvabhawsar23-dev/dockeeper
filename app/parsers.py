"""
parsers.py — Extract plain text from uploaded documents.

Each parser returns a list of page dicts:
    {"page": int, "text": str}               ← pdf, txt, md
    {"page": int, "text": str}               ← docx  (single page)
    {"page": int, "section": str, "text": str} ← xlsx (one entry per sheet)

Keeping parsers in their own module makes them easy to test independently
and keeps the route handler focused on HTTP concerns, not file parsing.
"""

import pathlib


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_pdf(path: str | pathlib.Path) -> list[dict]:
    """
    Extract text from a PDF, one dict per page.

    PyMuPDF (imported as `fitz`) processes one page at a time rather than
    loading the whole document into memory, which matters for large files.
    The document object is used as a context manager so it is closed
    automatically even if an exception is raised mid-way.
    """
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(str(path)) as doc:
        for page_obj in doc:
            # get_text() returns an empty string on image-only (scanned) pages.
            text = page_obj.get_text()
            pages.append({"page": page_obj.number + 1, "text": text})

    # Scanned-PDF detection: if the whole document produced almost no text,
    # the PDF is likely image-based and needs OCR (not implemented yet).
    # Returning a special marker lets the caller show a helpful status
    # instead of storing empty chunks that would be useless for search.
    total_text = "".join(p["text"] for p in pages)
    if len(total_text.strip()) < 50:
        return [{"page": 1, "text": "", "needs_ocr": True}]

    return pages


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def extract_docx(path: str | pathlib.Path) -> list[dict]:
    """
    Extract text from a Word document.

    DOCX files have no concept of pages — pagination is decided by the
    rendering engine (Word, LibreOffice).  We treat the whole document as
    a single "page 1" so the rest of the app doesn't need a special case.

    We include both paragraph text and table cell text because important
    content is often stored in tables (e.g. invoices, CVs, comparison grids).
    """
    from docx import Document

    doc = Document(str(path))
    lines = []

    # Regular paragraphs (headings, body text, list items …)
    for para in doc.paragraphs:
        if para.text.strip():
            lines.append(para.text)

    # Table cells — iterate row by row, join cells with " | " for readability
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(
                cell.text.strip() for cell in row.cells if cell.text.strip()
            )
            if row_text:
                lines.append(row_text)

    return [{"page": 1, "text": "\n".join(lines)}]


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

def extract_xlsx(path: str | pathlib.Path) -> list[dict]:
    """
    Extract text from an Excel workbook, one dict per sheet.

    read_only=True tells openpyxl to stream cells rather than load the
    whole workbook into memory — important for large spreadsheets.

    Empty rows are skipped because Excel files often have lots of trailing
    blank rows that would add noise without useful content.
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    results = []

    for sheet_number, sheet_name in enumerate(wb.sheetnames, start=1):
        ws = wb[sheet_name]
        row_lines = []

        for row in ws.iter_rows(values_only=True):
            # Skip rows where every cell is None or empty
            cells = [str(cell) for cell in row if cell is not None and str(cell).strip()]
            if cells:
                row_lines.append(" | ".join(cells))

        results.append({
            "page": sheet_number,
            "section": sheet_name,
            "text": "\n".join(row_lines),
        })

    wb.close()
    return results


# ---------------------------------------------------------------------------
# TXT / MD
# ---------------------------------------------------------------------------

def extract_txt(path: str | pathlib.Path) -> list[dict]:
    """
    Extract text from a plain-text or Markdown file.

    Both .txt and .md are just text — no special parsing needed.
    We read as UTF-8 and treat the whole file as a single "page".
    errors="replace" prevents a crash if the file has a few bad bytes.
    """
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    return [{"page": 1, "text": text}]


# ---------------------------------------------------------------------------
# Images (PNG, JPG, JPEG, WEBP)
# ---------------------------------------------------------------------------

IMAGE_MIME_TYPES = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


def extract_image(path: str | pathlib.Path) -> list[dict]:
    """
    Extract text from an image file using Gemini multimodal OCR.

    Reads raw image bytes and sends them to ocr_image() in app/ocr.py.
    Images have no pages, so the whole image is treated as page 1.
    If the image contains no readable text, raises a clear error.
    """
    from app.ocr import ocr_image

    file_path = pathlib.Path(path)
    ext = file_path.suffix.lstrip(".").lower()
    mime_type = IMAGE_MIME_TYPES.get(ext)
    if not mime_type:
        raise ValueError(
            f"Unsupported image type '.{ext}'. "
            f"Supported: {', '.join(IMAGE_MIME_TYPES.keys())}"
        )

    image_bytes = file_path.read_bytes()
    text = ocr_image(image_bytes=image_bytes, mime_type=mime_type)

    if not text:
        raise ValueError("No readable text found in this image")

    return [{"page": 1, "text": text}]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

# Map file extensions to their parser function.
# A dict is cleaner than a long if/elif chain and makes it obvious at a glance
# which types are supported.
_PARSERS = {
    "pdf":  extract_pdf,
    "docx": extract_docx,
    "xlsx": extract_xlsx,
    "txt":  extract_txt,
    "md":   extract_txt,   # Markdown is plain text; reuse the txt parser
    "png":  extract_image,
    "jpg":  extract_image,
    "jpeg": extract_image,
    "webp": extract_image,
}

SUPPORTED_TYPES = list(_PARSERS.keys())


def extract_text(path: str | pathlib.Path, file_type: str) -> list[dict]:
    """
    Dispatch to the right parser based on file_type (e.g. "pdf", "docx").

    Raises ValueError for unsupported types so the caller can return
    a clean 400 error rather than a confusing AttributeError or KeyError.
    """
    parser = _PARSERS.get(file_type.lower())
    if parser is None:
        raise ValueError(
            f"Unsupported file type '{file_type}'. "
            f"Supported: {', '.join(SUPPORTED_TYPES)}"
        )
    return parser(path)
