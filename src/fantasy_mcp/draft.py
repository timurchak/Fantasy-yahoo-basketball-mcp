from fantasy_mcp.domain import DraftPick, DraftState
from fantasy_mcp.errors import FantasyError
from fantasy_mcp.persistence import Store


def next_pick(state: DraftState, overall: int | None = None) -> tuple[int, int, str]:
    pick = overall if overall is not None else len(state.picks) + 1
    n = len(state.team_order)
    if not 1 <= pick <= state.rounds * n:
        raise FantasyError("INVALID_DRAFT_PICK", "Draft is complete or pick is out of range.")
    round_no, offset = divmod(pick - 1, n)
    index = n - offset - 1 if state.snake and round_no % 2 else offset
    return round_no + 1, offset + 1, state.team_order[index]


class DraftEngine:
    def __init__(self, store: Store):
        self.store = store

    def record(
        self,
        draft_id: str,
        player_key: str,
        team_key: str | None = None,
        expected_revision: int | None = None,
    ) -> DraftState:
        def change(state: DraftState) -> None:
            if any(p.player_key == player_key for p in state.picks):
                raise FantasyError(
                    "PLAYER_ALREADY_DRAFTED", "Player is already on this draft board."
                )
            r, p, team = next_pick(state)
            if team_key and team_key != team:
                raise FantasyError("INVALID_DRAFT_PICK", f"Next pick belongs to {team}.")
            state.picks.append(
                DraftPick(
                    overall_pick=len(state.picks) + 1,
                    round=r,
                    pick_in_round=p,
                    team_key=team,
                    player_key=player_key,
                )
            )

        return self.store.mutate_draft(draft_id, expected_revision, change)

    def undo(self, draft_id: str, expected_revision: int | None = None) -> DraftState:
        def change(state: DraftState) -> None:
            if not state.picks:
                raise FantasyError("INVALID_DRAFT_PICK", "No picks to undo.")
            state.picks.pop()

        return self.store.mutate_draft(draft_id, expected_revision, change)

    def correct(
        self, draft_id: str, overall_pick: int, player_key: str, expected_revision: int
    ) -> DraftState:
        def change(state: DraftState) -> None:
            if not 1 <= overall_pick <= len(state.picks):
                raise FantasyError("INVALID_DRAFT_PICK", "Pick has not been recorded.")
            if any(
                p.player_key == player_key and p.overall_pick != overall_pick for p in state.picks
            ):
                raise FantasyError("PLAYER_ALREADY_DRAFTED", "Player is already drafted elsewhere.")
            state.picks[overall_pick - 1].player_key = player_key
            state.picks[overall_pick - 1].source = "manual_correction"

        return self.store.mutate_draft(draft_id, expected_revision, change)

    def reset(self, draft_id: str, expected_revision: int) -> DraftState:
        return self.store.mutate_draft(
            draft_id, expected_revision, lambda state: state.picks.clear()
        )

    def sync(self, draft_id: str, picks: list[DraftPick], expected_revision: int) -> DraftState:
        def change(state: DraftState) -> None:
            if not picks:
                raise FantasyError("INSUFFICIENT_DATA", "Yahoo has no completed draft results.")
            seen: set[str] = set()
            for i, pick in enumerate(sorted(picks, key=lambda p: p.overall_pick), 1):
                r, p, team = next_pick(state, i)
                if (pick.overall_pick, pick.round, pick.team_key) != (i, r, team):
                    raise FantasyError(
                        "INVALID_DRAFT_PICK", "Yahoo results disagree with draft format."
                    )
                if pick.player_key in seen:
                    raise FantasyError(
                        "PLAYER_ALREADY_DRAFTED", "Yahoo results contain duplicates."
                    )
                seen.add(pick.player_key)
                pick.pick_in_round = p
            state.picks = sorted(picks, key=lambda p: p.overall_pick)

        return self.store.mutate_draft(draft_id, expected_revision, change)
