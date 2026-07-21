import operator
from collections.abc import MutableSequence
from dataclasses import FrozenInstanceError
from typing import Literal, cast

import pytest

from retro_backend.multiplayer.errors import StoredStateFault
from retro_backend.multiplayer.games.game_2048 import (
    TRANSITION_OVERFLOW_MESSAGE,
    Board,
    Game2048Adapter,
    Game2048State,
    MoveAccepted,
    MoveError,
    MoveNoop,
    Player2048State,
    SpawnedTile,
    can_move,
    empty_board,
    highest_tile,
    slide_and_merge,
    spawn_tile,
)
from retro_backend.multiplayer.games.rng import Mulberry32
from retro_backend.multiplayer.protocol import (
    MAX_SAFE_INTEGER,
    Direction,
    MoveCommand,
    ReadyCommand,
    RoomStatus,
)

INVALID_STATE_MESSAGE = "Stored 2048 state is invalid"
SEEDS = (0x11111111, 0x22222222)
type Seat2048 = Literal[0, 1]

MOVE_BOARD: Board = (
    (2, 2, 0, 0),
    (4, 0, 4, 0),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
)
ALL_DIRECTIONS_BOARD: Board = (
    (0, 2, 0, 2),
    (2, 0, 2, 0),
    (0, 4, 0, 4),
    (4, 0, 4, 0),
)
EDGE_PACKED_BOARD: Board = (
    (2, 4, 8, 16),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
)
DEAD_BOARD: Board = (
    (2, 4, 2, 4),
    (4, 2, 4, 2),
    (2, 4, 2, 4),
    (4, 2, 4, 2),
)
ALMOST_DEAD_BOARD: Board = (
    (2, 4, 2, 4),
    (4, 2, 4, 2),
    (2, 4, 2, 4),
    (4, 2, 4, 4),
)
DIFFERENT_HIGHEST_DEAD_BOARD: Board = (
    (8, 2, 4, 2),
    (2, 4, 2, 4),
    (4, 2, 4, 2),
    (2, 4, 2, 4),
)


def _started() -> Game2048State:
    return Game2048Adapter().start_with_seeds(SEEDS)


def _replace_player(
    state: Game2048State,
    seat: Seat2048,
    *,
    board: Board | None = None,
    score: int | None = None,
    highest_override: int | None = None,
    board_version: int | None = None,
    finished: bool | None = None,
    rng_state: int | None = None,
) -> Game2048State:
    assert state.boards is not None
    current = state.boards[seat]
    next_board = current.board if board is None else board
    replacement = Player2048State(
        seat=seat,
        board=next_board,
        score=current.score if score is None else score,
        highest_tile=(
            highest_tile(next_board) if highest_override is None else highest_override
        ),
        board_version=current.board_version if board_version is None else board_version,
        finished=current.finished if finished is None else finished,
        rng_state=current.rng_state if rng_state is None else rng_state,
    )
    boards = list(state.boards)
    boards[seat] = replacement
    return Game2048State(
        boards=(boards[0], boards[1]),
        winner_seat=state.winner_seat,
        draw=state.draw,
    )


def _terminal_state(
    score_zero: int,
    score_one: int,
    winner_seat: Seat2048 | None,
    draw: bool,
) -> Game2048State:
    state = _replace_player(
        _started(),
        0,
        board=DEAD_BOARD,
        score=score_zero,
        board_version=14 + score_zero // 4,
        finished=True,
    )
    state = _replace_player(
        state,
        1,
        board=DEAD_BOARD,
        score=score_one,
        board_version=14 + score_one // 4,
        finished=True,
    )
    return Game2048State(
        boards=state.boards,
        winner_seat=winner_seat,
        draw=draw,
    )


def _player_payload(payload: dict[str, object], seat: int = 0) -> dict[str, object]:
    boards = cast("list[object]", payload["boards"])
    return cast("dict[str, object]", boards[seat])


def _board_payload(player: dict[str, object]) -> list[list[object]]:
    return cast("list[list[object]]", player["board"])


def _assert_invalid(payload: object, status: RoomStatus = "active") -> None:
    with pytest.raises(StoredStateFault, match=f"^{INVALID_STATE_MESSAGE}$"):
        Game2048Adapter().decode_state(payload, status)


def _require_accepted(result: MoveAccepted | MoveNoop | MoveError) -> MoveAccepted:
    assert isinstance(result, MoveAccepted)
    return result


def _occupied(board: Board) -> int:
    return sum(tile != 0 for row in board for tile in row)


def _setitem(collection: object, index: int, value: object) -> None:
    operator.setitem(cast(MutableSequence[object], collection), index, value)


def test_mulberry32_matches_javascript_vectors_and_wraps_uint32() -> None:
    rng = Mulberry32(0)

    assert [rng.next(), rng.next(), rng.next()] == [
        0.26642920868471265,
        0.0003297457005828619,
        0.2232720274478197,
    ]
    assert rng.state == 1_199_730_143

    wrapped = Mulberry32(0xFFFF_FFFF)
    assert wrapped.next() == 0.8964226141106337
    assert wrapped.state == 1_831_565_812


@pytest.mark.parametrize("state", [-1, 0x1_0000_0000, True, 1.0, "1"])
def test_mulberry32_rejects_invalid_state(state: object) -> None:
    with pytest.raises((TypeError, ValueError), match="unsigned 32-bit integer"):
        Mulberry32(cast("int", state))


@pytest.mark.parametrize("upper_bound", [0, -1, True, 1.5, MAX_SAFE_INTEGER + 1])
def test_mulberry32_rejects_invalid_integer_bound(upper_bound: object) -> None:
    with pytest.raises((TypeError, ValueError), match="positive safe integer"):
        Mulberry32(1).int(cast("int", upper_bound))


def test_mulberry32_integer_draw_stays_in_half_open_range() -> None:
    rng = Mulberry32(73)

    draws = [rng.int(7) for _ in range(64)]

    assert all(0 <= value < 7 for value in draws)


def test_empty_board_and_engine_results_are_immutable() -> None:
    board = empty_board()
    moved = slide_and_merge(((2, 2, 2, 2), *board[1:]), "left")

    assert board == ((0, 0, 0, 0),) * 4
    assert isinstance(board, tuple)
    assert all(isinstance(row, tuple) for row in board)
    assert moved.board == ((4, 4, 0, 0), *board[1:])
    assert moved.gained == 8
    assert moved.moved is True
    assert board == ((0, 0, 0, 0),) * 4


@pytest.mark.parametrize(
    ("direction", "expected", "gained"),
    [
        (
            "left",
            ((2, 0, 0, 0), (4, 0, 0, 0), (2, 0, 0, 0), (0, 0, 0, 0)),
            4,
        ),
        (
            "right",
            ((0, 0, 0, 2), (0, 0, 0, 4), (0, 0, 0, 2), (0, 0, 0, 0)),
            4,
        ),
        (
            "up",
            ((4, 4, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0)),
            8,
        ),
        (
            "down",
            ((0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), (4, 4, 0, 0)),
            8,
        ),
    ],
)
def test_slide_and_merge_handles_every_direction(
    direction: Direction,
    expected: Board,
    gained: int,
) -> None:
    board: Board = (
        (2, 0, 0, 0),
        (2, 2, 0, 0),
        (0, 2, 0, 0),
        (0, 0, 0, 0),
    )

    result = slide_and_merge(board, direction)

    assert result.board == expected
    assert result.gained == gained
    assert result.moved is True
    assert board[0] == (2, 0, 0, 0)


def test_spawn_uses_row_major_empty_cells_and_exactly_two_draws() -> None:
    moved = slide_and_merge(MOVE_BOARD, "left")
    rng = Mulberry32(123)

    spawned = spawn_tile(moved.board, rng)

    assert spawned.board == (
        (4, 0, 0, 0),
        (8, 0, 0, 0),
        (0, 0, 0, 0),
        (0, 2, 0, 0),
    )
    assert spawned.spawned == SpawnedTile(row=3, column=1, value=2)
    assert rng.state == 3_663_131_749


def test_spawn_on_full_board_changes_nothing_and_consumes_no_draw() -> None:
    rng = Mulberry32(987)

    spawned = spawn_tile(DEAD_BOARD, rng)

    assert spawned.board == DEAD_BOARD
    assert spawned.spawned is None
    assert rng.state == 987


def test_can_move_and_highest_tile_are_rule_based() -> None:
    board_with_2048: Board = ((2048, 0, 0, 0), *empty_board()[1:])

    assert highest_tile(board_with_2048) == 2048
    assert can_move(board_with_2048) is True
    assert can_move(DEAD_BOARD) is False


def test_start_consumes_two_seeds_and_spawns_two_tiles_per_board() -> None:
    adapter = Game2048Adapter()
    waiting = adapter.waiting_state()
    seed_values = list(SEEDS)
    reads = 0

    def next_seed() -> int:
        nonlocal reads
        reads += 1
        return seed_values.pop(0)

    state = adapter.start(waiting, next_seed)

    assert reads == 2
    assert seed_values == []
    assert waiting == Game2048State(boards=None, winner_seat=None, draw=False)
    assert state.to_payload() == {
        "boards": [
            {
                "seat": 0,
                "board": [
                    [0, 0, 0, 0],
                    [0, 0, 2, 0],
                    [0, 0, 0, 2],
                    [0, 0, 0, 0],
                ],
                "score": 0,
                "highest_tile": 2,
                "board_version": 0,
                "finished": False,
                "rng_state": 3_317_627_109,
            },
            {
                "seat": 1,
                "board": [
                    [0, 0, 0, 0],
                    [0, 0, 2, 0],
                    [2, 0, 0, 0],
                    [0, 0, 0, 0],
                ],
                "score": 0,
                "highest_tile": 2,
                "board_version": 0,
                "finished": False,
                "rng_state": 3_603_958_262,
            },
        ],
        "winner_seat": None,
        "draw": False,
    }
    assert state.boards is not None
    assert state.boards[0] is not state.boards[1]
    assert state.boards[0].board is not state.boards[1].board
    assert all(_occupied(player.board) == 2 for player in state.boards)


def test_start_rejects_nonwaiting_state_before_reading_entropy() -> None:
    reads = 0

    def next_seed() -> int:
        nonlocal reads
        reads += 1
        return 1

    with pytest.raises(StoredStateFault, match=f"^{INVALID_STATE_MESSAGE}$"):
        Game2048Adapter().start(_started(), next_seed)

    assert reads == 0


def test_parity_vector_has_exact_score_spawn_version_and_rng() -> None:
    state = _replace_player(
        _started(),
        0,
        board=MOVE_BOARD,
        score=0,
        board_version=2,
        finished=False,
        rng_state=123,
    )

    accepted = _require_accepted(
        Game2048Adapter().apply_command(
            state,
            0,
            MoveCommand(direction="left", expected_board_version=2),
        )
    )

    assert accepted.completed is False
    assert accepted.action.to_payload() == {
        "direction": "left",
        "score_delta": 12,
        "spawned": {"row": 3, "column": 1, "value": 2},
        "resulting_board_version": 3,
    }
    assert accepted.action.payload == accepted.action.to_payload()
    assert accepted.state.boards is not None
    assert accepted.state.boards[0] == Player2048State(
        seat=0,
        board=((4, 0, 0, 0), (8, 0, 0, 0), (0, 0, 0, 0), (0, 2, 0, 0)),
        score=12,
        highest_tile=8,
        board_version=3,
        finished=False,
        rng_state=3_663_131_749,
    )


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_reducer_matches_engine_for_every_direction(direction: Direction) -> None:
    state = _replace_player(
        _started(),
        0,
        board=ALL_DIRECTIONS_BOARD,
        score=0,
        board_version=6,
        rng_state=73,
    )
    moved = slide_and_merge(ALL_DIRECTIONS_BOARD, direction)
    expected_rng = Mulberry32(73)
    expected_spawn = spawn_tile(moved.board, expected_rng)

    accepted = _require_accepted(
        Game2048Adapter().apply_move(
            state,
            seat=0,
            direction=direction,
            expected_board_version=6,
        )
    )

    assert accepted.state.boards is not None
    actor = accepted.state.boards[0]
    assert actor.board == expected_spawn.board
    assert actor.score == moved.gained
    assert actor.highest_tile == highest_tile(expected_spawn.board)
    assert actor.board_version == 7
    assert actor.finished is (not can_move(expected_spawn.board))
    assert actor.rng_state == expected_rng.state


def test_seat_one_changes_only_its_independent_board() -> None:
    state = _replace_player(
        _started(),
        1,
        board=MOVE_BOARD,
        board_version=2,
        rng_state=123,
    )
    assert state.boards is not None
    actor_zero = state.boards[0]

    accepted = _require_accepted(
        Game2048Adapter().apply_move(
            state,
            seat=1,
            direction="left",
            expected_board_version=2,
        )
    )

    assert accepted.state.boards is not None
    assert accepted.state.boards[0] == actor_zero
    assert accepted.state.boards[0] is not actor_zero
    assert accepted.state.boards[0].board is not actor_zero.board
    assert accepted.state.boards[1].board_version == 3


def test_unchanged_direction_returns_conserving_noop() -> None:
    state = _replace_player(
        _started(),
        0,
        board=EDGE_PACKED_BOARD,
        score=44,
        board_version=7,
        rng_state=987,
    )

    result = Game2048Adapter().apply_move(
        state,
        seat=0,
        direction="up",
        expected_board_version=7,
    )

    assert isinstance(result, MoveNoop)
    assert result == MoveNoop(state=state)
    assert result.state is state


def test_finished_actor_returns_the_same_conserving_noop() -> None:
    state = _replace_player(
        _started(),
        0,
        board=DEAD_BOARD,
        score=0,
        board_version=14,
        finished=True,
        rng_state=456,
    )

    result = Game2048Adapter().apply_move(
        state,
        seat=0,
        direction="left",
        expected_board_version=14,
    )

    assert isinstance(result, MoveNoop)
    assert result == MoveNoop(state=state)
    assert result.state is state


def test_stale_version_is_checked_before_finished_noop() -> None:
    state = _replace_player(
        _started(),
        0,
        board=DEAD_BOARD,
        score=0,
        board_version=14,
        finished=True,
    )

    result = Game2048Adapter().apply_move(
        state,
        seat=0,
        direction="left",
        expected_board_version=13,
    )

    assert result == MoveError(
        code="stale_board_version",
        message="Board version is stale",
        current_board_version=14,
    )


def test_invalid_actor_waiting_state_and_wrong_command_are_inactive() -> None:
    adapter = Game2048Adapter()
    inactive = MoveError(code="match_not_active", message="Match is not active")
    state = _started()

    assert adapter.apply_command(state, 2, MoveCommand(direction="left", expected_board_version=0)) == inactive
    assert adapter.apply_command(adapter.waiting_state(), 0, MoveCommand(direction="left", expected_board_version=0)) == inactive
    assert adapter.apply_command(state, 0, ReadyCommand(expected_version=0)) == inactive


def test_score_overflow_fails_closed_without_mutating_state() -> None:
    overflow_board: Board = (
        (2**46, 2**46, 2**45, 2**44),
        (2**43, 2**22, 0, 0),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
    )
    state = _replace_player(
        _started(),
        0,
        board=overflow_board,
        score=MAX_SAFE_INTEGER - 3,
        board_version=101_155_049_832_445,
        rng_state=123,
    )
    before = state.to_payload()

    with pytest.raises(RuntimeError, match=f"^{TRANSITION_OVERFLOW_MESSAGE}$"):
        Game2048Adapter().apply_move(
            state,
            seat=0,
            direction="left",
            expected_board_version=101_155_049_832_445,
        )

    assert state.to_payload() == before


def test_impossible_state_is_rejected_before_board_version_can_wrap() -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    largest_tile = 2**52
    player.update(
        {
            "board": [
                [largest_tile, 0, 0, 0],
                [largest_tile, 0, 0, 0],
                [largest_tile, 0, 0, 0],
                [largest_tile, 2, 0, 0],
            ],
            "highest_tile": largest_tile,
            "board_version": MAX_SAFE_INTEGER,
        }
    )

    _assert_invalid(payload)


def test_one_finished_board_does_not_complete_the_room() -> None:
    state = _replace_player(
        _started(),
        0,
        board=ALMOST_DEAD_BOARD,
        score=0,
        board_version=14,
        rng_state=123,
    )

    accepted = _require_accepted(
        Game2048Adapter().apply_move(
            state,
            seat=0,
            direction="left",
            expected_board_version=14,
        )
    )

    assert accepted.completed is False
    assert accepted.state.boards is not None
    assert accepted.state.boards[0].finished is True
    assert accepted.state.boards[1].finished is False
    assert accepted.state.winner_seat is None
    assert accepted.state.draw is False


@pytest.mark.parametrize(
    ("actor_score", "opponent_score", "winner_seat", "draw"),
    [(20, 12, 0, False), (0, 20, 1, False), (12, 20, None, True)],
)
def test_room_completes_only_after_both_boards_finish(
    actor_score: int,
    opponent_score: int,
    winner_seat: int | None,
    draw: bool,
) -> None:
    state = _replace_player(
        _started(),
        0,
        board=ALMOST_DEAD_BOARD,
        score=actor_score,
        board_version=14 + actor_score // 4,
        rng_state=123,
    )
    state = _replace_player(
        state,
        1,
        board=DEAD_BOARD,
        score=opponent_score,
        board_version=14 + opponent_score // 4,
        finished=True,
    )

    accepted = _require_accepted(
        Game2048Adapter().apply_move(
            state,
            seat=0,
            direction="left",
            expected_board_version=14 + actor_score // 4,
        )
    )

    assert accepted.completed is True
    assert accepted.state.winner_seat == winner_seat
    assert accepted.state.draw is draw


def test_equal_scores_draw_even_when_highest_tiles_differ() -> None:
    state = _terminal_state(20, 20, None, True)
    state = _replace_player(
        state,
        0,
        board=DIFFERENT_HIGHEST_DEAD_BOARD,
        score=20,
        highest_override=8,
        board_version=18,
        finished=True,
    )

    decoded = Game2048Adapter().decode_state(state, "completed")

    assert decoded == state
    assert decoded.draw is True
    assert decoded.winner_seat is None


def test_reaching_2048_does_not_finish_a_movable_board() -> None:
    board: Board = ((1024, 1024, 0, 0), *empty_board()[1:])
    state = _replace_player(
        _started(),
        0,
        board=board,
        score=16_384,
        board_version=510,
        rng_state=123,
    )

    accepted = _require_accepted(
        Game2048Adapter().apply_move(
            state,
            seat=0,
            direction="left",
            expected_board_version=510,
        )
    )

    assert accepted.state.boards is not None
    assert accepted.state.boards[0].highest_tile == 2048
    assert accepted.state.boards[0].finished is False
    assert accepted.completed is False


def test_generated_trajectory_round_trips_spawn_and_merge_shapes() -> None:
    adapter = Game2048Adapter()
    state = adapter.start_with_seeds((1, 2))
    directions = ("left", "down", "right", "up")
    saw_spawned_four = False
    merge_counts: set[int] = set()

    for step in range(128):
        assert state.boards is not None
        before = state.boards[0]
        accepted: MoveAccepted | None = None
        for offset in range(4):
            direction = directions[(step + offset) % 4]
            result = adapter.apply_move(
                state,
                seat=0,
                direction=direction,
                expected_board_version=before.board_version,
            )
            if isinstance(result, MoveAccepted):
                accepted = result
                break
        assert accepted is not None, f"expected accepted move at step {step}"
        assert accepted.state.boards is not None
        after = accepted.state.boards[0]
        merge_counts.add(_occupied(before.board) + 1 - _occupied(after.board))
        saw_spawned_four = saw_spawned_four or accepted.action.spawned.value == 4
        state = accepted.state
        assert adapter.decode_state(state, "active") == state

    assert state.boards is not None
    assert state.boards[0].board_version == 128
    assert saw_spawned_four is True
    assert 0 in merge_counts
    assert 1 in merge_counts
    assert any(count > 1 for count in merge_counts)


@pytest.mark.parametrize(
    ("board", "score", "board_version"),
    [
        (((2, 2, 0, 0), *empty_board()[1:]), 4, 0),
        (((4, 2, 0, 0), *empty_board()[1:]), 0, 1),
        (((4, 4, 0, 0), *empty_board()[1:]), 4, 0),
        (((4, 4, 0, 0), *empty_board()[1:]), 0, 2),
        (((8, 4, 0, 0), *empty_board()[1:]), 20, 1),
        (((2**52, 2, 0, 0), *empty_board()[1:]), 0, 2**51 - 1),
    ],
)
def test_exact_trajectory_conservation_rejects_loose_bound_mismatches(
    board: Board,
    score: int,
    board_version: int,
) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    player.update(
        {
            "board": [list(row) for row in board],
            "score": score,
            "highest_tile": highest_tile(board),
            "board_version": board_version,
        }
    )

    occupied = _occupied(board)
    tile_mass = sum(tile for row in board for tile in row)
    assert score % 4 == 0
    assert 2 <= occupied <= min(board_version + 2, 16)
    assert 2 * (board_version + 2) <= tile_mass <= 4 * (board_version + 2)
    _assert_invalid(payload)


@pytest.mark.parametrize(
    ("board", "score", "highest"),
    [
        (((2, 2, 0, 0), *empty_board()[1:]), 4, 2),
        (((8, 8, 0, 0), *empty_board()[1:]), 8, 8),
    ],
)
def test_version_one_rejects_mass_outside_exact_bounds(
    board: Board,
    score: int,
    highest: int,
) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    player.update(
        {
            "board": [list(row) for row in board],
            "score": score,
            "highest_tile": highest,
            "board_version": 1,
        }
    )

    _assert_invalid(payload)


@pytest.mark.parametrize(
    ("board", "score", "highest"),
    [
        (((4, 2, 0, 0), *empty_board()[1:]), 4, 4),
        (((8, 4, 0, 0), *empty_board()[1:]), 8, 8),
    ],
)
def test_version_one_accepts_both_exact_mass_bounds(
    board: Board,
    score: int,
    highest: int,
) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    player.update(
        {
            "board": [list(row) for row in board],
            "score": score,
            "highest_tile": highest,
            "board_version": 1,
        }
    )

    decoded = Game2048Adapter().decode_state(payload, "active")

    assert decoded.boards is not None
    assert decoded.boards[0].board == board


@pytest.mark.parametrize(
    "board",
    [
        empty_board(),
        ((4, 0, 0, 0), *empty_board()[1:]),
        ((2, 2, 2, 0), *empty_board()[1:]),
    ],
)
def test_zero_one_and_three_occupied_tiles_are_invalid_at_version_zero(board: Board) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    player["board"] = [list(row) for row in board]
    player["highest_tile"] = highest_tile(board)

    _assert_invalid(payload)


def test_score_conservation_implies_divisibility_by_four() -> None:
    payload = _started().to_payload()
    _player_payload(payload)["score"] = 1

    _assert_invalid(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("score", -1),
        ("score", 1.5),
        ("score", True),
        ("score", MAX_SAFE_INTEGER + 1),
        ("board_version", -1),
        ("board_version", 1.5),
        ("board_version", True),
        ("board_version", MAX_SAFE_INTEGER + 1),
        ("rng_state", -1),
        ("rng_state", 1.5),
        ("rng_state", True),
        ("rng_state", 0x1_0000_0000),
    ],
)
def test_decode_rejects_invalid_numeric_player_fields(field: str, value: object) -> None:
    payload = _started().to_payload()
    _player_payload(payload)[field] = value

    _assert_invalid(payload)


@pytest.mark.parametrize("tile", [-2, 2.5, True, 3, MAX_SAFE_INTEGER + 1])
def test_decode_rejects_invalid_tile_values(tile: object) -> None:
    payload = _started().to_payload()
    _board_payload(_player_payload(payload))[0][0] = tile

    _assert_invalid(payload)


@pytest.mark.parametrize(
    "mutator",
    [
        "short_board",
        "short_row",
        "wrong_board_type",
        "wrong_row_type",
    ],
)
def test_decode_rejects_wrong_board_dimensions_and_types(mutator: str) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    board = _board_payload(player)
    if mutator == "short_board":
        board.pop()
    elif mutator == "short_row":
        board[0].pop()
    elif mutator == "wrong_board_type":
        player["board"] = {"0": board[0]}
    else:
        board[0] = cast("list[object]", cast(object, {"0": 2}))

    _assert_invalid(payload)


@pytest.mark.parametrize(
    "tuple_level",
    ["boards", "player_board", "row"],
)
def test_persisted_mapping_rejects_tuple_containers(tuple_level: str) -> None:
    payload = _started().to_payload()
    player = _player_payload(payload)
    board = _board_payload(player)
    if tuple_level == "boards":
        payload["boards"] = tuple(cast("list[object]", payload["boards"]))
    elif tuple_level == "player_board":
        player["board"] = tuple(board)
    else:
        board[0] = cast("list[object]", cast(object, tuple(board[0])))

    _assert_invalid(payload)


def test_decode_requires_exact_keys_at_both_levels() -> None:
    extra_state = _started().to_payload()
    extra_state["extra"] = True
    _assert_invalid(extra_state)

    missing_state = _started().to_payload()
    missing_state.pop("draw")
    _assert_invalid(missing_state)

    extra_player = _started().to_payload()
    _player_payload(extra_player)["extra"] = True
    _assert_invalid(extra_player)

    missing_player = _started().to_payload()
    _player_payload(missing_player).pop("rng_state")
    _assert_invalid(missing_player)


def test_decode_rejects_bad_board_collection_and_seats() -> None:
    short = _started().to_payload()
    cast("list[object]", short["boards"]).pop()
    _assert_invalid(short)

    reversed_seats = _started().to_payload()
    boards = cast("list[dict[str, object]]", reversed_seats["boards"])
    boards[0]["seat"] = 1
    boards[1]["seat"] = 0
    _assert_invalid(reversed_seats)

    duplicate_seats = _started().to_payload()
    _player_payload(duplicate_seats, 1)["seat"] = 0
    _assert_invalid(duplicate_seats)

    boolean_seat = _started().to_payload()
    _player_payload(boolean_seat)["seat"] = False
    _assert_invalid(boolean_seat)


def test_decode_rejects_inconsistent_highest_and_finished_flags() -> None:
    highest = _started().to_payload()
    _player_payload(highest)["highest_tile"] = 4
    _assert_invalid(highest)

    movable_finished = _started().to_payload()
    _player_payload(movable_finished)["finished"] = True
    _assert_invalid(movable_finished)

    immovable_unfinished = _started().to_payload()
    player = _player_payload(immovable_unfinished)
    player.update(
        {
            "board": [list(row) for row in DEAD_BOARD],
            "score": 0,
            "highest_tile": 4,
            "board_version": 14,
            "finished": False,
        }
    )
    _assert_invalid(immovable_unfinished)


def test_decode_rejects_inconsistent_lifecycle_and_results() -> None:
    adapter = Game2048Adapter()
    _assert_invalid(adapter.waiting_state(), "active")
    _assert_invalid(_started(), "waiting")
    _assert_invalid(_started(), "completed")
    _assert_invalid(_terminal_state(12, 8, 0, False), "active")
    _assert_invalid(_terminal_state(20, 12, 1, False), "completed")
    _assert_invalid(_terminal_state(12, 12, 0, False), "completed")
    _assert_invalid(_terminal_state(20, 12, None, True), "completed")
    _assert_invalid(_terminal_state(20, 12, None, False), "completed")


def test_decode_accepts_valid_lifecycle_states_and_expired_underlays() -> None:
    adapter = Game2048Adapter()
    waiting = adapter.waiting_state()
    active = _started()
    winner = _terminal_state(20, 12, 0, False)
    draw = _terminal_state(12, 12, None, True)

    assert adapter.decode_state(waiting, "waiting") == waiting
    assert adapter.decode_state(active, "active") == active
    assert adapter.decode_state(winner, "completed") == winner
    assert adapter.decode_state(draw, "completed") == draw
    assert adapter.decode_state(waiting, "expired") == waiting
    assert adapter.decode_state(active, "expired") == active
    assert adapter.decode_state(winner, "expired") == winner


def test_domain_results_and_decoded_state_are_deeply_immutable() -> None:
    adapter = Game2048Adapter()
    state = _started()
    decoded = adapter.decode_state(state.to_payload(), "active")
    assert decoded.boards is not None
    assert state.boards is not None

    with pytest.raises(FrozenInstanceError):
        decoded.draw = True  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        decoded.boards[0].score = 4  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(TypeError):
        _setitem(decoded.boards[0].board[0], 0, 4)

    assert decoded is not state
    assert decoded.boards[0] is not state.boards[0]
    assert decoded.boards[0].board is not state.boards[0].board
    expired = adapter.expire(state)
    assert expired == state
    assert expired is not state
    assert expired.boards is not None
    assert expired.boards[1] is not state.boards[1]


def test_move_results_action_and_spawn_are_frozen() -> None:
    adapter = Game2048Adapter()
    state = _replace_player(
        _started(),
        0,
        board=MOVE_BOARD,
        board_version=2,
        rng_state=123,
    )
    accepted = _require_accepted(
        adapter.apply_move(state, 0, "left", expected_board_version=2)
    )
    noop = MoveNoop(state=state)
    error = MoveError(
        code="stale_board_version",
        message="Board version is stale",
        current_board_version=2,
    )

    mutation_attempts = (
        (accepted, "completed", True),
        (noop, "state", accepted.state),
        (error, "message", "changed"),
        (accepted.action, "score_delta", 0),
        (accepted.action.spawned, "value", 4),
    )
    for instance, attribute, value in mutation_attempts:
        with pytest.raises(FrozenInstanceError):
            setattr(instance, attribute, value)


def test_action_and_public_payloads_are_fresh_and_redact_rng_state() -> None:
    adapter = Game2048Adapter()
    state = _replace_player(
        _started(),
        0,
        board=MOVE_BOARD,
        board_version=2,
        rng_state=123,
    )
    accepted = _require_accepted(
        adapter.apply_move(state, 0, "left", expected_board_version=2)
    )

    first_action = accepted.action.to_payload()
    second_action = accepted.action.to_payload()
    cast("dict[str, object]", first_action["spawned"])["value"] = 4
    assert second_action["spawned"] == {"row": 3, "column": 1, "value": 2}

    public = adapter.public_state(accepted.state, "active")
    assert set(public) == {"boards", "winner_seat", "draw"}
    public_boards = cast("list[dict[str, object]]", public["boards"])
    assert all("rng_state" not in player for player in public_boards)
    public_grid = cast("list[list[int]]", public_boards[0]["board"])
    public_grid[0][0] = 4096
    assert accepted.state.boards is not None
    assert accepted.state.boards[0].board[0][0] != 4096


def test_games_package_exports_only_2048_and_rng_symbols() -> None:
    from retro_backend.multiplayer import games

    assert set(games.__all__) == {
        "Board",
        "EngineMove",
        "Game2048Adapter",
        "Game2048State",
        "MoveAccepted",
        "MoveAction",
        "MoveError",
        "MoveNoop",
        "Mulberry32",
        "Player2048State",
        "SpawnResult",
        "SpawnedTile",
        "can_move",
        "empty_board",
        "highest_tile",
        "slide_and_merge",
        "spawn_tile",
    }
