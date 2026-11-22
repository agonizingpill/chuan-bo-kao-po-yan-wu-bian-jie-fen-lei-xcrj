"""Route-level checks for the whole-batch standby flow (no live server needed)."""

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import routes, standby
from src.models import (
    Base, Berth, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState,
    RequestState, StandbyBatch, StandbyTugHold, TideWindow, Tug, TugAssignment, User,
)
from src.schemas import StandbyBatchInput


def dt(value):
    return datetime.fromisoformat(value)


@pytest.fixture()
def app_db(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(routes, 'Session', factory)
    with factory.begin() as db:
        db.add_all([Berth(code='B07', label='东七'), Berth(code='B09', label='东九')])
        db.add_all([Tug(code=code, label=code, bollard_pull_t=60) for code in ('TG12', 'TG18', 'TG23')])
        db.add_all([
            TideWindow(code='TW-071', berth_code='B07', starts_at=dt('2026-11-21T07:00'),
                       ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
            TideWindow(code='TW-091', berth_code='B09', starts_at=dt('2026-11-21T07:00'),
                       ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
        ])
        for index, (code, berth, window) in enumerate([
            ('BR-1', 'B07', 'TW-071'), ('BR-2', 'B09', 'TW-091'),
            ('BR-3', 'B07', 'TW-071'),
        ]):
            db.add(BerthRequest(code=code, vessel=f'船 {index}', berth_code=berth, window_code=window,
                                eta=dt(f'2026-11-21T09:0{index}'), maneuver_minutes=45, draft_m=10,
                                state=RequestState.CONFIRMED))
            db.add(DelayIncident(
                code=f'DL-{index}', request_code=code, radio_reference=f'VHF-{index}',
                observed_at=dt(f'2026-11-21T09:{index}0'), event_note='现场通报', features={},
                input_digest='0' * 64, state=IncidentState.READY, result={'score': 1.0 + index / 10},
                decision=DelayClass.PRIORITY, decided_by='supervisor',
            ))
    try:
        yield factory
    finally:
        engine.dispose()


USER = User(username='supervisor', role='supervisor', password_hash='x')


def test_publish_returns_and_records_return_over_http(app_db):
    plan = routes.plan_standby(StandbyBatchInput(code='SB-1', note='早班争用待命拖轮'), user=USER)
    assert plan['state'] == 'planned'
    assert [item['incident_code'] for item in plan['items']] == ['DL-2', 'DL-1', 'DL-0']
    target = plan['items'][2]
    with app_db.begin() as db:
        db.add(TugAssignment(request_code='BR-3', tug_code=target['tug_code'],
                             starts_at=dt('2026-11-21T09:00'), ends_at=dt('2026-11-21T09:45')))
    with pytest.raises(HTTPException) as raised:
        routes.publish_standby('SB-1', user=USER)
    assert raised.value.status_code == 409
    assert '抢先占用' in raised.value.detail
    batches = routes.standby_batches(user=USER)
    assert batches[0]['state'] == 'returned'
    with app_db() as db:
        assert db.get(StandbyBatch, 'SB-1').state == 'returned'
        assert db.scalar(select(StandbyTugHold)) is None
        assert db.scalar(select(DispatchReview)) is None
        assert {row.state for row in db.scalars(select(DelayIncident))} == {IncidentState.READY}


def test_watch_exposes_batches_and_publishes_over_http(app_db):
    routes.plan_standby(StandbyBatchInput(code='SB-1', note='整批确认后发布'), user=USER)
    published = routes.publish_standby('SB-1', user=USER)
    assert published['state'] == 'published'
    first = routes.watch(user=USER)
    assert len(first['reviews']) == 3
    assert first['standby'][0]['state'] == 'published'
    # Repeating the confirmation must not duplicate holds or instructions.
    again = routes.publish_standby('SB-1', user=USER)
    assert again['state'] == 'published'
    second = routes.watch(user=USER)
    assert len(second['reviews']) == 3
    with app_db() as db:
        assert len(list(db.scalars(select(DispatchReview)))) == 3
        assert len(list(db.scalars(select(StandbyTugHold)))) == 3
