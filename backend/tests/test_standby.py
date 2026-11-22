from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import standby
from src.models import (
    Base, Berth, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState,
    RequestState, StandbyBatch, StandbyBatchState, StandbyItemState, StandbyPlanItem,
    StandbyTugHold, TideWindow, Tug, TugAssignment, User,
)


@pytest.fixture()
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session.begin() as session:
        _world(session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def dt(value):
    return datetime.fromisoformat(value)


def _world(db):
    db.add_all([User(username='supervisor', role='supervisor', password_hash='x')])
    db.add_all([Tug(code=code, label=code, bollard_pull_t=60) for code in ('TG12', 'TG18', 'TG23')])
    db.add_all([Berth(code='B07', label='东七'), Berth(code='B09', label='东九')])
    db.add_all([
        TideWindow(code='TW-071', berth_code='B07', starts_at=dt('2026-11-21T07:00'),
                   ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
        TideWindow(code='TW-091', berth_code='B09', starts_at=dt('2026-11-21T07:00'),
                   ends_at=dt('2026-11-21T12:00'), max_draft_m=13, bulletin='TIDE-1'),
    ])
    for index, (code, berth, window) in enumerate([
        ('BR-1', 'B07', 'TW-071'), ('BR-2', 'B09', 'TW-091'),
        ('BR-3', 'B07', 'TW-071'), ('BR-4', 'B09', 'TW-091'),
    ]):
        db.add(BerthRequest(code=code, vessel=f'船 {index}', berth_code=berth, window_code=window,
                            eta=dt(f'2026-11-21T09:0{index}'), maneuver_minutes=45, draft_m=10,
                            state=RequestState.CONFIRMED))


def add_case(db, code, request_code, score, observed, *, decision=DelayClass.PRIORITY,
             state=IncidentState.READY):
    incident = DelayIncident(
        code=code, request_code=request_code, radio_reference=f'VHF-{code}',
        observed_at=observed, event_note='现场通报', features={}, input_digest='0' * 64,
        state=state, result={'score': score}, decision=decision,
        decided_by='supervisor' if decision else None,
    )
    db.add(incident)
    db.flush()
    return incident


def items(db, code='SB-1'):
    return list(db.scalars(select(StandbyPlanItem).where(
        StandbyPlanItem.batch_code == code).order_by(StandbyPlanItem.rank)))


def test_ranking_uses_score_then_earlier_observation_then_code(db):
    add_case(db, 'DL-A', 'BR-1', 1.4, dt('2026-11-21T09:10'))
    add_case(db, 'DL-B', 'BR-2', 2.1, dt('2026-11-21T09:30'))
    add_case(db, 'DL-C', 'BR-3', 1.4, dt('2026-11-21T09:05'))
    add_case(db, 'DL-D', 'BR-4', 1.4, dt('2026-11-21T09:05'))
    batch = standby.create_batch(db, 'supervisor', 'SB-1', '早班争用待命拖轮')
    assert [(item.rank, item.incident_code) for item in items(db)] == [
        (1, 'DL-B'), (2, 'DL-C'), (3, 'DL-D'), (4, 'DL-A'),
    ]
    assert batch.state == StandbyBatchState.PLANNED


def test_unfinished_cases_wait_and_are_not_ranked(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:10'))
    add_case(db, 'DL-Q', 'BR-2', None, dt('2026-11-21T09:20'), decision=None, state=IncidentState.QUEUED)
    add_case(db, 'DL-M', 'BR-3', 0.1, dt('2026-11-21T09:30'), decision=None, state=IncidentState.MARGIN_REVIEW)
    add_case(db, 'DL-F', 'BR-4', None, dt('2026-11-21T09:40'), decision=None, state=IncidentState.FAILED)
    standby.create_batch(db, 'supervisor', 'SB-1', '按分类排序')
    assert [item.incident_code for item in items(db)] == ['DL-A']
    assert [case['code'] for case in standby.waiting_cases(db)] == ['DL-Q', 'DL-M', 'DL-F']


def test_non_priority_or_already_dispatched_cases_are_excluded(db):
    add_case(db, 'DL-P', 'BR-1', 1.0, dt('2026-11-21T09:10'))
    add_case(db, 'DL-R', 'BR-2', 0.9, dt('2026-11-21T09:20'), decision=DelayClass.ROUTINE)
    add_case(db, 'DL-D', 'BR-3', 0.8, dt('2026-11-21T09:30'))
    db.add(DispatchReview(incident_code='DL-D', action='standby', instruction='已单独发令',
                          issued_by='supervisor', issued_at=dt('2026-11-21T09:35')))
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-1', '只排未发令的优先案')
    assert [item.incident_code for item in items(db)] == ['DL-P']


def test_overlapping_demand_reuses_free_tug_and_marks_blocked(db):
    # Three priority cases all need a tug over the same expected window.
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    add_case(db, 'DL-B', 'BR-2', 1.9, dt('2026-11-21T09:00'))
    add_case(db, 'DL-C', 'BR-3', 1.8, dt('2026-11-21T09:00'))
    # Occupy the third tug for the whole morning.
    db.add(TugAssignment(request_code='BR-4', tug_code='TG23',
                         starts_at=dt('2026-11-21T08:00'), ends_at=dt('2026-11-21T11:00')))
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-1', '早班争用')
    plan = items(db)
    assert [item.state for item in plan] == [StandbyItemState.SCHEDULED, StandbyItemState.SCHEDULED,
                                             StandbyItemState.BLOCKED]
    assert plan[0].tug_code != plan[1].tug_code
    assert plan[2].tug_code is None
    assert 'TG23 被 BR-4 的靠泊预约' in plan[2].block_reason
    assert 'TG12 已被本批 DL-A 排定' in plan[2].block_reason or 'TG18 已被本批' in plan[2].block_reason


def test_own_berth_reservation_does_not_block_standby_cover(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    db.add(TugAssignment(request_code='BR-1', tug_code='TG12',
                         starts_at=dt('2026-11-21T08:30'), ends_at=dt('2026-11-21T09:30')))
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-1', '复用本船拖轮')
    plan = items(db)
    assert plan[0].state == StandbyItemState.SCHEDULED
    assert plan[0].tug_code == 'TG12'


def test_whole_batch_publish_occupies_tugs_and_issues_every_instruction(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    add_case(db, 'DL-B', 'BR-2', 1.5, dt('2026-11-21T09:00'))
    standby.create_batch(db, 'supervisor', 'SB-1', '整批确认')
    db.flush()
    standby.publish(db, 'supervisor', 'SB-1')
    db.flush()
    assert db.get(StandbyBatch, 'SB-1').state == StandbyBatchState.PUBLISHED
    assert len(list(db.scalars(select(DispatchReview)))) == 2
    assert len(list(db.scalars(select(StandbyTugHold)))) == 2
    assert {row.state for row in db.scalars(select(DelayIncident))} == {IncidentState.ACTIONED}
    assert {row.action for row in db.scalars(select(DispatchReview))} == {'standby'}


def test_competing_booking_returns_whole_batch_and_issues_nothing(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    add_case(db, 'DL-B', 'BR-2', 1.5, dt('2026-11-21T09:00'))
    db.commit()
    standby.create_batch(db, 'supervisor', 'SB-1', '整批确认')
    db.commit()
    plan = items(db)
    # A separate booking grabs the second case's pre-planned slot after planning.
    db.add(TugAssignment(request_code='BR-3', tug_code=plan[1].tug_code,
                         starts_at=plan[1].starts_at, ends_at=plan[1].ends_at))
    db.commit()
    with pytest.raises(standby.BatchReturned) as returned:
        standby.publish(db, 'supervisor', 'SB-1')
    assert plan[1].incident_code in returned.value.message
    db.rollback()
    assert db.get(StandbyBatch, 'SB-1').state == StandbyBatchState.PLANNED
    assert list(db.scalars(select(DispatchReview))) == []
    assert list(db.scalars(select(StandbyTugHold))) == []
    assert {row.state for row in db.scalars(select(DelayIncident))} == {IncidentState.READY}


def test_republish_after_return_reuses_holds_without_duplicates(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    db.commit()
    standby.create_batch(db, 'supervisor', 'SB-1', '重排后再发')
    db.commit()
    first = items(db)[0]
    db.add(TugAssignment(request_code='BR-3', tug_code=first.tug_code,
                         starts_at=first.starts_at, ends_at=first.ends_at))
    db.commit()
    with pytest.raises(standby.BatchReturned):
        standby.publish(db, 'supervisor', 'SB-1')
    # The publish transaction rolls back; the caller records the return on its own.
    db.rollback()
    batch = db.get(StandbyBatch, 'SB-1')
    batch.state = StandbyBatchState.RETURNED
    db.commit()
    # Free the competing claim, then replan and publish.
    db.scalars(select(TugAssignment)).all()[-1].released_at = dt('2026-11-21T09:05')
    db.commit()
    standby.replan(db, 'supervisor', 'SB-1')
    db.commit()
    standby.publish(db, 'supervisor', 'SB-1')
    db.commit()
    standby.publish(db, 'supervisor', 'SB-1')
    db.commit()
    assert len(list(db.scalars(select(StandbyTugHold)))) == 1
    assert len(list(db.scalars(select(DispatchReview)))) == 1
    assert db.get(StandbyBatch, 'SB-1').attempts == 1


def test_batch_with_no_schedulable_item_cannot_be_published(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    add_case(db, 'DL-B', 'BR-2', 1.9, dt('2026-11-21T09:00'))
    add_case(db, 'DL-C', 'BR-3', 1.8, dt('2026-11-21T09:00'))
    db.add_all([TugAssignment(request_code='BR-4', tug_code=tug,
                              starts_at=dt('2026-11-21T08:00'), ends_at=dt('2026-11-21T11:00'))
                for tug in ('TG12', 'TG18', 'TG23')])
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-1', '全部排不上')
    with pytest.raises(Exception) as rejected:
        standby.publish(db, 'supervisor', 'SB-1')
    assert 'STANDBY_INCOMPLETE' in str(rejected.value)


def test_blocked_item_keeps_reason_and_is_not_issued(db):
    # One case can be placed, one cannot; the published batch issues only the placed one.
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    add_case(db, 'DL-B', 'BR-2', 1.9, dt('2026-11-21T09:00'))
    db.add_all([TugAssignment(request_code='BR-4', tug_code=tug,
                              starts_at=dt('2026-11-21T08:00'), ends_at=dt('2026-11-21T11:00'))
                for tug in ('TG18', 'TG23')])
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-1', '部分排不上')
    db.flush()
    plan = items(db)
    assert [item.state for item in plan] == [StandbyItemState.SCHEDULED, StandbyItemState.BLOCKED]
    assert '排不上' not in (plan[0].block_reason or '')
    assert plan[1].tug_code is None and '占用' in plan[1].block_reason
    standby.publish(db, 'supervisor', 'SB-1')
    db.flush()
    assert {row.incident_code for row in db.scalars(select(DispatchReview))} == {'DL-A'}
    assert {row.incident_code for row in db.scalars(select(StandbyTugHold))} == {'DL-A'}
    assert db.get(DelayIncident, 'DL-B').state == IncidentState.READY


def test_single_standby_instruction_is_blocked_while_batch_is_pending(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    standby.create_batch(db, 'supervisor', 'SB-1', '只能整批发')
    db.flush()
    from src import incidents
    from src.schemas import DispatchInput
    with pytest.raises(Exception) as rejected:
        incidents.dispatch(db, 'supervisor', 'DL-A', DispatchInput(action='standby', instruction='先单独发一条'))
    assert 'STANDBY_BATCH_PENDING' in str(rejected.value)
    with pytest.raises(Exception) as rejected:
        incidents.dispatch(db, 'supervisor', 'DL-A', DispatchInput(action='retime', instruction='单独改期也该等整批'))
    assert 'STANDBY_BATCH_PENDING' in str(rejected.value)


def test_closing_a_published_case_releases_its_tug_hold(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    db.commit()
    standby.create_batch(db, 'supervisor', 'SB-1', '发布后结案释放')
    db.commit()
    standby.publish(db, 'supervisor', 'SB-1')
    db.commit()
    from src import incidents
    from src.schemas import CloseInput
    incidents.close(db, 'supervisor', 'DL-A', CloseInput(all_fast_at=dt('2026-11-21T09:50'), outcome='全缆系妥，现场恢复。'))
    db.commit()
    holds = list(db.scalars(select(StandbyTugHold)))
    assert len(holds) == 1 and holds[0].released_at is not None
    # The released hold no longer blocks a later berth request on that tug.
    from src.planning import conflicting_tug
    assert conflicting_tug(db, db.get(BerthRequest, 'BR-2'), holds[0].tug_code) is None


def test_published_hold_blocks_later_berth_reservation(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    db.commit()
    standby.create_batch(db, 'supervisor', 'SB-1', '待命占用挡住靠泊预约')
    db.commit()
    standby.publish(db, 'supervisor', 'SB-1')
    db.commit()
    hold = db.scalar(select(StandbyTugHold))
    from src.planning import Rejected, reserve_tug
    # BR-2 is confirmed; make it editable again to isolate the tug calendar check.
    db.get(BerthRequest, 'BR-2').state = RequestState.REQUESTED
    with pytest.raises(Rejected) as rejected:
        reserve_tug(db, 'operator', 'BR-2', hold.tug_code)
    db.rollback()
    assert '待命批次 SB-1' in rejected.value.message


def test_new_plan_supersedes_only_pending_drafts(db):
    add_case(db, 'DL-A', 'BR-1', 2.0, dt('2026-11-21T09:00'))
    standby.create_batch(db, 'supervisor', 'SB-1', '首版')
    db.flush()
    standby.create_batch(db, 'supervisor', 'SB-2', '重排版本')
    db.flush()
    assert db.get(StandbyBatch, 'SB-1').state == StandbyBatchState.SUPERSEDED
    assert db.get(StandbyBatch, 'SB-2').state == StandbyBatchState.PLANNED
    assert [item.incident_code for item in items(db, 'SB-2')] == ['DL-A']
    # A returned batch stays visible as history for the audit trail.
    db.get(StandbyBatch, 'SB-2').state = StandbyBatchState.RETURNED
    standby.create_batch(db, 'supervisor', 'SB-3', '第三版')
    db.flush()
    assert db.get(StandbyBatch, 'SB-2').state == StandbyBatchState.RETURNED
    assert db.get(StandbyBatch, 'SB-3').state == StandbyBatchState.PLANNED


def test_empty_plan_is_rejected(db):
    with pytest.raises(Exception) as rejected:
        standby.create_batch(db, 'supervisor', 'SB-1', '没有候选')
    assert 'STANDBY_EMPTY' in str(rejected.value)
