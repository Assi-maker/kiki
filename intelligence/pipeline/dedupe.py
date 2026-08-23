from __future__ import annotations

from intelligence.schemas.event import RawRecord
from intelligence.storage.repository import Repository


def is_duplicate(repo: Repository, record: RawRecord) -> bool:
    return repo.has_seen_content_hash(record.source_id, record.content_hash)
