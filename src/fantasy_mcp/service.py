"""Composes data and analytics so common agent questions require one tool call."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any, Literal

from fantasy_mcp import __version__
from fantasy_mcp.analytics import (
    TeamAnalytics,
    ZScoreValuation,
    active_game_counts,
    aggregate,
    assign,
    category_scarcity,
    delta,
    punt_effects,
)
from fantasy_mcp.config import Config
from fantasy_mcp.domain import (
    Candidate,
    ChangeAnalysis,
    DraftRecommendationResult,
    DraftState,
    FantasyRoster,
    NBAPlayer,
    Provenance,
    PuntEffect,
    Result,
    ScarcityCategory,
    SlotAssignment,
    TeamProfile,
    ValuationMode,
    WeeklyAnalysis,
)
from fantasy_mcp.draft import DraftEngine
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.persistence import Store
from fantasy_mcp.providers import PlayerDataProvider, schedule_volume
from fantasy_mcp.yahoo import YahooRepository


class Service:
    def __init__(
        self, repo: YahooRepository, provider: PlayerDataProvider, store: Store, config: Config
    ):
        self.repo, self.provider, self.store, self.config = repo, provider, store, config
        self.drafts = DraftEngine(store)

    async def search_players(
        self,
        query: str = "",
        position: str | None = None,
        nba_team: str | None = None,
        availability: str | None = None,
        injured: bool | None = None,
        limit: int = 100,
        offset: int = 0,
        min_games: int | None = None,
        min_minutes: float | None = None,
        stat_min: dict[str, float] | None = None,
        stat_max: dict[str, float] | None = None,
        sort: str = "name",
        force_refresh: bool = False,
    ) -> Result[list[NBAPlayer]]:
        if not 1 <= limit <= 500 or offset < 0:
            raise FantasyError("INVALID_INPUT", "limit must be 1..500 and offset nonnegative")
        if availability not in {None, "free_agent", "waivers", "rostered", "unknown", "available"}:
            raise FantasyError("INVALID_INPUT", "Unsupported availability filter.")
        source = await self.repo.players(force_refresh)
        pool = [
            p
            for p in source.data
            if query.casefold() in p.name.casefold()
            and (not position or position in p.eligibility.positions)
            and (not nba_team or (p.nba_team or "").casefold() == nba_team.casefold())
            and (
                availability is None
                or p.ownership.availability == availability
                or (
                    availability == "available"
                    and p.ownership.availability in {"free_agent", "waivers"}
                )
            )
            and (injured is None or bool(p.status.code) == injured)
        ]
        if min_games is not None or min_minutes is not None or stat_min or stat_max:
            stats = await self.provider.get_stats([p.key for p in pool], self.config.season)
            source.provenance.extend(stats.provenance)
            by_key = {s.player_key: s for s in stats.data}
            minima = dict(stat_min or {})
            if min_games is not None:
                minima["GP"] = float(min_games)
            # Filters are season totals except min_minutes, explicitly per-game.
            filtered = []
            for p in pool:
                if p.key not in by_key:
                    continue
                s = by_key[p.key]
                values = s.values
                gp, minutes = values.get("GP"), values.get("MIN")
                mpg = (
                    minutes / gp if gp and minutes is not None and s.basis == "totals" else minutes
                )
                if min_minutes is not None and (mpg is None or mpg < min_minutes):
                    continue
                if any(
                    values.get(k) is None or float(values[k] or 0) < v for k, v in minima.items()
                ):
                    continue
                if any(
                    values.get(k) is None or float(values[k] or 0) > v
                    for k, v in (stat_max or {}).items()
                ):
                    continue
                filtered.append(p)
            pool = filtered
        if sort == "name":
            pool.sort(key=lambda p: (p.name.casefold(), p.key))
        elif sort == "percent_owned":
            pool.sort(key=lambda p: (-(p.ownership.percent_owned or 0), p.key))
        elif sort == "adp":
            pool.sort(key=lambda p: (p.adp if p.adp is not None else float("inf"), p.key))
        else:
            raise FantasyError("INVALID_INPUT", "sort must be name, percent_owned, or adp")
        return Result(
            data=pool[offset : offset + limit],
            provenance=source.provenance,
            limitations=[f"matched={len(pool)}; offset={offset}; limit={limit}"],
        )

    async def player(self, player_key: str) -> Result[NBAPlayer]:
        source = await self.repo.players()
        for p in source.data:
            if p.key == player_key:
                return Result(data=p, provenance=source.provenance)
        raise FantasyError(
            "PLAYER_NOT_FOUND", "Use search_players to resolve the Yahoo player key."
        )

    async def all_rosters(self, force_refresh: bool = False) -> Result[list[FantasyRoster]]:
        teams = await self.repo.teams(force_refresh)
        results = await asyncio.gather(
            *(self.repo.roster(t.key, force_refresh) for t in teams.data)
        )
        return Result(
            data=[r.data for r in results],
            provenance=teams.provenance + [p for r in results for p in r.provenance],
        )

    async def create_draft(
        self,
        draft_id: str,
        team_order: list[str],
        my_team: str,
        rounds: int,
        snake: bool,
        team_names: dict[str, str] | None = None,
    ) -> Result[DraftState]:
        league, teams = await asyncio.gather(self.repo.league(), self.repo.teams())
        if set(team_order) != {t.key for t in teams.data}:
            raise FantasyError(
                "INVALID_DRAFT_PICK", "Draft order must contain every league team exactly once."
            )
        state = DraftState(
            draft_id=draft_id,
            league_key=league.data.key,
            team_order=team_order,
            my_team=my_team,
            rounds=rounds,
            snake=snake,
            team_names=team_names or {t.key: t.name for t in teams.data},
        )
        self.store.create_draft(state)
        self.store.set_meta(f"active_draft:{league.data.key}", draft_id)
        return Result(data=state, provenance=[Provenance(source="local_draft")])

    async def draft_state(self, draft_id: str | None = None) -> DraftState:
        if draft_id:
            state = self.store.load_draft(draft_id)
            expected = self.config.league_key or self.store.get_meta(
                f"league_key:{self.config.season}:{self.config.league_id}"
            )
            if expected and state.league_key != expected:
                raise FantasyError("LEAGUE_MISMATCH", "Draft belongs to a different league.")
            return state
        league = (await self.repo.league()).data
        active = draft_id or self.store.get_meta(f"active_draft:{league.key}")
        if not active:
            raise FantasyError(
                "DRAFT_NOT_INITIALIZED", "Create a draft with known order, rounds and snake format."
            )
        state = self.store.load_draft(active)
        if state.league_key != league.key:
            raise FantasyError("LEAGUE_MISMATCH", "Draft belongs to a different league.")
        return state

    async def record_pick(
        self,
        player_key: str,
        draft_id: str | None = None,
        team_key: str | None = None,
        expected_revision: int | None = None,
    ) -> DraftState:
        await self.player(player_key)
        state = await self.draft_state(draft_id)
        return self.drafts.record(state.draft_id, player_key, team_key, expected_revision)

    async def context(
        self,
        team_key: str | None = None,
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        population: Literal["top", "rostered", "available"] = "top",
        use_draft: bool = False,
    ) -> tuple[TeamAnalytics, list[str], set[str], DraftState | None, list[Provenance], list[str]]:
        league, settings, players = await asyncio.gather(
            self.repo.league(), self.repo.settings(), self.repo.players()
        )
        prov = league.provenance + settings.provenance + players.provenance
        state = None
        if use_draft:
            state = await self.draft_state(draft_id)
            team = team_key or state.my_team
            if team not in state.team_order:
                raise FantasyError("TEAM_NOT_FOUND", "Team is not part of this draft.")
            rosters = [
                [p.player_key for p in state.picks if p.team_key == t] for t in state.team_order
            ]
            roster = [p.player_key for p in state.picks if p.team_key == team]
            rostered = {p.player_key for p in state.picks}
            available = {p.key for p in players.data} - rostered
            prov.append(Provenance(source="local_draft"))
        else:
            team = team_key or await self.repo.my_team()
            all_rosters = await self.all_rosters()
            prov.extend(all_rosters.provenance)
            by_team = {r.team_key: r for r in all_rosters.data}
            if team not in by_team:
                raise FantasyError("TEAM_NOT_FOUND", "Team is not in configured league.")
            rosters = [[e.player.key for e in r.entries] for r in all_rosters.data]
            roster = [e.player.key for e in by_team[team].entries]
            rostered = {p for r in rosters for p in r}
            available = {
                p.key for p in players.data if p.ownership.availability in {"free_agent", "waivers"}
            } - rostered
        all_keys = [p.key for p in players.data]
        limitations = [
            "Relative scores are not probabilities.",
            "Roster profiles include bench/injury reserves; they are not optimized daily lineups.",
        ]
        if mode == ValuationMode.PROJECTED:
            stats = await self.provider.get_projections(all_keys, league.data.season)
        else:
            stats = await self.provider.get_stats(all_keys, league.data.season)
            # Preseason fallback is labeled historical, never a projection.
            if not any(
                (s.values.get("GP") or 0) >= max(1, self.config.min_games) for s in stats.data
            ):
                stats = await self.provider.get_stats(all_keys, league.data.season - 1)
                limitations.append(
                    f"Using observed {league.data.season - 1} stats; rookies may be unvalued."
                )
        prov.extend(stats.provenance)
        pool = (
            sorted(rostered)
            if population == "rostered"
            else sorted(available)
            if population == "available"
            else None
        )
        valuation = ZScoreValuation(self.config).value(
            stats.data, settings.data, mode, punts or [], pool
        )
        for player_key in set(all_keys) - {s.player_key for s in stats.data}:
            valuation.excluded[player_key] = ["NO_STATS_RETURNED"]
        if valuation.excluded:
            limitations.append(
                f"{len(valuation.excluded)} stat lines excluded; see valuation.excluded."
            )
        engine = TeamAnalytics(
            settings.data, players.data, stats.data, valuation, self.config, rosters
        )
        return engine, roster, available, state, prov, limitations

    async def recommend(
        self,
        count: int = 10,
        team_key: str | None = None,
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        use_draft: bool = True,
    ) -> Result[DraftRecommendationResult]:
        if not 1 <= count <= 30:
            raise FantasyError("INVALID_INPUT", "count must be 1..30")
        engine, roster, available, state, prov, limitations = await self.context(
            team_key, draft_id, mode, punts, use_draft=use_draft
        )
        if state and len(state.picks) == state.rounds * len(state.team_order):
            raise FantasyError("DRAFT_COMPLETE", "Draft has no remaining picks.")
        scarcity = category_scarcity(
            engine.players, available, engine.valuation, engine.stats, engine.settings
        )
        before = engine.profile(roster)
        # Rank cheaply first; compute full simulation and replacement evidence for shortlist only.
        ranked = sorted(
            (v for v in engine.valuation.values if v.player_key in available),
            key=lambda v: (
                not assign(
                    [engine.by_key[p] for p in roster] + [engine.by_key[v.player_key]],
                    engine.settings.roster_slots,
                ).legal,
                -engine.contribution(v, before),
                v.player_key,
            ),
        )
        candidates = [
            engine.candidate(roster, v.player_key, scarcity) for v in ranked[: max(count * 3, 30)]
        ]
        limitations.append(
            "Final evidence reranks a shortlist of max(30, 3*count) contextual candidates."
        )
        progress = len(state.picks) / (state.rounds * len(state.team_order)) if state else 1.0
        for candidate in candidates:
            replacement = candidate.replacement.value_over_replacement or 0
            scarce_contribution = sum(
                max(0, candidate.generic_value.categories[c])
                for c in candidate.scarcity
                if c not in (punts or [])
            )
            candidate.score_components = {
                "contextual_value": candidate.contextual_value,
                "replacement_bonus": self.config.replacement_weight * replacement,
                "scarcity_bonus": self.config.scarcity_weight * progress * scarce_contribution,
            }
            candidate.recommendation_score = sum(candidate.score_components.values())
        candidates.sort(
            key=lambda c: (
                not c.roster_fit.legal,
                -c.recommendation_score,
                -(c.replacement.value_over_replacement or 0),
                c.player.key,
            )
        )
        if not use_draft:
            team = team_key or await self.repo.my_team()
            teams = (await self.repo.teams()).data
            used = next((t.weekly_adds_used for t in teams if t.key == team), None)
            cap = engine.settings.max_weekly_adds
            if cap is not None and used is not None and used >= cap:
                candidates = []
                limitations.append("WEEKLY_ACQUISITION_LIMIT_REACHED")
            elif used is None:
                limitations.append("Weekly acquisition usage unavailable; verify before adding.")
        return Result(
            data=DraftRecommendationResult(
                league=(await self.repo.league()).data,
                draft_context=state,
                team_profile=before,
                candidates=candidates[:count],
                valuation=engine.valuation.model_copy(
                    update={"values": [c.generic_value for c in candidates[:count]]}
                ),
            ),
            provenance=prov,
            limitations=limitations,
        )

    async def analyze_team(
        self,
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        draft_id: str | None = None,
    ) -> Result[TeamProfile]:
        engine, roster, _, _, prov, limits = await self.context(
            team_key, draft_id, mode, use_draft=draft_id is not None
        )
        return Result(data=engine.profile(roster), provenance=prov, limitations=limits)

    async def compare(
        self,
        player_keys: list[str],
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        draft_id: str | None = None,
    ) -> Result[list[Candidate]]:
        if not 1 <= len(player_keys) <= 20 or len(set(player_keys)) != len(player_keys):
            raise FantasyError("INVALID_INPUT", "Supply 1..20 distinct player keys.")
        engine, roster, available, _, prov, limits = await self.context(
            team_key, draft_id, mode, punts, use_draft=draft_id is not None
        )
        scarcity = category_scarcity(
            engine.players, available, engine.valuation, engine.stats, engine.settings
        )
        candidates = [
            engine.candidate([k for k in roster if k != p], p, scarcity) for p in player_keys
        ]
        return Result(data=candidates, provenance=prov, limitations=limits)

    async def scarcity(
        self, draft_id: str | None = None, mode: ValuationMode = ValuationMode.PER_GAME
    ) -> Result[dict[str, ScarcityCategory]]:
        engine, _, available, _, prov, limits = await self.context(
            draft_id=draft_id, mode=mode, use_draft=draft_id is not None
        )
        return Result(
            data=category_scarcity(
                engine.players, available, engine.valuation, engine.stats, engine.settings
            ),
            provenance=prov,
            limitations=limits,
        )

    async def punts(
        self,
        strategies: list[list[str]] | None = None,
        team_key: str | None = None,
        draft_id: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
    ) -> Result[list[PuntEffect]]:
        engine, roster, _, _, prov, limits = await self.context(
            team_key, draft_id, mode, use_draft=draft_id is not None
        )
        options = (
            strategies if strategies is not None else [[c.key] for c in engine.settings.categories]
        )
        if not 1 <= len(options) <= 30:
            raise FantasyError("INVALID_INPUT", "Supply 1..30 punt strategies.")
        return Result(
            data=punt_effects(engine, roster, options),
            provenance=prov,
            limitations=limits
            + ["Excluded-category score changes are not an automatic punt recommendation."],
        )

    async def change(
        self,
        outgoing: list[str],
        incoming: list[str],
        team_key: str | None = None,
        mode: ValuationMode = ValuationMode.PER_GAME,
        punts: list[str] | None = None,
        is_add: bool = False,
        start: date | None = None,
        end: date | None = None,
    ) -> Result[ChangeAnalysis]:
        if not incoming or len(incoming) > 20 or len(outgoing) > 20:
            raise FantasyError("INVALID_INPUT", "Specify incoming players, at most 20 per side.")
        engine, roster, available, _, prov, limits = await self.context(
            team_key=team_key, mode=mode, punts=punts
        )
        if is_add and not set(incoming) <= available:
            raise FantasyError(
                "PLAYER_NOT_AVAILABLE", "Incoming player is not a known free agent/waiver player."
            )
        result = engine.change(roster, outgoing, incoming)
        if is_add:
            teams = (await self.repo.teams()).data
            team = team_key or await self.repo.my_team()
            used = next((t.weekly_adds_used for t in teams if t.key == team), None)
            cap = engine.settings.max_weekly_adds
            if cap is not None and used is not None:
                result.acquisitions_remaining = max(0, cap - used)
                if len(incoming) > result.acquisitions_remaining:
                    result.risks.append("WEEKLY_ACQUISITION_LIMIT_REACHED")
            else:
                limits.append("Weekly acquisition usage/limit unavailable.")
            if any(engine.by_key[p].ownership.availability == "waivers" for p in incoming):
                result.risks.append("WAIVER_CLAIM_REQUIRED")
        if (start is None) != (end is None):
            raise FantasyError("INVALID_INPUT", "Provide both start and end for schedule effects.")
        if start is not None and end is not None:
            schedule = await self.provider.get_schedule(self.config.season)
            prov.extend(schedule.provenance)
            after_keys = [p for p in roster if p not in outgoing] + incoming
            before_games = active_game_counts(engine, roster, schedule.data, start, end)
            after_games = active_game_counts(engine, after_keys, schedule.data, start, end)
            result.schedule_before = aggregate(
                roster, engine.stats, engine.settings, games=before_games
            )
            result.schedule_after = aggregate(
                after_keys, engine.stats, engine.settings, games=after_games
            )
            result.schedule_deltas = delta(result.schedule_before, result.schedule_after)
            limits.append(
                "Schedule effects assume change effective on start date; verify deadlines."
            )
        return Result(data=result, provenance=prov, limitations=limits)

    async def validate_roster(self, player_keys: list[str]) -> Result[SlotAssignment]:
        settings, players = await asyncio.gather(self.repo.settings(), self.repo.players())
        by_key = {p.key: p for p in players.data}
        if set(player_keys) - by_key.keys():
            raise FantasyError("PLAYER_NOT_FOUND", "Resolve unknown players with search_players.")
        return Result(
            data=assign([by_key[p] for p in player_keys], settings.data.roster_slots),
            provenance=settings.provenance + players.provenance,
        )

    async def weekly(
        self,
        start: date,
        end: date,
        team_key: str | None = None,
        opponent_key: str | None = None,
        streaming: bool = False,
        count: int = 10,
    ) -> Result[WeeklyAnalysis]:
        if not 1 <= count <= 30:
            raise FantasyError("INVALID_INPUT", "count must be 1..30")
        schedule = await self.provider.get_schedule(self.config.season)
        engine, roster, available, _, prov, limits = await self.context(team_key=team_key)
        prov.extend(schedule.provenance)
        # Daily-Tomorrow adds cannot produce today. Forecast dates are explicit.
        deadline = str(engine.settings.rules.get("weekly_deadline", ""))
        if streaming and "tomorrow" in deadline.lower():
            start = max(start, date.today() + timedelta(days=1))
        games, b2b, days = schedule_volume(schedule.data, engine.players, start, end)
        our_games = active_game_counts(engine, roster, schedule.data, start, end)
        ours = aggregate(roster, engine.stats, engine.settings, games=our_games)
        limits.append("Daily active slots use relative value; injury absences remain uncertain.")
        limits.append("Forecast uses observed per-game rates; no win probabilities are estimated.")
        if streaming:
            candidates = []
            for player in engine.players:
                if (
                    player.key in available
                    and player.key in engine.values
                    and games.get(player.key, 0) > 0
                ):
                    contribution = aggregate(
                        [player.key], engine.stats, engine.settings, games=games
                    )
                    value = (
                        engine.contribution(engine.values[player.key], engine.profile(roster))
                        * games[player.key]
                    )
                    candidates.append(
                        {
                            "player": player.model_dump(mode="json"),
                            "games": games[player.key],
                            "contribution": contribution.model_dump(mode="json"),
                            "relative_score": value,
                            "reasons": ["STREAMING_VALUE"],
                            "back_to_backs": b2b[player.key],
                            "roster_fit": assign(
                                [engine.by_key[p] for p in roster] + [player],
                                engine.settings.roster_slots,
                            ).model_dump(mode="json"),
                        }
                    )
            candidates.sort(key=lambda c: -float(c["relative_score"]))  # type: ignore[arg-type]
            teams = (await self.repo.teams()).data
            team = team_key or await self.repo.my_team()
            used = next((t.weekly_adds_used for t in teams if t.key == team), None)
            cap = engine.settings.max_weekly_adds
            remaining = max(0, cap - used) if cap is not None and used is not None else None
            if remaining == 0:
                candidates = []
            if remaining is None:
                limits.append("Weekly acquisition usage unavailable.")
            return Result(
                data=WeeklyAnalysis.model_validate(
                    {
                        "candidates": candidates[:count],
                        "acquisitions_remaining": remaining,
                        "daily_game_volume": days,
                    }
                ),
                provenance=prov,
                limitations=limits,
            )
        if opponent_key is None:
            team = team_key or await self.repo.my_team()
            matchups = await self.repo.matchups()
            opponents = [
                p for m in matchups.data if team in m.team_keys for p in m.team_keys if p != team
            ]
            if len(opponents) != 1:
                raise FantasyError(
                    "MATCHUP_NOT_FOUND", "No unique current opponent; provide opponent_key."
                )
            opponent_key = opponents[0]
        opponent = await self.repo.roster(opponent_key)
        prov.extend(opponent.provenance)
        their_keys = [e.player.key for e in opponent.data.entries]
        their_games = active_game_counts(engine, their_keys, schedule.data, start, end)
        theirs = aggregate(their_keys, engine.stats, engine.settings, games=their_games)
        return Result(
            data=WeeklyAnalysis.model_validate(
                {
                    "our_totals": ours.model_dump(mode="json"),
                    "opponent_totals": theirs.model_dump(mode="json"),
                    "our_minus_opponent": delta(theirs, ours),
                    "our_remaining_games": sum(our_games.values()),
                    "opponent_remaining_games": sum(their_games.values()),
                    "daily_game_volume": days,
                    "probabilities": None,
                }
            ),
            provenance=prov,
            limitations=limits,
        )

    async def game_log(
        self, player_key: str, start: date, end: date
    ) -> Result[list[dict[str, Any]]]:
        if end < start or (end - start).days > 30:
            raise FantasyError("INVALID_INPUT", "Date-stat window must be 1..31 days.")
        results = await asyncio.gather(
            *(
                self.repo.stats([player_key], self.config.season, on_date=start + timedelta(days=i))
                for i in range((end - start).days + 1)
            )
        )
        return Result(
            data=[
                {
                    "date": (start + timedelta(days=i)).isoformat(),
                    "stats": [s.model_dump(mode="json") for s in result.data],
                }
                for i, result in enumerate(results)
            ],
            provenance=[p for result in results for p in result.provenance],
            limitations=[
                "Yahoo date-stat lines; zero/no-game and missing date coverage remain "
                "provider-dependent."
            ],
        )

    async def sync(self, force_refresh: bool = True) -> Result[dict[str, int]]:
        league = await self.repo.league(force_refresh)
        settings, teams, players, standings, transactions = await asyncio.gather(
            self.repo.settings(force_refresh),
            self.repo.teams(force_refresh),
            self.repo.players(force_refresh),
            self.repo.standings(force_refresh),
            self.repo.transactions(force=force_refresh),
        )
        rosters, stats = await asyncio.gather(
            self.all_rosters(force_refresh),
            self.repo.stats([p.key for p in players.data], league.data.season, force_refresh),
        )
        return Result(
            data={
                "teams": len(teams.data),
                "players": len(players.data),
                "rosters": len(rosters.data),
                "stats": len(stats.data),
                "standings": len(standings.data),
                "transactions": len(transactions.data),
                "categories": len(settings.data.categories),
            },
            provenance=league.provenance + stats.provenance,
        )

    async def doctor(self, live: bool = False) -> Result[dict[str, Any]]:
        checks: dict[str, Any] = {
            "version": __version__,
            "database": str(self.store.path),
            "database_ok": self.store.get_meta("schema_version") == "1",
            "season": self.config.season,
            "credentials_configured": bool(
                self.config.client_id.get_secret_value()
                and self.config.client_secret.get_secret_value()
            ),
            "tokens_present": (self.config.data_dir / "yahoo_tokens.json").exists(),
            "yahoo_writes": False,
            "live_checked": live,
        }
        if live:
            operations: list[tuple[str, Callable[..., Awaitable[Any]]]] = [
                ("league", self.repo.league),
                ("settings", self.repo.settings),
                ("teams", self.repo.teams),
                ("players", self.repo.players),
            ]
            for name, operation in operations:
                try:
                    result = await operation(True)
                    checks[name] = {"ok": True, "stale": any(p.stale for p in result.provenance)}
                except FantasyError as error:
                    checks[name] = {"ok": False, "code": error.code, "message": error.message}
        return Result(data=checks)
