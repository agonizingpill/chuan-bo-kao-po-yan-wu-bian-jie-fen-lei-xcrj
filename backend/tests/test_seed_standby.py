"""The demo shift must reproduce the competing-standby story end to end."""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import scripts.seed_demo as seed_demo
from src import standby
from src.models import (
    Base, DelayIncident, DispatchReview, IncidentState, StandbyBatch, StandbyItemState, StandbyPlanItem,
)


@pytest.fixture()
def seeded(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(seed_demo, 'Session', factory)
    counts = seed_demo.seed()
    session = factory()
    try:
        yield counts, session
    finally:
        session.close()
        engine.dispose()


def test_seed_creates_three_competing_priority_cases(seeded):
    counts, db = seeded
    cases = {row.code: row for row in db.scalars(
        select(DelayIncident).where(DelayIncident.code.in_(['DL-414', 'DL-415', 'DL-416'])))}
    assert set(cases) == {'DL-414', 'DL-415', 'DL-416'}
    for case in cases.values():
        assert case.state == IncidentState.READY
        assert case.decision == 'priority'
        assert case.result['prediction'] == 'priority'
        assert case.observed_at == datetime(2026, 11, 21, 11, 25)
    assert counts['delay_incidents'] >= 4


def test_seeded_competition_ranks_two_and_blocks_one(seeded):
    _, db = seeded
    batch = standby.create_batch(db, 'supervisor', 'SB-1121-1', '早班三船争用待命拖轮')
    db.flush()
    plan = list(db.scalars(select(StandbyPlanItem).where(
        StandbyPlanItem.batch_code == 'SB-1121-1').order_by(StandbyPlanItem.rank)))
    assert [item.incident_code for item in plan] == ['DL-415', 'DL-414', 'DL-416']
    assert [item.state for item in plan] == [
        StandbyItemState.SCHEDULED, StandbyItemState.SCHEDULED, StandbyItemState.BLOCKED]
    assert plan[2].tug_code is None
    assert 'BR-417' in plan[2].block_reason   # TG23 held by the other confirmed vessel
    assert '本批' in plan[2].block_reason      # TG12/TG18 already assigned in this batch


def test_seeded_batch_publishes_only_placed_cases(seeded):
    _, db = seeded
    standby.create_batch(db, 'supervisor', 'SB-1121-1', '早班三船争用待命拖轮')
    db.flush()
    standby.publish(db, 'supervisor', 'SB-1121-1')
    db.flush()
    assert {review.incident_code for review in db.scalars(select(DispatchReview))} == {'DL-414', 'DL-415'}
    assert db.get(DelayIncident, 'DL-416').state == IncidentState.READY
    assert db.get(StandbyBatch, 'SB-1121-1').state == 'published'
