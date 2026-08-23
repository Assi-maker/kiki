from datetime import UTC, datetime
from pathlib import Path

import pytest

from intelligence.schemas.assessments import QAAssessment
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "test.db")


def test_save_and_dedupe_by_content_hash(repo):
    source = Source(
        source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://x.com"
    )
    repo.save_source(source)
    event = Event(
        event_id="evt-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="trend",
        metric="score",
        baseline=10.0,
        deviation=30.0,
        description="d",
        raw_ref="hash-123",
    )
    assert repo.has_seen_content_hash("hn", "hash-123") is False
    repo.save_event(event)
    assert repo.has_seen_content_hash("hn", "hash-123") is True


def test_save_and_get_opportunity_roundtrip(repo):
    opp = Opportunity(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
    )
    repo.save_opportunity(opp)
    fetched = repo.get_opportunity("opp-1")
    assert fetched is not None
    assert fetched.opportunity_id == "opp-1"
    assert fetched.status == "candidate"


def test_update_status_persists(repo):
    opp = Opportunity(
        opportunity_id="opp-2",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
    )
    repo.save_opportunity(opp)
    repo.update_opportunity_status("opp-2", "rejected")
    fetched = repo.get_opportunity("opp-2")
    assert fetched.status == "rejected"


def test_save_assessment_attaches_to_opportunity(repo):
    opp = Opportunity(
        opportunity_id="opp-3",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
    )
    repo.save_opportunity(opp)
    qa = QAAssessment(
        agent_name="qa-agent",
        run_id="r1",
        created_at=datetime.now(UTC),
        status="ok",
        passed=True,
        violations=[],
    )
    repo.save_assessment("opp-3", "qa", qa)
    fetched = repo.get_opportunity("opp-3")
    assert fetched.qa is not None
    assert fetched.qa.passed is True


def test_log_run_event_does_not_raise(repo):
    repo.log_run_event(
        run_id="r1",
        event_id="evt-1",
        opportunity_id=None,
        agent_name="orchestrator",
        status="started",
    )
