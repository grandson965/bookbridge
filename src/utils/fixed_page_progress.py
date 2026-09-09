"""Helpers for page-based documents carried through KoSync.

KOReader stores ``progress`` as a page number for documents with ``has_pages``
(e.g. CBZ), while reflowable EPUB documents store an XPointer.  BookBridge's
normal ebook locator pipeline is EPUB/text based, so page-based documents need
a small marker that lets the sync clients bypass that parser path while the
sync manager continues to pass a LocatorResult between clients.
"""

from pathlib import Path
from typing import Optional
import zipfile


FIXED_PAGE_LOCATOR_CFI = "bookbridge:fixed-page"
FIXED_PAGE_TEXT_MARKER = "__bookbridge_fixed_page__"

_CBZ_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
}


def is_cbz_filename(filename: Optional[str]) -> bool:
    """Return True when *filename* identifies a CBZ archive."""
    if not filename:
        return False
    return Path(str(filename)).suffix.lower() == ".cbz"


def coerce_page(value) -> Optional[int]:
    """Convert a KoSync page value to a positive integer when possible."""
    if value in (None, ""):
        return None
    try:
        page = int(float(value))
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def marker_text(page: Optional[int] = None) -> str:
    """Return non-empty text used to bypass the EPUB text resolver for CBZ."""
    page = coerce_page(page)
    return f"{FIXED_PAGE_TEXT_MARKER}:{page or ''}"


def page_from_marker_text(text: Optional[str]) -> Optional[int]:
    if not isinstance(text, str) or not text.startswith(FIXED_PAGE_TEXT_MARKER):
        return None
    _, _, raw_page = text.partition(":")
    return coerce_page(raw_page)


def count_cbz_pages(ebook_parser, filename: Optional[str]) -> Optional[int]:
    """Return the number of image pages in a CBZ without EPUB parsing."""
    if not is_cbz_filename(filename) or ebook_parser is None:
        return None

    try:
        candidate = Path(str(filename))
        path = candidate if candidate.exists() else ebook_parser.resolve_book_path(filename)
        if path is None:
            return None
        with zipfile.ZipFile(path) as archive:
            page_count = sum(
                1
                for info in archive.infolist()
                if not info.is_dir()
                and Path(info.filename).suffix.lower() in _CBZ_IMAGE_EXTENSIONS
            )
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return None

    return page_count if page_count > 0 else None


def percentage_from_cbz_page(ebook_parser, filename: Optional[str], page) -> Optional[float]:
    """Return canonical 0..1 progress for a 1-based CBZ page."""
    page = coerce_page(page)
    page_count = count_cbz_pages(ebook_parser, filename)
    if page is None or not page_count:
        return None
    page = max(1, min(page_count, page))
    return page / float(page_count)


def estimate_cbz_page(ebook_parser, filename: Optional[str], percentage: float) -> Optional[int]:
    """Estimate a 1-based CBZ page from a 0..1 percentage."""
    page_count = count_cbz_pages(ebook_parser, filename)
    if not page_count:
        return None

    try:
        pct = max(0.0, min(1.0, float(percentage)))
    except (TypeError, ValueError):
        return None

    page = int(round(pct * page_count))
    return max(1, min(page_count, page))
