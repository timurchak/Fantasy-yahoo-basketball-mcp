# Yahoo integration

References checked: [Fantasy REST documentation](https://sports.yahoo.com/developer/docs/), [current access program](https://sports.yahoo.com/developer/access/), [OAuth flow](https://developer.yahoo.com/oauth2/guide/flows_authcode/). The legacy guide redirects to the newer portal. Access approval and live account behavior remain unverified until user setup.

## OAuth

Consent uses `https://api.login.yahoo.com/oauth2/request_auth`; exchange/refresh uses `https://api.login.yahoo.com/oauth2/get_token`. The CLI generates random state, validates the full returned redirect and exact state, then exchanges the code using HTTP Basic client authentication. It does not guess an undocumented scope. Register Fantasy read access on the Yahoo app.

The hidden callback-URL prompt avoids a local HTTPS callback server. A browser localhost error after consent is acceptable if the URL includes code/state. Redirect registration must match exactly. Never share that URL.

Tokens are atomically persisted with expiry and restricted POSIX permissions. Refresh starts 60 seconds before expiry. Async and cross-process file locks serialize rotation. An omitted replacement refresh token retains the previous one. A 401 refreshes once; 403 requires checking app/account access. No error includes a raw token response.

## Discovery and parsing

Authenticated user games/leagues are filtered to NBA and configured season; the visible league ID selects the returned full key. An explicit league-key override verifies its season. Historical requests discover their season's game key and map Yahoo player IDs while preserving caller identities.

The adapter centralizes XML, resource paths, settings, player/team/roster DTOs, standings, transactions and draft results. Game stat labels map Yahoo IDs; no permanent seasonal key or stat-ID table is assumed. Combined FGM/A and FTM/A split into makes/attempts. Missing numerics and '-' remain null.

Player pages and stat batches use 25 entries. Up to four Yahoo requests run concurrently. Pagination guards detect non-advancing pages. Transient failures back off; cached fallback is explicitly stale. League settings remain authoritative, with scalar Yahoo rules retained for inspection.

Yahoo projection/news/schedule feeds are not assumed. Optional BALLDONTLIE uses its documented games endpoint, normalizes team abbreviations, follows cursors and caches schedules. An authorized JSON export can supply projections or schedule coverage. Neither optional provider changes Yahoo league data.

## Troubleshooting

| Error | Action |
|---|---|
| `AUTH_REQUIRED` | Set credentials and run auth |
| `TOKEN_REFRESH_FAILED` | Verify app credentials/access; reauthorize |
| `YAHOO_ACCESS_DENIED` | Verify Fantasy approval/read permissions and league membership |
| `LEAGUE_NOT_FOUND` | Run leagues; check account, season and visible ID |
| `TEAM_NOT_IDENTIFIED` | Set team key from get_teams |
| `SEASON_NOT_FOUND` | Verify Yahoo published this NBA season |
| `PROVIDER_RESPONSE_INVALID` | Capture manually sanitized structural XML for maintenance |
| `DATA_PROVIDER_UNAVAILABLE` | Retry; inspect staleness of returned cached data |

`doctor --live` forces network checks and reports stale fallback as unverified. Bug reports should include version and error code, never `.env`, callback URL or token files. The automated suite uses explicitly synthetic fixtures; live integration is opt-in.
