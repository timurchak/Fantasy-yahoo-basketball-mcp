from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from fantasy_mcp.domain import Provenance, ScheduleGame, ScheduleInfo
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.oauth import OAuth
from fantasy_mcp.persistence import Store
from fantasy_mcp.providers import (
    BDLScheduleProvider,
    FilePlayerProvider,
    ProviderFile,
    schedule_volume,
)
from fantasy_mcp.yahoo import YahooClient, YahooRepository


def schedule():
    return ScheduleInfo(
        season=2026,
        coverage_start=date(2026, 10, 20),
        coverage_end=date(2026, 10, 26),
        provenance=Provenance(source="synthetic_schedule"),
        games=[
            ScheduleGame(game_id="1", date=date(2026, 10, 20), nba_teams=["BOS", "NYK"]),
            ScheduleGame(game_id="2", date=date(2026, 10, 21), nba_teams=["BOS", "LAL"]),
            ScheduleGame(
                game_id="3", date=date(2026, 10, 23), nba_teams=["NYK", "CHI"], status="postponed"
            ),
        ],
    )


def test_schedule_volume_and_coverage(players):
    volume, b2b, days = schedule_volume(schedule(), players, date(2026, 10, 20), date(2026, 10, 26))
    assert volume[players[0].key] == 2
    assert volume[players[1].key] == 1
    assert b2b[players[0].key] == ["2026-10-21"]
    assert days["2026-10-23"] == 0
    with pytest.raises(FantasyError, match="coverage"):
        schedule_volume(schedule(), players, date(2026, 10, 18), date(2026, 10, 26))


async def test_file_projection_identity_and_schedule(tmp_path, players, stats):
    for s in stats:
        s.kind = "projection"
    path = tmp_path / "provider.json"
    path.write_text(
        ProviderFile(players=players, stats=stats, schedules=[schedule()]).model_dump_json()
    )
    provider = FilePlayerProvider(path)
    assert (await provider.get_projections([players[0].key], 2026)).data[0].player_key == players[
        0
    ].key
    assert (await provider.get_schedule(2026)).data.season == 2026
    with pytest.raises(FantasyError):
        await provider.get_projections(["not-a-player"], 2026)


async def test_schedule_adapter_uses_documented_api_and_caches(config):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "date": "2026-10-20",
                        "status_state": "scheduled",
                        "home_team": {"abbreviation": "NYK"},
                        "visitor_team": {"abbreviation": "BOS"},
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        repo = YahooRepository(
            YahooClient(OAuth(config, http), http), Store(config.data_dir / "db.sqlite"), config
        )
        provider = BDLScheduleProvider(http, SecretStr("test-schedule-key"), repo)
        result = await provider.get_schedule(2026)
        assert result.data.games[0].nba_teams == ["NY", "BOS"]
        assert calls[0].url.path == "/v1/games"
        assert calls[0].url.params["seasons[]"] == "2026"
        assert (await provider.get_schedule(2026)).provenance[0].cached
        assert len(calls) == 1


async def test_weekly_and_scheduled_net_change(service, tmp_path, stats):
    from fantasy_mcp.providers import CompositePlayerProvider, YahooPlayerProvider

    path = tmp_path / "schedule.json"
    path.write_text(ProviderFile(schedules=[schedule()]).model_dump_json())
    service.provider = CompositePlayerProvider(
        YahooPlayerProvider(service.repo), FilePlayerProvider(path)
    )
    start, end = date(2026, 10, 20), date(2026, 10, 26)
    matchup = await service.weekly(start, end, opponent_key="999.l.30166.t.2")
    assert matchup.data.our_remaining_games == 3
    assert matchup.data.probabilities is None
    streamers = await service.weekly(start, end, streaming=True)
    assert streamers.data.candidates
    assert streamers.data.acquisitions_remaining == 3
    change = await service.change(["999.p.1"], ["999.p.5"], is_add=True, start=start, end=end)
    assert change.data.schedule_deltas["PTS"] == pytest.approx(
        2 * (stats[4].values["PTS"] - stats[0].values["PTS"])
    )


def test_daily_slot_capacity_is_not_all_scheduled_players(settings, players, stats, config):
    from fantasy_mcp.analytics import TeamAnalytics, ZScoreValuation, active_game_counts
    from fantasy_mcp.domain import RosterSlot, ValuationMode

    settings.roster_slots = [RosterSlot(position="Util", count=1)]
    valuation = ZScoreValuation(config).value(stats, settings, ValuationMode.PER_GAME, [])
    engine = TeamAnalytics(settings, players, stats, valuation, config)
    counts = active_game_counts(
        engine, [p.key for p in players[:4]], schedule(), date(2026, 10, 20), date(2026, 10, 26)
    )
    assert sum(counts.values()) == 2  # one active slot on each of two scheduled days
