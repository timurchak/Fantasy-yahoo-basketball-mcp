import asyncio
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from filelock import FileLock
from pydantic import BaseModel, SecretStr

from fantasy_mcp.config import Config
from fantasy_mcp.errors import FantasyError

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


class Token(BaseModel):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: float


class OAuth:
    def __init__(self, config: Config, http: httpx.AsyncClient):
        self.config, self.http = config, http
        self.path = config.data_dir / "yahoo_tokens.json"
        self.lock = asyncio.Lock()

    def credentials(self) -> tuple[str, str]:
        pair = (
            self.config.client_id.get_secret_value(),
            self.config.client_secret.get_secret_value(),
        )
        if not all(pair):
            raise FantasyError(
                "AUTH_REQUIRED", "Set Yahoo credentials in .env, then run fantasy-mcp auth."
            )
        return pair

    def authorization_url(self) -> tuple[str, str]:
        client, _ = self.credentials()
        state = secrets.token_urlsafe(32)
        return AUTH_URL + "?" + urlencode(
            {
                "client_id": client,
                "redirect_uri": self.config.redirect_uri,
                "response_type": "code",
                "state": state,
            }
        ), state

    def read(self) -> Token:
        try:
            return Token.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise FantasyError(
                "AUTH_REQUIRED", "Run fantasy-mcp auth to authorize this account."
            ) from None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(
                {
                    "access_token": token.access_token.get_secret_value(),
                    "refresh_token": token.refresh_token.get_secret_value(),
                    "expires_at": token.expires_at,
                },
                out,
            )
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.path)
        if os.name != "nt":
            self.path.chmod(0o600)

    async def exchange(self, data: dict[str, str], previous: Token | None = None) -> Token:
        try:
            response = await self.http.post(TOKEN_URL, data=data, auth=self.credentials())
            if response.status_code == 429 or response.status_code >= 500:
                raise FantasyError(
                    "DATA_PROVIDER_UNAVAILABLE", "Yahoo token service is temporarily unavailable."
                )
            if response.status_code != 200:
                raise FantasyError(
                    "TOKEN_REFRESH_FAILED" if previous else "AUTH_REQUIRED",
                    "Yahoo token request failed. Check app access and re-run auth.",
                )
            payload = response.json()
            refresh = payload.get("refresh_token")
            if not refresh and previous:
                refresh = previous.refresh_token.get_secret_value()
            if not refresh:
                raise ValueError("Missing refresh token")
            token = Token(
                access_token=payload["access_token"],
                refresh_token=refresh,
                expires_at=time.time() + float(payload["expires_in"]),
            )
            self.save(token)
            return token
        except httpx.HTTPError:
            raise FantasyError(
                "DATA_PROVIDER_UNAVAILABLE", "Yahoo token service cannot be reached."
            ) from None
        except (ValueError, KeyError):
            raise FantasyError(
                "TOKEN_REFRESH_FAILED" if previous else "AUTH_REQUIRED",
                "Yahoo token response unavailable or invalid; retry authorization.",
            ) from None

    async def finish(self, callback_url: str, state: str) -> Token:
        callback, expected = urlparse(callback_url), urlparse(self.config.redirect_uri)
        if (callback.scheme, callback.netloc, callback.path) != (
            expected.scheme,
            expected.netloc,
            expected.path,
        ):
            raise FantasyError(
                "AUTH_REQUIRED", "Callback does not match the configured redirect URI."
            )
        args = parse_qs(callback.query)
        actual = args.get("state", [""])[0]
        if not secrets.compare_digest(actual, state) or not args.get("code"):
            raise FantasyError(
                "AUTH_REQUIRED", "Missing code or mismatched OAuth state; restart auth."
            )
        return await self.exchange(
            {
                "grant_type": "authorization_code",
                "code": args["code"][0],
                "redirect_uri": self.config.redirect_uri,
            }
        )

    async def access_token(self, rejected: str | None = None) -> str:
        async with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Coordinate refresh-token rotation across CLI and multiple MCP client processes.
            file_lock = FileLock(str(self.path) + ".lock", thread_local=False)
            await asyncio.to_thread(file_lock.acquire, timeout=30)
            try:
                token = self.read()
                if token.expires_at <= time.time() + 60 or (
                    rejected is not None
                    and secrets.compare_digest(token.access_token.get_secret_value(), rejected)
                ):
                    token = await self.exchange(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": token.refresh_token.get_secret_value(),
                            "redirect_uri": self.config.redirect_uri,
                        },
                        token,
                    )
                return token.access_token.get_secret_value()
            finally:
                file_lock.release()
