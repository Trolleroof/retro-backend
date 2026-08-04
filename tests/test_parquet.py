import io
import struct

import pytest

from retro_backend.parquet import (
    CREATED_BY,
    PARQUET_MAGIC,
    ParquetColumn,
    write_parquet,
)

ALL_TYPE_COLUMNS = (
    ParquetColumn("text", "UTF8"),
    ParquetColumn("small", "INT32"),
    ParquetColumn("large", "INT64"),
    ParquetColumn("number", "DOUBLE"),
    ParquetColumn("flag", "BOOLEAN"),
)
ALL_TYPE_ROWS: tuple[tuple[object, ...], ...] = (
    ("alpha", 1, 10, 1.5, True),
    ("é中", -2, -20, -0.25, False),
    ("", 0, 0, 0.0, True),
)

NULLABLE_COLUMNS = (
    ParquetColumn("key", "UTF8"),
    ParquetColumn("label", "UTF8", optional=True),
    ParquetColumn("small", "INT32", optional=True),
    ParquetColumn("large", "INT64", optional=True),
    ParquetColumn("number", "DOUBLE", optional=True),
    ParquetColumn("flag", "BOOLEAN", optional=True),
)
NULLABLE_ROWS: tuple[tuple[object, ...], ...] = (
    ("a", None, 1, 10, 1.5, True),
    ("b", "x", None, None, None, None),
    ("c", "y", -2, -20, -0.25, False),
    ("d", None, None, None, None, True),
)

# One INT32 column holding the single value 7. Byte-for-byte: PAR1, a v1 data
# page header, the 4-byte PLAIN payload, the Thrift-compact FileMetaData, its
# little-endian length, and PAR1 again.
KNOWN_SINGLE_INT32_FILE = bytes.fromhex("".join((
    "50415231",  # PAR1
    "1500",  # PageHeader.type = DATA_PAGE
    "1508",  # PageHeader.uncompressed_page_size = 4
    "1508",  # PageHeader.compressed_page_size = 4
    "2c",  # PageHeader.data_page_header (field 5, struct)
    "1502",  # DataPageHeader.num_values = 1
    "1500",  # DataPageHeader.encoding = PLAIN
    "1506",  # DataPageHeader.definition_level_encoding = RLE
    "1506",  # DataPageHeader.repetition_level_encoding = RLE
    "0000",  # end DataPageHeader, end PageHeader
    "07000000",  # PLAIN int32 7
    "1502",  # FileMetaData.version = 1
    "192c",  # FileMetaData.schema: list of 2 structs
    "4806736368656d61",  # SchemaElement.name = "schema"
    "1502",  # SchemaElement.num_children = 1
    "00",
    "1502",  # SchemaElement.type = INT32
    "2500",  # SchemaElement.repetition_type = REQUIRED
    "18016e",  # SchemaElement.name = "n"
    "00",
    "1602",  # FileMetaData.num_rows = 1
    "191c",  # FileMetaData.row_groups: list of 1 struct
    "191c",  # RowGroup.columns: list of 1 struct
    "2608",  # ColumnChunk.file_offset = 4
    "1c",  # ColumnChunk.meta_data
    "1502",  # ColumnMetaData.type = INT32
    "1915 00",  # encodings: list of 1 i32, PLAIN
    "1918 016e",  # path_in_schema: list of 1 binary, "n"
    "1500",  # codec = UNCOMPRESSED
    "1602",  # num_values = 1
    "162a",  # total_uncompressed_size = 21
    "162a",  # total_compressed_size = 21
    "2608",  # data_page_offset = 4
    "0000",  # end ColumnMetaData, end ColumnChunk
    "162a",  # RowGroup.total_byte_size = 21
    "1602",  # RowGroup.num_rows = 1
    "00",  # end RowGroup
    "280d726574726f2d6261636b656e64",  # created_by = "retro-backend"
    "00",  # end FileMetaData
    "4a000000",  # FileMetaData length = 74
    "50415231",  # PAR1
)).replace(" ", ""))


def test_known_small_file_is_byte_for_byte_stable() -> None:
    encoded = write_parquet([ParquetColumn("n", "INT32")], [[7]])

    assert encoded == KNOWN_SINGLE_INT32_FILE


def test_file_is_framed_by_magic_and_a_consistent_footer_length() -> None:
    encoded = write_parquet(ALL_TYPE_COLUMNS, ALL_TYPE_ROWS)

    assert encoded.startswith(PARQUET_MAGIC)
    assert encoded.endswith(PARQUET_MAGIC)
    (footer_length,) = struct.unpack("<I", encoded[-8:-4])
    footer = encoded[-8 - footer_length : -8]
    assert len(footer) == footer_length
    assert CREATED_BY.encode("utf-8") in footer


def test_plain_payloads_are_little_endian_and_bit_packed() -> None:
    encoded = write_parquet(
        (
            ParquetColumn("flag", "BOOLEAN"),
            ParquetColumn("text", "UTF8"),
        ),
        [[True, "hi"], [False, ""], [True, "x"]],
    )

    # Booleans: bits 0 and 2 set in a single byte.
    assert bytes([0b101]) in encoded
    # BYTE_ARRAY: 4-byte little-endian length prefix per value.
    assert struct.pack("<I", 2) + b"hi" in encoded
    assert struct.pack("<I", 0) + struct.pack("<I", 1) + b"x" in encoded


def test_empty_row_set_still_produces_a_schema_only_file() -> None:
    encoded = write_parquet(ALL_TYPE_COLUMNS, [])

    assert encoded.startswith(PARQUET_MAGIC)
    assert encoded.endswith(PARQUET_MAGIC)
    for column in ALL_TYPE_COLUMNS:
        assert column.name.encode("utf-8") in encoded


@pytest.mark.parametrize(
    ("column", "value"),
    [
        (ParquetColumn("text", "UTF8"), 1),
        (ParquetColumn("small", "INT32"), "1"),
        (ParquetColumn("small", "INT32"), True),
        (ParquetColumn("small", "INT32"), 2**31),
        (ParquetColumn("large", "INT64"), 2**63),
        (ParquetColumn("number", "DOUBLE"), float("nan")),
        (ParquetColumn("number", "DOUBLE"), "1.0"),
        (ParquetColumn("flag", "BOOLEAN"), 1),
    ],
)
def test_values_that_do_not_match_the_column_type_are_rejected(
    column: ParquetColumn,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        write_parquet([column], [[value]])


def test_structural_misuse_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one column"):
        write_parquet([], [])
    with pytest.raises(ValueError, match="unique"):
        write_parquet(
            [ParquetColumn("n", "INT32"), ParquetColumn("n", "INT64")],
            [],
        )
    with pytest.raises(ValueError, match="one value per column"):
        write_parquet([ParquetColumn("n", "INT32")], [[1, 2]])
    with pytest.raises(ValueError, match="unsupported parquet column type"):
        ParquetColumn("n", "INT96")  # pyright: ignore[reportArgumentType]


def test_required_columns_reject_none_instead_of_substituting_a_default() -> None:
    with pytest.raises(ValueError, match="REQUIRED and rejects None"):
        write_parquet([ParquetColumn("n", "INT32")], [[None]])


def test_optional_columns_write_rle_definition_levels_and_omit_null_values() -> None:
    encoded = write_parquet(
        [ParquetColumn("flag", "BOOLEAN", optional=True)],
        [[True], [None], [None], [False]],
    )

    # A v1 data page prefixes its RLE levels with a little-endian byte length.
    # Levels are 1,0,0,1 -> three runs of (length<<1, value) at bit width 1.
    levels = _varint(1 << 1) + b"\x01" + _varint(2 << 1) + b"\x00" + _varint(1 << 1) + b"\x01"
    page = struct.pack("<I", len(levels)) + levels
    assert page in encoded
    # Only the two present booleans are bit-packed: 0b01 -> byte 0x01.
    assert encoded[encoded.index(page) + len(page)] == 0b01


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            encoded.append(chunk | 0x80)
        else:
            encoded.append(chunk)
            return bytes(encoded)


def test_pyarrow_reads_back_nullable_columns_as_nulls() -> None:
    parquet = pytest.importorskip("pyarrow.parquet")

    table = parquet.read_table(io.BytesIO(write_parquet(NULLABLE_COLUMNS, NULLABLE_ROWS)))

    assert [field.nullable for field in table.schema] == [
        False,
        True,
        True,
        True,
        True,
        True,
    ]
    assert table.to_pydict() == {
        "key": ["a", "b", "c", "d"],
        "label": [None, "x", "y", None],
        "small": [1, None, -2, None],
        "large": [10, None, -20, None],
        "number": [1.5, None, -0.25, None],
        "flag": [True, None, False, True],
    }


def test_pyarrow_reads_back_every_supported_type() -> None:
    parquet = pytest.importorskip("pyarrow.parquet")

    table = parquet.read_table(io.BytesIO(write_parquet(ALL_TYPE_COLUMNS, ALL_TYPE_ROWS)))

    assert table.schema.names == [column.name for column in ALL_TYPE_COLUMNS]
    assert all(not field.nullable for field in table.schema)
    assert table.to_pydict() == {
        "text": ["alpha", "é中", ""],
        "small": [1, -2, 0],
        "large": [10, -20, 0],
        "number": [1.5, -0.25, 0.0],
        "flag": [True, False, True],
    }


def test_pyarrow_reads_back_an_empty_file() -> None:
    parquet = pytest.importorskip("pyarrow.parquet")

    table = parquet.read_table(io.BytesIO(write_parquet(ALL_TYPE_COLUMNS, [])))

    assert table.num_rows == 0
    assert table.schema.names == [column.name for column in ALL_TYPE_COLUMNS]
