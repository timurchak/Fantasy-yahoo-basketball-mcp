"""Thin FastMCP registration; application services own all business decisions."""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from fantasy_mcp import __version__
from fantasy_mcp.config import Config
from fantasy_mcp.domain import (
    Candidate,
    ChangeAnalysis,
    DraftRecommendationResult,
    DraftState,
    FantasyRoster,
    NBAPlayer,
    PlayerOwnership,
    PlayerStats,
    Provenance,
    Result,
    RosterSlot,
    ScoringCategory,
    ValuationMode,
    WeeklyAnalysis,
)
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.oauth import OAuth
from fantasy_mcp.persistence import Store
from fantasy_mcp.providers import (
    BDLScheduleProvider,
    CompositePlayerProvider,
    FilePlayerProvider,
    YahooPlayerProvider,
)
from fantasy_mcp.service import Service
from fantasy_mcp.yahoo import YahooClient, YahooRepository

P = ParamSpec("P")
R = TypeVar("R")
log = logging.getLogger("fantasy_mcp.server")


def safe[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(fn)
    async def call(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        except FantasyError as error:
            raise ToolError(str(error)) from None
        except Exception as error:
            log.error(
                "operation_failed",
                extra={"operation": fn.__name__, "error_type": type(error).__name__},
            )
            raise ToolError(
                "INTERNAL_ERROR: Operation failed; run doctor and inspect local logs."
            ) from None
        finally:
            log.info(
                "operation_complete",
                extra={
                    "operation": fn.__name__,
                    "elapsed_ms": round((time.perf_counter() - start) * 1000),
                },
            )

    return call


def build_service(config: Config) -> tuple[Service, httpx.AsyncClient]:
    store = Store(config.data_dir / "fantasy.sqlite3")
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(30), follow_redirects=False, limits=httpx.Limits(max_connections=8)
    )
    repo = YahooRepository(YahooClient(OAuth(config, http), http), store, config)
    optional = FilePlayerProvider(config.player_data_file) if config.player_data_file else None
    schedule = (
        BDLScheduleProvider(http, config.bdl_api_key, repo)
        if config.bdl_api_key.get_secret_value()
        else None
    )
    provider = CompositePlayerProvider(YahooPlayerProvider(repo), optional, schedule)
    return Service(repo, provider, store, config), http


def create_server(service: Service | None = None, config: Config | None = None) -> FastMCP:
    owned_http = None
    if service is None:
        service, owned_http = build_service(config or Config())
    svc = service

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            if owned_http:
                await owned_http.aclose()

    mcp = FastMCP(
        "Fantasy Basketball Intelligence",
        version=__version__,
        lifespan=lifespan,
        instructions="Yahoo is read-only. Use league rules and result provenance. "
        "Never treat observed "
        "stats as forecasts. For offline draft: create_draft with known format, record_draft_pick, "
        "recommend_draft_picks. Local draft mutations do not update Yahoo.",
        mask_error_details=True,
    )

    def register(
        fn: Callable[..., Any],
        name: str,
        description: str,
        mutation: bool = False,
        destructive: bool = False,
    ) -> None:
        mcp.tool(
            safe(fn),
            name=name,
            description=description,
            annotations={
                "readOnlyHint": not mutation,
                "destructiveHint": destructive,
                "idempotentHint": not mutation,
                "openWorldHint": not mutation,
            },
        )

    registrations: list[tuple[Callable[..., Any], str, str]] = [
        (svc.repo.league, "get_league", "Discover the configured Yahoo NBA league and season."),
        (
            svc.repo.leagues,
            "discover_leagues",
            "Discover authenticated account NBA leagues for configured season.",
        ),
        (
            svc.repo.settings,
            "get_league_settings",
            "Retrieve authoritative normalized rules, categories and slots.",
        ),
        (svc.repo.teams, "get_teams", "League teams, managers and available acquisition usage."),
        (svc.repo.standings, "get_standings", "Current normalized league standings."),
        (svc.repo.matchups, "get_matchups", "Yahoo scoreboard category totals and matchup teams."),
        (
            svc.repo.transactions,
            "get_transactions",
            "Read league transactions with internal pagination.",
        ),
        (
            svc.search_players,
            "search_players",
            "Search cached player pool with filters; min_minutes is per-game.",
        ),
        (
            svc.player,
            "get_player",
            "Resolve a player by stable Yahoo key with ownership and injury status.",
        ),
        (
            svc.repo.roster,
            "get_team_roster",
            "Get team roster and selected slots, optionally on a date.",
        ),
        (svc.all_rosters, "get_all_rosters", "Fetch all league rosters with bounded concurrency."),
        (svc.game_log, "get_player_game_log", "Retrieve Yahoo date-stat lines for up to 31 dates."),
        (
            svc.analyze_team,
            "analyze_team_categories",
            "Aggregate volume-weighted ratios and peer-relative categories.",
        ),
        (
            svc.compare,
            "compare_players",
            "Compare player value, team effects, eligibility, scarcity and risks.",
        ),
        (
            svc.punts,
            "analyze_punt_strategies",
            "Revalue a fixed population with excluded categories and show rank changes.",
        ),
        (
            svc.scarcity,
            "analyze_category_scarcity",
            "Remaining production, contributor counts and tier dropoffs.",
        ),
        (
            svc.validate_roster,
            "validate_roster",
            "Maximum matching of Yahoo-eligible players to configured slots.",
        ),
        (
            svc.validate_roster,
            "find_optimal_slot_assignment",
            "Find a legal full assignment; does not maximize daily points.",
        ),
        (
            svc.doctor,
            "diagnostics",
            "Version, database, credentials presence; optional live checks. Never exposes secrets.",
        ),
    ]
    for fn, name, description in registrations:
        register(fn, name, description)

    async def get_scoring_categories() -> Result[list[ScoringCategory]]:
        result = await svc.repo.settings()
        return Result(data=result.data.categories, provenance=result.provenance)

    async def get_roster_positions() -> Result[list[RosterSlot]]:
        result = await svc.repo.settings()
        return Result(data=result.data.roster_slots, provenance=result.provenance)

    async def get_my_roster() -> Result[FantasyRoster]:
        return await svc.repo.roster(await svc.repo.my_team())

    async def get_rostered_players() -> Result[list[NBAPlayer]]:
        rosters = await svc.all_rosters()
        return Result(
            data=[e.player for r in rosters.data for e in r.entries], provenance=rosters.provenance
        )

    async def get_available_players(
        limit: int = 100, offset: int = 0, position: str | None = None
    ) -> Result[list[NBAPlayer]]:
        return await svc.search_players(
            availability="available", position=position, limit=limit, offset=offset
        )

    async def get_injured_players(limit: int = 100, offset: int = 0) -> Result[list[NBAPlayer]]:
        return await svc.search_players(injured=True, limit=limit, offset=offset)

    async def get_player_ownership(player_key: str) -> Result[PlayerOwnership]:
        result = await svc.player(player_key)
        return Result(data=result.data.ownership, provenance=result.provenance)

    async def get_player_stats(
        player_keys: list[str], season: int | None = None, force_refresh: bool = False
    ) -> Result[list[PlayerStats]]:
        if not 1 <= len(player_keys) <= 500:
            raise FantasyError("INVALID_INPUT", "Provide 1..500 player keys.")
        return await svc.repo.stats(player_keys, season or svc.config.season, force_refresh)

    async def get_draft_state(draft_id: str | None = None) -> Result[DraftState]:
        return Result(
            data=await svc.draft_state(draft_id), provenance=[Provenance(source="local_draft")]
        )

    async def load_draft(draft_id: str) -> Result[DraftState]:
        state = await svc.draft_state(draft_id)
        svc.store.set_meta(f"active_draft:{state.league_key}", draft_id)
        return Result(data=state, provenance=[Provenance(source="local_draft")])

    async def undo_draft_pick(
        draft_id: str | None = None, expected_revision: int | None = None
    ) -> Result[DraftState]:
        state = await svc.draft_state(draft_id)
        return Result(
            data=svc.drafts.undo(state.draft_id, expected_revision),
            provenance=[Provenance(source="local_draft")],
        )

    async def reset_draft(
        expected_revision: int, draft_id: str | None = None
    ) -> Result[DraftState]:
        state = await svc.draft_state(draft_id)
        return Result(
            data=svc.drafts.reset(state.draft_id, expected_revision),
            provenance=[Provenance(source="local_draft")],
        )

    async def correct_draft_pick(
        overall_pick: int, player_key: str, expected_revision: int, draft_id: str | None = None
    ) -> Result[DraftState]:
        await svc.player(player_key)
        state = await svc.draft_state(draft_id)
        return Result(
            data=svc.drafts.correct(state.draft_id, overall_pick, player_key, expected_revision),
            provenance=[Provenance(source="local_draft")],
        )

    async def sync_draft_results(
        expected_revision: int, draft_id: str | None = None
    ) -> Result[DraftState]:
        state = await svc.draft_state(draft_id)
        results = await svc.repo.draft_results()
        return Result(
            data=svc.drafts.sync(state.draft_id, results.data, expected_revision),
            provenance=results.provenance,
        )

    async def get_available_draft_players(
        draft_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> Result[list[NBAPlayer]]:
        if not 1 <= limit <= 500 or offset < 0:
            raise FantasyError("INVALID_INPUT", "limit must be 1..500 and offset nonnegative")
        state = await svc.draft_state(draft_id)
        players = await svc.repo.players()
        picked = {p.player_key for p in state.picks}
        return Result(
            data=[p for p in players.data if p.key not in picked][offset : offset + limit],
            provenance=players.provenance + [Provenance(source="local_draft")],
        )

    async def get_my_draft_roster(draft_id: str | None = None) -> Result[list[NBAPlayer]]:
        state = await svc.draft_state(draft_id)
        players = await svc.repo.players()
        picked = {p.player_key for p in state.picks if p.team_key == state.my_team}
        return Result(
            data=[p for p in players.data if p.key in picked], provenance=players.provenance
        )

    async def recommend_draft_picks(
        count: int = 10,
        team_key: str | None = None,
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
    ) -> Result[DraftRecommendationResult]:
        return await svc.recommend(count, team_key, draft_id, mode, punts)

    async def recommend_adds(
        count: int = 10,
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
    ) -> Result[DraftRecommendationResult]:
        return await svc.recommend(count, team_key, mode=mode, punts=punts, use_draft=False)

    async def evaluate_player_for_team(
        player_key: str,
        team_key: str | None = None,
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
    ) -> Result[list[Candidate]]:
        return await svc.compare([player_key], team_key, mode, punts, draft_id)

    async def simulate_draft_pick(
        player_keys: list[str],
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
    ) -> Result[list[Candidate]]:
        state = await svc.draft_state(draft_id)
        if {p.player_key for p in state.picks}.intersection(player_keys):
            raise FantasyError(
                "PLAYER_ALREADY_DRAFTED", "Cannot simulate an already drafted candidate."
            )
        return await svc.compare(player_keys, state.my_team, mode, punts, state.draft_id)

    async def evaluate_add_drop(
        add_player: str,
        drop_player: str | None = None,
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Result[ChangeAnalysis]:
        return await svc.change(
            [drop_player] if drop_player else [],
            [add_player],
            team_key,
            mode,
            punts,
            True,
            start,
            end,
        )

    async def evaluate_trade(
        outgoing: list[str],
        incoming: list[str],
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Result[ChangeAnalysis]:
        return await svc.change(outgoing, incoming, team_key, mode, punts, start=start, end=end)

    async def analyze_matchup(
        start: date, end: date, team_key: str | None = None, opponent_key: str | None = None
    ) -> Result[WeeklyAnalysis]:
        return await svc.weekly(start, end, team_key, opponent_key)

    async def recommend_streamers(
        start: date, end: date, team_key: str | None = None, count: int = 10
    ) -> Result[WeeklyAnalysis]:
        return await svc.weekly(start, end, team_key, streaming=True, count=count)

    handlers: list[Callable[..., Any]] = [
        get_scoring_categories,
        get_roster_positions,
        get_my_roster,
        get_rostered_players,
        get_available_players,
        get_injured_players,
        get_player_ownership,
        get_player_stats,
        get_draft_state,
        get_available_draft_players,
        get_my_draft_roster,
        recommend_draft_picks,
        recommend_adds,
        evaluate_player_for_team,
        simulate_draft_pick,
        evaluate_add_drop,
        evaluate_trade,
        analyze_matchup,
        recommend_streamers,
    ]
    for fn in handlers:
        register(fn, fn.__name__, fn.__name__.replace("_", " ").capitalize())
    register(get_draft_state, "get_draft_board", "Current persistent draft board, in pick order.")
    mutations: list[Callable[..., Any]] = [
        svc.create_draft,
        load_draft,
        svc.record_pick,
        undo_draft_pick,
        correct_draft_pick,
        reset_draft,
        sync_draft_results,
    ]
    for fn in mutations:
        name = "record_draft_pick" if fn == svc.record_pick else fn.__name__
        register(
            fn,
            name,
            f"Local draft only: {name.replace('_', ' ')}.",
            True,
            name in {"reset_draft", "undo_draft_pick", "correct_draft_pick", "sync_draft_results"},
        )
    register(svc.sync, "sync_league", "Refresh and snapshot league data; no Yahoo writes.", True)

    @mcp.resource("fantasy://league/settings")
    async def settings_resource() -> str:
        return (await safe(svc.repo.settings)()).model_dump_json()

    @mcp.resource("fantasy://draft/current")
    async def draft_resource() -> str:
        return (await safe(get_draft_state)()).model_dump_json()

    @mcp.resource("fantasy://roster/mine")
    async def roster_resource() -> str:
        return (await safe(get_my_roster)()).model_dump_json()

    @mcp.resource("fantasy://analytics/methodology")
    def methodology_resource() -> str:
        return (
            "Volume impact: FGM - pool_FG% * FGA; analogously FT. Population standard deviations "
            "(ddof=0), TO inverted, configured category weights. Missing lines "
            "excluded explicitly. "
            "Team percentages=sum(makes)/sum(attempts). Fixed-pool punt comparisons. "
            "Replacement: maximum-weight matchable league slot basis; injury slots excluded. "
            "Context weighting is bounded to +/-25% by default. Observations are not projections. "
            "See docs/analytics.md for limitations, pool selection, and formulas."
        )

    return mcp
