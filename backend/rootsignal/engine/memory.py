"""Incident memory: confirmed root causes, found again by symptom similarity (pgvector).

Only incidents a human has confirmed get stored. That keeps the agent from learning
from its own mistakes.

The default embedder is feature hashing: local, free, deterministic, no API calls. It
matches on shared symptom vocabulary ("db_pool inventory timed out acquiring database
connection"), which works well for recurring incidents. A semantic embedding model can
replace it behind the same `Embedder` interface.
"""

import hashlib
import math
import re
from collections.abc import Iterable
from typing import Protocol

from sqlalchemy import Float, bindparam, select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal.db.models import EMBEDDING_DIM, IncidentMemoryRow
from rootsignal.engine.checks import Observation

MIN_SIMILARITY = 0.35


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Bag of words + bigrams, hashed into `dim` signed buckets, L2-normalized."""

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def _features(self, text: str) -> Iterable[str]:
        words = re.findall(r"[a-z][a-z_]+", text.lower())
        yield from words
        yield from (f"{a} {b}" for a, b in zip(words, words[1:], strict=False))

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature in self._features(text):
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            vec[0], norm = 1.0, 1.0  # avoid a zero vector (cosine undefined)
        return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def symptom_signature(title: str, service: str, triage: list[Observation]) -> str:
    """Text describing what was observed. Numbers are dropped so it generalizes."""
    parts = [title, service]
    for o in triage:
        if o.available and o.anomalous:
            parts.append(f"{o.check.value} {o.service}")
            parts.append(re.sub(r"[\d.:%]+", " ", o.summary))
    return " ".join(parts)


def _to_dict(row: IncidentMemoryRow, similarity: float) -> dict:
    return {
        "id": row.id,
        "investigation_id": row.investigation_id,
        "title": row.title,
        "service": row.service,
        "category": row.category,
        "root_cause": row.root_cause,
        "resolution": row.resolution,
        "similarity": round(similarity, 3),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


class MemoryStore:
    def __init__(self, embedder: Embedder | None = None):
        self.embedder = embedder or HashingEmbedder()

    async def add(
        self,
        session: AsyncSession,
        *,
        title: str,
        service: str,
        category: str,
        root_cause: str,
        signature: str,
        resolution: str = "",
        investigation_id: str | None = None,
    ) -> IncidentMemoryRow:
        row = IncidentMemoryRow(
            investigation_id=investigation_id,
            title=title,
            service=service,
            category=category,
            root_cause=root_cause,
            resolution=resolution,
            signature=signature,
            embedding=self.embedder.embed(signature),
        )
        session.add(row)
        await session.commit()
        return row

    async def search(
        self,
        session: AsyncSession,
        text: str,
        limit: int = 3,
        min_similarity: float = MIN_SIMILARITY,
    ) -> list[dict]:
        query_vec = self.embedder.embed(text)
        if session.get_bind().dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            distance = IncidentMemoryRow.embedding.op("<=>", return_type=Float)(
                bindparam("q", query_vec, type_=Vector(self.embedder.dim))
            )
            rows = (
                await session.execute(
                    select(IncidentMemoryRow, distance.label("d")).order_by(distance).limit(limit)
                )
            ).all()
            scored = [(row, 1 - float(d)) for row, d in rows]
        else:  # SQLite (tests / local dev): brute force in Python
            rows = (await session.scalars(select(IncidentMemoryRow).limit(5000))).all()
            scored = sorted(
                ((r, cosine(query_vec, r.embedding)) for r in rows), key=lambda x: -x[1]
            )[:limit]
        return [_to_dict(r, s) for r, s in scored if s >= min_similarity]
