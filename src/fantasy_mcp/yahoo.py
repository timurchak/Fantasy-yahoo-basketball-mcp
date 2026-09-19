"""Read-only transport and centralized XML DTO boundary. No XML outside this module."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Literal, TypeVar
from xml.etree.ElementTree import Element

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import TypeAdapter

from fantasy_mcp.config import Config
from fantasy_mcp.domain import (
    DraftPick,
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
    PlayerStatus,
    Provenance,
    Result,
    RosterEntry,
    RosterSlot,
    ScoringCategory,
    Standing,
    Transaction,
    now,
)
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.oauth import OAuth
from fantasy_mcp.persistence import Store

log = logging.getLogger("fantasy_mcp.yahoo")
API = "https://fantasysports.yahooapis.com/fantasy/v2/"
U = TypeVar("U")


def parse_xml(content: bytes) -> Element:
    try:
        root = fromstring(content)
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        if root.tag != "fantasy_content":
            raise ValueError("Unexpected root")
        return root
    except Exception:
        raise FantasyError(
            "PROVIDER_RESPONSE_INVALID", "Yahoo returned invalid Fantasy XML."
        ) from None


def txt(node: Element, path: str, default: str = "") -> str:
    return node.findtext(path, default=default) or default


def number(value: str) -> float | None:
    try:
        result = float(value.replace(",", ""))
        return result if result == result and abs(result) != float("inf") else None
    except ValueError:
        return None


def integer(value: str) -> int | None:
    n = number(value)
    return int(n) if n is not None else None


def required(node: Element, path: str) -> str:
    value = txt(node, path)
    if not value:
        raise FantasyError("PROVIDER_RESPONSE_INVALID", f"Yahoo response lacks {path}.")
    return value


def key(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]+(?:\.[lpt]\.\d+)*", value):
        raise FantasyError("INVALID_INPUT", "Invalid Yahoo resource key.")
    return value


# Normalize labels from authoritative game stat_categories, never presume seasonal IDs.
ALIASES = {
    "FG%": "FG%",
    "FT%": "FT%",
    "3PTM": "3PTM",
    "3PM": "3PTM",
    "3PT": "3PTM",
    "PTS": "PTS",
    "REB": "REB",
    "AST": "AST",
    "ST": "ST",
    "STL": "ST",
    "BLK": "BLK",
    "TO": "TO",
    "TOV": "TO",
    "GP": "GP",
    "MIN": "MIN",
    "FGM": "FGM",
    "FGA": "FGA",
    "FTM": "FTM",
    "FTA": "FTA",
    "FGM/A": "FGM/A",
    "FTM/A": "FTM/A",
}


def stat_map(root: Element) -> dict[str, str]:
    return {
        required(s, "stat_id"): ALIASES.get(txt(s, "display_name"), txt(s, "display_name"))
        for s in root.findall(".//stat_categories/stats/stat")
    }


def parse_league(node: Element) -> League:
    league_key = required(node, "league_key")
    return League(
        key=league_key,
        league_id=required(node, "league_id"),
        game_key=league_key.split(".")[0],
        season=int(required(node, "season")),
        name=required(node, "name"),
        current_week=integer(txt(node, "current_week")),
        draft_status=txt(node, "draft_status") or None,
    )


def parse_settings(root: Element, league: League, mapping: dict[str, str]) -> LeagueSettings:
    node = root.find(".//settings")
    if node is None:
        raise FantasyError("PROVIDER_RESPONSE_INVALID", "Yahoo returned no league settings.")
    cats = []
    for stat in node.findall("stat_categories/stats/stat"):
        if txt(stat, "enabled") == "0":
            continue
        sid = required(stat, "stat_id")
        label = mapping.get(sid, txt(stat, "display_name", f"unknown:{sid}"))
        display_flags = stat.findall("stat_position_types/stat_position_type/is_only_display_stat")
        if txt(stat, "is_only_display_stat") == "1" or (
            display_flags and all(flag.text == "1" for flag in display_flags)
        ):
            continue
        ratio = {"FG%": ("FGM", "FGA"), "FT%": ("FTM", "FTA")}.get(label)
        # Yahoo sort_order: 0 is ascending (lower is better), 1 descending.
        order = txt(stat, "sort_order")
        cats.append(
            ScoringCategory(
                key=label,
                name=txt(stat, "name", label),
                yahoo_stat_id=sid,
                lower_is_better=order == "0" if order else label == "TO",
                numerator=ratio[0] if ratio else None,
                denominator=ratio[1] if ratio else None,
            )
        )
    slots = []
    for slot in node.findall("roster_positions/roster_position"):
        pos = required(slot, "position")
        slots.append(
            RosterSlot(
                position=pos,
                count=int(required(slot, "count")),
                kind="bench"
                if pos == "BN"
                else "injury"
                if pos in {"IL", "IL+", "IR"}
                else "active",
            )
        )
    league_node = root.find(".//league")
    teams = integer(txt(node, "num_teams"))
    if teams is None and league_node is not None:
        teams = integer(txt(league_node, "num_teams"))
    if teams is None:
        raise FantasyError("PROVIDER_RESPONSE_INVALID", "Yahoo settings lack number of teams.")
    rules: dict[str, str | int | float | bool | None] = {c.tag: c.text for c in node if len(c) == 0}
    weekly_cap = integer(txt(node, "max_weekly_adds"))
    return LeagueSettings(
        scoring_type=required(node, "scoring_type"),
        categories=cats,
        roster_slots=slots,
        max_teams=teams,
        max_weekly_adds=weekly_cap if weekly_cap is not None and weekly_cap >= 0 else None,
        draft_type=txt(node, "draft_type") or None,
        rules=rules,
    )


def parse_team(node: Element) -> FantasyTeam:
    return FantasyTeam(
        key=required(node, "team_key"),
        name=required(node, "name"),
        weekly_adds_used=(
            integer(txt(node, "roster_adds/value"))
            if txt(node, "roster_adds/coverage_type") == "week"
            else None
        ),
        weekly_adds_week=integer(txt(node, "roster_adds/coverage_value")),
        is_owned_by_current_login=txt(node, "is_owned_by_current_login") == "1",
        managers=[
            FantasyManager(
                manager_id=txt(m, "manager_id"),
                nickname=txt(m, "nickname") or None,
                is_current_login=txt(m, "is_current_login") == "1",
            )
            for m in node.findall("managers/manager")
        ],
    )


def parse_player(node: Element) -> NBAPlayer:
    ownership = node.find("ownership")
    owner = PlayerOwnership()
    if ownership is not None:
        raw = txt(ownership, "ownership_type")
        owner = PlayerOwnership(
            availability={"team": "rostered", "freeagents": "free_agent", "waivers": "waivers"}.get(
                raw, "unknown"
            ),  # type: ignore[arg-type]
            team_key=txt(ownership, "owner_team_key") or None,
            waiver_date=txt(ownership, "waiver_date") or None,
        )
    owner.percent_owned = number(txt(node, "percent_owned/value"))
    positions = [p.text for p in node.findall("eligible_positions/position") if p.text]
    return NBAPlayer(
        key=required(node, "player_key"),
        yahoo_id=required(node, "player_id"),
        name=required(node, "name/full"),
        nba_team=txt(node, "editorial_team_abbr") or None,
        eligibility=PlayerEligibility(positions=positions),
        ownership=owner,
        status=PlayerStatus(
            code=txt(node, "status") or None,
            description=txt(node, "status_full") or None,
            injury_eligible=True if {"IL", "IL+", "IR"}.intersection(positions) else None,
        ),
        adp=number(txt(node, "draft_analysis/average_pick")),
    )


def parse_stats(
    node: Element,
    season: int,
    mapping: dict[str, str],
    prov: Provenance,
    kind: Literal["current", "historical"] = "current",
) -> PlayerStats:
    values: dict[str, float | None] = {}
    for stat in node.findall("player_stats/stats/stat"):
        label = mapping.get(txt(stat, "stat_id"))
        if not label:
            continue
        raw = txt(stat, "value")
        if label in {"FGM/A", "FTM/A"}:
            parts = raw.split("/")
            prefix = label[:2]
            values[prefix + "M"] = number(parts[0]) if len(parts) == 2 else None
            values[prefix + "A"] = number(parts[1]) if len(parts) == 2 else None
        else:
            values[label] = number(raw)
    return PlayerStats(
        player_key=required(node, "player_key"),
        season=season,
        values=values,
        kind=kind,
        provenance=prov.model_copy(
            update={
                "season": season,
                "kind": kind,
                "sample_size": int(values["GP"] or 0) if values.get("GP") is not None else None,
            }
        ),
    )


class YahooClient:
    def __init__(self, oauth: OAuth, http: httpx.AsyncClient):
        self.oauth, self.http = oauth, http
        self.semaphore = asyncio.Semaphore(4)

    async def get(self, path: str) -> Element:
        # Path is constructed by repository methods; no arbitrary URL tool is exposed.
        if path.startswith("/") or "://" in path or ".." in path:
            raise FantasyError("INVALID_INPUT", "Invalid Yahoo endpoint.")
        token = await self.oauth.access_token()
        async with self.semaphore:
            start = time.perf_counter()
            for attempt in range(4):
                try:
                    response = await self.http.get(
                        API + path,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/xml"},
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 3:
                        raise FantasyError(
                            "DATA_PROVIDER_UNAVAILABLE", "Yahoo network request failed."
                        ) from None
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                log.info(
                    "yahoo_request",
                    extra={
                        "status": response.status_code,
                        "elapsed_ms": round((time.perf_counter() - start) * 1000),
                        "attempt": attempt,
                    },
                )
                if response.status_code == 401 and attempt == 0:
                    token = await self.oauth.access_token(rejected=token)
                    continue
                if response.status_code == 403:
                    raise FantasyError(
                        "YAHOO_ACCESS_DENIED",
                        "Check Yahoo Fantasy app approval, permissions, and account league access.",
                    )
                if response.status_code == 401:
                    raise FantasyError(
                        "AUTH_REQUIRED", "Yahoo rejected authorization. Run auth again."
                    )
                if response.status_code == 404:
                    raise FantasyError(
                        "RESOURCE_NOT_FOUND", "Yahoo resource does not exist in this season."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 3:
                        break
                    delay = number(response.headers.get("Retry-After", ""))
                    await asyncio.sleep(
                        min(10, max(0, delay if delay is not None else 0.5 * 2**attempt))
                    )
                    continue
                if response.status_code != 200:
                    raise FantasyError(
                        "YAHOO_REQUEST_FAILED",
                        f"Yahoo returned HTTP {response.status_code}; verify endpoint support.",
                    )
                return parse_xml(response.content)
        raise FantasyError(
            "DATA_PROVIDER_UNAVAILABLE", "Yahoo rate limited or temporarily unavailable."
        )


class YahooRepository:
    def __init__(self, client: YahooClient, store: Store, config: Config):
        self.client, self.store, self.config = client, store, config
        self.locks: dict[str, asyncio.Lock] = {}

    async def cached(
        self,
        cache_key: str,
        adapter: TypeAdapter[U],
        ttl: int,
        fetch: Callable[[], Awaitable[U]],
        force: bool = False,
        season: int | None = None,
        source: str = "yahoo",
    ) -> Result[U]:
        async with self.locks.setdefault(cache_key, asyncio.Lock()):
            cached = self.store.get(cache_key)
            if cached and not force and not cached[1].stale:
                log.debug("cache_hit")
                return Result(data=adapter.validate_python(cached[0]), provenance=[cached[1]])
            log.debug("cache_miss")
            try:
                data = await fetch()
            except FantasyError as error:
                if (
                    cached
                    and error.code == "DATA_PROVIDER_UNAVAILABLE"
                    and (now() - cached[1].retrieved_at).total_seconds()
                    <= self.config.stale_max_age
                ):
                    prov = cached[1]
                    prov.stale = True
                    prov.warnings.append("STALE_DATA: Yahoo unavailable; refresh before acting.")
                    return Result(data=adapter.validate_python(cached[0]), provenance=[prov])
                raise
            prov = Provenance(source=source, season=season or self.config.season)
            self.store.put(cache_key, adapter.dump_python(data, mode="json"), prov, ttl)
            return Result(data=data, provenance=[prov])

    async def leagues(self, force: bool = False) -> Result[list[League]]:
        async def fetch() -> list[League]:
            root = await self.client.get(
                f"users;use_login=1/games;game_codes=nba;seasons={self.config.season}/leagues"
            )
            return [parse_league(node) for node in root.findall(".//league")]

        return await self.cached(
            f"leagues:{self.config.season}",
            TypeAdapter(list[League]),
            self.config.settings_ttl,
            fetch,
            force,
        )

    async def league(self, force: bool = False) -> Result[League]:
        if self.config.league_key:

            async def fetch() -> League:
                root = await self.client.get(f"league/{key(self.config.league_key or '')}")
                node = root.find(".//league")
                if node is None:
                    raise FantasyError("LEAGUE_NOT_FOUND", "Yahoo returned no league.")
                league = parse_league(node)
                if league.season != self.config.season:
                    raise FantasyError("SEASON_MISMATCH", "League key belongs to another season.")
                return league

            return await self.cached(
                f"league:{self.config.league_key}",
                TypeAdapter(League),
                self.config.settings_ttl,
                fetch,
                force,
            )
        leagues = await self.leagues(force)
        matches = [league for league in leagues.data if league.league_id == self.config.league_id]
        if len(matches) != 1:
            raise FantasyError(
                "LEAGUE_NOT_FOUND",
                "No unique matching NBA league for configured season/account. "
                "Run leagues or set league key.",
            )
        self.store.set_meta(
            f"league_key:{self.config.season}:{self.config.league_id}", matches[0].key
        )
        return Result(data=matches[0], provenance=leagues.provenance)

    async def mapping(self, game_key: str) -> Result[dict[str, str]]:
        async def fetch() -> dict[str, str]:
            return stat_map(await self.client.get(f"game/{key(game_key)}/stat_categories"))

        return await self.cached(
            f"stat_map:{game_key}", TypeAdapter(dict[str, str]), self.config.historical_ttl, fetch
        )

    async def settings(self, force: bool = False) -> Result[LeagueSettings]:
        league = (await self.league()).data

        async def fetch() -> LeagueSettings:
            root, mapping = await asyncio.gather(
                self.client.get(f"league/{key(league.key)}/settings"), self.mapping(league.game_key)
            )
            return parse_settings(root, league, mapping.data)

        return await self.cached(
            f"settings:{league.key}",
            TypeAdapter(LeagueSettings),
            self.config.settings_ttl,
            fetch,
            force,
        )

    async def teams(self, force: bool = False) -> Result[list[FantasyTeam]]:
        league = (await self.league()).data

        async def fetch() -> list[FantasyTeam]:
            root = await self.client.get(f"league/{key(league.key)}/teams")
            teams = [parse_team(n) for n in root.findall(".//teams/team")]
            for team in teams:
                if league.current_week is not None and team.weekly_adds_week != league.current_week:
                    team.weekly_adds_used = None
            return teams

        return await self.cached(
            f"teams:{league.key}",
            TypeAdapter(list[FantasyTeam]),
            self.config.ownership_ttl,
            fetch,
            force,
        )

    async def my_team(self) -> str:
        teams = (await self.teams()).data
        if self.config.team_key:
            matches = [t for t in teams if t.key == self.config.team_key]
        else:
            matches = [
                t
                for t in teams
                if t.is_owned_by_current_login or any(m.is_current_login for m in t.managers)
            ]
        if len(matches) != 1:
            raise FantasyError(
                "TEAM_NOT_IDENTIFIED", "Set FANTASY_TEAM_KEY to your team from get_teams."
            )
        self.store.set_meta("my_team:" + matches[0].key.rsplit(".t.", 1)[0], matches[0].key)
        return matches[0].key

    async def roster(
        self, team_key: str, force: bool = False, on_date: date | None = None
    ) -> Result[FantasyRoster]:
        async def fetch() -> FantasyRoster:
            suffix = f";date={on_date.isoformat()}" if on_date else ""
            root = await self.client.get(f"team/{key(team_key)}/roster{suffix}/players")
            return FantasyRoster(
                team_key=team_key,
                date=on_date,
                entries=[
                    RosterEntry(
                        player=parse_player(p),
                        selected_position=txt(p, "selected_position/position") or None,
                    )
                    for p in root.findall(".//roster/players/player")
                ],
            )

        return await self.cached(
            f"roster:{team_key}:{on_date or 'current'}",
            TypeAdapter(FantasyRoster),
            self.config.ownership_ttl,
            fetch,
            force,
        )

    async def players(self, force: bool = False) -> Result[list[NBAPlayer]]:
        league = (await self.league()).data

        async def fetch() -> list[NBAPlayer]:
            found: dict[str, NBAPlayer] = {}
            # 25 is Yahoo's documented collection page size; batch metadata per page.
            for start in range(0, 2000, 25):
                root = await self.client.get(
                    f"league/{key(league.key)}/players;start={start};count=25;out=ownership,percent_owned,draft_analysis"
                )
                page = [parse_player(p) for p in root.findall(".//players/player")]
                previous = len(found)
                found.update({p.key: p for p in page})
                if len(page) < 25:
                    return list(found.values())
                if len(found) == previous:
                    raise FantasyError(
                        "PROVIDER_RESPONSE_INVALID", "Yahoo pagination did not advance."
                    )
            raise FantasyError(
                "PROVIDER_RESPONSE_INVALID", "Player collection exceeded safe pagination bound."
            )

        return await self.cached(
            f"players:{league.key}",
            TypeAdapter(list[NBAPlayer]),
            self.config.ownership_ttl,
            fetch,
            force,
        )

    async def game_for_season(self, season: int) -> str:
        async def fetch() -> str:
            root = await self.client.get(f"games;game_codes=nba;seasons={season}")
            games = root.findall(".//games/game")
            if len(games) != 1:
                raise FantasyError(
                    "SEASON_NOT_FOUND", "Yahoo NBA game for that season is unavailable."
                )
            return required(games[0], "game_key")

        return (
            await self.cached(
                f"game:{season}", TypeAdapter(str), self.config.historical_ttl, fetch, season=season
            )
        ).data

    async def stats(
        self, player_keys: list[str], season: int, force: bool = False, on_date: date | None = None
    ) -> Result[list[PlayerStats]]:
        if not player_keys:
            return Result(data=[])
        game = await self.game_for_season(season)
        mapping = (await self.mapping(game)).data
        unique = list(dict.fromkeys(player_keys))

        async def batch(requested: list[str]) -> Result[list[PlayerStats]]:
            cache_key = f"stats:{season}:{on_date}:" + ",".join(sorted(requested))

            async def fetch() -> list[PlayerStats]:
                ids = [f"{game}.p.{key(p).split('.p.')[-1]}" for p in requested]
                back = dict(zip(ids, requested, strict=True))
                suffix = (
                    f";type=date;date={on_date}" if on_date else f";type=season;season={season}"
                )
                root = await self.client.get(f"players;player_keys={','.join(ids)}/stats{suffix}")
                prov = Provenance(source="yahoo", season=season)
                stats = [
                    parse_stats(
                        p,
                        season,
                        mapping,
                        prov,
                        "current" if season == self.config.season else "historical",
                    )
                    for p in root.findall(".//players/player")
                ]
                for line in stats:
                    line.player_key = back.get(line.player_key, line.player_key)
                return stats

            ttl = (
                self.config.historical_ttl if season < self.config.season else self.config.stats_ttl
            )
            return await self.cached(
                cache_key, TypeAdapter(list[PlayerStats]), ttl, fetch, force, season
            )

        results = await asyncio.gather(
            *(batch(unique[i : i + 25]) for i in range(0, len(unique), 25))
        )
        return Result(
            data=[s for r in results for s in r.data],
            provenance=[p for r in results for p in r.provenance],
        )

    async def standings(self, force: bool = False) -> Result[list[Standing]]:
        league = (await self.league()).data

        async def fetch() -> list[Standing]:
            root = await self.client.get(f"league/{key(league.key)}/standings")
            return [
                Standing(
                    team_key=required(t, "team_key"),
                    rank=integer(txt(t, "team_standings/rank")),
                    wins=integer(txt(t, "team_standings/outcome_totals/wins")),
                    losses=integer(txt(t, "team_standings/outcome_totals/losses")),
                    ties=integer(txt(t, "team_standings/outcome_totals/ties")),
                )
                for t in root.findall(".//standings/teams/team")
            ]

        return await self.cached(
            f"standings:{league.key}",
            TypeAdapter(list[Standing]),
            self.config.stats_ttl,
            fetch,
            force,
        )

    async def matchups(self, week: int | None = None, force: bool = False) -> Result[list[Matchup]]:
        league = (await self.league()).data
        if week is not None and not 1 <= week <= 40:
            raise FantasyError("INVALID_INPUT", "week must be 1..40")

        async def fetch() -> list[Matchup]:
            root = await self.client.get(
                f"league/{key(league.key)}/scoreboard" + (f";week={week}" if week else "")
            )
            mapping = (await self.mapping(league.game_key)).data
            return [
                Matchup(
                    week=integer(txt(m, "week")),
                    status=txt(m, "status") or None,
                    team_keys=[required(t, "team_key") for t in m.findall("teams/team")],
                    category_totals={
                        required(t, "team_key"): {
                            mapping.get(txt(s, "stat_id"), txt(s, "stat_id")): number(
                                txt(s, "value")
                            )
                            for s in t.findall("team_stats/stats/stat")
                        }
                        for t in m.findall("teams/team")
                    },
                )
                for m in root.findall(".//matchup")
            ]

        return await self.cached(
            f"matchups:{league.key}:{week}",
            TypeAdapter(list[Matchup]),
            self.config.ownership_ttl,
            fetch,
            force,
        )

    async def transactions(
        self, limit: int = 100, offset: int = 0, force: bool = False
    ) -> Result[list[Transaction]]:
        if not 1 <= limit <= 500 or offset < 0:
            raise FantasyError("INVALID_INPUT", "limit must be 1..500 and offset nonnegative")
        league = (await self.league()).data

        async def fetch() -> list[Transaction]:
            output: list[Transaction] = []
            for start in range(offset, offset + limit, 25):
                count = min(25, offset + limit - start)
                root = await self.client.get(
                    f"league/{key(league.key)}/transactions;start={start};count={count}"
                )
                nodes = root.findall(".//transactions/transaction")
                output.extend(
                    Transaction(
                        key=required(t, "transaction_key"),
                        type=required(t, "type"),
                        status=txt(t, "status") or None,
                        timestamp=txt(t, "timestamp") or None,
                        player_keys=[
                            required(p, "player_key") for p in t.findall("players/player")
                        ],
                    )
                    for t in nodes
                )
                if len(nodes) < count:
                    break
            return output

        return await self.cached(
            f"transactions:{league.key}:{offset}:{limit}",
            TypeAdapter(list[Transaction]),
            self.config.ownership_ttl,
            fetch,
            force,
        )

    async def draft_results(self) -> Result[list[DraftPick]]:
        league = (await self.league()).data
        root = await self.client.get(f"league/{key(league.key)}/draftresults")
        teams = (await self.settings()).data.max_teams
        picks = [
            DraftPick(
                overall_pick=int(required(p, "pick")),
                round=int(required(p, "round")),
                pick_in_round=(int(required(p, "pick")) - 1) % teams + 1,
                team_key=required(p, "team_key"),
                player_key=required(p, "player_key"),
                source="yahoo",
            )
            for p in root.findall(".//draft_result")
        ]
        return Result(data=picks, provenance=[Provenance(source="yahoo", season=league.season)])
