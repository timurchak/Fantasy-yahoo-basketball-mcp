import pytest
from fastmcp import Client

from fantasy_mcp.server import create_server


async def test_representative_mcp_workflow(service):
    # Real MCP client serialization and validation against a deterministic data boundary.
    async with Client(create_server(service)) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert len(names) >= 40
        assert "recommend_draft_picks" in names
        assert all(t.outputSchema for t in tools)
        for name, args in [
            ("get_league_settings", {}),
            ("get_my_roster", {}),
            ("search_players", {"query": "Synthetic"}),
            ("get_available_players", {}),
            ("analyze_team_categories", {}),
            ("compare_players", {"player_keys": ["999.p.5", "999.p.6"]}),
            ("analyze_punt_strategies", {"strategies": [["FT%"], ["AST", "TO"]]}),
            ("analyze_category_scarcity", {}),
            ("evaluate_add_drop", {"add_player": "999.p.5", "drop_player": "999.p.1"}),
            ("evaluate_trade", {"incoming": ["999.p.3", "999.p.4"], "outgoing": ["999.p.1"]}),
        ]:
            result = await client.call_tool(name, args)
            assert not result.is_error, name
            assert result.structured_content is not None
        await client.call_tool(
            "create_draft",
            {
                "draft_id": "test",
                "team_order": ["999.l.30166.t.1", "999.l.30166.t.2"],
                "my_team": "999.l.30166.t.1",
                "rounds": 4,
                "snake": True,
            },
        )
        for name, args in [
            ("get_draft_state", {}),
            ("record_draft_pick", {"player_key": "999.p.1", "expected_revision": 0}),
            ("simulate_draft_pick", {"player_keys": ["999.p.5", "999.p.6"]}),
            ("recommend_draft_picks", {"count": 3}),
            ("undo_draft_pick", {"expected_revision": 1}),
        ]:
            result = await client.call_tool(name, args)
            assert not result.is_error, name
        assert not service.store.load_draft("test").picks
        assert len(await client.list_resources()) == 4
        assert await client.read_resource("fantasy://analytics/methodology")


async def test_recommendation_reflects_pick_and_simulation_is_read_only(service):
    await service.create_draft(
        "test", ["999.l.30166.t.1", "999.l.30166.t.2"], "999.l.30166.t.1", 4, True
    )
    before = await service.recommend(count=3)
    picked = before.data.candidates[0].player.key
    await service.record_pick(picked)
    after = await service.recommend(count=3)
    assert picked not in [c.player.key for c in after.data.candidates]
    assert len(after.data.draft_context.picks) == 1
    assert service.store.load_draft("test").revision == 1


async def test_missing_schedule_is_actionable(service):
    from datetime import date

    from fantasy_mcp.errors import FantasyError

    with pytest.raises(FantasyError, match="schedule"):
        await service.weekly(date(2026, 10, 20), date(2026, 10, 25))
