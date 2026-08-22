import pytest
from pydantic import ValidationError

from intelligence.schemas.source import Source


def test_source_valid():
    s = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://news.ycombinator.com")
    assert s.reliability_score == 0.6


def test_reliability_score_must_be_0_to_1():
    with pytest.raises(ValidationError):
        Source(source_id="hn", name="Hacker News", type="forum", reliability_score=1.5, url="https://x.com")
