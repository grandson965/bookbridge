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


def estimate_cbz_page(ebook_parser, filename: Optional[str], percentage: float) -> Optional[int]:
    """Estimate a CBZ page from percentage by counting image entries.

    This is used when Grimmory is the leader. Grimmory exposes a CBX percentage
    to BookBridge, while KoSync requires the concrete page number for KOReader's
    ``GotoPage`` path. Counting images gives us the same page domain without
    attempting to parse the CBZ as an EPUB.
    """
    if not is_cbz_filename(filename) or ebook_parser is None:
        return None

    try:
        path = ebook_parser.resolve_book_path(filename)
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

    if page_count <= 0:
        return None

    try:
        pct = max(0.0, min(1.0, float(percentage)))
    except (TypeError, ValueError):
        return None

    # KOReader reports page-based progress close to page / total-pages. Keep the
    # result in the 1..N range for a non-zero reading position.
    page = int(round(pct * page_count))
    return max(1, min(page_count, page))
