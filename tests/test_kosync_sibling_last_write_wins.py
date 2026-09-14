import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.api import kosync_server
from src.sync_clients.kosync_sync_client import KoSyncSyncClient
from src.utils.config_loader import ALL_SETTINGS, DEFAULT_CONFIG

USER_ID = 7
OTHER_USER_ID = 8
CANONICAL = "a" * 32
SIBLING_A = "b" * 32
SIBLING_B = "c" * 32


class FakeDatabase:
    def __init__(self, state, rows):
        self.state = state
        self.rows = rows
        self.saved_state = None

    def get_states_for_book(self, _abs_id):
        return [self.state] if self.state is not None else []

    def get_state(self, _abs_id, _client_name, *, user_id):
        return self.state if getattr(self.state, "user_id", user_id) == user_id else None

    def get_user_kosync_progress_for_book(self, _abs_id, user_id):
        return [row for row in self.rows if row.user_id == user_id]

    def get_kosync_documents_for_book(self, _abs_id):
        return self.rows

    def save_state(self, state):
        self.saved_state = state
        self.state = state
        return state


class FakeKoSyncApi:
    def is_configured(self):
        return True

    def get_progress_with_metadata(self, _doc_id):
        return 0.5, "/body/DocFragment[1]/p.0", {}


def make_book(*, filename="book.epub"):
    return SimpleNamespace(
        abs_id="book-1",
        abs_title="Sibling Test",
        kosync_doc_id=CANONICAL,
        sync_mode="ebook",
        ebook_filename=filename,
        original_ebook_filename=filename,
    )


def make_state(
    pct,
    progress,
    *,
    authoritative_at=None,
    last_updated=None,
    locator=None,
    user_id=USER_ID,
):
    metadata = dict(locator or {})
    if authoritative_at is not None:
        metadata["kosync_authoritative_put_at"] = authoritative_at
    return SimpleNamespace(
        client_name="kosync",
        percentage=pct,
        xpath=progress,
        cfi="",
        timestamp=int(authoritative_at) if authoritative_at is not None else None,
        last_updated=(
            last_updated
            if last_updated is not None
            else int(authoritative_at) if authoritative_at is not None else time.time()
        ),
        locator_json=json.dumps(metadata) if metadata else None,
        user_id=user_id,
    )


def make_row(document_hash, pct, progress, timestamp, *, user_id=USER_ID):
    naive_utc = datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
    return SimpleNamespace(
        document_hash=document_hash,
        percentage=pct,
        progress=progress,
        timestamp=naive_utc,
        device="reader",
        device_id=f"device-{document_hash[0]}",
        user_id=user_id,
    )


@pytest.fixture(autouse=True)
def clean_recent_puts(monkeypatch):
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()
    monkeypatch.setenv("KOSYNC_XPATH_ORDER_ENABLED", "false")
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "600")
    monkeypatch.setenv("KOSYNC_SIBLING_LAST_WRITE_WINS", "true")
    yield
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()


def respond(db, *, user_id=USER_ID, book=None):
    app = Flask(__name__)
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        with app.test_request_context("/"):
            g.kosync_user_id = user_id
            response, status = kosync_server._respond_from_book_states(
                CANONICAL, book or make_book()
            )
            return response.get_json(), status
    finally:
        kosync_server._database_service = old_db


def test_recent_lower_sibling_put_beats_higher_sibling_after_marker_loss():
    now = time.time()
    db = FakeDatabase(make_state(0.9137, "page-42", authoritative_at=now), [
        make_row(SIBLING_A, 1.0, "page-58", now - 30.0),
        make_row(SIBLING_B, 0.9137, "page-42", now),
    ])

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.9137)
    assert data["progress"] == "page-42"


def test_newer_synced_state_overrides_old_recent_put():
    now = time.time()
    put_at = now - 10.0
    db = FakeDatabase(
        make_state(0.40, "synced-40", authoritative_at=put_at, last_updated=now),
        [make_row(SIBLING_A, 0.09, "reader-9", put_at)],
    )
    kosync_server._record_recent_external_kosync_put(
        SIBLING_A, "reader-a", "device-a", 0.09, put_at, USER_ID
    )

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.40)
    assert data["progress"] == "synced-40"
    assert "_bridge_recent_external_put" not in data


def test_authoritative_lower_position_survives_recent_put_ttl(monkeypatch):
    cutoff = time.time() - 5.0
    db = FakeDatabase(make_state(0.9137, "page-42", authoritative_at=cutoff), [
        make_row(SIBLING_A, 1.0, "page-58", cutoff - 30.0),
        make_row(SIBLING_B, 0.9137, "page-42", cutoff),
    ])
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "1")

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.9137)
    assert data["progress"] == "page-42"


def test_feature_flag_restores_furthest_wins(monkeypatch):
    now = time.time()
    db = FakeDatabase(make_state(0.50, "reader-50", authoritative_at=now), [
        make_row(SIBLING_A, 0.90, "reader-90", now - 30.0),
        make_row(SIBLING_B, 0.50, "reader-50", now),
    ])
    monkeypatch.setenv("KOSYNC_SIBLING_LAST_WRITE_WINS", "false")

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.90)
    assert data["progress"] == "reader-90"


def test_stale_lower_sibling_without_fresh_put_is_pulled_forward():
    now = time.time()
    db = FakeDatabase(make_state(0.80, "canonical-80"), [
        make_row(SIBLING_A, 0.40, "stale-40", now - 30.0),
    ])

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)
    assert data["progress"] == "canonical-80"


def test_recent_put_cutoff_is_user_scoped():
    now = time.time()
    db = FakeDatabase(make_state(0.80, "user-a-current"), [
        make_row(SIBLING_A, 0.80, "user-a-current", now, user_id=USER_ID),
        make_row(SIBLING_B, 0.30, "other-user-row", now, user_id=OTHER_USER_ID),
    ])

    data, status = respond(db, user_id=USER_ID)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)
    assert data["progress"] == "user-a-current"


def test_legacy_unstamped_sibling_uses_persisted_user_cutoff():
    now = time.time()
    db = FakeDatabase(make_state(0.35, "legacy-35", authoritative_at=now), [
        make_row(SIBLING_A, 0.90, "old-90", now - 30.0, user_id=None),
        make_row(SIBLING_B, 0.35, "legacy-35", now, user_id=None),
    ])

    data, status = respond(db)

    assert status == 200
    assert data["percentage"] == pytest.approx(0.35)
    assert data["progress"] == "legacy-35"


def test_external_cbz_put_merges_existing_locator_metadata():
    cutoff = datetime(2026, 7, 15, 12, 34, 56)
    previous = make_state(
        0.50,
        "41",
        locator={
            "page": 41,
            "kosync_approved_rewind_at": 1234.5,
            "future_metadata": "keep-me",
        },
    )
    db = FakeDatabase(previous, [])
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        kosync_server._record_user_kosync_state(
            make_book(filename="book.cbz"), 0.55, "42", cutoff, USER_ID
        )
    finally:
        kosync_server._database_service = old_db

    locator = json.loads(db.saved_state.locator_json)
    expected = cutoff.replace(tzinfo=timezone.utc).timestamp()
    assert locator == {
        "future_metadata": "keep-me",
        "kosync_approved_rewind_at": 1234.5,
        "kosync_authoritative_put_at": expected,
        "page": 41,
    }
    assert db.saved_state.percentage == pytest.approx(0.55)
    assert db.saved_state.xpath == "42"


def test_kosync_client_carries_authoritative_cutoff_from_persisted_state():
    cutoff = time.time() - 3.0
    prev_state = make_state(0.5, "/body/DocFragment[1]/p.0", authoritative_at=cutoff)
    client = KoSyncSyncClient(FakeKoSyncApi(), SimpleNamespace())

    service_state = client.get_service_state(make_book(), prev_state)

    assert service_state.current["kosync_authoritative_put_at"] == pytest.approx(cutoff)


def test_setting_is_registered_defaulted_and_rendered():
    assert "KOSYNC_SIBLING_LAST_WRITE_WINS" in ALL_SETTINGS
    assert DEFAULT_CONFIG["KOSYNC_SIBLING_LAST_WRITE_WINS"] == "true"
    template = (Path(__file__).parent.parent / "templates" / "settings.html").read_text(
        encoding="utf-8"
    )
    assert 'name="KOSYNC_SIBLING_LAST_WRITE_WINS"' in template
    assert "Prefer the Latest Reader Action" in template
