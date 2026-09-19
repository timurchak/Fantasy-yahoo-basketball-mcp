import time
from pathlib import Path
from xml.etree.ElementTree import tostring

import httpx
import pytest
from pydantic import SecretStr

from fantasy_mcp.oauth import OAuth, Token
from fantasy_mcp.persistence import Store
from fantasy_mcp.yahoo import YahooClient, YahooRepository, parse_xml


def league_xml():
    root = parse_xml((Path(__file__).parent / "fixtures/yahoo.xml").read_bytes())
    return tostring(root.find("league")).decode()


async def test_discovery_caches_keys_and_settings(config):
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("stat_categories"):
            data = (
                "<game><stat_categories><stats><stat><stat_id>5</stat_id>"
                "<display_name>FG%</display_name></stat><stat><stat_id>12</stat_id>"
                "<display_name>TO</display_name></stat></stats></stat_categories></game>"
            )
        elif "users;" in path:
            data = (
                "<users><user><games><game><leagues>"
                + league_xml()
                + "</leagues></game></games></user></users>"
            )
        else:
            data = league_xml()
        return httpx.Response(200, text="<fantasy_content>" + data + "</fantasy_content>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuth(config, http)
        oauth.save(
            Token(
                access_token="fixture",
                refresh_token="fixture-refresh",
                expires_at=time.time() + 3600,
            )
        )
        store = Store(config.data_dir / "db.sqlite")
        repo = YahooRepository(YahooClient(oauth, http), store, config)
        result = await repo.settings()
        assert result.data.max_weekly_adds == 4
        assert store.get_meta("league_key:2026:30166") == "999.l.30166"
        count = len(requests)
        assert (await repo.settings()).provenance[0].cached
        assert len(requests) == count
        assert all(r.method == "GET" for r in requests)


async def test_401_refresh_once_then_get(config):
    config.client_id = SecretStr("fixture")
    config.client_secret = SecretStr("fixture")
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        if request.headers.get("Authorization") == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, text="<fantasy_content><games/></fantasy_content>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuth(config, http)
        oauth.save(
            Token(access_token="old", refresh_token="preserved", expires_at=time.time() + 3600)
        )
        assert (await YahooClient(oauth, http).get("games")).tag == "fantasy_content"
        assert [r.method for r in calls] == ["GET", "POST", "GET"]
        assert oauth.read().refresh_token.get_secret_value() == "preserved"


async def test_multi_page_players_no_missing_last_page(config):
    def player(i):
        return (
            f"<player><player_key>999.p.{i}</player_key><player_id>{i}</player_id>"
            f"<name><full>Test {i}</full></name></player>"
        )

    calls = []

    def handler(request):
        calls.append(request)
        if "users;" in request.url.path:
            data = league_xml()
        else:
            start = 25 if ";start=25;" in request.url.path else 0
            count = 5 if start else 25
            data = (
                "<players>" + "".join(player(i) for i in range(start, start + count)) + "</players>"
            )
        return httpx.Response(200, text="<fantasy_content>" + data + "</fantasy_content>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        oauth = OAuth(config, http)
        oauth.save(
            Token(access_token="fixture", refresh_token="fixture", expires_at=time.time() + 3600)
        )
        repo = YahooRepository(
            YahooClient(oauth, http), Store(config.data_dir / "db.sqlite"), config
        )
        players = await repo.players()
        assert len(players.data) == 30
        assert players.data[-1].key == "999.p.29"


@pytest.mark.yahoo_live
async def test_live_yahoo_only_when_explicitly_enabled():
    import os

    from fantasy_mcp.config import Config
    from fantasy_mcp.server import build_service

    if os.environ.get("FANTASY_RUN_LIVE") != "1":
        pytest.skip("Set FANTASY_RUN_LIVE=1 and authorize Yahoo to enable live checks")
    service, http = build_service(Config())
    try:
        result = await service.doctor(True)
        for name in ["league", "settings", "teams", "players"]:
            assert result.data[name]["ok"], result.data[name]
            assert not result.data[name]["stale"]
        assert (await service.repo.roster(await service.repo.my_team())).data.entries
    finally:
        await http.aclose()
