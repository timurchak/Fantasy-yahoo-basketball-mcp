from datetime import UTC, datetime
from datetime import date as Date
from enum import StrEnum
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Provenance(Model):
    source: str
    retrieved_at: datetime = Field(default_factory=now)
    season: int | None = None
    stale: bool = False
    cached: bool = False
    sample_size: int | None = None
    kind: str = "observed"
    warnings: list[str] = Field(default_factory=list)


T = TypeVar("T")


class Result[T](Model):
    data: T
    provenance: list[Provenance] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=now)
    schema_version: int = 1


class ScoringCategory(Model):
    key: str
    name: str
    yahoo_stat_id: str | None = None
    lower_is_better: bool = False
    numerator: str | None = None
    denominator: str | None = None
    weight: float = 1


class RosterSlot(Model):
    position: str
    count: int = Field(ge=0)
    kind: Literal["active", "bench", "injury"] = "active"


class LeagueSettings(Model):
    scoring_type: str
    categories: list[ScoringCategory]
    roster_slots: list[RosterSlot]
    max_teams: int = Field(gt=0)
    max_weekly_adds: int | None = None
    draft_type: str | None = None
    rules: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    source: str = "yahoo"


class League(Model):
    key: str
    league_id: str
    game_key: str
    season: int
    name: str
    current_week: int | None = None
    draft_status: str | None = None


class FantasyManager(Model):
    manager_id: str
    nickname: str | None = None
    is_current_login: bool = False


class FantasyTeam(Model):
    key: str
    name: str
    managers: list[FantasyManager] = Field(default_factory=list)
    weekly_adds_used: int | None = None
    weekly_adds_week: int | None = None
    is_owned_by_current_login: bool = False


class PlayerEligibility(Model):
    positions: list[str] = Field(default_factory=list)


class PlayerStatus(Model):
    code: str | None = None
    description: str | None = None
    injury_eligible: bool | None = None


class PlayerOwnership(Model):
    availability: Literal["free_agent", "waivers", "rostered", "unknown"] = "unknown"
    team_key: str | None = None
    percent_owned: float | None = None
    waiver_date: str | None = None


class NBAPlayer(Model):
    key: str
    yahoo_id: str
    name: str
    nba_team: str | None = None
    eligibility: PlayerEligibility = Field(default_factory=PlayerEligibility)
    status: PlayerStatus = Field(default_factory=PlayerStatus)
    ownership: PlayerOwnership = Field(default_factory=PlayerOwnership)
    external_ids: dict[str, str] = Field(default_factory=dict)
    adp: float | None = None
    preseason_rank: int | None = None
    current_rank: int | None = None


Nonnegative = Annotated[float, Field(ge=0)]


class PlayerStats(Model):
    player_key: str
    season: int
    basis: Literal["totals", "per_game"] = "totals"
    kind: Literal["historical", "current", "projection"] = "current"
    values: dict[str, Nonnegative | None]
    provenance: Provenance

    @model_validator(mode="after")
    def valid_shooting(self) -> "PlayerStats":
        for made, attempted in [("FGM", "FGA"), ("FTM", "FTA")]:
            m, a = self.values.get(made), self.values.get(attempted)
            if m is not None and a is not None and m > a:
                raise ValueError(f"{made} cannot exceed {attempted}")
        return self


class PlayerProjection(PlayerStats):
    kind: Literal["projection"] = "projection"


class RosterEntry(Model):
    player: NBAPlayer
    selected_position: str | None = None


class FantasyRoster(Model):
    team_key: str
    entries: list[RosterEntry]
    date: Date | None = None


class Matchup(Model):
    week: int | None = None
    status: str | None = None
    team_keys: list[str]
    category_totals: dict[str, dict[str, float | None]] = Field(default_factory=dict)


class Standing(Model):
    team_key: str
    rank: int | None = None
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None


class Transaction(Model):
    key: str
    type: str
    status: str | None = None
    timestamp: str | None = None
    player_keys: list[str] = Field(default_factory=list)


class DraftPick(Model):
    overall_pick: int = Field(gt=0)
    round: int = Field(gt=0)
    pick_in_round: int = Field(gt=0)
    team_key: str
    player_key: str
    timestamp: datetime = Field(default_factory=now)
    source: str = "manual"


class DraftState(Model):
    draft_id: str
    league_key: str
    team_order: list[str]
    team_names: dict[str, str] = Field(default_factory=dict)
    my_team: str
    rounds: int = Field(gt=0, le=100)
    snake: bool
    picks: list[DraftPick] = Field(default_factory=list)
    revision: int = 0

    @model_validator(mode="after")
    def valid_order(self) -> "DraftState":
        if len(self.team_order) < 2 or len(set(self.team_order)) != len(self.team_order):
            raise ValueError("Draft order requires at least two distinct teams")
        if self.my_team not in self.team_order:
            raise ValueError("my_team must occur in team_order")
        return self


class ScheduleGame(Model):
    game_id: str
    date: Date
    nba_teams: list[str]
    status: Literal["scheduled", "final", "postponed"] = "scheduled"


class ScheduleInfo(Model):
    season: int
    games: list[ScheduleGame]
    provenance: Provenance
    coverage_start: Date | None = None
    coverage_end: Date | None = None


class ValuationMode(StrEnum):
    PER_GAME = "per_game"
    TOTAL = "total_season"
    PROJECTED = "projected"


class PlayerValue(Model):
    player_key: str
    categories: dict[str, float]
    overall: float
    rank: int = 0
    percentile: float = 0
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class ValuationResult(Model):
    mode: ValuationMode
    values: list[PlayerValue]
    population: list[str]
    baseline: dict[str, float]
    deviations: dict[str, float]
    excluded: dict[str, list[str]]
    punts: list[str]


class TeamProfile(Model):
    values: dict[str, float | None]
    category_z: dict[str, float] = Field(default_factory=dict)
    ranks: dict[str, int] = Field(default_factory=dict)
    percentiles: dict[str, float] = Field(default_factory=dict)
    strong: list[str] = Field(default_factory=list)
    weak: list[str] = Field(default_factory=list)
    balanced: list[str] = Field(default_factory=list)
    missing_players: list[str] = Field(default_factory=list)


class SlotAssignment(Model):
    legal: bool
    assignments: dict[str, str]
    unassigned: list[str]
    open_slots: list[str]


class ReplacementValue(Model):
    value_over_replacement: float | None
    replacement_player: str | None
    eligible_slots: list[str]
    unfilled_league_slots: int
    methodology: str = "weighted_transversal_matroid"


class Candidate(Model):
    player: NBAPlayer
    stats: PlayerStats
    generic_value: PlayerValue
    contextual_value: float
    replacement: ReplacementValue
    before: TeamProfile
    after: TeamProfile
    deltas: dict[str, float | None]
    roster_fit: SlotAssignment
    scarcity: list[str]
    risks: list[str]
    reasons: list[str]
    recommendation_score: float = 0
    score_components: dict[str, float] = Field(default_factory=dict)


class DraftRecommendationResult(Model):
    league: League
    draft_context: DraftState | None
    team_profile: TeamProfile
    candidates: list[Candidate]
    valuation: ValuationResult


class ChangeAnalysis(Model):
    before: TeamProfile
    after: TeamProfile
    deltas: dict[str, float | None]
    generic_value_delta: float
    contextual_value_delta: float
    roster_fit: SlotAssignment
    acquisitions_remaining: int | None = None
    risks: list[str] = Field(default_factory=list)
    schedule_before: TeamProfile | None = None
    schedule_after: TeamProfile | None = None
    schedule_deltas: dict[str, float | None] | None = None


class ScarcityCategory(Model):
    remaining_production: float
    strong_contributors: int
    available_count: int
    median_z: float | None
    tier_dropoff: float | None
    position_concentration: dict[str, int]


class PuntEffect(Model):
    ignored: list[str]
    roster_value_delta: float
    player_deltas: dict[str, float]
    rank_changes: dict[str, int]
    current_profile: TeamProfile


class StreamCandidate(Model):
    player: NBAPlayer
    games: int
    contribution: TeamProfile
    relative_score: float
    reasons: list[str]
    back_to_backs: list[str]
    roster_fit: SlotAssignment


class WeeklyAnalysis(Model):
    candidates: list[StreamCandidate] = Field(default_factory=list)
    acquisitions_remaining: int | None = None
    daily_game_volume: dict[str, int]
    our_totals: TeamProfile | None = None
    opponent_totals: TeamProfile | None = None
    our_minus_opponent: dict[str, float | None] | None = None
    our_remaining_games: int | None = None
    opponent_remaining_games: int | None = None
    probabilities: dict[str, float] | None = None
