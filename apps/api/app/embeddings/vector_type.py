"""A minimal SQLAlchemy column type for pgvector's `vector`.

WHY THIS EXISTS RATHER THAN THE `pgvector` PYTHON PACKAGE. That package
is the reference client, and it is what this module should be replaced
by the moment CareerLens actually runs a similarity query — its real
value is the distance operators (`<->`, `<=>`, `<#>`). This slice is
forbidden from computing similarity or querying nearest neighbours, so
adopting a dependency for an API we may not call would buy nothing today
(CLAUDE.md rule 6). What IS needed is the narrow part below: DDL that
emits `vector(n)`, and a value conversion asyncpg accepts. Swapping to
the package later is an import change plus a no-op migration — the
column type on the database side is identical either way.

HOW THE VALUE CROSSES THE DRIVER. asyncpg has no codec for `vector`
(it is an extension type, not a builtin), so the value travels in
pgvector's own text form, `[0.1,0.2,...]`:

    write -> `bind_processor` renders the list as that text, and
             `bind_expression` wraps the parameter in a CAST so
             PostgreSQL parses it back into a real `vector`
    read  -> asyncpg hands back the same text form, and
             `result_processor` parses it into a list[float]

PRECISION. pgvector stores `real` (float4), so a float64 the application
computed is NOT guaranteed to come back bit-identical. That is a
property of the column type, not of this conversion, and it is why
app/embeddings/provider.py rounds its output to float32 before returning
it — see the note there.
"""

import struct
from collections.abc import Callable
from typing import Any

from sqlalchemy import Dialect, cast
from sqlalchemy.sql.elements import BindParameter, ColumnElement
from sqlalchemy.types import UserDefinedType


def to_float32(value: float) -> float:
    """Round a float64 to the nearest float32, returned as a Python float.

    This is the precision a `vector` column can actually hold, so it is
    the precision every value entering or leaving one is expressed in.
    Lives here because it is a fact about the COLUMN; app/embeddings/
    provider.py imports it so a freshly computed vector compares equal
    to a stored one instead of differing in the low bits.
    """
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


class Vector(UserDefinedType[list[float]]):
    """A fixed-width pgvector column, e.g. `Vector(384)` -> `vector(384)`.

    `cache_ok = True` because two instances with the same `dim` are
    interchangeable for statement-caching purposes; `dim` is the only
    state, and it is part of the compiled SQL.
    """

    cache_ok = True

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError(f"vector dimension must be positive, got {dim}")
        self.dim = dim

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dim})"

    def bind_processor(self, dialect: Dialect) -> Callable[[list[float] | None], str | None]:
        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            # `repr` rather than `str` so no precision is lost on the way
            # out; what float4 does to the value afterwards is the
            # column's business, not this function's.
            return "[" + ",".join(repr(float(component)) for component in value) + "]"

        return process

    def bind_expression(self, bindvalue: BindParameter[list[float]]) -> ColumnElement[list[float]]:
        """Wrap the parameter in `CAST(? AS vector(n))`.

        Without this the driver sends a bare string and PostgreSQL has
        no context to coerce it from — the insert fails on a type
        mismatch rather than on anything the caller did wrong.
        """
        return cast(bindvalue, self)

    def result_processor(
        self, dialect: Dialect, coltype: object
    ) -> Callable[[Any], list[float] | None]:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            if isinstance(value, list):
                return [to_float32(float(component)) for component in value]
            inner = str(value).strip().lstrip("[").rstrip("]")
            if not inner:
                return []
            # Re-rounded to float32, not just parsed. pgvector prints the
            # SHORTEST text that round-trips to the same float4, so
            # float("-0.049519576") is a float64 that is close to, but
            # not equal to, the float32 the column stored. Widening it
            # back through float32 recovers the value exactly.
            return [to_float32(float(component)) for component in inner.split(",")]

        return process
