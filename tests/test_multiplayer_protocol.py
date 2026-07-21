import json
import operator
import shutil
import subprocess
from collections.abc import MutableSequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast, get_args

import pytest
from pydantic import ValidationError

from retro_backend.multiplayer import protocol as protocol_module
from retro_backend.multiplayer.errors import (
    HTTP_STATUS_BY_ERROR_CODE,
    PUBLIC_ERROR_MESSAGES,
    ErrorEnvelope,
    ProtocolFault,
    RoomErrorCode,
    RoomFault,
    error_envelope,
    http_status_for_fault,
    rpc_failure,
    rpc_success,
)
from retro_backend.multiplayer.protocol import (
    MAX_CLIENT_FRAME_BYTES,
    MAX_SAFE_INTEGER,
    ClientCommand,
    FrameTooLarge,
    Game2048BoardSnapshot,
    Game2048Snapshot,
    GameDescriptor,
    MoveCommand,
    ReadyCommand,
    RoomSnapshot,
    ServerErrorMessage,
    ServerSnapshotMessage,
    canonical_http_origin,
    parse_client_frame,
    parse_room_id,
)

ROOM_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeJsURLConstructor:
    def __init__(self, parsed_url: object) -> None:
        self.parsed_url = parsed_url
        self.calls: list[str] = []

    def new(self, value: str) -> object:
        self.calls.append(value)
        return self.parsed_url


def _fake_js_url(**changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "protocol": "https:",
        "username": "",
        "password": "",
        "pathname": "/",
        "search": "",
        "hash": "",
        "origin": "https://foo_bar.internal",
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("HTTPS://ARCADE.EXAMPLE:443/", "https://arcade.example"),
        ("http://arcade.example:80", "http://arcade.example"),
        ("https://arcade.example:8443", "https://arcade.example:8443"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://[2001:0db8::1]", "http://[2001:db8::1]"),
        ("http://[::ffff:127.0.0.1]", "http://[::ffff:7f00:1]"),
        ("http://[::ffff:7f00:1]", "http://[::ffff:7f00:1]"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
        ("https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example", "https://xn--bcher-kva.example"),
        ("https://xn--fa-hia.de", "https://xn--fa-hia.de"),
        ("https://fa\N{LATIN SMALL LETTER SHARP S}.de", "https://xn--fa-hia.de"),
        ("https://xn--strae-oqa.de", "https://xn--strae-oqa.de"),
        ("https://stra\N{LATIN SMALL LETTER SHARP S}e.de", "https://xn--strae-oqa.de"),
    ],
)
def test_canonical_http_origin_matches_browser_host_semantics(
    value: str,
    expected: str,
) -> None:
    assert canonical_http_origin(value) == expected


def test_cpython_origin_fallback_rejects_scoped_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol_module, "_JS_URL", None, raising=False)

    assert canonical_http_origin("http://[::1%25eth0]") is None


def test_cpython_origin_fallback_accepts_browser_underscore_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol_module, "_JS_URL", None, raising=False)

    assert (
        canonical_http_origin("https://FOO_BAR.internal:443")
        == "https://foo_bar.internal"
    )


def test_worker_origin_adapter_uses_js_url_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = FakeJsURLConstructor(_fake_js_url())
    monkeypatch.setattr(protocol_module, "_JS_URL", constructor, raising=False)

    assert (
        canonical_http_origin("https://foo_bar.internal")
        == "https://foo_bar.internal"
    )
    assert constructor.calls == ["https://foo_bar.internal"]


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": "ftp:"},
        {"username": "user"},
        {"password": "secret"},
        {"pathname": "/path"},
        {"search": "?query"},
        {"hash": "#fragment"},
        {"origin": None},
    ],
)
def test_worker_origin_adapter_validates_js_url_properties(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
) -> None:
    constructor = FakeJsURLConstructor(_fake_js_url(**changes))
    monkeypatch.setattr(protocol_module, "_JS_URL", constructor, raising=False)

    assert canonical_http_origin("https://foo_bar.internal") is None
    assert constructor.calls == ["https://foo_bar.internal"]


def test_cpython_origin_fallback_matches_generated_node_host_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for WHATWG URL differential coverage")
    values = [
        *[f"https://foo_{index}.internal" for index in range(32)],
        *[f"http://0x7f.{index}" for index in range(32)],
        *[f"https://%66oo_{index}.internal" for index in range(32)],
        *[f"http://[::1%25eth{index}]" for index in range(32)],
    ]
    script = """
const values = JSON.parse(process.argv[1]);
const origins = values.map((value) => {
  try {
    const url = new URL(value);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.username !== "" ||
      url.password !== "" ||
      url.pathname !== "/" ||
      url.search !== "" ||
      url.hash !== ""
    ) return null;
    return url.origin;
  } catch {
    return null;
  }
});
process.stdout.write(JSON.stringify(origins));
"""
    completed = subprocess.run(
        [node, "-e", script, json.dumps(values)],
        check=True,
        capture_output=True,
        text=True,
    )
    expected = cast(list[str | None], json.loads(completed.stdout))
    monkeypatch.setattr(protocol_module, "_JS_URL", None, raising=False)

    assert [canonical_http_origin(value) for value in values] == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://127.1", "https://127.0.0.1"),
        ("https://127.0.1", "https://127.0.0.1"),
        ("https://1.2", "https://1.0.0.2"),
        ("https://1.2.3", "https://1.2.0.3"),
        ("https://2130706433", "https://127.0.0.1"),
        ("https://0x7f.1", "https://127.0.0.1"),
        ("https://0177.1", "https://127.0.0.1"),
        ("https://00127.0001", "https://87.0.0.1"),
        ("https://127.0.0.1.", "https://127.0.0.1"),
        ("https://256", "https://0.0.1.0"),
        ("https://4294967295", "https://255.255.255.255"),
        ("https://0x.1", "https://0.0.0.1"),
        ("https://%65xample.com", "https://example.com"),
        ("https://example%2ecom", "https://example.com"),
        ("https://%C3%BC.example", "https://xn--tda.example"),
        ("https://%31%32%37.1", "https://127.0.0.1"),
        ("https://%30x7f.1", "https://127.0.0.1"),
    ],
)
def test_canonical_http_origin_matches_whatwg_special_host_parsing(
    value: str,
    expected: str,
) -> None:
    assert canonical_http_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://1.2.3.256",
        "https://1.2.3.4.5",
        "https://4294967296",
        "https://0400.1",
        "https://0x100000000",
        "https://08.1",
        "https://example.1",
        "https://%2F.example",
        "https://%40.example",
        "https://%25.example",
        "https://%FF.example",
        "https://%00.example",
        "https://%09example.com",
        "https://%20example.com",
        "https://%.example",
        "https://%6.example",
    ],
)
def test_canonical_http_origin_rejects_invalid_whatwg_special_hosts(
    value: str,
) -> None:
    assert canonical_http_origin(value) is None


def test_canonical_http_origin_bounds_invalid_alabel_processing() -> None:
    assert canonical_http_origin(f"https://xn--{'a' * 10_000}.example") is None


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "ftp://arcade.example",
        "https://user@arcade.example",
        "https://arcade.example/path",
        "https://arcade.example?query",
        "https://arcade.example#fragment",
        "https://xn--a.example",
    ],
)
def test_canonical_http_origin_rejects_non_origins_and_invalid_alabels(value: str) -> None:
    assert canonical_http_origin(value) is None


def _append(collection: object, value: object) -> None:
    operator.methodcaller("append", value)(collection)


def _setitem(collection: object, index: int, value: object) -> None:
    operator.setitem(cast(MutableSequence[object], collection), index, value)


def test_game_descriptor_accepts_only_exact_2048_1v1_pair() -> None:
    descriptor = GameDescriptor.model_validate(
        {"game_type": "2048", "mode": "1v1"}
    )

    assert descriptor.model_dump() == {"game_type": "2048", "mode": "1v1"}


@pytest.mark.parametrize(
    "payload",
    [
        {"game_type": "chess", "mode": "solo"},
        {"game_type": "chess", "mode": "1v1"},
        {"game_type": "2048", "mode": "solo"},
        {"game_type": "tetris", "mode": "1v1"},
        {"game_type": "2048", "mode": "1v1", "seed": 1},
        {"game_type": 2048, "mode": "1v1"},
    ],
)
def test_game_descriptor_rejects_other_pairs_types_and_extra_keys(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GameDescriptor.model_validate(payload)


def test_game_descriptor_is_frozen() -> None:
    descriptor = GameDescriptor(game_type="2048", mode="1v1")

    with pytest.raises(ValidationError):
        descriptor.mode = "1v1"  # type: ignore[misc]


def test_client_command_and_room_snapshot_types_are_2048_only() -> None:
    assert get_args(ClientCommand.__value__) == (ReadyCommand, MoveCommand)
    assert RoomSnapshot is Game2048Snapshot


@pytest.mark.parametrize(
    "room_id",
    [
        ROOM_ID,
        "123e4567-e89b-82d3-b456-426614174000",
    ],
)
def test_parse_room_id_accepts_canonical_supported_uuid_versions(room_id: str) -> None:
    assert parse_room_id(room_id) == room_id


@pytest.mark.parametrize(
    "room_id",
    [
        "123E4567-E89B-12D3-A456-426614174000",
        "123e4567-e89b-02d3-a456-426614174000",
        "123e4567-e89b-92d3-a456-426614174000",
        "123e4567-e89b-12d3-7456-426614174000",
        "{123e4567-e89b-12d3-a456-426614174000}",
        "123e4567e89b12d3a456426614174000",
        123,
    ],
)
def test_invalid_or_noncanonical_room_id_maps_to_room_not_found(
    room_id: object,
) -> None:
    with pytest.raises(RoomFault) as raised:
        parse_room_id(room_id)

    assert raised.value.code == "room_not_found"
    assert error_envelope(raised.value) == {
        "error": {"code": "room_not_found", "message": "Room not found"}
    }


@pytest.mark.parametrize("expected_version", [0, MAX_SAFE_INTEGER])
def test_ready_accepts_nonnegative_js_safe_version(expected_version: int) -> None:
    command = parse_client_frame(
        json.dumps({"type": "ready", "expected_version": expected_version}),
        GameDescriptor(game_type="2048", mode="1v1"),
    )

    assert command == ReadyCommand(expected_version=expected_version)


@pytest.mark.parametrize(
    "expected_version",
    [-1, MAX_SAFE_INTEGER + 1, True, 1.0, "1"],
)
def test_ready_rejects_invalid_version_with_stable_message(
    expected_version: object,
) -> None:
    frame = json.dumps({"type": "ready", "expected_version": expected_version})

    with pytest.raises(ProtocolFault, match="^Invalid ready command$"):
        parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "ready"},
        {"type": "ready", "expected_version": 0, "extra": False},
        {"type": "ready", "expected_version": 0, "request_id": "ready-1"},
    ],
)
def test_ready_requires_exact_keys(payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolFault, match="^Invalid ready command$"):
        parse_client_frame(
            json.dumps(payload), GameDescriptor(game_type="2048", mode="1v1")
        )


@pytest.mark.parametrize(
    "frame",
    [
        '{"type":"spin","expected_version":0}',
        '{"type":"spin","direction":"clockwise","expected_version":0}',
    ],
)
def test_parser_rejects_unknown_command_types(frame: str) -> None:
    with pytest.raises(ProtocolFault, match="^Unknown client command type$"):
        parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))


def test_parse_move_preserves_valid_request_id() -> None:
    command = parse_client_frame(
        '{"type":"move","direction":"left","expected_board_version":3,'
        + '"request_id":"move_1"}',
        GameDescriptor(game_type="2048", mode="1v1"),
    )

    assert command == MoveCommand(
        direction="left", expected_board_version=3, request_id="move_1"
    )


@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_move_accepts_each_direction_without_request_id(direction: str) -> None:
    command = parse_client_frame(
        json.dumps(
            {"type": "move", "direction": direction, "expected_board_version": 0}
        ),
        GameDescriptor(game_type="2048", mode="1v1"),
    )

    assert isinstance(command, MoveCommand)
    assert command.direction == direction
    assert command.expected_board_version == 0
    assert command.request_id is None


@pytest.mark.parametrize("request_id", ["a", "A_0-9", "x" * 64])
def test_move_accepts_request_id_boundaries(request_id: str) -> None:
    command = parse_client_frame(
        json.dumps(
            {
                "type": "move",
                "direction": "up",
                "expected_board_version": 0,
                "request_id": request_id,
            }
        ),
        GameDescriptor(game_type="2048", mode="1v1"),
    )

    assert isinstance(command, MoveCommand)
    assert command.request_id == request_id


@pytest.mark.parametrize("request_id", ["", "x" * 65, "with space", "a.b", None])
def test_move_rejects_malformed_request_id(request_id: object) -> None:
    frame = json.dumps(
        {
            "type": "move",
            "direction": "up",
            "expected_board_version": 0,
            "request_id": request_id,
        }
    )

    with pytest.raises(ProtocolFault, match="^Invalid move command$"):
        parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "move", "direction": "north", "expected_board_version": 0},
        {"type": "move", "direction": "up"},
        {
            "type": "move",
            "direction": "up",
            "expected_board_version": True,
        },
        {
            "type": "move",
            "direction": "up",
            "expected_board_version": MAX_SAFE_INTEGER + 1,
        },
        {
            "type": "move",
            "direction": "up",
            "expected_board_version": 0,
            "seed": 1,
        },
    ],
)
def test_move_requires_exact_keys_and_types(payload: dict[str, object]) -> None:
    with pytest.raises(ProtocolFault, match="^Invalid move command$"):
        parse_client_frame(
            json.dumps(payload), GameDescriptor(game_type="2048", mode="1v1")
        )


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (b'{"type":"ready","expected_version":0}', "Client frames must be text"),
        ("not-json", "Client frame must contain valid JSON"),
        ("[]", "Client command must be an object with a type"),
        ("{}", "Client command must be an object with a type"),
        ('{"type":1}', "Client command must be an object with a type"),
        ('{"type":"pause"}', "Unknown client command type"),
    ],
)
def test_parser_preserves_stable_messages(frame: object, message: str) -> None:
    with pytest.raises(ProtocolFault, match=f"^{message}$") as raised:
        parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))

    assert raised.value.code == "invalid_request"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parser_rejects_nonstandard_json_constants(constant: str) -> None:
    frame = f'{{"type":"ready","expected_version":{constant}}}'

    with pytest.raises(
        ProtocolFault, match="^Client frame must contain valid JSON$"
    ) as raised:
        parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))

    assert raised.value.code == "invalid_request"


def test_parser_enforces_frame_limit_in_utf8_bytes() -> None:
    at_limit = "e" * MAX_CLIENT_FRAME_BYTES

    with pytest.raises(
        ProtocolFault, match="^Client frame must contain valid JSON$"
    ):
        parse_client_frame(at_limit, GameDescriptor(game_type="2048", mode="1v1"))

    over_limit = "\u00e9" * (MAX_CLIENT_FRAME_BYTES // 2 + 1)
    with pytest.raises(FrameTooLarge) as raised:
        parse_client_frame(over_limit, GameDescriptor(game_type="2048", mode="1v1"))

    assert raised.value.code == "invalid_request"
    assert raised.value.close_code == 1009
    assert len(over_limit.encode("utf-8")) == MAX_CLIENT_FRAME_BYTES + 2


def _common_snapshot_fields(participants: list[dict[str, object]]) -> dict[str, Any]:
    created_at = datetime(2026, 7, 20, 12, tzinfo=UTC)
    return {
        "room_id": ROOM_ID,
        "status": "active",
        "created_at": created_at,
        "expires_at": created_at + timedelta(minutes=30),
        "version": 4,
        "participants": participants,
    }


def _game_2048_participant() -> dict[str, object]:
    return {
        "user_id": "usr_one",
        "seat": 0,
        "ready": True,
        "connected": True,
    }


def _game_2048_boards() -> list[dict[str, object]]:
    return [
        {
            "seat": 0,
            "board": [[2, 0, 0, 0], *([[0, 0, 0, 0]] * 3)],
            "score": 0,
            "highest_tile": 2,
            "board_version": 0,
            "finished": False,
        },
        {
            "seat": 1,
            "board": [[0, 2, 0, 0], *([[0, 0, 0, 0]] * 3)],
            "score": 0,
            "highest_tile": 2,
            "board_version": 0,
            "finished": False,
        },
    ]


def test_2048_board_snapshot_rejects_mismatched_highest_tile() -> None:
    board = _game_2048_boards()[0]
    board["highest_tile"] = 1

    with pytest.raises(
        ValidationError, match="highest_tile must equal the board's maximum tile"
    ):
        Game2048BoardSnapshot.model_validate(board)


def test_2048_snapshot_rejects_tile_one() -> None:
    boards = _game_2048_boards()
    first_grid = cast(list[list[int]], boards[0]["board"])
    first_grid[0][0] = 1

    with pytest.raises(
        ValidationError, match="2048 tiles must be zero or powers of two"
    ):
        Game2048Snapshot.model_validate(
            {
                **_common_snapshot_fields([_game_2048_participant()]),
                "game_type": "2048",
                "mode": "1v1",
                "boards": boards,
                "winner_seat": None,
                "draw": False,
            }
        )


def _active_game_2048_snapshot() -> Game2048Snapshot:
    return Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "game_type": "2048",
            "mode": "1v1",
            "boards": _game_2048_boards(),
            "winner_seat": None,
            "draw": False,
        }
    )


def test_2048_waiting_snapshot_has_no_boards() -> None:
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "status": "waiting",
            "version": 0,
            "game_type": "2048",
            "mode": "1v1",
            "boards": None,
            "winner_seat": None,
            "draw": False,
        }
    )

    assert snapshot.boards is None
    assert "rng_state" not in snapshot.model_dump_json()
    assert json.loads(snapshot.model_dump_json())["boards"] is None


def test_2048_snapshot_is_strict_frozen_and_contains_no_rng_state() -> None:
    snapshot = _active_game_2048_snapshot()

    with pytest.raises(ValidationError):
        snapshot.version = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Game2048Snapshot.model_validate(
            {
                **snapshot.model_dump(),
                "rng_state": 123,
            }
        )


def test_2048_snapshot_defensively_converts_mutable_inputs() -> None:
    participants = [_game_2048_participant()]
    boards = _game_2048_boards()
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields(participants),
            "game_type": "2048",
            "mode": "1v1",
            "boards": boards,
            "winner_seat": None,
            "draw": False,
        }
    )

    participants[0]["ready"] = False
    first_grid = cast(list[list[int]], boards[0]["board"])
    first_grid[0][0] = 4

    assert snapshot.participants[0].ready is True
    assert snapshot.boards is not None
    assert snapshot.boards[0].board[0][0] == 2


def test_2048_snapshot_exposes_public_boards_without_rng_state() -> None:
    first_board = Game2048BoardSnapshot.model_validate(
        {
            "seat": 0,
            "board": [[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            "score": 0,
            "highest_tile": 2,
            "board_version": 0,
            "finished": False,
        }
    )
    second_board = Game2048BoardSnapshot.model_validate(
        {
            "seat": 1,
            "board": [[0, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            "score": 0,
            "highest_tile": 2,
            "board_version": 0,
            "finished": False,
        }
    )
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "game_type": "2048",
            "mode": "1v1",
            "boards": [first_board.model_dump(), second_board.model_dump()],
            "winner_seat": None,
            "draw": False,
        }
    )

    assert snapshot.boards == (first_board, second_board)
    assert snapshot.participants[0].model_dump() == _game_2048_participant()
    assert "team" not in snapshot.participants[0].model_dump()
    assert "rng_state" not in snapshot.model_dump_json()


def test_2048_snapshot_collections_are_deeply_immutable() -> None:
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "game_type": "2048",
            "mode": "1v1",
            "boards": _game_2048_boards(),
            "winner_seat": None,
            "draw": False,
        }
    )
    boards = snapshot.boards
    assert boards is not None

    with pytest.raises(TypeError):
        _setitem(boards[0].board[0], 0, 4)
    with pytest.raises(AttributeError):
        _append(boards[0].board[0], 4)
    with pytest.raises(AttributeError):
        _append(boards[0].board, (0, 0, 0, 0))
    with pytest.raises(AttributeError):
        _append(boards, boards[0])
    with pytest.raises(TypeError):
        _setitem(boards, 0, boards[1])
    with pytest.raises(AttributeError):
        _append(snapshot.participants, snapshot.participants[0])
    with pytest.raises(ValidationError):
        snapshot.boards = boards[::-1]  # type: ignore[misc]


def test_2048_snapshot_serializes_collections_as_json_arrays() -> None:
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "game_type": "2048",
            "mode": "1v1",
            "boards": _game_2048_boards(),
            "winner_seat": None,
            "draw": False,
        }
    )

    payload = snapshot.model_dump(mode="json")
    encoded = json.loads(snapshot.model_dump_json())

    assert isinstance(payload["boards"], list)
    assert isinstance(payload["boards"][0]["board"], list)
    assert isinstance(payload["boards"][0]["board"][0], list)
    assert isinstance(payload["participants"], list)
    assert isinstance(encoded["boards"], list)
    assert isinstance(encoded["boards"][0]["board"], list)
    assert isinstance(encoded["boards"][0]["board"][0], list)
    assert isinstance(encoded["participants"], list)
    assert payload["winner_seat"] is None
    assert encoded["winner_seat"] is None


def test_room_snapshot_serializes_timestamps_like_javascript_dates() -> None:
    created_at = datetime(2026, 7, 20, 12, tzinfo=UTC)
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "created_at": created_at,
            "expires_at": created_at.replace(microsecond=123_999),
            "game_type": "2048",
            "mode": "1v1",
            "boards": _game_2048_boards(),
            "winner_seat": None,
            "draw": False,
        }
    )

    payload = snapshot.model_dump(mode="json")

    assert payload["created_at"] == "2026-07-20T12:00:00.000Z"
    assert payload["expires_at"] == "2026-07-20T12:00:00.123Z"


@pytest.mark.parametrize(
    "participant",
    [
        {
            "user_id": "usr_one",
            "seat": 0,
            "team": 1,
            "ready": True,
            "connected": True,
        },
        {
            "user_id": "usr_one",
            "seat": 2,
            "ready": True,
            "connected": True,
        },
        {
            "user_id": "usr_one",
            "seat": False,
            "ready": True,
            "connected": True,
        },
    ],
)
def test_2048_snapshot_rejects_team_and_non_player_seats(
    participant: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Game2048Snapshot.model_validate(
            {
                **_common_snapshot_fields([participant]),
                "game_type": "2048",
                "mode": "1v1",
                "boards": _game_2048_boards(),
                "winner_seat": None,
                "draw": False,
            }
        )


@pytest.mark.parametrize(
    "boards",
    [
        _game_2048_boards()[:1],
        [_game_2048_boards()[0], _game_2048_boards()[0]],
    ],
)
def test_2048_populated_boards_require_exact_seats(
    boards: list[dict[str, object]],
) -> None:
    with pytest.raises(ValidationError):
        Game2048Snapshot.model_validate(
            {
                **_common_snapshot_fields([_game_2048_participant()]),
                "game_type": "2048",
                "mode": "1v1",
                "boards": boards,
                "winner_seat": None,
                "draw": False,
            }
        )


def test_server_snapshot_and_error_messages_have_exact_shapes() -> None:
    snapshot = _active_game_2048_snapshot()

    assert ServerSnapshotMessage(
        snapshot=snapshot, request_id="move_1"
    ).model_dump(mode="json") == {
        "type": "snapshot",
        "snapshot": snapshot.model_dump(mode="json"),
        "request_id": "move_1",
    }


def test_server_error_omits_absent_optional_fields_from_all_serializers() -> None:
    message = ServerErrorMessage(
        code="unauthorized",
        message="Unauthorized",
        current_version=4,
    )
    expected = {
        "type": "error",
        "code": "unauthorized",
        "message": "Unauthorized",
        "current_version": 4,
    }

    assert message.model_dump(mode="json") == expected
    assert json.loads(message.model_dump_json()) == expected


def test_server_error_preserves_present_board_version_in_all_serializers() -> None:
    message = ServerErrorMessage(
        code="stale_board_version",
        message="Board version is stale",
        current_version=4,
        current_board_version=2,
        request_id="move_1",
    )
    expected = {
        "type": "error",
        "code": "stale_board_version",
        "message": "Board version is stale",
        "current_version": 4,
        "current_board_version": 2,
        "request_id": "move_1",
    }

    assert message.model_dump(mode="json") == expected
    assert json.loads(message.model_dump_json()) == expected
    assert ServerErrorMessage(
        code="stale_board_version",
        message="Board version is stale",
        current_version=4,
        current_board_version=2,
        request_id="move_1",
    ).model_dump(exclude_none=True) == {
        "type": "error",
        "code": "stale_board_version",
        "message": "Board version is stale",
        "current_version": 4,
        "current_board_version": 2,
        "request_id": "move_1",
    }


def test_server_snapshot_omits_absent_request_id_but_preserves_snapshot_nulls() -> None:
    snapshot = Game2048Snapshot.model_validate(
        {
            **_common_snapshot_fields([_game_2048_participant()]),
            "status": "waiting",
            "version": 0,
            "game_type": "2048",
            "mode": "1v1",
            "boards": None,
            "winner_seat": None,
            "draw": False,
        }
    )

    payload = ServerSnapshotMessage(snapshot=snapshot).model_dump(mode="json")
    encoded = json.loads(
        ServerSnapshotMessage(snapshot=snapshot).model_dump_json()
    )

    assert "request_id" not in payload
    assert payload["snapshot"]["boards"] is None  # type: ignore[index]
    assert payload["snapshot"]["winner_seat"] is None  # type: ignore[index]
    assert encoded["snapshot"]["boards"] is None
    assert encoded["snapshot"]["winner_seat"] is None


def test_server_snapshot_rejects_explicit_null_request_id() -> None:
    snapshot = _active_game_2048_snapshot()

    with pytest.raises(ValidationError):
        ServerSnapshotMessage.model_validate(
            {"type": "snapshot", "snapshot": snapshot, "request_id": None}
        )


def test_server_error_rejects_explicit_null_request_id() -> None:
    with pytest.raises(ValidationError):
        ServerErrorMessage.model_validate(
            {
                "type": "error",
                "code": "stale_version",
                "message": "Room version is stale",
                "current_version": 1,
                "request_id": None,
            }
        )


@pytest.mark.parametrize("current_version", [-1, MAX_SAFE_INTEGER + 1, True, 1.0])
def test_server_error_requires_js_safe_current_version(
    current_version: object,
) -> None:
    with pytest.raises(ValidationError):
        ServerErrorMessage.model_validate(
            {
                "type": "error",
                "code": "stale_version",
                "message": "Room version is stale",
                "current_version": current_version,
            }
        )


def test_server_error_requires_current_version_field() -> None:
    with pytest.raises(ValidationError):
        ServerErrorMessage.model_validate(
            {
                "type": "error",
                "code": "unauthorized",
                "message": "Unauthorized",
            }
        )


@pytest.mark.parametrize("request_id", ["", "x" * 65, "with space", "a.b"])
def test_server_snapshot_rejects_invalid_request_id(request_id: str) -> None:
    snapshot = _active_game_2048_snapshot()

    with pytest.raises(ValidationError):
        ServerSnapshotMessage.model_validate(
            {
                "type": "snapshot",
                "snapshot": snapshot,
                "request_id": request_id,
            }
        )


def test_server_messages_accept_request_and_version_upper_bounds() -> None:
    snapshot = _active_game_2048_snapshot()

    snapshot_message = ServerSnapshotMessage(
        snapshot=snapshot,
        request_id="x" * 64,
    )
    error_message = ServerErrorMessage(
        code="stale_version",
        message="Room version is stale",
        current_version=MAX_SAFE_INTEGER,
    )

    assert snapshot_message.request_id == "x" * 64
    assert error_message.current_version == MAX_SAFE_INTEGER


def test_public_room_error_code_vocabulary_is_exact() -> None:
    expected = (
        "unauthorized",
        "service_unavailable",
        "invalid_request",
        "room_not_found",
        "room_full",
        "not_joined",
        "room_expired",
        "match_not_active",
        "stale_version",
        "stale_board_version",
        "invalid_ticket",
        "ticket_expired",
        "ticket_consumed",
    )

    assert get_args(RoomErrorCode) == expected
    assert tuple(PUBLIC_ERROR_MESSAGES) == expected


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("invalid_request", 400),
        ("invalid_ticket", 401),
        ("ticket_expired", 401),
        ("ticket_consumed", 401),
        ("not_joined", 403),
        ("room_not_found", 404),
        ("room_full", 409),
        ("room_expired", 410),
        ("service_unavailable", 503),
    ],
)
def test_room_fault_has_stable_http_mapping(
    code: RoomErrorCode, expected_status: int
) -> None:
    fault = RoomFault(code, "Public message")

    assert HTTP_STATUS_BY_ERROR_CODE[code] == expected_status
    assert http_status_for_fault(fault) == expected_status


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("service_unavailable", "Room service unavailable"),
        ("not_joined", "User has not joined this room"),
        ("room_expired", "Room has expired"),
        ("ticket_expired", "Ticket has expired"),
        ("ticket_consumed", "Ticket has already been consumed"),
    ],
)
def test_room_fault_uses_accepted_public_messages(
    code: RoomErrorCode,
    message: str,
) -> None:
    assert RoomFault.for_code(code).message == message


def test_error_and_rpc_envelopes_only_serialize_public_fault_fields() -> None:
    internal_error = RuntimeError("database password leaked")
    fault = RoomFault.service_unavailable()
    fault.__cause__ = internal_error

    assert ErrorEnvelope.from_fault(fault).model_dump() == {
        "error": {
            "code": "service_unavailable",
            "message": "Room service unavailable",
        }
    }
    assert rpc_success({"version": 1}) == {"ok": True, "value": {"version": 1}}
    assert rpc_failure(fault) == {
        "ok": False,
        "error": {
            "code": "service_unavailable",
            "message": "Room service unavailable",
        },
    }
    assert "database password" not in json.dumps(rpc_failure(fault))


@pytest.mark.parametrize("code", get_args(RoomErrorCode))
def test_room_fault_preserves_public_error_codes(code: RoomErrorCode) -> None:
    fault = RoomFault(code, "Stable public message")
    failure = rpc_failure(fault)
    error = failure["error"]

    assert isinstance(error, dict)
    assert error["code"] == code
