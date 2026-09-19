"""Optional normalized file provider; never substitute forecasts for observed statistics."""

import asyncio
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import SecretStr

from fantasy_mcp.domain import (
    Model,
    NBAPlayer,
    PlayerStats,
    Provenance,
    Result,
    ScheduleGame,
    ScheduleInfo,
)
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.yahoo import YahooRepository


class PlayerDataProvider(Protocol):
    async def get_player(self, player_key: str) -> NBAPlayer: ...
    async def get_stats(self, player_keys: list[str], season: int) -> Result[list[PlayerStats]]: ...
    async def get_projections(
        self, player_keys: list[str], season: int
    ) -> Result[list[PlayerStats]]: ...
    async def get_schedule(self, season: int) -> Result[ScheduleInfo]: ...


class YahooPlayerProvider:
    def __init__(self, repo: YahooRepository):
        self.repo = repo

    async def get_player(self, player_key: str) -> NBAPlayer:
        players = await self.repo.players()
        for p in players.data:
            if p.key == player_key:
                return p
        raise FantasyError("PLAYER_NOT_FOUND", "Player key not found in this league season.")

    async def get_stats(self, player_keys: list[str], season: int) -> Result[list[PlayerStats]]:
        return await self.repo.stats(player_keys, season)

    async def get_projections(
        self, player_keys: list[str], season: int
    ) -> Result[list[PlayerStats]]:
        raise FantasyError(
            "DATA_PROVIDER_UNAVAILABLE",
            "No documented Yahoo projection feed configured. "
            "Import licensed projections or use per_game mode.",
        )

    async def get_schedule(self, season: int) -> Result[ScheduleInfo]:
        raise FantasyError(
            "DATA_PROVIDER_UNAVAILABLE",
            "No reliable schedule feed configured. Set FANTASY_PLAYER_DATA_FILE.",
        )


class ProviderFile(Model):
    schema_version: int = 1
    players: list[NBAPlayer] = []
    stats: list[PlayerStats] = []
    schedules: list[ScheduleInfo] = []


class FilePlayerProvider:
    """Explicit user-supplied provider export keyed by Yahoo identity, not fuzzy name matching."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> ProviderFile:
        try:
            data = ProviderFile.model_validate_json(self.path.read_text(encoding="utf-8"))
            if data.schema_version != 1:
                raise ValueError("Unsupported schema")
            ids = [(s.player_key, s.season, s.kind) for s in data.stats]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate stat lines")
            for schedule in data.schedules:
                if len({g.game_id for g in schedule.games}) != len(schedule.games):
                    raise ValueError("Duplicate schedule games")
            return data
        except (OSError, ValueError):
            raise FantasyError(
                "PROVIDER_FILE_INVALID", "Check normalized provider JSON and unique identities."
            ) from None

    async def get_player(self, player_key: str) -> NBAPlayer:
        for p in self.read().players:
            if p.key == player_key:
                return p
        raise FantasyError("PLAYER_NOT_FOUND", "Player not in provider export.")

    async def get_stats(self, player_keys: list[str], season: int) -> Result[list[PlayerStats]]:
        stats = [
            s
            for s in self.read().stats
            if s.player_key in player_keys and s.season == season and s.kind != "projection"
        ]
        return Result(data=stats, provenance=[s.provenance for s in stats])

    async def get_projections(
        self, player_keys: list[str], season: int
    ) -> Result[list[PlayerStats]]:
        stats = [
            s
            for s in self.read().stats
            if s.player_key in player_keys and s.season == season and s.kind == "projection"
        ]
        if not stats:
            raise FantasyError(
                "DATA_PROVIDER_UNAVAILABLE", "Export has no projections for this season."
            )
        return Result(data=stats, provenance=[s.provenance for s in stats])

    async def get_schedule(self, season: int) -> Result[ScheduleInfo]:
        for s in self.read().schedules:
            if s.season == season:
                return Result(data=s, provenance=[s.provenance])
        raise FantasyError("DATA_PROVIDER_UNAVAILABLE", "Export has no schedule for this season.")


class CompositePlayerProvider:
    def __init__(
        self,
        yahoo: PlayerDataProvider,
        optional: PlayerDataProvider | None,
        schedule_provider: "BDLScheduleProvider | None" = None,
    ):
        self.yahoo, self.optional = yahoo, optional
        self.schedule_provider = schedule_provider

    async def get_player(self, player_key: str) -> NBAPlayer:
        return await self.yahoo.get_player(player_key)

    async def get_stats(self, player_keys: list[str], season: int) -> Result[list[PlayerStats]]:
        return await self.yahoo.get_stats(player_keys, season)

    async def get_projections(
        self, player_keys: list[str], season: int
    ) -> Result[list[PlayerStats]]:
        return await (self.optional or self.yahoo).get_projections(player_keys, season)

    async def get_schedule(self, season: int) -> Result[ScheduleInfo]:
        if self.schedule_provider:
            return await self.schedule_provider.get_schedule(season)
        return await (self.optional or self.yahoo).get_schedule(season)


def schedule_volume(
    schedule: ScheduleInfo, players: list[NBAPlayer], start: date, end: date
) -> tuple[dict[str, int], dict[str, list[str]], dict[str, int]]:
    if start > end or (end - start).days > 31:
        raise FantasyError("INVALID_INPUT", "Schedule window must be ordered and at most 32 days.")
    if (
        schedule.coverage_start is None
        or schedule.coverage_end is None
        or start < schedule.coverage_start
        or end > schedule.coverage_end
    ):
        raise FantasyError(
            "INSUFFICIENT_DATA", "Schedule does not attest coverage of requested dates."
        )
    games = [g for g in schedule.games if start <= g.date <= end and g.status == "scheduled"]
    counts: dict[str, int] = {}
    back_to_backs: dict[str, list[str]] = {}
    for p in players:
        if not p.nba_team:
            continue  # Unknown NBA affiliation is unknown volume, not zero games.
        dates = sorted(g.date for g in games if p.nba_team in g.nba_teams)
        counts[p.key] = len(dates)
        back_to_backs[p.key] = [d.isoformat() for d in dates if d - timedelta(days=1) in dates]
    days = {
        d.isoformat(): sum(g.date == d for g in games)
        for d in [start + timedelta(days=i) for i in range((end - start).days + 1)]
    }
    return counts, back_to_backs, days


class BDLScheduleProvider:
    """Optional documented games API. No paid stats/projections endpoints are used."""

    def __init__(self, http: httpx.AsyncClient, api_key: SecretStr, repo: YahooRepository):
        self.http, self.api_key, self.repo = http, api_key, repo

    async def get_schedule(self, season: int) -> Result[ScheduleInfo]:
        from pydantic import TypeAdapter

        async def fetch() -> ScheduleInfo:
            games: dict[str, ScheduleGame] = {}
            cursor: int | None = None
            seen: set[int] = set()
            for _ in range(40):
                params = {"seasons[]": str(season), "per_page": "100", "postseason": "false"}
                if cursor is not None:
                    params["cursor"] = str(cursor)
                try:
                    response = await self.http.get(
                        "https://api.balldontlie.io/v1/games",
                        params=params,
                        headers={"Authorization": self.api_key.get_secret_value()},
                    )
                    if response.status_code in {401, 403}:
                        raise FantasyError(
                            "PROVIDER_AUTH_REQUIRED", "Verify BALLDONTLIE_API_KEY and plan access."
                        )
                    if response.status_code != 200:
                        raise FantasyError(
                            "DATA_PROVIDER_UNAVAILABLE", "Schedule API unavailable; retry later."
                        )
                    payload = response.json()
                    for raw in payload["data"]:
                        status = raw.get("status_state")
                        if status not in {"scheduled", "final", "postponed"}:
                            # Do not guess whether unknown/in-progress games remain playable.
                            continue
                        aliases = {"GSW": "GS", "NYK": "NY", "NOP": "NO", "SAS": "SA"}
                        teams = [
                            aliases.get(raw[t]["abbreviation"], raw[t]["abbreviation"])
                            for t in ["home_team", "visitor_team"]
                        ]
                        game = ScheduleGame(
                            game_id=str(raw["id"]),
                            date=date.fromisoformat(raw["date"][:10]),
                            nba_teams=teams,
                            status=status,
                        )
                        games[game.game_id] = game
                    cursor = payload.get("meta", {}).get("next_cursor")
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    raise FantasyError(
                        "DATA_PROVIDER_UNAVAILABLE", "Schedule response unavailable or invalid."
                    ) from None
                if cursor is None:
                    break
                if cursor in seen:
                    raise FantasyError(
                        "PROVIDER_RESPONSE_INVALID", "Schedule pagination did not advance."
                    )
                seen.add(cursor)
                # Public free-tier rate limits can be low; cache full schedule after bootstrap.
                await asyncio.sleep(12)
            else:
                raise FantasyError(
                    "PROVIDER_RESPONSE_INVALID", "Schedule pagination exceeded safe bound."
                )
            if not games:
                raise FantasyError(
                    "INSUFFICIENT_DATA", "Schedule for this NBA season is not published."
                )
            return ScheduleInfo(
                season=season,
                games=list(games.values()),
                coverage_start=min(g.date for g in games.values()),
                coverage_end=max(g.date for g in games.values()),
                provenance=Provenance(
                    source="balldontlie",
                    season=season,
                    warnings=[
                        "Coverage spans published games; future schedule changes are possible."
                    ],
                ),
            )

        return await self.repo.cached(
            f"bdl_schedule:{season}",
            TypeAdapter(ScheduleInfo),
            21600,
            fetch,
            season=season,
            source="balldontlie",
        )
