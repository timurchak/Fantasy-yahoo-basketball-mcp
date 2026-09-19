from pathlib import Path

import pytest

from fantasy_mcp.config import Config
from fantasy_mcp.domain import (
    FantasyManager,
    FantasyRoster,
    FantasyTeam,
    League,
    LeagueSettings,
    Matchup,
    NBAPlayer,
    PlayerEligibility,
    PlayerOwnership,
    PlayerStats,
    Provenance,
    Result,
    RosterEntry,
    RosterSlot,
    ScoringCategory,
    Standing,
    Transaction,
)
from fantasy_mcp.persistence import Store
from fantasy_mcp.providers import YahooPlayerProvider
from fantasy_mcp.service import Service


@pytest.fixture
def settings() -> LeagueSettings:
    return LeagueSettings(
        scoring_type="head",
        max_teams=2,
        max_weekly_adds=4,
        categories=[
            ScoringCategory(
                key=c,
                name=c,
                lower_is_better=c == "TO",
                numerator={"FG%": "FGM", "FT%": "FTM"}.get(c),
                denominator={"FG%": "FGA", "FT%": "FTA"}.get(c),
            )
            for c in ["FG%", "FT%", "3PTM", "PTS", "REB", "AST", "ST", "BLK", "TO"]
        ],
        roster_slots=[
            RosterSlot(position="PG", count=1),
            RosterSlot(position="C", count=1),
            RosterSlot(position="Util", count=1),
            RosterSlot(position="BN", count=1, kind="bench"),
        ],
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(_env_file=None, data_dir=tmp_path, min_games=1, min_minutes=0)


@pytest.fixture
def players() -> list[NBAPlayer]:
    return [
        NBAPlayer(
            key=f"999.p.{i}",
            yahoo_id=str(i),
            name=f"Synthetic Player {i}",
            nba_team="BOS" if i % 2 else "NYK",
            eligibility=PlayerEligibility(positions=["PG", "SG"] if i % 2 else ["PF", "C"]),
            ownership=PlayerOwnership(
                availability="rostered" if i <= 4 else "free_agent",
                team_key=f"999.l.30166.t.{1 if i <= 2 else 2}" if i <= 4 else None,
            ),
        )
        for i in range(1, 21)
    ]


@pytest.fixture
def stats(players: list[NBAPlayer]) -> list[PlayerStats]:
    return [
        PlayerStats(
            player_key=p.key,
            season=2026,
            basis="per_game",
            values={
                "FGM": 3 + i * 0.2,
                "FGA": 7 + i * 0.35,
                "FTM": 1 + i * 0.1,
                "FTA": 2 + i * 0.12,
                "3PTM": 1 + i * 0.08,
                "PTS": 10 + i * 0.4,
                "REB": 3 + (i % 5),
                "AST": 2 + (i % 7),
                "ST": 0.5 + i * 0.03,
                "BLK": 0.3 + (i % 6) * 0.2,
                "TO": 1 + i * 0.04,
                "GP": 50,
                "MIN": 25,
            },
            provenance=Provenance(source="synthetic_test_fixture", season=2026),
        )
        for i, p in enumerate(players)
    ]


class FixtureRepository:
    """Deliberately synthetic, used only by tests. Not a production/demo fallback."""

    def __init__(
        self, settings: LeagueSettings, players: list[NBAPlayer], stats: list[PlayerStats]
    ):
        self._settings, self._players, self._stats = settings, players, stats

    @staticmethod
    def result(data):
        return Result(data=data, provenance=[Provenance(source="synthetic_test_fixture")])

    async def league(self, force: bool = False) -> Result[League]:
        return self.result(
            League(
                key="999.l.30166",
                league_id="30166",
                game_key="999",
                season=2026,
                name="Synthetic Test League",
            )
        )

    async def leagues(self, force: bool = False) -> Result[list[League]]:
        return self.result([(await self.league()).data])

    async def settings(self, force: bool = False) -> Result[LeagueSettings]:
        return self.result(self._settings)

    async def players(self, force: bool = False) -> Result[list[NBAPlayer]]:
        return self.result(self._players)

    async def stats(self, player_keys, season, force=False, on_date=None):
        return self.result([s for s in self._stats if s.player_key in player_keys])

    async def teams(self, force: bool = False) -> Result[list[FantasyTeam]]:
        return self.result(
            [
                FantasyTeam(
                    key=f"999.l.30166.t.{i}",
                    name=f"Test team {i}",
                    managers=[FantasyManager(manager_id=str(i), is_current_login=i == 1)],
                    weekly_adds_used=1,
                )
                for i in [1, 2]
            ]
        )

    async def my_team(self):
        return "999.l.30166.t.1"

    async def roster(
        self, team_key: str, force: bool = False, on_date=None
    ) -> Result[FantasyRoster]:
        group = self._players[:2] if team_key.endswith(".1") else self._players[2:4]
        return self.result(
            FantasyRoster(team_key=team_key, entries=[RosterEntry(player=p) for p in group])
        )

    async def standings(self, force: bool = False) -> Result[list[Standing]]:
        return self.result([])

    async def matchups(self, week: int | None = None, force: bool = False) -> Result[list[Matchup]]:
        return self.result([])

    async def transactions(self, limit: int = 100, offset: int = 0) -> Result[list[Transaction]]:
        return self.result([])


@pytest.fixture
def service(settings, players, stats, config):
    repo = FixtureRepository(settings, players, stats)
    return Service(repo, YahooPlayerProvider(repo), Store(config.data_dir / "test.sqlite3"), config)
