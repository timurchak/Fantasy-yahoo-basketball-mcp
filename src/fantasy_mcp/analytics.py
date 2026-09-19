"""Pure deterministic analytics over normalized data; no network or persistence."""

from collections import Counter
from datetime import date, timedelta
from statistics import mean, median, pstdev
from typing import Protocol

from fantasy_mcp.config import Config
from fantasy_mcp.domain import (
    Candidate,
    ChangeAnalysis,
    LeagueSettings,
    NBAPlayer,
    PlayerStats,
    PlayerValue,
    PuntEffect,
    ReplacementValue,
    RosterSlot,
    ScarcityCategory,
    ScheduleInfo,
    SlotAssignment,
    TeamProfile,
    ValuationMode,
    ValuationResult,
)
from fantasy_mcp.errors import FantasyError


def stat_line(stats: PlayerStats, mode: ValuationMode) -> dict[str, float | None]:
    if mode == ValuationMode.PROJECTED and stats.kind != "projection":
        raise FantasyError(
            "INSUFFICIENT_DATA", "Projected mode requires actual provider projections."
        )
    gp = stats.values.get("GP")
    convert = (mode == ValuationMode.PER_GAME) != (stats.basis == "per_game")
    if convert and (gp is None or gp <= 0):
        raise FantasyError("INSUFFICIENT_DATA", "Games played is required for basis conversion.")
    factor = (1 / gp if mode == ValuationMode.PER_GAME else gp) if convert and gp else 1
    return {
        k: v * factor if v is not None and k not in {"GP", "FG%", "FT%"} else v
        for k, v in stats.values.items()
    }


def required_stats(settings: LeagueSettings) -> set[str]:
    return {
        s
        for c in settings.categories
        for s in ([c.numerator, c.denominator] if c.numerator and c.denominator else [c.key])
    }


class ValuationStrategy(Protocol):
    def value(
        self,
        stats: list[PlayerStats],
        settings: LeagueSettings,
        mode: ValuationMode,
        punts: list[str],
        population_keys: list[str] | None = None,
    ) -> ValuationResult: ...


class ZScoreValuation:
    def __init__(self, config: Config):
        self.config = config

    def value(
        self,
        stats: list[PlayerStats],
        settings: LeagueSettings,
        mode: ValuationMode,
        punts: list[str],
        population_keys: list[str] | None = None,
    ) -> ValuationResult:
        if settings.scoring_type not in {"head", "headone", "roto"}:
            raise FantasyError("UNSUPPORTED_SCORING", "This strategy requires category scoring.")
        categories = settings.categories
        if any(c.key.endswith("%") and not (c.numerator and c.denominator) for c in categories):
            raise FantasyError(
                "UNSUPPORTED_CATEGORIES",
                "Percentage category requires explicit make/attempt mapping.",
            )
        if len({s.player_key for s in stats}) != len(stats):
            raise FantasyError("INVALID_INPUT", "Duplicate player stat identities.")
        if len({s.season for s in stats}) > 1:
            raise FantasyError("INVALID_INPUT", "Valuation cannot mix seasons.")
        if not categories or set(punts) - {c.key for c in categories}:
            raise FantasyError("INVALID_CATEGORIES", "Punts must be configured scoring categories.")
        if len(set(punts)) >= len(categories):
            raise FantasyError("INVALID_CATEGORIES", "At least one scored category must remain.")
        lines: dict[str, dict[str, float]] = {}
        excluded: dict[str, list[str]] = {}
        for s in stats:
            try:
                line = stat_line(s, mode)
            except FantasyError as e:
                excluded[s.player_key] = [e.message]
                continue
            missing = [k for k in required_stats(settings) if line.get(k) is None]
            if missing:
                excluded[s.player_key] = [f"missing:{k}" for k in sorted(missing)]
                continue
            gp = s.values.get("GP")
            minutes = s.values.get("MIN")
            mpg = minutes / gp if minutes is not None and gp and s.basis == "totals" else minutes
            if s.kind != "projection" and (
                gp is None
                or gp < self.config.min_games
                or mpg is None
                or mpg < self.config.min_minutes
            ):
                excluded[s.player_key] = ["minimum_games_or_minutes_not_met"]
                continue
            lines[s.player_key] = {k: v for k, v in line.items() if v is not None}
        if len(lines) < 2:
            raise FantasyError(
                "INSUFFICIENT_DATA",
                "Need two complete stat lines passing games/minutes filters. "
                "Sync stats or import projections.",
            )

        # Explicit pools stay fixed; default preliminary full-pool scores select top N, then refit.
        def fit(
            pool: list[str],
        ) -> tuple[dict[str, float], dict[str, float], dict[str, dict[str, float]]]:
            refs: dict[str, float] = {}
            for c in categories:
                if c.numerator and c.denominator:
                    attempts = sum(lines[p][c.denominator] for p in pool)
                    if attempts <= 0:
                        raise FantasyError(
                            "INSUFFICIENT_DATA", f"No shooting attempts for {c.key} baseline."
                        )
                    refs[c.key] = sum(lines[p][c.numerator] for p in pool) / attempts
            impacts = {
                p: {
                    c.key: (
                        line[c.numerator] - refs[c.key] * line[c.denominator]
                        if c.numerator and c.denominator
                        else line[c.key]
                    )
                    for c in categories
                }
                for p, line in lines.items()
            }
            averages = {c.key: mean(impacts[p][c.key] for p in pool) for c in categories}
            deviations = {c.key: pstdev(impacts[p][c.key] for p in pool) for c in categories}
            scores = {
                p: {
                    c.key: (
                        (values[c.key] - averages[c.key]) / deviations[c.key]
                        if deviations[c.key] > 1e-12
                        else 0
                    )
                    * (-1 if c.lower_is_better else 1)
                    for c in categories
                }
                for p, values in impacts.items()
            }
            return (
                {**averages, **{f"reference:{k}": v for k, v in refs.items()}},
                deviations,
                scores,
            )

        if population_keys is not None:
            pool = [p for p in dict.fromkeys(population_keys) if p in lines]
        else:
            pool = sorted(lines)
        if len(pool) < 2:
            raise FantasyError(
                "INSUFFICIENT_DATA", "Selected population needs two eligible players."
            )
        baseline, deviations, scores = fit(pool)
        if population_keys is None and len(pool) > self.config.population_size:
            pool = sorted(pool, key=lambda p: (-sum(scores[p].values()), p))[
                : self.config.population_size
            ]
            baseline, deviations, scores = fit(pool)
        values = [
            PlayerValue(
                player_key=p,
                categories=z,
                overall=sum(z[c.key] * c.weight for c in categories if c.key not in punts),
                strengths=[c for c, v in z.items() if v >= 1],
                weaknesses=[c for c, v in z.items() if v <= -1],
            )
            for p, z in scores.items()
        ]
        values.sort(key=lambda v: (-v.overall, v.player_key))
        for v in values:
            v.rank = 1 + sum(other.overall > v.overall for other in values)
            v.percentile = (
                100 * sum(other.overall < v.overall for other in values) / max(1, len(values) - 1)
            )
        return ValuationResult(
            mode=mode,
            values=values,
            population=pool,
            baseline=baseline,
            deviations=deviations,
            excluded=excluded,
            punts=punts,
        )


def aggregate(
    players: list[str],
    stats: dict[str, PlayerStats],
    settings: LeagueSettings,
    mode: ValuationMode = ValuationMode.PER_GAME,
    games: dict[str, int] | None = None,
) -> TeamProfile:
    if len(players) != len(set(players)):
        raise FantasyError("INVALID_ROSTER", "Duplicate players in roster.")
    lines: list[dict[str, float | None]] = []
    missing = []
    for p in players:
        if p not in stats:
            missing.append(p)
            lines.append({})
            continue
        try:
            line = stat_line(stats[p], ValuationMode.PER_GAME if games is not None else mode)
        except FantasyError:
            missing.append(p)
            lines.append({})
            continue
        if any(line.get(k) is None for k in required_stats(settings)):
            missing.append(p)
        if games is not None:
            if p not in games:
                missing.append(p)
                lines.append({})
                continue
            line = {k: v * games[p] if v is not None else None for k, v in line.items()}
        lines.append(line)
    totals: dict[str, float | None] = {}
    for stat in required_stats(settings):
        totals[stat] = (
            sum(float(line[stat] or 0) for line in lines)
            if all(line.get(stat) is not None for line in lines)
            else None
        )
    for c in settings.categories:
        if c.numerator and c.denominator:
            m, a = totals[c.numerator], totals[c.denominator]
            totals[c.key] = m / a if m is not None and a is not None and a > 0 else None
    return TeamProfile(values=totals, missing_players=missing)


def relative_profile(
    profile: TeamProfile, peers: list[TeamProfile], settings: LeagueSettings
) -> TeamProfile:
    result = profile.model_copy(deep=True)
    for cat in settings.categories:
        value = profile.values.get(cat.key)
        values = [p.values[cat.key] for p in peers if p.values.get(cat.key) is not None]
        if value is None or len(values) < 2:
            continue
        observed = [float(v) for v in values if v is not None]
        sd = pstdev(observed)
        sign = -1 if cat.lower_is_better else 1
        z = (value - mean(observed)) / sd * sign if sd > 1e-12 else 0
        result.category_z[cat.key] = z
        result.ranks[cat.key] = 1 + sum(v * sign > value * sign for v in observed)
        result.percentiles[cat.key] = (
            100 * sum(v * sign < value * sign for v in observed) / len(observed)
        )
        (result.strong if z >= 0.5 else result.weak if z <= -0.5 else result.balanced).append(
            cat.key
        )
    return result


def eligible(player: NBAPlayer, position: str, allow_injury: bool = True) -> bool:
    positions = set(player.eligibility.positions)
    if position == "BN":
        return True
    if position in {"IL", "IL+", "IR"}:
        return allow_injury and position in positions
    if position == "Util":
        return bool(positions & {"PG", "SG", "SF", "PF", "C", "G", "F", "Util"})
    if position == "G":
        return bool(positions & {"PG", "SG", "G"})
    if position == "F":
        return bool(positions & {"SF", "PF", "F"})
    return position in positions


def slot_names(slots: list[RosterSlot], copies: int = 1, injury: bool = True) -> list[str]:
    return [
        f"{slot.position}:{team}:{group}:{i}"
        for team in range(copies)
        for group, slot in enumerate(slots)
        if injury or slot.kind != "injury"
        for i in range(slot.count)
    ]


def assign(
    players: list[NBAPlayer], slots: list[RosterSlot], copies: int = 1, injury: bool = True
) -> SlotAssignment:
    if len({p.key for p in players}) != len(players):
        raise FantasyError("INVALID_ROSTER", "Duplicate player identities.")
    names = slot_names(slots, copies, injury)
    by_key = {p.key: p for p in players}
    owners: dict[str, str] = {}
    edges = {
        p: [s for s in names if eligible(player, s.split(":")[0], injury)]
        for p, player in by_key.items()
    }

    def augment(player_key: str, seen: set[str]) -> bool:
        for slot in edges[player_key]:
            if slot in seen:
                continue
            seen.add(slot)
            if slot not in owners or augment(owners[slot], seen):
                owners[slot] = player_key
                return True
        return False

    # Augmenting paths reassign earlier flexible players, unlike first-fit greedy placement.
    for player in players:
        augment(player.key, set())
    assignment = {player: slot for slot, player in owners.items()}
    return SlotAssignment(
        legal=len(assignment) == len(players),
        assignments=assignment,
        unassigned=[p.key for p in players if p.key not in assignment],
        open_slots=[s for s in names if s not in owners],
    )


class ReplacementModel:
    """Maximum-weight matchable basis via weighted matroid greedy + augmenting paths."""

    def __init__(
        self, players: list[NBAPlayer], values: list[PlayerValue], settings: LeagueSettings
    ):
        by_key = {p.key: p for p in players}
        self.scores = {v.player_key: v.overall for v in values}
        self.players, self.settings = players, settings
        names = slot_names(settings.roster_slots, settings.max_teams, False)
        self.edges = {
            p.key: [s for s in names if eligible(p, s.split(":")[0], False)] for p in players
        }
        self.owners: dict[str, str] = {}
        selected: list[NBAPlayer] = []
        for value in values:
            if value.player_key in by_key:
                candidate = by_key[value.player_key]
                if self._augment(candidate.key, self.owners, set()):
                    selected.append(candidate)
        self.selected = selected
        self.selected_keys = {p.key for p in selected}
        self.unselected = [
            by_key[v.player_key]
            for v in values
            if v.player_key in by_key and v.player_key not in self.selected_keys
        ]
        self.unfilled = len(slot_names(settings.roster_slots, settings.max_teams, False)) - len(
            selected
        )

    def _augment(self, player_key: str, owners: dict[str, str], seen: set[str]) -> bool:
        for slot in self.edges[player_key]:
            if slot not in seen and slot not in owners:
                owners[slot] = player_key
                return True
        for slot in self.edges[player_key]:
            if slot in seen:
                continue
            seen.add(slot)
            if self._augment(owners[slot], owners, seen):
                owners[slot] = player_key
                return True
        return False

    def _can_exchange(self, outgoing: str, incoming: str) -> bool:
        owners = {s: p for s, p in self.owners.items() if p != outgoing}
        return self._augment(incoming, owners, set())

    def evaluate(self, player: NBAPlayer) -> ReplacementValue:
        replacement = None
        if player.key in self.selected_keys:
            for other in self.unselected:
                if self._can_exchange(player.key, other.key):
                    replacement = other
                    break
        else:
            # Candidate is already below replacement; lowest displaced selected player is threshold.
            for other in reversed(self.selected):
                if self._can_exchange(other.key, player.key):
                    replacement = other
                    break
        return ReplacementValue(
            value_over_replacement=(
                self.scores[player.key] - self.scores[replacement.key]
                if replacement and self.unfilled == 0
                else None
            ),
            replacement_player=replacement.key if replacement and self.unfilled == 0 else None,
            eligible_slots=[
                s.position
                for s in self.settings.roster_slots
                if s.kind != "injury" and eligible(player, s.position)
            ],
            unfilled_league_slots=self.unfilled,
        )


def category_scarcity(
    players: list[NBAPlayer],
    available: set[str],
    valuation: ValuationResult,
    stats: dict[str, PlayerStats],
    settings: LeagueSettings,
) -> dict[str, ScarcityCategory]:
    values = {v.player_key: v for v in valuation.values}
    pool = [p for p in players if p.key in available and p.key in values]
    output = {}
    for c in settings.categories:
        ordered = sorted((values[p.key].categories[c.key] for p in pool), reverse=True)
        strong = [p for p in pool if values[p.key].categories[c.key] >= 1]
        production = 0.0
        for p in pool:
            line = stat_line(stats[p.key], valuation.mode)
            if c.numerator and c.denominator:
                impact = float(line[c.numerator] or 0) - valuation.baseline[
                    f"reference:{c.key}"
                ] * float(line[c.denominator] or 0)
                production += max(0, impact * (-1 if c.lower_is_better else 1))
            elif c.lower_is_better:
                production += max(0, valuation.baseline[c.key] - float(line[c.key] or 0))
            else:
                production += float(line[c.key] or 0)
        tier = settings.max_teams
        output[c.key] = ScarcityCategory(
            remaining_production=production,
            strong_contributors=len(strong),
            available_count=len(pool),
            median_z=median(ordered) if ordered else None,
            tier_dropoff=(mean(ordered[:tier]) - mean(ordered[tier : 2 * tier]))
            if len(ordered) > tier
            else None,
            position_concentration=dict(
                Counter(pos for p in strong for pos in p.eligibility.positions)
            ),
        )
    return output


def active_game_counts(
    engine: "TeamAnalytics", roster: list[str], schedule: ScheduleInfo, start: date, end: date
) -> dict[str, int]:
    """Maximum-cardinality, then maximum-value legal active lineup per scheduled day."""
    from fantasy_mcp.providers import schedule_volume

    volume, _, _ = schedule_volume(schedule, engine.players, start, end)
    counts = {p: 0 for p in roster if p in volume}
    slots = [s for s in engine.settings.roster_slots if s.kind == "active"]
    profile = engine.profile(roster)
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        teams = {
            t
            for g in schedule.games
            if g.date == day and g.status == "scheduled"
            for t in g.nba_teams
        }
        pool = [
            engine.by_key[p] for p in roster if p in volume and engine.by_key[p].nba_team in teams
        ]
        pool.sort(
            key=lambda p: (
                -engine.contribution(engine.values[p.key], profile)
                if p.key in engine.values
                else float("inf"),
                p.key,
            )
        )
        selected: list[NBAPlayer] = []
        for player in pool:
            if assign([*selected, player], slots, injury=False).legal:
                selected.append(player)
        for player in selected:
            counts[player.key] += 1
    return counts


def delta(before: TeamProfile, after: TeamProfile) -> dict[str, float | None]:
    return {
        k: after.values[k] - v if v is not None and after.values.get(k) is not None else None  # type: ignore[operator]
        for k, v in before.values.items()
    }


class TeamAnalytics:
    def __init__(
        self,
        settings: LeagueSettings,
        players: list[NBAPlayer],
        stats: list[PlayerStats],
        valuation: ValuationResult,
        config: Config,
        rosters: list[list[str]] | None = None,
    ):
        self.settings, self.players, self.valuation, self.config = (
            settings,
            players,
            valuation,
            config,
        )
        self.stats = {s.player_key: s for s in stats}
        self.by_key = {p.key: p for p in players}
        self.values = {v.player_key: v for v in valuation.values}
        self.peers = [aggregate(r, self.stats, settings, valuation.mode) for r in rosters or []]
        self.replacement = ReplacementModel(players, valuation.values, settings)

    def profile(self, roster: list[str]) -> TeamProfile:
        return relative_profile(
            aggregate(roster, self.stats, self.settings, self.valuation.mode),
            self.peers,
            self.settings,
        )

    def contribution(self, value: PlayerValue, before: TeamProfile) -> float:
        # Bounded roster-need adjustment, not a probability of category wins.
        return sum(
            z
            * c.weight
            * (1 + self.config.context_weight * max(-1, min(1, -before.category_z.get(c.key, 0))))
            for c in self.settings.categories
            if c.key not in self.valuation.punts
            for z in [value.categories[c.key]]
        )

    def candidate(
        self, roster: list[str], player_key: str, scarcity: dict[str, ScarcityCategory]
    ) -> Candidate:
        if player_key not in self.values:
            raise FantasyError(
                "INSUFFICIENT_DATA", "Candidate has no complete eligible valuation line."
            )
        if player_key in roster:
            raise FantasyError("INVALID_ROSTER", "Candidate already belongs to roster.")
        before, after = self.profile(roster), self.profile([*roster, player_key])
        p, v = self.by_key[player_key], self.values[player_key]
        fit = assign([self.by_key[k] for k in [*roster, player_key]], self.settings.roster_slots)
        risks = ["INJURY_RISK"] if p.status.code else []
        if self.valuation.mode != ValuationMode.PROJECTED:
            risks.append("OBSERVED_STATS_NOT_PROJECTIONS")
        if before.missing_players:
            risks.append("INCOMPLETE_ROSTER_STATS")
        scarce = [
            c
            for c, z in v.categories.items()
            if z >= 1
            and c in scarcity
            and scarcity[c].strong_contributors < self.settings.max_teams
        ]
        reasons = [f"STRONG_{c}" for c in v.strengths] + [f"WEAK_{c}" for c in v.weaknesses]
        if scarce:
            reasons.append("CATEGORY_SCARCITY")
        if any(c in before.weak for c in v.strengths):
            reasons.append("ROSTER_NEED")
        if not fit.legal:
            reasons.append("REQUIRES_ROSTER_CHANGE")
        replacement = self.replacement.evaluate(p)
        contextual = self.contribution(v, before)
        return Candidate(
            player=p,
            stats=self.stats[player_key],
            generic_value=v,
            contextual_value=contextual,
            replacement=replacement,
            before=before,
            after=after,
            deltas=delta(before, after),
            roster_fit=fit,
            scarcity=scarce,
            risks=risks,
            reasons=reasons,
        )

    def change(self, roster: list[str], outgoing: list[str], incoming: list[str]) -> ChangeAnalysis:
        if len(set(outgoing)) != len(outgoing) or len(set(incoming)) != len(incoming):
            raise FantasyError("INVALID_ROSTER", "Duplicate trade/add/drop players.")
        if set(outgoing) - set(roster) or set(incoming) & set(roster):
            raise FantasyError(
                "INVALID_ROSTER", "Outgoing players must be rostered; incoming must be new."
            )
        if any(p not in self.values for p in outgoing + incoming):
            raise FantasyError(
                "INSUFFICIENT_DATA", "Trade/add-drop participants need complete stat lines."
            )
        after_roster = [p for p in roster if p not in outgoing] + incoming
        before, after = self.profile(roster), self.profile(after_roster)
        return ChangeAnalysis(
            before=before,
            after=after,
            deltas=delta(before, after),
            generic_value_delta=sum(self.values[p].overall for p in incoming)
            - sum(self.values[p].overall for p in outgoing),
            contextual_value_delta=sum(self.contribution(self.values[p], before) for p in incoming)
            - sum(self.contribution(self.values[p], before) for p in outgoing),
            roster_fit=assign([self.by_key[p] for p in after_roster], self.settings.roster_slots),
            risks=["OBSERVED_STATS_NOT_PROJECTIONS"]
            if self.valuation.mode != ValuationMode.PROJECTED
            else [],
        )


def punt_effects(
    engine: TeamAnalytics, roster: list[str], strategies: list[list[str]]
) -> list[PuntEffect]:
    output = []
    base = ZScoreValuation(engine.config).value(
        list(engine.stats.values()),
        engine.settings,
        engine.valuation.mode,
        [],
        engine.valuation.population,
    )
    base_values = {v.player_key: v for v in base.values}
    for strategy in strategies:
        alternative = ZScoreValuation(engine.config).value(
            list(engine.stats.values()),
            engine.settings,
            engine.valuation.mode,
            strategy,
            base.population,
        )
        deltas = {
            v.player_key: v.overall - base_values[v.player_key].overall for v in alternative.values
        }
        output.append(
            PuntEffect(
                ignored=strategy,
                roster_value_delta=sum(deltas.get(p, 0) for p in roster),
                player_deltas=deltas,
                rank_changes={
                    v.player_key: base_values[v.player_key].rank - v.rank
                    for v in alternative.values
                },
                current_profile=engine.profile(roster),
            )
        )
    return output
