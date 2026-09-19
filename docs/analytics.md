# Deterministic analytics

Background references: [NIST standardization](https://itl.nist.gov/div898/software/dataplot/refman2/auxillar/standard.htm) and [MIT matroid/greedy algorithm notes](https://ocw.mit.edu/courses/18-997-topics-in-combinatorial-optimization-spring-2004/pages/lecture-notes/). The shooting-impact derivation below is explicit algebra, not a copied proprietary ranking.

LeagueSettings supplies categories, direction, weight, slots, team count and acquisition limits. The reference league file is not used as authoritative configuration. Analytics returns data; the LLM interprets it.

## Basis and population

Stats have player key, season, basis (`totals`/`per_game`), kind (`current`/`historical`/`projection`) and provenance. GP is exposure, not divided during per-game conversion. Counts and minutes are divided/multiplied by GP as needed. Projected mode rejects observations; total-season mode does not extrapolate incomplete totals.

Missing required stats exclude a player with a reason. Missing attempts and TO never become zero. Observations must meet configured GP/MPG thresholds; forecasts require complete stats but bypass observed-sample thresholds. Duplicates, mixed seasons, nonfinite/negative values and makes above attempts are rejected.

Default pool: fit all eligible complete observations, take the top configured N by preliminary category sum, then refit. Score all eligible players against that subset. The strategy/service also accepts fixed, rostered-level or available-player pools. Two eligible lines are required. This is a transparent baseline approximation, not a universal ranking.

## Z-scores and shooting

```text
z_ic = direction_c * (x_ic - pool_mean_c) / pool_population_SD_c
direction = -1 when lower is better; otherwise +1
overall_i = sum(weight_c * z_ic for retained categories)
```

Standard deviation uses ddof=0. Constant categories score zero. Ranks use competition ranking for ties; percentile is the fraction of other scored players strictly below the candidate.

FG/FT use surplus makes, not raw percentage z-scores:

```text
reference_FG = sum_pool(FGM) / sum_pool(FGA)
FG_impact_i = FGM_i - reference_FG * FGA_i
FT_impact_i = FTM_i - reference_FT * FTA_i
```

Standardize these impacts. A 9/10 FT shooter has ten times the impact of a 0.9/1 shooter at the same baseline. Algebraically, adding a player to a reference roster with A attempts changes its percentage by `impact_i / (A + attempts_i)`. Surplus makes is the additive numerator approximation; simulation calculates the exact roster ratio delta. Ordinary z-score normalization and pooled-ratio algebra justify the formula; it does not imply a calibrated category-win probability.

Team percentages are `sum(FGM)/sum(FGA)` and `sum(FTM)/sum(FTA)`. Zero attempts give null. Missing inputs make affected categories unknown. Team z-scores/ranks use actual peer-roster aggregates when at least two usable peers exist. General profiles include reserves. A per-game roster profile means one game from each player, not a weekly forecast.

## Punts and contextual score

Punts exclude categories while keeping the baseline pool fixed. Output includes score/rank changes and the retained profile. Removing a positive category mechanically lowers a sum; that alone does not indicate a bad punt. The lowest-ranked category is not automatically recommended for a punt.

```text
need_c = clamp(-team_category_z_c, -1, +1)
contextual_i = sum(weight_c * z_ic * (1 + context_weight * need_c))
```

Default context weight 0.25 bounds multipliers to 0.75–1.25. Without usable peers the multiplier is one. Exact before/after totals and percentage deltas accompany this heuristic.

Recommendations shortlist `max(30, 3*count)` by legal fit and contextual value, then rerank by fit and:

```text
score = contextual + 0.1 * value_over_replacement
                   + 0.1 * draft_progress * scarce_category_contribution
```

The weights are configurable. Progress is recorded/configured total picks (one for free agents). Unknown replacement remains null and adds no bonus. Scarce contribution sums positive, non-punted category z-scores with fewer than one strong remaining contributor per league team. Components are returned. Market ADP stays separate, and injury flags do not imply an invented availability discount.

## Eligibility/replacement

A bipartite graph connects Yahoo-eligible players and slots. PG/SG can fill G, SF/PF fill F, standard court positions fill Util, any player fills BN, and injury slots require matching Yahoo eligibility. Augmenting paths move flexible players to find legal maximum matching.

Expand all league non-injury slots. Process players in descending value, keeping each only when the selected set remains matchable. Matchable sets form a transversal matroid; this is a maximum-weight basis for the represented market. Incremental matching avoids rebuilding the solution for every player.

Remove a selected player and find the highest-valued unselected player that allows a full rematch. For a below-replacement player, find the lowest-valued selected player they can displace. The score difference is value over replacement. If the pool cannot fill the market, return null plus unfilled slots. Bench counts; temporary injury slots do not. This models eligibility rather than an arbitrary “top 156,” without predicting other managers' picks.

## Scarcity, transactions, schedule

Scarcity returns remaining production, strong contributors (z≥1), median z, first/second league-sized tier dropoff and position concentration. Ratio production means positive surplus makes; TO means savings below baseline. Multi-position players can occur in several position counts.

Add/drop and multi-player trades replace actual roster members together, recomputing category ratios and slot legality. Adds require known availability and expose weekly limits/waiver constraints. This assesses our roster, not both managers' utility or trade acceptance.

Covered scheduled games multiply observed per-game rates. Postponed/final/unknown games add no remaining volume. Weekly matchup forecasts maximize daily active-slot cardinality and then relative value; returned counts are modeled starts. Injury absences, future lineup changes and waiver success remain unknown. Streaming screens contextual rate value × games with fit/limit evidence; it is not a global multi-transaction optimizer. Verify transaction effective dates and league locks.

Tests cover ratio/volume/TO math, null versus zero, modes, fixed-pool punts, flexible matching, replacement against exhaustive small-market solutions, contextual net effects and schedule coverage. These verify implementation, not forecast accuracy. Historical snapshots support future empirical calibration.
