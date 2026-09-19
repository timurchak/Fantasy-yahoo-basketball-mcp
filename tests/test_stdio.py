import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from fantasy_mcp.domain import Provenance


async def test_real_stdio_process_with_cached_yahoo_data(service, tmp_path):
    """Spawn installed CLI, initialize MCP, then exercise real repository over stdio."""
    prov = Provenance(source="synthetic_stdio_fixture", season=2026)
    league = (await service.repo.league()).data
    service.store.put("leagues:2026", [league.model_dump(mode="json")], prov, 3600)
    service.store.put(
        "settings:" + league.key,
        (await service.repo.settings()).data.model_dump(mode="json"),
        prov,
        3600,
    )
    service.store.put(
        "players:" + league.key,
        [p.model_dump(mode="json") for p in (await service.repo.players()).data],
        prov,
        3600,
    )
    # Production defaults use fantasy.sqlite3. Keep test data in a private temporary directory.
    service.store.path.rename(tmp_path / "fantasy.sqlite3")
    env_file = tmp_path / "stdio.env"
    env_file.write_text(f'FANTASY_DATA_DIR="{tmp_path.as_posix()}"\nFANTASY_SEASON=2026\n')
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "fantasy_mcp", "--env-file", str(env_file), "serve"],
        cwd=str(tmp_path),
        log_file=tmp_path / "server.log",
    )
    async with Client(transport) as client:
        result = await client.call_tool("diagnostics", {})
        assert result.structured_content["data"]["version"] == "0.1.0"
        settings = await client.call_tool("get_league_settings", {})
        assert settings.structured_content["data"]["max_teams"] == 2
        available = await client.call_tool("get_available_players", {})
        assert len(available.structured_content["data"]) == 16
        error = await client.call_tool(
            "get_player", {"player_key": "unknown"}, raise_on_error=False
        )
        assert error.is_error
        assert "PLAYER_NOT_FOUND" in str(error.content)
