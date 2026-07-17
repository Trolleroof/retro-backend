from sqlalchemy import inspect

from retro_backend.models import Base


def test_database_metadata_contains_only_users_and_high_scores() -> None:
    assert set(Base.metadata.tables) == {"users", "high_scores"}


def test_user_and_high_score_schema_matches_api_contract() -> None:
    users = Base.metadata.tables["users"]
    high_scores = Base.metadata.tables["high_scores"]

    assert set(users.columns.keys()) == {"id", "auth_token_hash", "joined_at"}
    assert users.c.auth_token_hash.unique is True
    assert users.c.auth_token_hash.nullable is False

    assert set(high_scores.columns.keys()) == {"user_id", "game_type", "score", "achieved_at"}
    assert [column.name for column in high_scores.primary_key.columns] == ["user_id", "game_type"]
    assert high_scores.c.user_id.foreign_keys

    indexes = {str(index.name): index for index in inspect(high_scores).indexes}
    ranking = indexes["ix_high_scores_game_score_desc"]
    assert [str(expression) for expression in ranking.expressions] == [
        "high_scores.game_type",
        "high_scores.score DESC",
    ]
