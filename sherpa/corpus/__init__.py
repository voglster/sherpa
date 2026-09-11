"""Local Slack corpus: ingest, chunking, embedding, and hybrid retrieval."""

from sherpa.corpus.db import EMBED_DIM, open_db

__all__ = ["EMBED_DIM", "open_db"]
