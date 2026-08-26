"""The provider-agnostic embedding interface, and a deterministic mock.

THE WHOLE ABSTRACTION IS THE PROTOCOL BELOW. One method, two properties.
Nothing in the application constructs a concrete provider directly — it
asks `get_embedding_provider()` — so a real provider can be added later
without touching a single call site, exactly the way
app/storage/base.py lets an S3 backend replace the local filesystem
without any resume domain code changing.

ASYNC, on purpose. The mock is pure CPU and needs no concurrency, but
every realistic provider is either an HTTP call or a model load, and
both would block the event loop of an app that is async end to end. A
synchronous signature here would have to be broken later, changing every
caller; an async one absorbs the real implementation unchanged.

THERE IS NO REAL PROVIDER IN THIS SLICE and no credential for one. No
API key is read, requested or named anywhere in this module.
"""

import hashlib
import struct
from typing import Protocol

from app.embeddings.hashing import normalize_text

# `to_float32` comes from the vector column's own module: the storage
# precision is a property of the COLUMN, so that is where the fact
# lives, rather than being duplicated here.
from app.embeddings.vector_type import to_float32
from app.settings import Settings, get_settings

# The mock's identifier. It says "mock" first so no reader, log line or
# stored row can mistake it for a real model — the deduplication key
# includes this string, so an accidental swap of a real model's id would
# silently make two incomparable vectors look like the same embedding.
MOCK_MODEL_IDENTIFIER = "mock-deterministic-v1"

MOCK_PROVIDER_NAME = "mock"

# One SHA-256 block is 32 bytes = 8 uint32 = 8 vector components.
_COMPONENTS_PER_BLOCK = 8
_UINT32_SCALE = float(1 << 32)


class EmbeddingProvider(Protocol):
    """What the application depends on instead of a concrete provider."""

    @property
    def model_identifier(self) -> str:
        """Stored with every vector this provider produces, and part of
        the deduplication identity — see app/models/embedding.py."""
        ...

    @property
    def dimension(self) -> int:
        """Length of the vectors this provider returns."""
        ...

    async def embed_text(self, text: str) -> list[float]:
        """Embed one piece of text."""
        ...


class MockEmbeddingProvider:
    """A deterministic fake, for tests and local development.

    NOT RANDOM AND NOT `hash()`-BASED. The vector is derived from
    SHA-256 over the model identifier and the normalized text, so the
    same input yields byte-identical output in a different process, a
    different run and on a different machine. A random provider would
    make every dedup and change-detection test unfalsifiable, and a
    `hash()`-based one would change per process (see
    app/embeddings/hashing.py).

    It is a fake, not a model: the vectors carry no semantic structure
    whatsoever, so two texts about the same subject are no closer than
    two unrelated ones. That is the honest thing for a mock to be, and
    it is also why this slice must not compute similarity — there is
    nothing here for a similarity number to mean.

    HOW IT WORKS. A seed digest is taken over (model identifier, text).
    Successive counter blocks are hashed from that seed, each yielding
    eight uint32s, each mapped into [-1, 1). The result is L2-normalized
    so vectors have unit length like a real model's, then ROUNDED TO
    float32 — pgvector's column is `real`, so returning float64 values
    would mean what comes back out of the database differs in the low
    bits from what went in. Rounding here makes storage lossless and
    lets tests assert exact equality instead of an epsilon.
    """

    def __init__(
        self,
        *,
        dimension: int,
        model_identifier: str = MOCK_MODEL_IDENTIFIER,
    ) -> None:
        if dimension <= 0:
            raise ValueError(f"embedding dimension must be positive, got {dimension}")
        self._dimension = dimension
        self._model_identifier = model_identifier

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_text(self, text: str) -> list[float]:
        return self.embed_text_sync(text)

    def embed_text_sync(self, text: str) -> list[float]:
        """The mock's actual work, exposed synchronously as well.

        Kept public because the pure hashing/determinism tests have no
        reason to spin up an event loop to check an arithmetic property.
        `embed_text` is the interface; this is an implementation detail
        of the fake that happens to be convenient.
        """
        seed = hashlib.sha256(
            self._model_identifier.encode("utf-8") + b"\x00" + normalize_text(text).encode("utf-8")
        ).digest()

        components: list[float] = []
        counter = 0
        while len(components) < self._dimension:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for value in struct.unpack(">8I", block):
                components.append(value / _UINT32_SCALE * 2.0 - 1.0)
            counter += 1
        components = components[: self._dimension]

        norm = sum(component * component for component in components) ** 0.5
        # A zero norm is unreachable for a SHA-256-derived vector, but
        # dividing by it would be a silent NaN rather than a loud error.
        scale = 1.0 / norm if norm else 1.0
        return [to_float32(component * scale) for component in components]


def get_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """Build the configured provider.

    Raises on an unknown name rather than falling back to the mock: a
    typo in `EMBEDDING_PROVIDER` that silently produced fake vectors
    would be discovered as bad search results much later, and would have
    written a table full of rows labelled with the wrong model.
    """
    settings = settings or get_settings()
    if settings.embedding_provider == MOCK_PROVIDER_NAME:
        return MockEmbeddingProvider(
            dimension=settings.embedding_dimension,
            model_identifier=settings.embedding_model_identifier,
        )
    raise ValueError(
        f"unknown embedding provider {settings.embedding_provider!r} "
        f"(known providers: {MOCK_PROVIDER_NAME})"
    )
