"""Real HTTP round-trip through the FastAPI app, JWT auth and routers."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import main, routes, security
from src.models import (
    Base, Berth, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState,
    RequestState, Role, StandbyTugHold, TideWindow, Tug, TugAssignment, User,
)
from src.security import password_hash


def dt(value):
    return datetime.fromisoformat(value)


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (main, routes, security):
        monkeypatch.setattr(module, 'Session', factory, raising=False)
    monkeypatch.setattr(main, 'engine', engine)
    with factory.begin() as db:
        world(db)
    # Instantiate without the context manager so the app's own lifespan (real MySQL
    # engine + demo seed) does not run; the schema above is already in place.
    test_client = TestClient(main.app)
    try:
        yield test_client, factory
    finally:
        test_client.close()
        engine.dispose()


def world(db):
    db.add_all([
        User(username='supervisor', role=Role.SUPERVISOR, password_hash=password_hash('Harbor1074!')),
        User(username='viewer', role=Role.VIEWER, password_hash=password_hash('Harbor1074!')),
    ])
    db.add_all([Berth(code='B07', label='东七'), Berth(code='B09', label='东九')])
    db.add_all([Tug(code=code, label=code, bollard_pull_t=60) for code in ('TG12', 'TG18', 'TG23')])
    db.add_all([
        TideWindow(code='TW-071', berth_code='B07', starts_at=dt('2026-11-21T07:00'),
                   ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
        TideWindow(code='TW-091', berth_code='B09', starts_at=dt('2026-11-21T07:00'),
                   ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
    ])
    for index, (code, berth, window) in enumerate([
        ('BR-1', 'B07', 'TW-071'), ('BR-2', 'B09', 'TW-091'), ('BR-3', 'B07', 'TW-071'),
    ]):
        db.add(BerthRequest(code=code, vessel=f'船 {index}', berth_code=berth, window_code=window,
                            eta=dt(f'2026-11-21T09:0{index}'), maneuver_minutes=45, draft_m=10,
                            state=RequestState.CONFIRMED))
        db.add(DelayIncident(
            code=f'DL-{index}', request_code=code, radio_reference=f'VHF-{index}',
            observed_at=dt('2026-11-21T09:30'), event_note='现场通报', features={},
            input_digest='0' * 64, state=IncidentState.READY, result={'score': 2.0 - index / 10},
            decision=DelayClass.PRIORITY, decided_by='supervisor',
        ))


def bearer(client, username='supervisor'):
    response = client.post('/auth/login', json={'username': username, 'password': 'Harbor1074!'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


def test_standby_flow_requires_login_and_supervisor_role(client):
    http, _ = client
    body = {'code': 'SB-1', 'note': '早班争用待命拖轮'}
    assert http.post('/standby-batches', json=body).status_code == 401
    viewer = bearer(http, 'viewer')
    assert http.post('/standby-batches', json=body, headers=viewer).status_code == 403
    assert http.get('/standby-batches', headers=viewer).json() == []


def test_plan_publish_and_hold_visibility_over_http(client):
    http, factory = client
    auth = bearer(http)

    plan = http.post('/standby-batches', json={'code': 'SB-1', 'note': '早班争用待命拖轮'}, headers=auth)
    assert plan.status_code == 201, plan.text
    planned = plan.json()
    assert planned['state'] == 'planned'
    assert [item['incident_code'] for item in planned['items']] == ['DL-0', 'DL-1', 'DL-2']
    assert all(item['state'] == 'scheduled' for item in planned['items'])
    assert all(item['hold_active'] is False for item in planned['items'])

    # Pre-planning must not occupy tugs or issue anything.
    watch = http.get('/watch', headers=auth).json()
    assert watch['assignments'] == [] and watch['reviews'] == []
    assert {incident['state'] for incident in watch['incidents']} == {'ready'}

    published = http.post('/standby-batches/SB-1/publish', headers=auth)
    assert published.status_code == 200, published.text
    assert published.json()['state'] == 'published'
    assert all(item['hold_active'] for item in published.json()['items'])

    watch = http.get('/watch', headers=auth).json()
    assert len(watch['reviews']) == 3 and {review['action'] for review in watch['reviews']} == {'standby'}
    assert {incident['state'] for incident in watch['incidents']} == {'actioned'}
    assert watch['standby'][0]['state'] == 'published'

    # Repeat confirmation is idempotent: no duplicate holds or instructions.
    again = http.post('/standby-batches/SB-1/publish', headers=auth)
    assert again.status_code == 200
    with factory() as db:
        assert len(list(db.scalars(select(DispatchReview)))) == 3
        assert len(list(db.scalars(select(StandbyTugHold)))) == 3


def test_competing_booking_causes_whole_batch_return_over_http(client):
    http, factory = client
    auth = bearer(http)
    planned = http.post('/standby-batches', json={'code': 'SB-1', 'note': '早班争用待命拖轮'}, headers=auth).json()
    target = planned['items'][1]

    with factory.begin() as db:
        db.add(TugAssignment(request_code='BR-3', tug_code=target['tug_code'],
                             starts_at=dt('2026-11-21T09:30'), ends_at=dt('2026-11-21T10:15')))

    response = http.post('/standby-batches/SB-1/publish', headers=auth)
    assert response.status_code == 409, response.text
    assert '抢先占用' in response.json()['detail']

    batches = http.get('/standby-batches', headers=auth).json()
    assert batches[0]['state'] == 'returned'
    assert '抢先占用' in batches[0]['return_reason']
    watch = http.get('/watch', headers=auth).json()
    assert watch['reviews'] == []
    assert {incident['state'] for incident in watch['incidents']} == {'ready'}
    with factory() as db:
        assert db.scalar(select(StandbyTugHold)) is None

    # After the competing claim is released, replan and publish succeed exactly once.
    with factory.begin() as db:
        db.scalar(select(TugAssignment)).released_at = dt('2026-11-21T09:31')
    assert http.post('/standby-batches/SB-1/replan', headers=auth).status_code == 200
    assert http.post('/standby-batches/SB-1/publish', headers=auth).status_code == 200
    assert http.post('/standby-batches/SB-1/publish', headers=auth).status_code == 200
    with factory() as db:
        assert len(list(db.scalars(select(DispatchReview)))) == 3
        assert len(list(db.scalars(select(StandbyTugHold)))) == 3


def test_published_hold_appears_in_tug_ledger_and_blocks_reservation(client):
    http, factory = client
    auth = bearer(http)
    http.post('/standby-batches', json={'code': 'SB-1', 'note': '早班争用待命拖轮'}, headers=auth)
    http.post('/standby-batches/SB-1/publish', headers=auth)

    with factory() as db:
        hold = db.scalar(select(StandbyTugHold))
    # A vessel unrelated to the batch overlaps the held window.
    with factory.begin() as db:
        db.add(BerthRequest(code='BR-9', vessel='外船 1', berth_code='B07', window_code='TW-071',
                            eta=hold.starts_at.replace(minute=40), maneuver_minutes=30, draft_m=10,
                            state=RequestState.REQUESTED))
    blocked = http.put('/berth-requests/BR-9/tug', json={'tug_code': hold.tug_code}, headers=auth)
    assert blocked.status_code == 409, blocked.text
    assert '待命批次 SB-1' in blocked.json()['detail']
    # The case's own berth reservation is not treated as a competing claim.
    watch = http.get('/watch', headers=auth).json()
    assert len(watch['standby'][0]['items']) == 3
