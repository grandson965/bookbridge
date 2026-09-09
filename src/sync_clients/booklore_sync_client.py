import os
from typing import Optional
import logging

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.utils.fixed_page_progress import (
    FIXED_PAGE_LOCATOR_CFI,
    is_cbz_filename,
    marker_text,
    page_from_marker_text,
)
from src.utils.progress_metadata import parse_service_timestamp
from src.sync_clients.sync_client_interface import (
    LocatorResult,
    SyncClient,
    SyncResult,
    UpdateProgressRequest,
    ServiceState,
)

logger = logging.getLogger(__name__)

class BookloreSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_supported_sync_types(self) -> set:
        """Grimmory participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    @staticmethod
    def _resolve_epub_filename(book: Book) -> Optional[str]:
        return getattr(book, "original_ebook_filename", None) or getattr(book, "ebook_filename", None)

    def _is_cbz_book(self, book: Book) -> bool:
        return is_cbz_filename(self._resolve_epub_filename(book))

    def supports_book(self, book: Book) -> bool:
        epub = self._resolve_epub_filename(book)
        if not epub:
            return False

        # An explicit ebook source is authoritative. Match Grimmory regardless of
        # the tag variant it was saved under ('BookLore'/'Booklore'/'Grimmory');
        # never hijack a book explicitly owned by another ebook source (e.g.
        # BookOrbit), even if Grimmory hosts the same file.
        src = (getattr(book, "ebook_source", None) or "").strip().lower()
        if src:
            return src in ("booklore", "grimmory")

        # Otherwise (legacy/unsourced) only participate when the ebook can actually
        # be resolved against the Grimmory library cache.
        target = self.booklore_client.find_book_by_filename(epub, allow_refresh=False)
        return bool(target)

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # Prefer the original filename for tri-links, then fall back to the standard filename.
        epub = self._resolve_epub_filename(book)

        # Rich read when available (adds href + Grimmory's own lastReadTime and
        # readStatus); non-dict results (older/mocked clients) fall back to the
        # classic (pct, cfi) tuple.
        rich = None
        if hasattr(self.booklore_client, "get_progress_rich"):
            candidate = self.booklore_client.get_progress_rich(epub)
            if isinstance(candidate, dict):
                rich = candidate
        if rich is not None:
            bl_pct, bl_cfi = rich.get("pct"), rich.get("cfi")
        else:
            bl_pct, bl_cfi = self.booklore_client.get_progress(epub)

        if bl_pct is None:
            logger.debug("Grimmory percentage is None - returning no service state")
            return None

        # Get previous BookLore state
        prev_booklore_pct = prev_state.percentage if prev_state else 0

        delta = abs(bl_pct - prev_booklore_pct)

        current = {"pct": bl_pct, "cfi": bl_cfi}
        if rich is not None:
            if rich.get("href"):
                current["href"] = rich["href"]
            if rich.get("page") is not None:
                current["page"] = rich["page"]
            service_updated_at = parse_service_timestamp(rich.get("last_read_time"))
            if service_updated_at is not None:
                current["service_updated_at"] = service_updated_at
            if rich.get("status"):
                current["status"] = rich["status"]

        return ServiceState(
            current=current,
            previous_pct=prev_booklore_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("Grimmory", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        # CBZ is a fixed-page format. It has no EPUB text/CFI to resolve. Return a
        # private marker so SyncManager can continue its normal locator handoff
        # without sending the archive through EbookParser.
        if self._is_cbz_book(book):
            return marker_text(state.current.get("page"))

        bl_pct = state.current.get('pct')
        bl_cfi = state.current.get('cfi')
        epub = self._resolve_epub_filename(book)
        if bl_cfi and epub and self.ebook_parser:
            txt = self.ebook_parser.get_text_around_cfi(epub, bl_cfi)
            if txt:
                return txt
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def get_locator_from_text(self, txt: str, epub_file_name: str, hint_percentage: float) -> Optional[LocatorResult]:
        if is_cbz_filename(epub_file_name) and isinstance(txt, str) and txt.startswith("__bookbridge_fixed_page__"):
            page = page_from_marker_text(txt)
            return LocatorResult(
                percentage=hint_percentage,
                # A private non-EPUB marker prevents SyncManager from attempting
                # CFI hydration for Grimmory. update_progress strips it before the
                # Grimmory write.
                cfi=FIXED_PAGE_LOCATOR_CFI,
                fragment=str(page) if page is not None else None,
            )
        return super().get_locator_from_text(txt, epub_file_name, hint_percentage)

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        # Prefer the original filename for updates too.
        epub = self._resolve_epub_filename(book)
        pct = request.locator_result.percentage
        locator = request.locator_result

        # Grimmory's CBX progress endpoint is percentage/page based. Never pass
        # the private fixed-page marker as though it were an EPUB CFI.
        if is_cbz_filename(epub):
            page = page_from_marker_text(getattr(request, "txt", None))
            if page is None and getattr(locator, "cfi", None) == FIXED_PAGE_LOCATOR_CFI:
                page = getattr(locator, "fragment", None)
            locator = LocatorResult(
                percentage=pct,
                fragment=str(page) if page is not None else None,
            )

        success = self.booklore_client.update_progress(epub, pct, locator)
        if success:
            try:
                from src.services.write_tracker import record_write
                record_write('BookLore', book.abs_id, pct)
            except ImportError:
                pass
        updated_state = {
            'pct': pct
        }
        if not is_cbz_filename(epub) and locator and locator.cfi:
            updated_state['cfi'] = locator.cfi
        return SyncResult(pct, success, updated_state)