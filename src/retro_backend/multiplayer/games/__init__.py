"""Authoritative 2048 rules and deterministic random-number generation."""

from .game_2048 import (
    Board,
    EngineMove,
    Game2048Adapter,
    Game2048State,
    MoveAccepted,
    MoveAction,
    MoveError,
    MoveNoop,
    Player2048State,
    SpawnedTile,
    SpawnResult,
    can_move,
    empty_board,
    highest_tile,
    slide_and_merge,
    spawn_tile,
)
from .rng import Mulberry32

__all__ = [
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
]
