"""Dependency-free Parquet serializer: PLAIN encoding, uncompressed, one row group.

Pyodide ships no pyarrow and a Python Worker cannot import npm packages, so the
episode dataset is serialized here by hand. The output is deliberately the
simplest legal Parquet file: one flat row group, one v1 DATA_PAGE per column,
PLAIN values. Columns may be REQUIRED or OPTIONAL; an OPTIONAL column carries
RLE-encoded definition levels (max level 1) ahead of its values, and null values
are omitted from the value section entirely rather than substituted.

The only fiddly part is the Thrift compact encoding of the footer, whose field
ids are quoted from ``parquet.thrift`` inline below.

Layout::

    PAR1
    <page header><[def levels]><page data>  x one per column
    <FileMetaData>
    <uint32 little-endian FileMetaData length>
    PAR1
"""

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

PARQUET_MAGIC = b"PAR1"
CREATED_BY = "retro-backend"

ParquetType = Literal["UTF8", "INT32", "INT64", "DOUBLE", "BOOLEAN"]

# parquet.thrift `Type`
_PHYSICAL_TYPE: dict[ParquetType, int] = {
    "BOOLEAN": 0,
    "INT32": 1,
    "INT64": 2,
    "DOUBLE": 5,
    "UTF8": 6,  # BYTE_ARRAY
}
_CONVERTED_TYPE_UTF8 = 0  # parquet.thrift `ConvertedType.UTF8`
_REPETITION_REQUIRED = 0  # parquet.thrift `FieldRepetitionType.REQUIRED`
_REPETITION_OPTIONAL = 1  # parquet.thrift `FieldRepetitionType.OPTIONAL`
_ENCODING_PLAIN = 0  # parquet.thrift `Encoding.PLAIN`
_ENCODING_RLE = 3  # parquet.thrift `Encoding.RLE`
_CODEC_UNCOMPRESSED = 0  # parquet.thrift `CompressionCodec.UNCOMPRESSED`
_PAGE_TYPE_DATA_PAGE = 0  # parquet.thrift `PageType.DATA_PAGE`
_FORMAT_VERSION = 1

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# Thrift compact protocol element types.
_CT_TRUE = 1
_CT_FALSE = 2
_CT_I32 = 5
_CT_I64 = 6
_CT_BINARY = 8
_CT_LIST = 9
_CT_STRUCT = 12


@dataclass(frozen=True, slots=True)
class ParquetColumn:
    """One leaf column of the flat schema.

    ``optional`` maps to ``FieldRepetitionType.OPTIONAL``; such a column accepts
    ``None`` and is written with definition levels.
    """

    name: str
    type: ParquetType
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("parquet column name must not be empty")
        if self.type not in _PHYSICAL_TYPE:
            raise ValueError(f"unsupported parquet column type: {self.type}")


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


class _ThriftCompactWriter:
    """Just enough Thrift compact protocol to emit the Parquet footer."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._field_id = 0
        self._field_id_stack: list[int] = []

    def to_bytes(self) -> bytes:
        return bytes(self._buffer)

    def write_zigzag(self, value: int) -> None:
        self._buffer += _varint((value << 1) ^ (value >> 63))

    def write_binary(self, value: bytes) -> None:
        self._buffer += _varint(len(value))
        self._buffer += value

    def field(self, field_id: int, compact_type: int) -> None:
        delta = field_id - self._field_id
        if 0 < delta <= 15:
            self._buffer.append((delta << 4) | compact_type)
        else:
            self._buffer.append(compact_type)
            self.write_zigzag(field_id)
        self._field_id = field_id

    def begin_struct(self) -> None:
        self._field_id_stack.append(self._field_id)
        self._field_id = 0

    def end_struct(self) -> None:
        self._buffer.append(0)  # STOP
        self._field_id = self._field_id_stack.pop()

    def list_header(self, element_type: int, size: int) -> None:
        if size <= 14:
            self._buffer.append((size << 4) | element_type)
        else:
            self._buffer.append(0xF0 | element_type)
            self._buffer += _varint(size)

    def i32(self, field_id: int, value: int) -> None:
        self.field(field_id, _CT_I32)
        self.write_zigzag(value)

    def i64(self, field_id: int, value: int) -> None:
        self.field(field_id, _CT_I64)
        self.write_zigzag(value)

    def string(self, field_id: int, value: str) -> None:
        self.field(field_id, _CT_BINARY)
        self.write_binary(value.encode("utf-8"))

    def boolean(self, field_id: int, value: bool) -> None:
        # A compact-protocol bool lives entirely in the field header's type nibble.
        self.field(field_id, _CT_TRUE if value else _CT_FALSE)


def _require_str(value: object, column: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"column {column} requires a str value")
    return value


def _require_bool(value: object, column: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"column {column} requires a bool value")
    return value


def _require_int(value: object, column: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"column {column} requires an int value")
    if not low <= value <= high:
        raise ValueError(f"column {column} value is out of range")
    return value


def _require_double(value: object, column: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"column {column} requires a numeric value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"column {column} requires a finite value")
    return number


def _rle_levels(levels: Sequence[int], bit_width: int) -> bytes:
    """RLE/bit-packing hybrid, emitting one RLE run per stretch of equal levels."""
    value_bytes = (bit_width + 7) // 8
    encoded = bytearray()
    index = 0
    while index < len(levels):
        value = levels[index]
        run = 1
        while index + run < len(levels) and levels[index + run] == value:
            run += 1
        encoded += _varint(run << 1)  # low bit 0 marks an RLE run
        encoded += value.to_bytes(value_bytes, "little")
        index += run
    return bytes(encoded)


def _encode_plain(column: ParquetColumn, values: Sequence[object]) -> bytes:
    """PLAIN-encode one column's non-null values (spec: Encodings.md)."""
    if column.type == "BOOLEAN":
        # Bit-packed, one bit per value, least significant bit first.
        packed = bytearray((len(values) + 7) // 8)
        for index, value in enumerate(values):
            if _require_bool(value, column.name):
                packed[index // 8] |= 1 << (index % 8)
        return bytes(packed)

    buffer = bytearray()
    for value in values:
        if column.type == "UTF8":
            encoded = _require_str(value, column.name).encode("utf-8")
            buffer += struct.pack("<I", len(encoded))
            buffer += encoded
        elif column.type == "INT32":
            buffer += struct.pack(
                "<i", _require_int(value, column.name, _INT32_MIN, _INT32_MAX)
            )
        elif column.type == "INT64":
            buffer += struct.pack(
                "<q", _require_int(value, column.name, _INT64_MIN, _INT64_MAX)
            )
        else:
            buffer += struct.pack("<d", _require_double(value, column.name))
    return bytes(buffer)


def _encode_page_data(column: ParquetColumn, values: Sequence[object]) -> bytes:
    if not column.optional:
        if any(value is None for value in values):
            raise ValueError(f"column {column.name} is REQUIRED and rejects None")
        return _encode_plain(column, values)

    # OPTIONAL leaf: max definition level 1, so one bit per level. A v1 data page
    # prefixes RLE levels with their little-endian byte length.
    levels = [0 if value is None else 1 for value in values]
    encoded_levels = _rle_levels(levels, 1)
    present = [value for value in values if value is not None]
    return (
        struct.pack("<I", len(encoded_levels))
        + encoded_levels
        + _encode_plain(column, present)
    )


def _data_page_header(num_values: int, page_size: int) -> bytes:
    writer = _ThriftCompactWriter()
    writer.begin_struct()  # PageHeader
    writer.i32(1, _PAGE_TYPE_DATA_PAGE)  # 1: type
    writer.i32(2, page_size)  # 2: uncompressed_page_size
    writer.i32(3, page_size)  # 3: compressed_page_size
    writer.field(5, _CT_STRUCT)  # 5: data_page_header
    writer.begin_struct()  # DataPageHeader
    writer.i32(1, num_values)  # 1: num_values (nulls included)
    writer.i32(2, _ENCODING_PLAIN)  # 2: encoding
    # 3/4 are required fields; a flat schema writes no repetition levels, and a
    # REQUIRED column writes no definition levels either.
    writer.i32(3, _ENCODING_RLE)  # 3: definition_level_encoding
    writer.i32(4, _ENCODING_RLE)  # 4: repetition_level_encoding
    writer.end_struct()
    writer.end_struct()
    return writer.to_bytes()


@dataclass(frozen=True, slots=True)
class _ChunkLayout:
    column: ParquetColumn
    offset: int
    size: int
    num_values: int


def _write_schema(writer: _ThriftCompactWriter, columns: Sequence[ParquetColumn]) -> None:
    """FileMetaData field 2: the flat schema, root element first."""
    writer.field(2, _CT_LIST)
    writer.list_header(_CT_STRUCT, len(columns) + 1)

    writer.begin_struct()  # SchemaElement (root)
    writer.string(4, "schema")  # 4: name
    writer.i32(5, len(columns))  # 5: num_children
    writer.end_struct()

    for column in columns:
        writer.begin_struct()  # SchemaElement (leaf)
        writer.i32(1, _PHYSICAL_TYPE[column.type])  # 1: type
        writer.i32(  # 3: repetition_type
            3,
            _REPETITION_OPTIONAL if column.optional else _REPETITION_REQUIRED,
        )
        writer.string(4, column.name)  # 4: name
        if column.type == "UTF8":
            writer.i32(6, _CONVERTED_TYPE_UTF8)  # 6: converted_type
        writer.end_struct()


def _write_column_chunk(writer: _ThriftCompactWriter, chunk: _ChunkLayout) -> None:
    writer.begin_struct()  # ColumnChunk
    writer.i64(2, chunk.offset)  # 2: file_offset
    writer.field(3, _CT_STRUCT)  # 3: meta_data
    writer.begin_struct()  # ColumnMetaData
    writer.i32(1, _PHYSICAL_TYPE[chunk.column.type])  # 1: type
    writer.field(2, _CT_LIST)  # 2: encodings
    writer.list_header(_CT_I32, 2 if chunk.column.optional else 1)
    writer.write_zigzag(_ENCODING_PLAIN)
    if chunk.column.optional:
        writer.write_zigzag(_ENCODING_RLE)
    writer.field(3, _CT_LIST)  # 3: path_in_schema
    writer.list_header(_CT_BINARY, 1)
    writer.write_binary(chunk.column.name.encode("utf-8"))
    writer.i32(4, _CODEC_UNCOMPRESSED)  # 4: codec
    writer.i64(5, chunk.num_values)  # 5: num_values
    writer.i64(6, chunk.size)  # 6: total_uncompressed_size
    writer.i64(7, chunk.size)  # 7: total_compressed_size
    writer.i64(9, chunk.offset)  # 9: data_page_offset
    writer.end_struct()
    writer.end_struct()


def _file_metadata(
    columns: Sequence[ParquetColumn],
    chunks: Sequence[_ChunkLayout],
    num_rows: int,
) -> bytes:
    writer = _ThriftCompactWriter()
    writer.begin_struct()  # FileMetaData
    writer.i32(1, _FORMAT_VERSION)  # 1: version
    _write_schema(writer, columns)  # 2: schema
    writer.i64(3, num_rows)  # 3: num_rows

    writer.field(4, _CT_LIST)  # 4: row_groups
    writer.list_header(_CT_STRUCT, 1 if chunks else 0)
    if chunks:
        writer.begin_struct()  # RowGroup
        writer.field(1, _CT_LIST)  # 1: columns
        writer.list_header(_CT_STRUCT, len(chunks))
        for chunk in chunks:
            _write_column_chunk(writer, chunk)
        writer.i64(2, sum(chunk.size for chunk in chunks))  # 2: total_byte_size
        writer.i64(3, num_rows)  # 3: num_rows
        writer.end_struct()

    writer.string(6, CREATED_BY)  # 6: created_by
    writer.end_struct()
    return writer.to_bytes()


def write_parquet(
    columns: Sequence[ParquetColumn],
    rows: Sequence[Sequence[object]],
) -> bytes:
    """Serialize ``rows`` (values in ``columns`` order) into one Parquet file."""
    if not columns:
        raise ValueError("parquet file requires at least one column")
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise ValueError("parquet column names must be unique")
    for row in rows:
        if len(row) != len(columns):
            raise ValueError("every parquet row must supply one value per column")

    body = bytearray(PARQUET_MAGIC)
    chunks: list[_ChunkLayout] = []
    if rows:
        for index, column in enumerate(columns):
            data = _encode_page_data(column, [row[index] for row in rows])
            header = _data_page_header(len(rows), len(data))
            offset = len(body)
            body += header
            body += data
            chunks.append(
                _ChunkLayout(
                    column=column,
                    offset=offset,
                    size=len(header) + len(data),
                    num_values=len(rows),
                )
            )

    metadata = _file_metadata(columns, chunks, len(rows))
    body += metadata
    body += struct.pack("<I", len(metadata))
    body += PARQUET_MAGIC
    return bytes(body)


__all__ = ["CREATED_BY", "PARQUET_MAGIC", "ParquetColumn", "ParquetType", "write_parquet"]
