import itertools

import pytest

from fantasy_mcp.analytics import (
    ReplacementModel,
    TeamAnalytics,
    ZScoreValuation,
    aggregate,
    assign,
    category_scarcity,
    eligible,
    punt_effects,
    stat_line,
)
from fantasy_mcp.domain import PlayerEligibility, RosterSlot, ValuationMode
from fantasy_mcp.errors import FantasyError


def test_ratio_aggregation_is_volume_weighted(settings, stats):
    a, b = stats[:2]
    a.values.update(FGM=1, FGA=2, FTM=1, FTA=1)
    b.values.update(FGM=9, FGA=10, FTM=1, FTA=9)
    profile = aggregate([a.player_key, b.player_key], {s.player_key: s for s in [a, b]}, settings)
    assert profile.values["FG%"] == pytest.approx(10 / 12)
    assert profile.values["FT%"] == pytest.approx(0.2)
    assert profile.values["FG%"] != pytest.approx(0.7)


def test_volume_impact_and_inverse_turnovers(settings, stats, config):
    stats[0].values.update(FTM=0.9, FTA=1, TO=1)
    stats[1].values.update(FTM=9, FTA=10, TO=4)
    result = ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
    v = {x.player_key: x for x in result.values}
    assert v[stats[1].player_key].categories["FT%"] > v[stats[0].player_key].categories["FT%"]
    assert v[stats[0].player_key].categories["TO"] > v[stats[1].player_key].categories["TO"]
    assert sum(x.categories["FT%"] for x in result.values) == pytest.approx(0, abs=1e-10)


def test_modes_not_collapsed_and_missing_not_zero(settings, stats, config):
    stats[0].basis = "totals"
    stats[0].values.update(GP=10, PTS=100)
    assert stat_line(stats[0], ValuationMode.PER_GAME)["PTS"] == 10
    assert stat_line(stats[0], ValuationMode.TOTAL)["PTS"] == 100
    with pytest.raises(FantasyError, match="projections"):
        stat_line(stats[0], ValuationMode.PROJECTED)
    stats[0].values["FGA"] = None
    result = ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
    assert "missing:FGA" in result.excluded[stats[0].player_key]
    profile = aggregate(
        [s.player_key for s in stats[:2]], {s.player_key: s for s in stats}, settings
    )
    assert profile.values["FG%"] is None
    assert aggregate([], {}, settings).values["FT%"] is None


def test_zero_deviation_and_punts(settings, stats, players, config):
    for s in stats:
        s.values["TO"] = 1
    result = ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
    assert all(v.categories["TO"] == 0 for v in result.values)
    engine = TeamAnalytics(settings, players, stats, result, config)
    effect = punt_effects(engine, [players[0].key], [["FT%", "AST"]])[0]
    v = next(v for v in result.values if v.player_key == players[0].key)
    assert effect.roster_value_delta == pytest.approx(-v.categories["FT%"] - v.categories["AST"])
    with pytest.raises(FantasyError, match="At least one"):
        ZScoreValuation(config).value(
            stats, settings, ValuationMode.PER_GAME, [c.key for c in settings.categories]
        )


def test_assignment_repositions_flexible_player(players):
    flexible, only_pg = players[:2]
    flexible.eligibility = PlayerEligibility(positions=["PG", "C"])
    only_pg.eligibility = PlayerEligibility(positions=["PG"])
    slots = [RosterSlot(position="PG", count=1), RosterSlot(position="C", count=1)]
    result = assign([flexible, only_pg], slots)
    assert result.legal
    assert result.assignments[flexible.key].startswith("C:")
    assert eligible(only_pg, "G")
    assert not eligible(only_pg, "F")
    assert not eligible(only_pg, "IL")
    only_pg.eligibility.positions.append("IL")
    assert eligible(only_pg, "IL")
    with pytest.raises(FantasyError, match="Duplicate"):
        assign([flexible, flexible], slots)


def test_replacement_matches_bruteforce_optimum(settings, players, stats, config):
    settings.max_teams = 1
    settings.roster_slots = [RosterSlot(position="PG", count=1), RosterSlot(position="C", count=1)]
    result = ZScoreValuation(config).value(stats[:6], settings, ValuationMode.PER_GAME, [])
    model = ReplacementModel(players[:6], result.values, settings)
    scores = {v.player_key: v.overall for v in result.values}
    optimum = max(
        sum(scores[p.key] for p in group)
        for group in itertools.combinations(players[:6], 2)
        if assign(list(group), settings.roster_slots).legal
    )
    assert sum(scores[p.key] for p in model.selected) == pytest.approx(optimum)
    assert model.evaluate(model.selected[0]).value_over_replacement is not None
    assert model.evaluate(model.unselected[-1]).value_over_replacement <= 0


def test_scarcity_and_trade_are_net_effects(settings, players, stats, config):
    result = ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
    engine = TeamAnalytics(settings, players, stats, result, config)
    roster = [players[0].key, players[1].key]
    candidate = players[4].key
    scarcity = category_scarcity(players, {candidate}, result, engine.stats, settings)
    assert scarcity["PTS"].available_count == 1
    assert scarcity["PTS"].remaining_production == stats[4].values["PTS"]
    analysis = engine.change(roster, [roster[0]], [candidate])
    assert analysis.deltas["PTS"] == pytest.approx(stats[4].values["PTS"] - stats[0].values["PTS"])
    assert engine.candidate(roster, candidate, scarcity).roster_fit.legal
    with pytest.raises(FantasyError, match="Outgoing"):
        engine.change(roster, [candidate], [players[5].key])


def test_repeated_slot_labels_have_distinct_capacity(players):
    slots = [RosterSlot(position="Util", count=1), RosterSlot(position="Util", count=1)]
    assert assign(players[:2], slots).legal


def test_duplicate_and_mixed_season_stats_fail(settings, stats, config):
    with pytest.raises(FantasyError, match="Duplicate"):
        ZScoreValuation(config).value([stats[0], stats[0]], settings, ValuationMode.PER_GAME, [])
    stats[0].season -= 1
    with pytest.raises(FantasyError, match="mix seasons"):
        ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
