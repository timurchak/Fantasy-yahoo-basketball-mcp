# Fantasy Basketball Intelligence MCP

A local Python 3.12+ MCP server combining read-only Yahoo league data, deterministic basketball analytics, and a persistent offline draft board. Standard stdio works with Codex, Claude Desktop, Claude Code and other MCP clients. No OpenAI or Anthropic API key is required.

**Status:** implemented and locally tested, including a spawned stdio server. Live Yahoo league verification requires your authorized account. Synthetic test data is never used as a production fallback. No Yahoo roster, waiver or trade writes exist.

## Install

macOS/Linux:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Windows PowerShell, using Python 3.12+:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Alternatively, `uv python install 3.12` and `uv venv --python 3.12` obtain a suitable runtime. `uv sync --extra dev --locked` reproduces the checked dependency resolution.

## Yahoo setup — required manual step

1. Visit the [Yahoo Fantasy developer portal](https://sports.yahoo.com/developer/) and [access application](https://sports.yahoo.com/developer/access/). Applications are reviewed; an OAuth client alone may not grant Fantasy access.
2. Configure private Fantasy Sports **read** access. Register a redirect URI such as `https://localhost:8765/callback`, if accepted by Yahoo. Put the exact registered value in `.env`.
3. Set `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, and `YAHOO_REDIRECT_URI` in `.env`. Do not paste credentials into chat or commit them.
4. Run:

```sh
fantasy-mcp auth
fantasy-mcp doctor --live
fantasy-mcp leagues
fantasy-mcp sync
fantasy-mcp serve
```

`auth` opens Yahoo consent in your browser. Copy the **full final callback URL** into the hidden CLI prompt. A localhost connection/certificate error is expected because this flow does not start an HTTPS callback server. The URL must retain `code` and `state`. See [OAuth details](docs/yahoo.md).

Defaults target visible league ID `30166`, season `2026` (2026–27). Game and league keys are discovered. `FANTASY_TEAM_KEY` is optional when Yahoo identifies exactly one team as yours. `doctor` without `--live` checks local configuration without network access. Starting without credentials is supported; uncached Yahoo calls return `AUTH_REQUIRED`.

## Connect clients

Use absolute paths and `--env-file`: clients may start in another directory. Windows uses `.venv/Scripts/python.exe`; macOS/Linux use `.venv/bin/python`.

Codex `~/.codex/config.toml`, following [official MCP configuration](https://developers.openai.com/codex/mcp):

```toml
[mcp_servers.fantasy_basketball]
command = "/absolute/path/Yahoo-basket/.venv/bin/python"
args = ["-m", "fantasy_mcp", "--env-file", "/absolute/path/Yahoo-basket/.env", "serve"]
startup_timeout_sec = 30
tool_timeout_sec = 300
```

Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fantasy_basketball": {
      "command": "/absolute/path/Yahoo-basket/.venv/bin/python",
      "args": ["-m", "fantasy_mcp", "--env-file", "/absolute/path/Yahoo-basket/.env", "serve"]
    }
  }
}
```

Claude Code:

```sh
claude mcp add --transport stdio fantasy_basketball -- /absolute/path/Yahoo-basket/.venv/bin/python -m fantasy_mcp --env-file /absolute/path/Yahoo-basket/.env serve
```

Ready-to-edit configs are in [examples](examples), including Windows Codex. The service reserves stdout for MCP; logs go to stderr.

## Draft workflow

Authorize and `sync` before the offline draft. Ask the agent to inspect teams and rules. Supply the actual draft order, snake/linear format, rounds and your team; `create_draft` deliberately does not guess them.

- “What should I pick?” → `recommend_draft_picks` loads the active board, your roster, available pool and cached stats internally.
- “We drafted X. Record it.” → resolve the Yahoo player key, then `record_draft_pick`. The next recommendation excludes X.
- `simulate_draft_pick` compares hypothetical picks without mutation.
- `undo_draft_pick` removes the last pick; `correct_draft_pick` replaces an earlier player. Corrections, resets and imports require the latest revision.
- `sync_draft_results` imports Yahoo picks when they agree with the configured format.

`fantasy-mcp draft status --draft-id your-id` reads saved state. All edits are transaction-safe and snapshotted.

## In-season analytics

`evaluate_add_drop` and `evaluate_trade` recompute the roster's net category effects and legality, including multi-player trades. Optional `start`/`end` adds schedule-based net effects. `recommend_adds` screens candidates; a full roster is marked as requiring a drop. `recommend_streamers` and `analyze_matchup` require reliable schedule coverage. None of these tools submits transactions.

FG/FT valuation uses volume impact (`makes − reference rate × attempts`); team percentages use summed makes/attempts. Counting stats use population z-scores, with turnovers inverted. Punts hold the reference population fixed. Multi-position matching determines slot fit and replacement cost. See [formulas](docs/analytics.md), [architecture](docs/architecture.md), [Yahoo integration](docs/yahoo.md), and [tool/resource catalog](docs/mcp.md).

## Data and limitations

Yahoo is authoritative for league state. Modes remain separate: `per_game`, `total_season` (observed totals), and `projected` (actual imported forecasts). If the current pool has no usable games, the default analysis explicitly falls back to prior-season observations; rookies may be unvalued.

Optional inputs:

- `BALLDONTLIE_API_KEY`: the documented [games API](https://docs.balldontlie.io/) supplies schedules. Its first full-season load can take several minutes because requests are paced; subsequent calls use a six-hour cache. The adapter is mock-tested; live key/plan access is unverified.
- `FANTASY_PLAYER_DATA_FILE`: authorized projections/schedules in normalized JSON. See [schema](examples/provider.schema.json) and [example](examples/provider.example.json). Identities must use Yahoo keys. Schedule exports must attest coverage dates.

No automatic projection/news feed, injury return date, ADP survival probability or category-win probability is invented. Missing fields remain null or produce an actionable error. Cached results expose timestamps/staleness. Draft ranking reranks a bounded shortlist; general roster profiles include reserves. Weekly forecasts optimize active slots but do not predict injury absences or future lineup changes. Streaming is a candidate screen, not a globally optimal multi-transaction plan.

## Verification and security

```sh
pytest -q
ruff check src tests
ruff format --check src tests
mypy src
fantasy-mcp doctor
```

Tests cover analytics, matching/replacement, draft concurrency, SQLite staleness, Yahoo XML/discovery/pagination, OAuth, schedule providers, composed MCP workflows and real stdio. Live tests require `FANTASY_RUN_LIVE=1 pytest -m yahoo_live`; normal tests make no external requests.

Storage defaults to `~/.fantasy-mcp/fantasy.sqlite3` and `yahoo_tokens.json`. Use a separate `FANTASY_DATA_DIR` per account. Back it up to preserve drafts; treat it as private data. POSIX token permissions are restricted; Windows users should secure its ACLs. Tokens are plaintext on disk, never logged. `.env`, tokens and databases are git-ignored. Reauthorization clears current remote caches while retaining draft history.

Next improvements after live verification: sanitized real NBA fixtures, a licensed projection feed with rookie coverage, empirical uncertainty calibration, and multi-day acquisition optimization. Yahoo writes require a separate confirmation design and remain outside this version.
