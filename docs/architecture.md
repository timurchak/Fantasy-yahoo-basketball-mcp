# Architecture

The implementation uses a compact package rather than empty folders for each layer:

```text
server.py / cli.py   MCP schemas, transport lifecycle, safe errors, bootstrap
service.py          cached context and composed analytical workflows
yahoo.py            GET transport, XML DTOs, normalized repository
oauth.py            consent, atomic tokens, serialized refresh
providers.py        replaceable player/projection/schedule providers
domain.py           Pydantic v2 contracts
analytics.py        deterministic valuation, matching, roster effects
draft.py            local draft rules and revision-aware edits
persistence.py      SQLite cache, snapshots, metadata and transactions
config.py           typed environment/.env settings
```

Yahoo-specific XML and resource paths remain in the adapter. MCP handlers delegate to the service. Analytical functions consume normalized models; they perform no HTTP requests. One HTTP client is reused and closed by MCP lifespan or CLI cleanup. Stdio is the only enabled transport; adding another belongs at the server boundary and would require an authentication decision.

## Configuration

| Variable | Default / role |
|---|---|
| `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET` | OAuth credentials |
| `YAHOO_REDIRECT_URI` | `https://localhost:8765/callback`, must match registration |
| `FANTASY_LEAGUE_ID`, `FANTASY_SEASON` | `30166`, `2026` |
| `FANTASY_LEAGUE_KEY`, `FANTASY_TEAM_KEY` | Optional discovered identity overrides |
| `FANTASY_DATA_DIR` | `~/.fantasy-mcp` |
| `FANTASY_PLAYER_DATA_FILE` | Optional authorized normalized export |
| `BALLDONTLIE_API_KEY` | Optional schedule access |
| `FANTASY_MIN_GAMES`, `FANTASY_MIN_MINUTES` | 10 games, 10 minutes/game |
| `FANTASY_POPULATION_SIZE` | 200 |
| `FANTASY_CONTEXT_WEIGHT` | 0.25 |

`Config` also exposes `replacement_weight=0.1`, `scarcity_weight=0.1` and TTL fields, configurable through uppercase field names. There is no hidden recent-window blending or projection weighting.

## Persistence/cache

| Data | TTL |
|---|---:|
| League/settings/discovery | 24 hours |
| Game stat map / historical stats | 30 days |
| Current stats / standings | 1 hour |
| Player ownership/injury/market collection, rosters, teams, transactions, matchups | 2 minutes |
| BDL schedule | 6 hours |
| Draft state | Always fresh SQLite read |

The combined player collection uses the shortest constituent TTL. `metadata_ttl` is reserved for a future separate metadata cache; it does not extend ownership freshness. Stats are fetched in batches of 25, with at most four Yahoo requests in flight. Batch cache keys include season/date and player identities. A cold full pool is paginated; run sync before drafting.

Per-key async locks avoid same-process cache stampedes. SQLite WAL uses short connections and a busy timeout. Draft mutations use `BEGIN IMMEDIATE`, validate order/duplicate/revision, update state and append history atomically. Cross-process refresh-token rotation uses a file lock.

Tables: `cache` (normalized JSON/provenance/expiry), `snapshots` (each refreshed result and draft edit), `meta` (schema version and discovered IDs), `drafts` (current state). Schema version 1 rejects newer unsupported formats. `Store.prune(before)` explicitly prunes only non-draft history; nothing deletes history automatically.

Transient network/429/5xx failures can return cached data up to seven days old with `stale=true` and a warning. Auth/permission failures are never masked by stale fallback. Live doctor treats stale responses as unverified. Local history is private account data; use separate directories per account.

## Security/observability

Yahoo transport exposes GET only. OAuth POST is limited to token exchange. No raw URL tool or Yahoo transaction method exists. XML uses defusedxml. Errors expose actionable codes, not provider bodies. Structured stderr logs include operation, status, timing and exception class, never tokens, callback URLs or HTTP headers. HTTP library debug logs are suppressed.

Implementation stages: models/storage → OAuth/XML/repository → draft/math → service/MCP → mocked HTTP and real stdio tests → documentation/full validation. Actual Yahoo approval and user consent remain external setup requirements.
