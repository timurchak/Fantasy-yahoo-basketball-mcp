# Validation record

Validated locally on Windows with Python 3.12.14 and FastMCP 3.4.7 on 2026-09-19.

| Check | Result |
|---|---|
| Full automated suite | 32 passed, 1 explicitly opt-in Yahoo live test skipped |
| Ruff lint | Passed |
| Ruff formatting | Passed, 22 Python files |
| Strict mypy on source | Passed, 14 modules |
| Source distribution and wheel build | Passed |
| Wheel install/import in isolated environment | Passed |
| Installed CLI stdio subprocess | MCP initialization, tool enumeration, cached data calls, safe errors passed |
| Production tool/resource discovery | 47 tools, all with output schemas; 4 resources |
| Local doctor | SQLite healthy; credentials absent; tokens absent |
| 500-player valuation + league replacement benchmark | About 0.30 seconds locally; synthetic pool, excludes HTTP |
| Secret/build exclusions | `.env`, virtualenv and distributions ignored by git |

Representative MCP workflow tests exercise settings, my roster, search/availability, draft state,
pick/undo, team analysis, comparison, punts, scarcity, simulation, recommendations, add/drop and trades.
Schedule tests cover coverage checks, back-to-backs, postponed games, active-slot capacity, weekly
analysis and schedule-based net roster changes. Provider tests mock HTTP; they do not prove live
provider access. OAuth tests cover state, refresh rotation, concurrent calls, 401 retry, safe 403 errors,
and explicitly stale cache use when the token service cannot be reached.

Subsequent live setup on the same day confirmed a successful Yahoo OAuth authorization and token
persistence. Fantasy API discovery returned HTTP 403 with an application-not-authorized response,
including on the game catalog and authenticated-user endpoints. League 30166 data therefore remains
unverified pending Yahoo Fantasy API application approval. Credentials and tokens remain local and
are excluded from the repository. The optional BALLDONTLIE adapter has no live key verification.

Exact Windows commands from the checkout after configuration:

```powershell
.venv\Scripts\python.exe -m fantasy_mcp auth
.venv\Scripts\python.exe -m fantasy_mcp doctor --live
.venv\Scripts\python.exe -m fantasy_mcp sync
.venv\Scripts\python.exe -m fantasy_mcp serve
```

Optional live test:

```powershell
$env:FANTASY_RUN_LIVE = '1'
.venv\Scripts\python.exe -m pytest -m yahoo_live -q
```

These results establish local implementation/protocol behavior. Production league readiness still
requires Yahoo approval/consent and verification of actual NBA endpoint responses. CI includes Linux,
macOS and Windows jobs, but remote CI has not been run from this local checkout.
