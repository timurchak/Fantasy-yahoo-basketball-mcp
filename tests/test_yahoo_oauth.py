import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr, TypeAdapter

from fantasy_mcp.domain import Provenance
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.oauth import OAuth, Token
from fantasy_mcp.persistence import Store
from fantasy_mcp.yahoo import (
    YahooClient,
    YahooRepository,
    key,
    parse_league,
    parse_player,
    parse_settings,
    parse_stats,
    parse_xml,
)


def test_xml_normalization_and_namespace():
    root = parse_xml((Path(__file__).parent / "fixtures/yahoo.xml").read_bytes())
    league = parse_league(root.find("league"))
    mapping = {"3": "FGM/A", "5": "FG%", "12": "TO"}
    settings = parse_settings(root, league, mapping)
    assert settings.max_teams == 12
    assert settings.max_weekly_adds == 4
    assert settings.categories[1].lower_is_better
    assert settings.roster_slots[2].kind == "injury"
    player_node = root.find(".//player")
    player = parse_player(player_node)
    assert player.ownership.availability == "free_agent"
    assert player.eligibility.positions == ["PG", "SG"]
    line = parse_stats(player_node, 2026, mapping, Provenance(source="fixture"))
    assert line.values["FGM"] == 50
    assert line.values["FGA"] == 100
    assert line.values["TO"] is None
    with pytest.raises(FantasyError):
        parse_xml(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>')
    with pytest.raises(FantasyError):
        key("1.p.1/../../secrets")


async def test_oauth_state_refresh_rotation_and_secret_storage(config):
    config.client_id = SecretStr("test-client")
    config.client_secret = SecretStr("test-secret")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "test-access-2",
                "refresh_token": "test-refresh-2",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuth(config, http)
        url, state = oauth.authorization_url()
        assert parse_qs(urlparse(url).query)["state"] == [state]
        with pytest.raises(FantasyError, match="state"):
            await oauth.finish(config.redirect_uri + "?state=wrong&code=unused", state)
        oauth.save(
            Token(access_token="expired", refresh_token="test-refresh", expires_at=time.time() - 1)
        )
        values = await asyncio.gather(*[oauth.access_token() for _ in range(5)])
        assert values == ["test-access-2"] * 5
        assert len(calls) == 1
        assert b"grant_type=refresh_token" in calls[0].content
        stored = json.loads(oauth.path.read_text())
        assert stored["refresh_token"] == "test-refresh-2"
        assert "test-refresh-2" not in repr(oauth.read())


async def test_errors_are_sanitized_and_403_not_retried(config):
    config.client_id = SecretStr("test")
    config.client_secret = SecretStr("test")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="SECRET_PROVIDER_BODY")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuth(config, http)
        oauth.save(
            Token(
                access_token="test-access",
                refresh_token="test-refresh",
                expires_at=time.time() + 3600,
            )
        )
        with pytest.raises(FantasyError, match="YAHOO_ACCESS_DENIED") as error:
            await YahooClient(oauth, http).get("games;game_codes=nba")
        assert "SECRET_PROVIDER_BODY" not in str(error.value)
        assert len(calls) == 1


async def test_stale_fallback_is_explicit_but_not_for_auth(config):
    store = Store(config.data_dir / "cache.sqlite")
    store.put("x", [1], Provenance(source="yahoo"), -1)
    async with httpx.AsyncClient() as http:
        repo = YahooRepository(YahooClient(OAuth(config, http), http), store, config)

        async def unavailable():
            raise FantasyError("DATA_PROVIDER_UNAVAILABLE", "unavailable")

        result = await repo.cached("x", TypeAdapter(list[int]), 60, unavailable)
        assert result.provenance[0].stale
        assert "STALE_DATA" in result.provenance[0].warnings[0]

        async def forbidden():
            raise FantasyError("AUTH_REQUIRED", "login required")

        with pytest.raises(FantasyError, match="AUTH_REQUIRED"):
            await repo.cached("x", TypeAdapter(list[int]), 60, forbidden)


def test_display_only_shooting_components_not_scored():
    from xml.etree.ElementTree import SubElement

    root = parse_xml((Path(__file__).parent / "fixtures/yahoo.xml").read_bytes())
    stats = root.find(".//settings/stat_categories/stats")
    stat = SubElement(stats, "stat")
    SubElement(stat, "stat_id").text = "3"
    types = SubElement(stat, "stat_position_types")
    position = SubElement(types, "stat_position_type")
    SubElement(position, "is_only_display_stat").text = "1"
    settings = parse_settings(
        root, parse_league(root.find("league")), {"3": "FGM/A", "5": "FG%", "12": "TO"}
    )
    assert [c.key for c in settings.categories] == ["FG%", "TO"]


async def test_offline_token_refresh_allows_marked_cached_result(config):
    config.client_id = SecretStr("test")
    config.client_secret = SecretStr("test")

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as http:
        oauth = OAuth(config, http)
        oauth.save(Token(access_token="expired", refresh_token="retained", expires_at=0))
        store = Store(config.data_dir / "offline.sqlite")
        store.put("offline", [1], Provenance(source="yahoo"), -1)
        repo = YahooRepository(YahooClient(oauth, http), store, config)

        async def fetch():
            await repo.client.get("games")
            return []

        result = await repo.cached("offline", TypeAdapter(list[int]), 60, fetch)
        assert result.data == [1]
        assert result.provenance[0].stale


def test_season_label_does_not_follow_calendar_year():
    root = parse_xml((Path(__file__).parent / "fixtures/yahoo.xml").read_bytes())
    player = root.find(".//player")
    line = parse_stats(player, 2025, {"3": "FGM/A"}, Provenance(source="fixture"), "current")
    assert line.kind == "current"  # 2025-26 can still be current in calendar 2026.
    assert line.provenance.kind == "current"
