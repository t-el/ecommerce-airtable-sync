from conftest import airtable_rows

from ecom_sync.models import Order
from ecom_sync.normalize import from_shopify
from ecom_sync.reconcile import reconcile
from mock_apis import fixtures


def test_backfill_gumroad_paginates_and_upserts(engine, mock):
    state, _ = mock
    rep = engine.backfill("gumroad")
    c = rep.counts
    assert c["created"] == 3 and c["updated"] == 3 and c["dead_lettered"] == 1   # JPY not a select option
    assert airtable_rows(state, "gumroad:gS3cT3")[0]["fields"]["Status"] == "refunded"   # missed refund healed
    assert airtable_rows(state, "gumroad:gS2bR2")[0]["fields"]["Gross"] == 49.0          # manual typo healed
    assert engine.store.get_cursor("gumroad") == "2026-11-16T11:30:00Z"


def test_backfill_shopify_survives_503_on_page_two(engine, mock, clock):
    state, http = mock
    http.post("/admin/faults", json={"shopify_503": 2})
    rep = engine.backfill("shopify")
    assert rep.counts["created"] == 4
    assert state.page2_calls == 2 and len(clock.sleeps) >= 2


def test_duplicate_rows_in_airtable_go_to_dlq_not_crash(engine, mock):
    rep = engine.backfill("shopify")
    dl = [d for d in engine.store.dead_letters() if d["order_key"] == "shopify:5512000002"]
    assert dl and "more than one record" in dl[0]["error"]
    assert rep.counts["dead_lettered"] == 1


def test_second_run_is_idempotent_and_writes_nothing(engine, mock):
    state, _ = mock
    engine.backfill("gumroad")
    rows_before, reqs_before = len(state.airtable), engine.airtable.requests
    rep = engine.backfill("gumroad", since=None)
    assert rep.counts.get("unchanged") == 6 and "created" not in rep.counts
    assert len(state.airtable) == rows_before
    # only the still-broken JPY order is retried; the 6 good ones cost zero Airtable requests
    assert engine.airtable.requests == reqs_before + 1


def test_out_of_order_shopify_update_is_ignored(engine, mock):
    state, _ = mock
    newer = fixtures.SHOPIFY_ORDERS[2] | {"financial_status": "refunded", "updated_at": "2026-11-18T10:00:00Z",
                                         "refunds": [{"transactions": [{"kind": "refund", "amount": "45.50"}]}]}
    engine.handle_raw("shopify", newer)
    rep = engine.handle_raw("shopify", fixtures.SHOPIFY_ORDERS[2])       # the older "paid" event arrives late
    assert rep.outcomes[0].outcome == "stale"
    assert airtable_rows(state, "shopify:5512000003")[0]["fields"]["Status"] == "refunded"


def test_gumroad_refund_never_downgraded_by_late_sale_ping(engine, mock):
    sale = fixtures.GUMROAD_SALES[6]
    engine.handle_raw("gumroad", sale | {"refunded": True})
    assert engine.handle_raw("gumroad", sale).outcomes[0].outcome == "stale"


def test_invalid_payload_dead_lettered_and_replayable(engine, mock):
    bad = fixtures.SHOPIFY_ORDERS[5] | {"email": None}
    assert engine.handle_raw("shopify", bad).outcomes[0].outcome == "invalid"
    item = engine.store.dead_letters()[0]
    assert item["step"] == "normalize" and item["payload"]["raw"]["id"] == 5512000006


def test_replay_after_fix_resolves_dead_letter(engine, mock):
    state, http = mock
    engine.backfill("gumroad")
    dl = engine.store.dead_letters()[0]
    assert dl["order_key"] == "gumroad:gS5eV5"
    http.post("/admin/airtable/select-option", json={"field": "Currency", "option": "JPY"})   # client adds option
    rep = engine.replay(dl["id"])
    assert rep.counts == {"created": 1}
    assert engine.store.get_dead_letter(dl["id"])["status"] == "resolved"


def test_repeated_failures_bump_attempts_instead_of_duplicating(engine, mock):
    engine.backfill("gumroad")
    engine.backfill("gumroad", since=None)
    items = engine.store.dead_letters()
    assert len(items) == 1 and items[0]["attempts"] == 2


def test_order_round_trip_through_dlq_payload():
    o = from_shopify(fixtures.SHOPIFY_ORDERS[1])
    assert Order.from_dict(o.to_dict()).fingerprint() == o.fingerprint()


def test_reconcile_finds_every_kind_of_drift(engine, mock, tmp_path):
    rep = reconcile(engine, out_dir=tmp_path)
    assert len(rep.missing) == 8
    assert {m.order_key for m in rep.mismatched} == {"gumroad:gS2bR2", "gumroad:gS3cT3", "shopify:5512000002"}
    assert list(rep.duplicates) == ["shopify:5512000002"]
    assert list(rep.orphaned) == ["shopify:5512000999"]
    assert (tmp_path / "reconciliation-report.md").read_text().startswith("# Reconciliation report")


def test_reconcile_fix_then_clean_except_human_decisions(engine, mock):
    state, http = mock
    reconcile(engine, fix=True)
    http.post("/admin/airtable/select-option", json={"field": "Currency", "option": "JPY"})
    http.delete("/admin/airtable/records/recB2chris00002b")
    for d in engine.store.dead_letters():
        engine.replay(d["id"])
    rep = reconcile(engine)
    assert not rep.missing and not rep.mismatched and not rep.duplicates
    assert list(rep.orphaned) == ["shopify:5512000999"]      # never auto-deleted
