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

TWO IMPLEMENTATIONS, ONE PROTOCOL. `MockEmbeddingProvider` is
deterministic-by-construction and carries no meaning — it exists so unit
tests can assert exact vectors without a model. `LocalSentenceEmbedding
Provider` (Prompt 5.2b) runs a real 384-dimension sentence model
locally. Neither reads a credential: there is no hosted API and no API
key named anywhere in this module.
"""

import asyncio
import hashlib
import struct
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: no cover - import cost is paid lazily at runtime
    from fastembed import TextEmbedding

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

LOCAL_PROVIDER_NAME = "local"

# The real model. 384 dimensions — the width `vector(384)` was sized for
# in Prompt 5.1, chosen then precisely because this was the most likely
# first real provider, so adopting it needs no migration.
LOCAL_MODEL_IDENTIFIER = "sentence-transformers/all-MiniLM-L6-v2"

# The model's fixed output width. Not configurable: it is a property of
# the weights, and it must equal app/models/embedding.py's
# EMBEDDING_DIMENSION or the store refuses to write.
_LOCAL_DIMENSION = 384

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


@lru_cache(maxsize=2)
def _load_model(model_name: str) -> "TextEmbedding":
    """The loaded ONNX model, once per process per model name.

    Cached because loading is the expensive part: measured at ~25s the
    first time (it downloads ~90 MB of weights) and ~0.17s from the
    on-disk cache afterwards, against ~3ms to embed one text. Loading per
    request would make every request pay that, so the instance is held
    here for the life of the process.

    Imported inside the function, not at module scope: `fastembed` pulls
    in onnxruntime and numpy, and nothing that only uses the mock — the
    whole unit-test suite included — should pay that import cost.
    """
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name)


class LocalSentenceEmbeddingProvider:
    """A real sentence-embedding model, running locally.

    NO HOSTED API AND NO CREDENTIAL. Inference happens in this process
    via onnxruntime. The only network access is a one-time model-weight
    download on first use, cached on disk by `fastembed` thereafter.

    Output is L2-normalised and rounded to float32 for exactly the
    reasons app/embeddings/vector_type.py documents: `vector` stores
    `real`, so a float64 would not survive the round trip unchanged, and
    unit-length vectors make cosine distance and dot product agree.

    Deterministic in practice: onnxruntime inference over the same input
    on the same build produced bit-identical output when measured. That
    is a property of the runtime rather than a guarantee this class can
    make, so tests assert repeatability WITHIN a process and do not
    hard-code expected vectors.
    """

    def __init__(self, *, model_identifier: str = LOCAL_MODEL_IDENTIFIER) -> None:
        self._model_identifier = model_identifier

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def dimension(self) -> int:
        return _LOCAL_DIMENSION

    async def embed_text(self, text: str) -> list[float]:
        """Embed off the event loop.

        The work is CPU-bound and synchronous, and the first call also
        pays the model load, so running it inline would stall every other
        request in an app that is async end to end.
        """
        return await asyncio.to_thread(self.embed_text_sync, text)

    def embed_text_sync(self, text: str) -> list[float]:
        model = _load_model(self._model_identifier)
        vector = [float(component) for component in next(iter(model.embed([text])))]
        norm = sum(component * component for component in vector) ** 0.5
        scale = 1.0 / norm if norm else 1.0
        return [to_float32(component * scale) for component in vector]


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
    if settings.embedding_provider == LOCAL_PROVIDER_NAME:
        return LocalSentenceEmbeddingProvider(model_identifier=settings.embedding_local_model)
    raise ValueError(
        f"unknown embedding provider {settings.embedding_provider!r} "
        f"(known providers: {MOCK_PROVIDER_NAME}, {LOCAL_PROVIDER_NAME})"
    )
