"""Reuse vectors within a repository across index generations.

Keys include the endpoint, model, dimension and exact text hash. Only vectors
for the current source set survive a successful index activation.
"""

import hashlib
import json
import logging
import math
import os
from uuid import uuid4

log = logging.getLogger("devflow.embeddings")


def _valid_vector(value, dimension):
    return isinstance(value, list) and len(value) == dimension and all(
        isinstance(number, (float, int)) and not isinstance(number, bool) and math.isfinite(number)
        for number in value
    )


class EmbeddingCache:
    def __init__(self, settings, repo_id):
        self.dimension = settings.embedding_dimension
        identity = [settings.embedding_base_url, settings.embedding_model, self.dimension]
        self.config_hash = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.path = settings.storage_root / "indexes" / str(repo_id) / "embedding-cache.json"
        self.vectors = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("config_hash") == self.config_hash:
                self.vectors = {
                    key: vector for key, vector in payload["vectors"].items()
                    if _valid_vector(vector, self.dimension)
                }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Cache damage must not prevent a fresh, validated embedding request.
            self.vectors = {}

    def embed(self, texts, embedder):
        keys = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
        missing = {key: text for key, text in zip(keys, texts, strict=True) if key not in self.vectors}
        if missing:
            fresh = embedder.embed(list(missing.values()))
            if len(fresh) != len(missing) or not all(_valid_vector(v, self.dimension) for v in fresh):
                raise ValueError("Embedding response count, dimension or numeric values are invalid")
            self.vectors.update(zip(missing, fresh, strict=True))
        self.vectors = {key: self.vectors[key] for key in keys}
        return [self.vectors[key] for key in keys], len(keys) - len(missing), len(missing)

    def save(self):
        temporary = self.path.with_name(f".{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"config_hash": self.config_hash, "vectors": self.vectors}), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.warning("embedding_cache_write_failed")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.warning("embedding_cache_cleanup_failed")
