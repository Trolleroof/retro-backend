"""Pure independent-board 2048 rules and strict persisted-state decoding."""

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Literal, cast

from ..errors import StoredStateFault
from ..protocol import MAX_SAFE_INTEGER, Direction, GameDescriptor, MoveCommand, RoomStatus
from .rng import Mulberry32

type Seat2048 = Literal[0, 1]
type BoardRow = tuple[int, int, int, int]
type Board = tuple[BoardRow, BoardRow, BoardRow, BoardRow]
type MoveErrorCode = Literal["match_not_active", "stale_board_version"]

SIZE = 4
UINT32_MAX = 0xFFFF_FFFF
INVALID_STATE_MESSAGE = "Stored 2048 state is invalid"
TRANSITION_OVERFLOW_MESSAGE = "2048 transition exceeds safe integer bounds"
SPAWN_INVARIANT_MESSAGE = "2048 spawn invariant violated"
SEED_ERROR_MESSAGE = "2048 seed must be an unsigned 32-bit integer"

_PLAYER_KEYS = frozenset(
    {
        "seat",
        "board",
        "score",
        "highest_tile",
        "board_version",
        "finished",
        "rng_state",
    }
)
_STATE_KEYS = frozenset({"boards", "winner_seat", "draw"})
_DIRECTIONS = frozenset({"up", "down", "left", "right"})


def _is_safe_nonnegative_integer(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def _require_safe_nonnegative_integer(value: object, label: str) -> int:
    if not _is_safe_nonnegative_integer(value):
        raise ValueError(f"{label} must be a nonnegative safe integer")
    return cast(int, value)


def _require_uint32(value: object, message: str) -> int:
    if type(value) is not int or not 0 <= value <= UINT32_MAX:
        raise ValueError(message)
    return value


def _is_tile(value: object) -> bool:
    if not _is_safe_nonnegative_integer(value):
        return False
    tile = cast(int, value)
    return tile == 0 or (tile >= 2 and tile & (tile - 1) == 0)


def _as_direction(value: object) -> Direction | None:
    if type(value) is str and value in _DIRECTIONS:
        return cast(Direction, value)
    return None


def _normalize_board(value: object) -> Board:
    if type(value) not in {list, tuple} or len(cast(Sequence[object], value)) != SIZE:
        raise ValueError("2048 board must be a 4 by 4 sequence")

    rows: list[BoardRow] = []
    for source_row in cast(Sequence[object], value):
        if type(source_row) not in {list, tuple}:
            raise ValueError("2048 board rows must be sequences")
        row_values = cast(Sequence[object], source_row)
        if len(row_values) != SIZE or any(not _is_tile(tile) for tile in row_values):
            raise ValueError("2048 board contains invalid tiles")
        rows.append(
            (
                cast(int, row_values[0]),
                cast(int, row_values[1]),
                cast(int, row_values[2]),
                cast(int, row_values[3]),
            )
        )
    return (rows[0], rows[1], rows[2], rows[3])


def _decode_stored_board(value: object) -> Board:
    if type(value) is not list:
        raise ValueError("Stored 2048 board must be an array")
    rows = cast(list[object], value)
    if len(rows) != SIZE or any(type(row) is not list for row in rows):
        raise ValueError("Stored 2048 board rows must be arrays")
    return _normalize_board(rows)


def _clone_board(board: Board) -> Board:
    return cast(Board, tuple(tuple(tile for tile in row) for row in board))


def empty_board() -> Board:
    return ((0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0))


def highest_tile(board: Board) -> int:
    normalized = _normalize_board(board)
    return max(tile for row in normalized for tile in row)


def can_move(board: Board) -> bool:
    normalized = _normalize_board(board)
    if any(tile == 0 for row in normalized for tile in row):
        return True
    for row_index in range(SIZE):
        for column_index in range(SIZE):
            tile = normalized[row_index][column_index]
            if row_index + 1 < SIZE and normalized[row_index + 1][column_index] == tile:
                return True
            if column_index + 1 < SIZE and normalized[row_index][column_index + 1] == tile:
                return True
    return False


def _collapse_row(row: BoardRow) -> tuple[BoardRow, int]:
    tiles = [tile for tile in row if tile != 0]
    collapsed: list[int] = []
    gained = 0
    index = 0
    while index < len(tiles):
        if index + 1 < len(tiles) and tiles[index] == tiles[index + 1]:
            merged = tiles[index] * 2
            collapsed.append(merged)
            gained += merged
            index += 2
        else:
            collapsed.append(tiles[index])
            index += 1
    collapsed.extend([0] * (SIZE - len(collapsed)))
    return cast(BoardRow, tuple(collapsed)), gained


def _transpose(board: Board) -> Board:
    return cast(
        Board,
        tuple(tuple(board[row][column] for row in range(SIZE)) for column in range(SIZE)),
    )


def _reverse_rows(board: Board) -> Board:
    return cast(Board, tuple(tuple(reversed(row)) for row in board))


@dataclass(frozen=True, slots=True)
class EngineMove:
    board: Board
    gained: int
    moved: bool


def slide_and_merge(board: Board, direction: object) -> EngineMove:
    """Apply one direction without spawning and without mutating the input."""
    source = _normalize_board(board)
    checked_direction = _as_direction(direction)
    if checked_direction is None:
        raise ValueError("2048 direction is invalid")

    normalized = _clone_board(source)
    if checked_direction == "up":
        normalized = _transpose(normalized)
    elif checked_direction == "down":
        normalized = _reverse_rows(_transpose(normalized))
    elif checked_direction == "right":
        normalized = _reverse_rows(normalized)

    rows: list[BoardRow] = []
    gained = 0
    for row in normalized:
        collapsed, row_gained = _collapse_row(row)
        rows.append(collapsed)
        gained += row_gained
    moved_board: Board = (rows[0], rows[1], rows[2], rows[3])

    if checked_direction == "up":
        moved_board = _transpose(moved_board)
    elif checked_direction == "down":
        moved_board = _transpose(_reverse_rows(moved_board))
    elif checked_direction == "right":
        moved_board = _reverse_rows(moved_board)

    return EngineMove(board=moved_board, gained=gained, moved=moved_board != source)


@dataclass(frozen=True, slots=True)
class SpawnedTile:
    row: int
    column: int
    value: Literal[2, 4]

    def __post_init__(self) -> None:
        if type(self.row) is not int or not 0 <= self.row < SIZE:
            raise ValueError("Spawn row is invalid")
        if type(self.column) is not int or not 0 <= self.column < SIZE:
            raise ValueError("Spawn column is invalid")
        if type(self.value) is not int or self.value not in {2, 4}:
            raise ValueError("Spawn value is invalid")

    def to_payload(self) -> dict[str, int]:
        return {"row": self.row, "column": self.column, "value": self.value}


@dataclass(frozen=True, slots=True)
class SpawnResult:
    board: Board
    spawned: SpawnedTile | None


def spawn_tile(board: Board, rng: object) -> SpawnResult:
    """Spawn one tile using row-major empty-cell selection."""
    source = _normalize_board(board)
    if not isinstance(rng, Mulberry32):
        raise TypeError("2048 spawning requires Mulberry32")
    generator = rng
    empty_cells = [
        (row, column)
        for row in range(SIZE)
        for column in range(SIZE)
        if source[row][column] == 0
    ]
    if not empty_cells:
        return SpawnResult(board=_clone_board(source), spawned=None)

    row, column = empty_cells[generator.int(len(empty_cells))]
    value: Literal[2, 4] = 2 if generator.next() < 0.9 else 4
    mutable = [list(source_row) for source_row in source]
    mutable[row][column] = value
    spawned = SpawnedTile(row=row, column=column, value=value)
    return SpawnResult(board=_normalize_board(mutable), spawned=spawned)


def _tile_merge_potential(tile: int) -> int:
    exponent = tile.bit_length() - 1
    return (exponent - 1) * tile


def _has_exact_trajectory_conservation(
    board: Board,
    score: int,
    board_version: int,
) -> bool:
    occupied = 0
    tile_mass = 0
    final_potential = 0
    for row in board:
        for tile in row:
            if tile == 0:
                continue
            occupied += 1
            tile_mass += tile
            final_potential += _tile_merge_potential(tile)

    base_tile_count = board_version + 2
    maximum_occupied = min(base_tile_count, SIZE * SIZE)
    minimum_tile_mass = 2 * base_tile_count
    maximum_tile_mass = 4 * base_tile_count
    if (
        occupied < 2
        or occupied > maximum_occupied
        or tile_mass < minimum_tile_mass
        or tile_mass > maximum_tile_mass
    ):
        return False

    base_four_mass = tile_mass - minimum_tile_mass
    if base_four_mass % 2 != 0:
        return False
    base_four_count = base_four_mass // 2
    expected_score = final_potential - 4 * base_four_count
    return score == expected_score


@dataclass(frozen=True, slots=True)
class Player2048State:
    seat: Seat2048
    board: Board
    score: int
    highest_tile: int
    board_version: int
    finished: bool
    rng_state: int

    def __post_init__(self) -> None:
        if type(self.seat) is not int or self.seat not in {0, 1}:
            raise ValueError("2048 seat is invalid")
        normalized = _normalize_board(self.board)
        object.__setattr__(self, "board", normalized)
        score = _require_safe_nonnegative_integer(self.score, "2048 score")
        version = _require_safe_nonnegative_integer(
            self.board_version,
            "2048 board version",
        )
        if not _is_tile(self.highest_tile) or self.highest_tile != highest_tile(normalized):
            raise ValueError("2048 highest tile is inconsistent")
        if type(self.finished) is not bool or self.finished is not (not can_move(normalized)):
            raise ValueError("2048 finished flag is inconsistent")
        _require_uint32(self.rng_state, "2048 RNG state must be an unsigned 32-bit integer")
        if not _has_exact_trajectory_conservation(normalized, score, version):
            raise ValueError("2048 trajectory conservation is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "seat": self.seat,
            "board": [list(row) for row in self.board],
            "score": self.score,
            "highest_tile": self.highest_tile,
            "board_version": self.board_version,
            "finished": self.finished,
            "rng_state": self.rng_state,
        }


@dataclass(frozen=True, slots=True)
class Game2048State:
    boards: tuple[Player2048State, Player2048State] | None
    winner_seat: Seat2048 | None
    draw: bool

    def __post_init__(self) -> None:
        if self.boards is not None:
            if type(self.boards) is not tuple or len(self.boards) != 2:
                raise ValueError("2048 state requires an ordered pair of boards")
            if self.boards[0].seat != 0 or self.boards[1].seat != 1:
                raise ValueError("2048 board seats are invalid")
        if self.winner_seat is not None and (
            type(self.winner_seat) is not int or self.winner_seat not in {0, 1}
        ):
            raise ValueError("2048 winner seat is invalid")
        if type(self.draw) is not bool:
            raise ValueError("2048 draw flag is invalid")

    def to_payload(self) -> dict[str, object]:
        return {
            "boards": (
                None
                if self.boards is None
                else [player.to_payload() for player in self.boards]
            ),
            "winner_seat": self.winner_seat,
            "draw": self.draw,
        }


@dataclass(frozen=True, slots=True)
class MoveAction:
    direction: Direction
    score_delta: int
    spawned: SpawnedTile
    resulting_board_version: int

    type: ClassVar[Literal["move"]] = "move"

    def __post_init__(self) -> None:
        if _as_direction(self.direction) is None:
            raise ValueError("2048 direction is invalid")
        _require_safe_nonnegative_integer(self.score_delta, "2048 score delta")
        _require_safe_nonnegative_integer(
            self.resulting_board_version,
            "2048 resulting board version",
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "score_delta": self.score_delta,
            "spawned": self.spawned.to_payload(),
            "resulting_board_version": self.resulting_board_version,
        }

    @property
    def payload(self) -> dict[str, object]:
        return self.to_payload()


@dataclass(frozen=True, slots=True)
class MoveAccepted:
    state: Game2048State
    completed: bool
    action: MoveAction
    kind: Literal["accepted"] = field(default="accepted", init=False)

    def __post_init__(self) -> None:
        if type(self.completed) is not bool:
            raise ValueError("2048 completion flag is invalid")

    @property
    def action_payload(self) -> dict[str, object]:
        return self.action.to_payload()


@dataclass(frozen=True, slots=True)
class MoveNoop:
    state: Game2048State
    kind: Literal["noop"] = field(default="noop", init=False)


@dataclass(frozen=True, slots=True)
class MoveError:
    code: MoveErrorCode
    message: str
    current_board_version: int | None = None
    kind: Literal["error"] = field(default="error", init=False)

    def __post_init__(self) -> None:
        if self.code not in {"match_not_active", "stale_board_version"}:
            raise ValueError("2048 move error code is invalid")
        if type(self.message) is not str or not self.message:
            raise ValueError("2048 move error message is invalid")
        if self.current_board_version is not None:
            _require_safe_nonnegative_integer(
                self.current_board_version,
                "2048 current board version",
            )


type MoveResult = MoveAccepted | MoveNoop | MoveError


def _clone_player(player: Player2048State) -> Player2048State:
    return Player2048State(
        seat=player.seat,
        board=_clone_board(player.board),
        score=player.score,
        highest_tile=player.highest_tile,
        board_version=player.board_version,
        finished=player.finished,
        rng_state=player.rng_state,
    )


def _clone_state(state: Game2048State) -> Game2048State:
    return Game2048State(
        boards=(
            None
            if state.boards is None
            else (_clone_player(state.boards[0]), _clone_player(state.boards[1]))
        ),
        winner_seat=state.winner_seat,
        draw=state.draw,
    )


def _has_exact_keys(value: Mapping[object, object], keys: frozenset[str]) -> bool:
    return (
        len(value) == len(keys)
        and all(type(key) is str for key in value)
        and all(key in value for key in keys)
    )


def _decode_player(value: object, expected_seat: Seat2048) -> Player2048State:
    if not isinstance(value, Mapping) or not _has_exact_keys(value, _PLAYER_KEYS):
        raise ValueError("2048 player shape is invalid")
    if value["seat"] != expected_seat or type(value["seat"]) is not int:
        raise ValueError("2048 player seat is invalid")
    return Player2048State(
        seat=expected_seat,
        board=_decode_stored_board(value["board"]),
        score=_require_safe_nonnegative_integer(value["score"], "2048 score"),
        highest_tile=cast(int, value["highest_tile"]),
        board_version=_require_safe_nonnegative_integer(
            value["board_version"],
            "2048 board version",
        ),
        finished=cast(bool, value["finished"]),
        rng_state=_require_uint32(
            value["rng_state"],
            "2048 RNG state must be an unsigned 32-bit integer",
        ),
    )


def _result_is_consistent(state: Game2048State) -> bool:
    if state.boards is None:
        return state.winner_seat is None and not state.draw
    player_zero, player_one = state.boards
    if not player_zero.finished or not player_one.finished:
        return state.winner_seat is None and not state.draw
    if player_zero.score == player_one.score:
        return state.winner_seat is None and state.draw
    expected_winner = 0 if player_zero.score > player_one.score else 1
    return state.winner_seat == expected_winner and not state.draw


def _both_boards_finished(state: Game2048State) -> bool:
    return (
        state.boards is not None
        and state.boards[0].finished
        and state.boards[1].finished
    )


def _decode_compatible_state(value: object) -> Game2048State:
    try:
        if isinstance(value, Game2048State):
            state = _clone_state(value)
        else:
            if not isinstance(value, Mapping) or not _has_exact_keys(value, _STATE_KEYS):
                raise ValueError("2048 state shape is invalid")
            winner = value["winner_seat"]
            if winner is not None and (type(winner) is not int or winner not in {0, 1}):
                raise ValueError("2048 winner seat is invalid")
            draw = value["draw"]
            if type(draw) is not bool:
                raise ValueError("2048 draw flag is invalid")

            raw_boards = value["boards"]
            boards: tuple[Player2048State, Player2048State] | None
            if raw_boards is None:
                boards = None
            else:
                if type(raw_boards) is not list:
                    raise ValueError("2048 boards shape is invalid")
                board_values = cast(list[object], raw_boards)
                if len(board_values) != 2:
                    raise ValueError("2048 boards shape is invalid")
                boards = (
                    _decode_player(board_values[0], 0),
                    _decode_player(board_values[1], 1),
                )
            state = Game2048State(
                boards=boards,
                winner_seat=cast(Seat2048 | None, winner),
                draw=draw,
            )
        if not _result_is_consistent(state):
            raise ValueError("2048 result is inconsistent")
        return state
    except StoredStateFault:
        raise
    except Exception as error:
        raise StoredStateFault(INVALID_STATE_MESSAGE) from error


def _player_from_seed(seat: Seat2048, seed: int) -> Player2048State:
    checked_seed = _require_uint32(seed, SEED_ERROR_MESSAGE)
    rng = Mulberry32(checked_seed)
    first = spawn_tile(empty_board(), rng)
    second = spawn_tile(first.board, rng)
    return Player2048State(
        seat=seat,
        board=second.board,
        score=0,
        highest_tile=highest_tile(second.board),
        board_version=0,
        finished=False,
        rng_state=rng.state,
    )


def _inactive_error() -> MoveError:
    return MoveError(code="match_not_active", message="Match is not active")


def _transition_values_are_safe(
    board: Board,
    gained: int,
    score: int,
    board_version: int,
) -> bool:
    return (
        _is_safe_nonnegative_integer(gained)
        and _is_safe_nonnegative_integer(score)
        and _is_safe_nonnegative_integer(board_version)
        and all(_is_tile(tile) for row in board for tile in row)
    )


class Game2048Adapter:
    descriptor: ClassVar[GameDescriptor] = GameDescriptor(game_type="2048", mode="1v1")
    seat_count: ClassVar[Literal[2]] = 2

    def waiting_state(self) -> Game2048State:
        return Game2048State(boards=None, winner_seat=None, draw=False)

    def start(
        self,
        state: object,
        random_uint32: Callable[[], int] | None = None,
    ) -> Game2048State:
        self.decode_state(state, "waiting")
        source = random_uint32 if random_uint32 is not None else lambda: secrets.randbits(32)
        seeds = (source(), source())
        return self.start_with_seeds(seeds)

    def start_with_seeds(self, seeds: Sequence[int]) -> Game2048State:
        if type(seeds) not in {list, tuple} or len(seeds) != 2:
            raise ValueError("2048 start requires exactly two seeds")
        seed_zero = _require_uint32(seeds[0], SEED_ERROR_MESSAGE)
        seed_one = _require_uint32(seeds[1], SEED_ERROR_MESSAGE)
        return Game2048State(
            boards=(_player_from_seed(0, seed_zero), _player_from_seed(1, seed_one)),
            winner_seat=None,
            draw=False,
        )

    def decode_state(self, value: object, status: RoomStatus) -> Game2048State:
        state = _decode_compatible_state(value)
        terminal = _both_boards_finished(state)
        valid_for_status = (
            (status == "waiting" and state.boards is None)
            or (status == "active" and state.boards is not None and not terminal)
            or (status == "completed" and state.boards is not None and terminal)
            or status == "expired"
        )
        if type(status) is not str or status not in {"waiting", "active", "completed", "expired"}:
            valid_for_status = False
        if not valid_for_status:
            raise StoredStateFault(INVALID_STATE_MESSAGE)
        return _clone_state(state)

    def apply_command(self, state: object, seat: int, command: object) -> MoveResult:
        current = _decode_compatible_state(state)
        noop_state = state if isinstance(state, Game2048State) else current
        if (
            not isinstance(command, MoveCommand)
            or type(seat) is not int
            or seat not in {0, 1}
            or current.boards is None
        ):
            return _inactive_error()
        return self._apply_decoded(
            current,
            cast(Seat2048, seat),
            command.direction,
            command.expected_board_version,
            noop_state,
        )

    def apply_move(
        self,
        state: object,
        seat: int,
        direction: object,
        expected_board_version: int | None = None,
    ) -> MoveResult:
        current = _decode_compatible_state(state)
        noop_state = state if isinstance(state, Game2048State) else current
        if (
            type(seat) is not int
            or seat not in {0, 1}
            or current.boards is None
        ):
            return _inactive_error()
        checked_direction = _as_direction(direction)
        if checked_direction is None:
            return _inactive_error()
        player = current.boards[seat]
        expected = player.board_version if expected_board_version is None else expected_board_version
        if not _is_safe_nonnegative_integer(expected):
            return _inactive_error()
        return self._apply_decoded(
            current,
            cast(Seat2048, seat),
            checked_direction,
            expected,
            noop_state,
        )

    def _apply_decoded(
        self,
        current: Game2048State,
        seat: Seat2048,
        direction: Direction,
        expected_board_version: int,
        noop_state: Game2048State,
    ) -> MoveResult:
        if current.boards is None:
            return _inactive_error()
        player = current.boards[seat]
        if expected_board_version != player.board_version:
            return MoveError(
                code="stale_board_version",
                message="Board version is stale",
                current_board_version=player.board_version,
            )
        if player.finished:
            return MoveNoop(state=noop_state)

        moved = slide_and_merge(player.board, direction)
        if not moved.moved:
            return MoveNoop(state=noop_state)

        next_score = player.score + moved.gained
        next_board_version = player.board_version + 1
        if not _transition_values_are_safe(
            moved.board,
            moved.gained,
            next_score,
            next_board_version,
        ):
            raise RuntimeError(TRANSITION_OVERFLOW_MESSAGE)

        rng = Mulberry32.from_state(player.rng_state)
        spawned_result = spawn_tile(moved.board, rng)
        if spawned_result.spawned is None:
            raise RuntimeError(SPAWN_INVARIANT_MESSAGE)
        next_player = Player2048State(
            seat=seat,
            board=spawned_result.board,
            score=next_score,
            highest_tile=highest_tile(spawned_result.board),
            board_version=next_board_version,
            finished=not can_move(spawned_result.board),
            rng_state=rng.state,
        )
        next_boards = [_clone_player(current.boards[0]), _clone_player(current.boards[1])]
        next_boards[seat] = next_player

        completed = next_boards[0].finished and next_boards[1].finished
        winner_seat: Seat2048 | None = None
        draw = False
        if completed:
            if next_boards[0].score == next_boards[1].score:
                draw = True
            else:
                winner_seat = 0 if next_boards[0].score > next_boards[1].score else 1

        next_state = Game2048State(
            boards=(next_boards[0], next_boards[1]),
            winner_seat=winner_seat,
            draw=draw,
        )
        return MoveAccepted(
            state=next_state,
            completed=completed,
            action=MoveAction(
                direction=direction,
                score_delta=moved.gained,
                spawned=spawned_result.spawned,
                resulting_board_version=next_board_version,
            ),
        )

    def public_state(self, state: object, status: RoomStatus) -> dict[str, object]:
        decoded = self.decode_state(state, status)
        return {
            "boards": (
                None
                if decoded.boards is None
                else [
                    {
                        "seat": player.seat,
                        "board": [list(row) for row in player.board],
                        "score": player.score,
                        "highest_tile": player.highest_tile,
                        "board_version": player.board_version,
                        "finished": player.finished,
                    }
                    for player in decoded.boards
                ]
            ),
            "winner_seat": decoded.winner_seat,
            "draw": decoded.draw,
        }

    def expire(self, state: object) -> Game2048State:
        return _clone_state(_decode_compatible_state(state))


__all__ = [
    "Board",
    "EngineMove",
    "Game2048Adapter",
    "Game2048State",
    "MoveAccepted",
    "MoveAction",
    "MoveError",
    "MoveNoop",
    "Player2048State",
    "SpawnResult",
    "SpawnedTile",
    "can_move",
    "empty_board",
    "highest_tile",
    "slide_and_merge",
    "spawn_tile",
]
