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
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_pdf(path: str | pathlib.Path, allow_ocr: bool = False) -> list[dict]:
    """
    Extract text from a PDF, one dict per page.

    PyMuPDF (imported as `fitz`) processes one page at a time rather than
    loading the whole document into memory, which matters for large files.
    The document object is used as a context manager so it is closed
    automatically even if an exception is raised mid-way.

    Parameters
    ----------
    path : str | pathlib.Path
        Path to the PDF file on disk.
    allow_ocr : bool, default False
        When False (default): fast text extraction without OCR. If the document
        has little to no digital text, returns a needs_ocr flag so callers
        (like preview) know it is scanned without consuming Gemini vision quota.
        When True: pages with under 50 characters of digital text are rendered to
        in-memory PNGs at OCR_RENDER_DPI and transcribed using Gemini OCR.
    """
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(str(path)) as doc:
        for page_obj in doc:
            # get_text() returns an empty string on image-only (scanned) pages.
            text = page_obj.get_text()
            pages.append({"page": page_obj.number + 1, "text": text})

        # When OCR is not allowed (e.g. preview endpoint), return early to save quota
        if not allow_ocr:
            total_text = "".join(p["text"] for p in pages)
            if len(total_text.strip()) < 50:
                return [{"page": 1, "text": "", "needs_ocr": True}]
            return pages

        # ── OCR branch (allow_ocr=True) ──────────────────────────────────
        from app.config import OCR_MAX_PAGES, OCR_RENDER_DPI
        from app.ocr import ocr_image

        # Why check for images on low-text pages?
        # Blank pages and divider pages in normal text PDFs have < 50 characters
        # but contain no images. Sending them to OCR wastes quota and falsely triggers
        # the OCR_MAX_PAGES refusal on large digital PDFs.
        needs_ocr_indices = [
            i for i, p in enumerate(pages)
            if len(p["text"].strip()) < 50 and bool(doc[i].get_images(full=True))
        ]
        n = len(needs_ocr_indices)
        t = len(doc)

        if n > 0:
            if n > OCR_MAX_PAGES:
                # If mostly scanned (>= 50% of total pages), fail fast before wasting quota
                if n >= (t / 2):
                    raise ValueError(
                        f"This PDF has {n} pages that need OCR; the limit is {OCR_MAX_PAGES}."
                    )
                # Otherwise, it's a mostly-text PDF: OCR only up to the limit and skip the rest
                skipped = n - OCR_MAX_PAGES
                logger.warning(
                    "Skipping OCR for %d pages exceeding the limit of %d",
                    skipped,
                    OCR_MAX_PAGES,
                )
                pages_to_ocr = needs_ocr_indices[:OCR_MAX_PAGES]
            else:
                pages_to_ocr = needs_ocr_indices

            actual_ocr_count = len(pages_to_ocr)
            for ocr_idx, page_idx in enumerate(pages_to_ocr, start=1):
                page_obj = doc[page_idx]
                page_num = pages[page_idx]["page"]
                logger.info("OCR page %d of %d", ocr_idx, actual_ocr_count)

                # Why in-memory rendering?
                # PyMuPDF can render directly to raw PNG bytes in RAM, avoiding
                # disk I/O and temporary file management/cleanup.
                pix = page_obj.get_pixmap(dpi=OCR_RENDER_DPI)
                png_bytes = pix.tobytes("png")
                del pix  # free pixmap memory immediately

                try:
                    ocr_text = ocr_image(image_bytes=png_bytes, mime_type="image/png")
                except Exception as exc:
                    raise RuntimeError(f"OCR failed on page {page_num}: {exc}") from exc
                finally:
                    del png_bytes  # free image buffer immediately to keep memory usage low

                pages[page_idx]["text"] = ocr_text

    total_text = "".join(p["text"] for p in pages)
    if len(total_text.strip()) < 50:
        raise ValueError("No readable text found in this PDF")

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


def extract_text(path: str | pathlib.Path, file_type: str, allow_ocr: bool = False) -> list[dict]:
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
    if file_type.lower() == "pdf":
        return parser(path, allow_ocr=allow_ocr)
    return parser(path)
