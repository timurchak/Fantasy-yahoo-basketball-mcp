# MCP tools and resources

Uses [FastMCP typed tools](https://gofastmcp.com/servers/tools) over standard stdio. Every tool has an output schema. Result envelopes carry version, generation time, provenance and limitations. Player inputs use Yahoo keys; resolve names with search first.

| Family | Tools |
|---|---|
| League | discover_leagues, get_league, get_league_settings, get_scoring_categories, get_roster_positions, get_teams, get_standings, get_matchups, get_transactions |
| Players | search_players, get_player, get_player_stats, get_player_game_log, get_player_ownership, get_available_players, get_injured_players |
| Rosters | get_team_roster, get_my_roster, get_all_rosters, get_rostered_players, validate_roster, find_optimal_slot_assignment |
| Draft | create_draft, load_draft, reset_draft, record_draft_pick, undo_draft_pick, correct_draft_pick, get_draft_state, get_draft_board, get_available_draft_players, get_my_draft_roster, sync_draft_results |
| Analytics | analyze_team_categories, compare_players, evaluate_player_for_team, analyze_punt_strategies, analyze_category_scarcity, simulate_draft_pick, recommend_draft_picks, recommend_adds, evaluate_add_drop, evaluate_trade, analyze_matchup, recommend_streamers |
| Operations | sync_league, diagnostics |

Resources: `fantasy://league/settings`, `fantasy://draft/current`, `fantasy://roster/mine`, `fantasy://analytics/methodology`.

`get_draft_board` returns ordered revisioned state. `find_optimal_slot_assignment` means maximum-cardinality legal placement, not a points objective. Local draft mutations are annotated; inspection tools never modify Yahoo. Sync updates local storage only.

## Composition and inputs

Create a draft with `draft_id`, complete `team_order`, `my_team`, `rounds`, and explicit `snake`. All teams must belong to the configured league. Recommendation can then use `{ "count": 5, "mode": "per_game", "punts": ["FT%"] }`; active draft context is inferred. Record a pick with `player_key` and preferably the latest `expected_revision`. Reset/correct/import require a revision. Simulation accepts `player_keys` and never mutates state.

Trade inputs: `outgoing`/`incoming` lists plus optional team, mode, punts and start/end schedule dates. Add/drop: `add_player`, optional `drop_player`, same analytical options. Weekly tools need explicit covered dates. Player stats take up to 500 keys and optional season. Search filters include position, NBA team, availability, injury, minimum games/minutes, stat ranges, name/ownership/ADP sort and limit/offset. Minimum minutes is per-game; stat ranges refer to source stats.

High-level tools compose cached repositories internally. Recommendation outputs include shortlisted values with global ranks and baseline evidence, rather than all ranked players. Known errors become safe MCP tool errors with stable codes; clients must not treat these as empty results. Null means unknown, not zero.

Client configs are in [examples](../examples) and [README](../README.md). Use absolute executable/.env paths. Allow longer tool timeouts for cold bulk sync or paced schedule bootstrap. Authorize in a terminal first; OAuth never prompts inside a tool. No client-vendor API dependency exists.
