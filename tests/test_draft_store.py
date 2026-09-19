from concurrent.futures import ThreadPoolExecutor

import pytest

from fantasy_mcp.domain import DraftState, Provenance
from fantasy_mcp.draft import DraftEngine, next_pick
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.persistence import Store


def state():
    return DraftState(
        draft_id="test",
        league_key="999.l.1",
        team_order=["a", "b", "c"],
        my_team="a",
        rounds=3,
        snake=True,
    )


def test_snake_and_linear():
    draft = state()
    assert [next_pick(draft, i)[2] for i in range(1, 10)] == list("abccbaabc")
    draft.snake = False
    assert [next_pick(draft, i)[2] for i in range(1, 10)] == list("abcabcabc")


def test_persist_undo_duplicate_correction_and_reset(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.create_draft(state())
    engine = DraftEngine(store)
    first = engine.record("test", "p1", expected_revision=0)
    assert first.revision == 1
    with pytest.raises(FantasyError, match="PLAYER_ALREADY_DRAFTED"):
        engine.record("test", "p1")
    with pytest.raises(FantasyError, match="DRAFT_CONFLICT"):
        engine.record("test", "p2", expected_revision=0)
    engine.record("test", "p2")
    corrected = engine.correct("test", 1, "p3", 2)
    assert corrected.picks[0].player_key == "p3"
    assert Store(store.path).load_draft("test").picks[0].player_key == "p3"
    undone = engine.undo("test")
    assert len(undone.picks) == 1
    assert not engine.reset("test", undone.revision).picks
    assert len(store.snapshots("draft:test")) == 6


def test_atomic_concurrent_duplicate_prevention(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.create_draft(state())

    def record():
        try:
            DraftEngine(Store(store.path)).record("test", "same")
            return "ok"
        except FantasyError as e:
            return e.code

    with ThreadPoolExecutor(2) as executor:
        outcomes = list(executor.map(lambda _: record(), range(2)))
    assert sorted(outcomes) == ["PLAYER_ALREADY_DRAFTED", "ok"]
    assert len(store.load_draft("test").picks) == 1


def test_cache_ttl_and_history(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.put("key", {"data": 1}, Provenance(source="test"), -1)
    value, provenance = store.get("key")
    assert value == {"data": 1}
    assert provenance.stale and provenance.cached
    store.put("key", {"data": 2}, Provenance(source="test"), 60)
    assert not store.get("key")[1].stale
    assert len(store.snapshots("key")) == 2
