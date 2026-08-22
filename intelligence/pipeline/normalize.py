from __future__ import annotations

from intelligence.schemas.event import NormalizedRecord, RawRecord


def _normalize_hackernews(record: RawRecord) -> NormalizedRecord:
    return NormalizedRecord(
        source_id=record.source_id,
        observed_at=record.fetched_at,
        metric="score",
        value=float(record.payload.get("score", 0)),
        raw_ref=record.content_hash,
    )


def _normalize_alpha_vantage(record: RawRecord) -> NormalizedRecord:
    quote = record.payload.get("Global Quote", {})
    price = quote.get("05. price", "0")
    return NormalizedRecord(
        source_id=record.source_id,
        observed_at=record.fetched_at,
        metric="price",
        value=float(price),
        raw_ref=record.content_hash,
    )


_NORMALIZERS = {
    "forum": _normalize_hackernews,
    "market_data": _normalize_alpha_vantage,
}


def normalize_record(record: RawRecord, source_type: str) -> NormalizedRecord:
    normalizer = _NORMALIZERS.get(source_type)
    if normalizer is None:
        raise ValueError(f"ingen normalizer registrerad för source_type={source_type!r}")
    return normalizer(record)
