"""Milvus vector channel (README 13.3, 13.4).

The vector store is optional: when pymilvus is missing, the URI is not
configured or the server is unreachable, callers fall back to the BM25-only
path and must mark the result degraded. Access control and document state are
never decided here - candidate ids always go back through PostgreSQL.
"""

import logging

from app.core.config import Settings

log = logging.getLogger("devflow.vector")

CONNECT_TIMEOUT_SECONDS = 5
UPSERT_BATCH = 500


class VectorUnavailable(Exception):
    """The vector channel cannot serve this request; callers degrade."""


class MilvusVectorStore:
    def __init__(self, settings: Settings):
        if not settings.vector_configured:
            raise VectorUnavailable("vector channel not configured")
        try:
            from pymilvus import (  # noqa: PLC0415 - optional dependency by design
                Collection,
                CollectionSchema,
                DataType,
                FieldSchema,
                connections,
                utility,
            )
        except ImportError:
            raise VectorUnavailable("pymilvus is not installed (uv sync --group knowledge)") from None
        self._Collection = Collection
        self._utility = utility
        try:
            connections.connect(alias="devflow", uri=settings.milvus_uri, timeout=CONNECT_TIMEOUT_SECONDS)
        except Exception as error:  # pymilvus raises various connection errors
            raise VectorUnavailable(f"milvus unreachable: {type(error).__name__}") from None
        self.settings = settings
        self.name = f"devflow_chunks_{settings.embedding_dimension}"
        self._ensure_collection(Collection, CollectionSchema, DataType, FieldSchema, utility)

    def _ensure_collection(self, Collection, CollectionSchema, DataType, FieldSchema, utility):
        if utility.has_collection(self.name, using="devflow"):
            self.collection = Collection(self.name, using="devflow")
            return
        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="repo_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="generation_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="document_version_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(
                name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension
            ),
        ]
        schema = CollectionSchema(fields, description="DevFlow knowledge chunks")
        collection = Collection(self.name, schema=schema, using="devflow")
        collection.create_index(
            field_name="vector",
            index_params={"index_type": "AUTOINDEX", "metric_type": "COSINE"},
        )
        self.collection = collection

    def upsert(self, rows: list[dict]) -> int:
        written = 0
        for start in range(0, len(rows), UPSERT_BATCH):
            batch = rows[start : start + UPSERT_BATCH]
            self.collection.upsert(
                [
                    [row["chunk_id"] for row in batch],
                    [row["repo_id"] for row in batch],
                    [row["generation_id"] for row in batch],
                    [row["document_version_id"] for row in batch],
                    [row["vector"] for row in batch],
                ]
            )
            written += len(batch)
        if written:
            self.collection.flush()
        return written

    def search(self, vector: list[float], *, generation_id: str, k: int) -> list[tuple[str, float]]:
        self.collection.load()
        results = self.collection.search(
            data=[vector],
            anns_field="vector",
            param={"metric_type": "COSINE"},
            limit=k,
            expr=f'generation_id == "{generation_id}"',
            output_fields=["chunk_id"],
        )
        hits: list[tuple[str, float]] = []
        for hit in results[0]:
            hits.append((str(hit.entity.get("chunk_id")), float(hit.score)))
        return hits

    def delete_generation(self, generation_id: str) -> None:
        self.collection.load()
        self.collection.delete(expr=f'generation_id == "{generation_id}"')
