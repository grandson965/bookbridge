import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.api import kosync_server
from src.sync_clients.kosync_sync_client import KoSyncSyncClient

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
    def get_user_kosync_progress_for_book(self, _abs_id, user_id):
        return [row for row in self.rows if row.user_id == user_id]
    def get_kosync_documents_for_book(self, _abs_id):
        return []
    def save_state(self, state):
        self.saved_state = state
        return state

class FakeKoSyncApi:
    def is_configured(self):
        return True
    def get_progress_with_metadata(self, _doc_id):
        return 0.5, "/body/DocFragment[1]/p.0", {}

def make_book():
    return SimpleNamespace(
        abs_id="book-1", abs_title="Sibling Test", kosync_doc_id=CANONICAL,
        sync_mode="ebook", ebook_filename="book.epub", original_ebook_filename="book.epub",
    )

def make_state(pct, progress, *, authoritative_at=None):
    locator = {"kosync_authoritative_put_at": authoritative_at} if authoritative_at else {}
    return SimpleNamespace(
        client_name="kosync", percentage=pct, xpath=progress, cfi="",
        timestamp=authoritative_at, last_updated=authoritative_at or time.time(),
        locator_json=json.dumps(locator) if locator else None,
    )

def make_row(document_hash, pct, progress, timestamp, *, user_id=USER_ID):
    return SimpleNamespace(
        document_hash=document_hash, percentage=pct, progress=progress,
        timestamp=datetime.fromtimestamp(timestamp), device="reader",
        device_id=f"device-{document_hash[0]}", user_id=user_id,
    )

@pytest.fixture(autouse=True)
def clean_recent_puts(monkeypatch):
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()
    monkeypatch.setenv("KOSYNC_XPATH_ORDER_ENABLED", "false")
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "600")
    yield
    with kosync_server._kosync_recent_external_puts_lock:
        kosync_server._kosync_recent_external_puts.clear()

def respond(db, *, user_id=USER_ID):
    app = Flask(__name__)
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        with app.test_request_context("/"):
            g.kosync_user_id = user_id
            response, status = kosync_server._respond_from_book_states(CANONICAL, make_book())
            return response.get_json(), status
    finally:
        kosync_server._database_service = old_db

def test_recent_lower_sibling_put_beats_higher_sibling():
    now = time.time()
    db = FakeDatabase(make_state(0.9137, "page-42", authoritative_at=now), [
        make_row(SIBLING_A, 1.0, "page-58", now),
        make_row(SIBLING_B, 0.9137, "page-42", now),
    ])
    kosync_server._record_recent_external_kosync_put(
        SIBLING_B, "reader-b", "device-b", 0.9137, time.time(), USER_ID
    )
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.9137)
    assert data["progress"] == "page-42"
    assert data["_bridge_recent_external_put"] is True

def test_authoritative_lower_position_survives_recent_put_ttl(monkeypatch):
    cutoff = time.time() - 5.0
    db = FakeDatabase(make_state(0.9137, "page-42", authoritative_at=cutoff), [
        make_row(SIBLING_A, 1.0, "page-58", cutoff - 0.25),
        make_row(SIBLING_B, 0.9137, "page-42", cutoff),
    ])
    monkeypatch.setenv("KOSYNC_RECENT_EXTERNAL_PUT_SECONDS", "1")
    kosync_server._record_recent_external_kosync_put(
        SIBLING_B, "reader-b", "device-b", 0.9137, time.time() - 10.0, USER_ID
    )
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.9137)
    assert data["progress"] == "page-42"
    assert "_bridge_recent_external_put" not in data

def test_stale_lower_sibling_without_fresh_put_is_pulled_forward():
    now = time.time()
    db = FakeDatabase(make_state(0.80, "canonical-80"), [
        make_row(SIBLING_A, 0.40, "stale-40", now - 30.0),
    ])
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)
    assert data["progress"] == "canonical-80"

def test_recent_forward_sibling_put_still_wins():
    now = time.time()
    db = FakeDatabase(make_state(0.70, "canonical-70", authoritative_at=now), [
        make_row(SIBLING_A, 0.70, "page-40", now),
        make_row(SIBLING_B, 0.85, "page-49", now),
    ])
    kosync_server._record_recent_external_kosync_put(
        SIBLING_B, "reader-b", "device-b", 0.85, time.time(), USER_ID
    )
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.85)
    assert data["progress"] == "page-49"

def test_recent_put_marker_is_user_scoped():
    now = time.time()
    db = FakeDatabase(make_state(0.80, "user-a-current"), [
        make_row(SIBLING_A, 0.80, "user-a-current", now, user_id=USER_ID),
        make_row(SIBLING_B, 0.30, "other-user-row", now, user_id=OTHER_USER_ID),
    ])
    kosync_server._record_recent_external_kosync_put(
        SIBLING_B, "reader-b", "device-b", 0.30, time.time(), OTHER_USER_ID
    )
    data, status = respond(db, user_id=USER_ID)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.80)
    assert data["progress"] == "user-a-current"

def test_cbz_style_page_and_percentage_stay_coherent():
    now = time.time()
    db = FakeDatabase(make_state(0.72, "42", authoritative_at=now), [
        make_row(SIBLING_A, 1.0, "58", now),
        make_row(SIBLING_B, 0.72, "42", now),
    ])
    kosync_server._record_recent_external_kosync_put(
        SIBLING_B, "reader-b", "device-b", 0.72, time.time(), USER_ID
    )
    data, status = respond(db)
    assert status == 200
    assert data["percentage"] == pytest.approx(0.72)
    assert data["progress"] == "42"

def test_external_put_state_persists_authoritative_cutoff():
    cutoff = time.time() - 2.0
    db = FakeDatabase(None, [])
    old_db = kosync_server._database_service
    kosync_server._database_service = db
    try:
        kosync_server._record_user_kosync_state(
            make_book(), 0.55, "page-32", datetime.fromtimestamp(cutoff), USER_ID
        )
    finally:
        kosync_server._database_service = old_db
    locator = json.loads(db.saved_state.locator_json)
    assert locator["kosync_authoritative_put_at"] == pytest.approx(cutoff)
    assert db.saved_state.percentage == pytest.approx(0.55)
    assert db.saved_state.xpath == "page-32"

def test_kosync_client_carries_authoritative_cutoff_from_persisted_state():
    cutoff = time.time() - 3.0
    prev_state = make_state(0.5, "/body/DocFragment[1]/p.0", authoritative_at=cutoff)
    client = KoSyncSyncClient(FakeKoSyncApi(), SimpleNamespace())
    service_state = client.get_service_state(make_book(), prev_state)
    assert service_state.current["kosync_authoritative_put_at"] == pytest.approx(cutoff)
