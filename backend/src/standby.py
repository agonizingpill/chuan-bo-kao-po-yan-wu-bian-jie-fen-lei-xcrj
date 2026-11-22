"""Competing standby-tug demand: ranked pre-plan, whole-batch confirmation."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DatabaseSession

from .models import (
    Action, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState,
    StandbyBatch, StandbyBatchState, StandbyItemState, StandbyPlanItem, StandbyTugHold, Tug, TugAssignment,
)
from .planning import Rejected, active_tug, audit, find, utc_now


class BatchReturned(Exception):
    """Pre-planned slot lost to a competing booking; nothing is issued."""

    def __init__(self, message, batch=None):
        self.message = message
        self.batch = batch


def score_of(incident):
    value = (incident.result or {}).get('score')
    return value if isinstance(value, (int, float)) else float('-inf')


def requirement(incident, request):
    """Tug standby covers the manoeuvre the delayed vessel is expected to make.

    The observed arrival replaces the planned pilot time, so the window starts
    at the frozen observation and lasts one manoeuvre.
    """
    starts = incident.observed_at
    return starts, starts + timedelta(minutes=request.maneuver_minutes)


def ordered_candidates(db: DatabaseSession):
    """Priority cases awaiting an instruction, ranked by score then report time.

    Ties break on the earlier freezing observation, then on code for stability.
    """
    rows = list(db.scalars(select(DelayIncident).where(
        DelayIncident.state == IncidentState.READY,
        DelayIncident.decision == DelayClass.PRIORITY,
        DelayIncident.code.notin_(select(DispatchReview.incident_code)),
    )))
    rows.sort(key=lambda item: (-score_of(item), item.observed_at, item.code))
    return rows


def pending_incident_codes(db: DatabaseSession):
    """Cases already sitting in a not-yet-published draft."""
    return set(db.scalars(select(StandbyPlanItem.incident_code).join(
        StandbyBatch, StandbyBatch.code == StandbyPlanItem.batch_code,
    ).where(StandbyBatch.state == StandbyBatchState.PLANNED)))


def waiting_cases(db: DatabaseSession):
    rows = db.scalars(select(DelayIncident).where(
        DelayIncident.state.in_([IncidentState.QUEUED, IncidentState.FAILED, IncidentState.MARGIN_REVIEW]),
    ).order_by(DelayIncident.observed_at, DelayIncident.code))
    return [{
        'code': row.code, 'request_code': row.request_code, 'state': row.state,
        'observed_at': row.observed_at,
    } for row in rows]


def commitments(db: DatabaseSession, starts, ends, *, exclude_request=None):
    """Everything already claiming tug time in [starts, ends).

    A case's own berth reservation is excluded: that tug is already working this
    vessel, so converting it into standby cover is not a new competing claim.
    """
    busy = []
    assignments = select(TugAssignment).where(
        TugAssignment.released_at.is_(None),
        TugAssignment.starts_at < ends, TugAssignment.ends_at > starts,
    )
    if exclude_request:
        assignments = assignments.where(TugAssignment.request_code != exclude_request)
    for row in db.scalars(assignments):
        busy.append((row.tug_code, f'{row.request_code} 的靠泊预约', row.starts_at, row.ends_at))
    for row in db.scalars(select(StandbyTugHold).where(
        StandbyTugHold.released_at.is_(None),
        StandbyTugHold.starts_at < ends, StandbyTugHold.ends_at > starts,
    )):
        busy.append((row.tug_code, f'待命批次 {row.batch_code} 的 {row.incident_code}', row.starts_at, row.ends_at))
    return busy


def blocked_reason(db: DatabaseSession, starts, ends, *, exclude_request=None, planned=()):
    """Explain every tug's unavailability for one requested window.

    ``planned`` carries tugs already assigned to higher-ranked cases in the same
    batch; both those and pre-existing commitments count as unavailable.
    """
    clauses = []
    for tug_code, incident_code, planned_starts, planned_ends in planned:
        if starts < planned_ends and ends > planned_starts:
            clauses.append(f'{tug_code} 已被本批 {incident_code} 排定 {planned_starts:%H:%M}–{planned_ends:%H:%M}')
    for tug, reference, busy_start, busy_end in commitments(db, starts, ends, exclude_request=exclude_request):
        clauses.append(f'{tug} 被 {reference} {busy_start:%H:%M}–{busy_end:%H:%M} 占用')
    if not clauses:
        return '该时段没有可用拖轮'
    return ('；'.join(clauses))[:240]


def supersede_others(db: DatabaseSession, batch):
    """A fresh plan replaces other pending drafts. Returned batches stay visible
    as history because nothing was issued from them."""
    for other in db.scalars(select(StandbyBatch).where(
        StandbyBatch.code != batch.code,
        StandbyBatch.state == StandbyBatchState.PLANNED,
    )):
        other.state = StandbyBatchState.SUPERSEDED


def build_plan(db: DatabaseSession, actor: str, batch):
    supersede_others(db, batch)
    for item in list(db.scalars(select(StandbyPlanItem).where(StandbyPlanItem.batch_code == batch.code))):
        db.delete(item)
    db.flush()
    tug_codes = [row.code for row in db.scalars(select(Tug).order_by(Tug.code))]
    planned = []
    for rank, incident in enumerate(ordered_candidates(db), 1):
        request = db.get(BerthRequest, incident.request_code)
        starts, ends = requirement(incident, request)
        own = active_tug(db, request.code)
        preference = ([own.tug_code] if own else []) + [code for code in tug_codes if not own or code != own.tug_code]
        busy = commitments(db, starts, ends, exclude_request=request.code)
        chosen = None
        for tug_code in preference:
            if any(tug_code == used and starts < used_ends and ends > used_starts
                   for used, _, used_starts, used_ends in planned):
                continue
            if any(tug_code == busy_tug and starts < busy_end and ends > busy_start
                   for busy_tug, _, busy_start, busy_end in busy):
                continue
            chosen = tug_code
            break
        if chosen:
            planned.append((chosen, incident.code, starts, ends))
            item = StandbyPlanItem(batch_code=batch.code, incident_code=incident.code, rank=rank, tug_code=chosen,
                                   starts_at=starts, ends_at=ends, state=StandbyItemState.SCHEDULED)
        else:
            item = StandbyPlanItem(batch_code=batch.code, incident_code=incident.code, rank=rank,
                                   starts_at=starts, ends_at=ends, state=StandbyItemState.BLOCKED,
                                   block_reason=blocked_reason(db, starts, ends, exclude_request=request.code,
                                                               planned=planned))
        db.add(item)
    batch.state = StandbyBatchState.PLANNED
    batch.planned_at = utc_now()
    batch.returned_at = None
    batch.return_reason = None
    return batch


def create_batch(db: DatabaseSession, actor: str, code: str, note: str):
    if db.get(StandbyBatch, code):
        raise Rejected('DUPLICATE_STANDBY_BATCH', '待命批次编号已存在', code)
    if not ordered_candidates(db):
        raise Rejected('STANDBY_EMPTY', '当前没有已判定优先且待发布指令的案件', code)
    now = utc_now()
    batch = StandbyBatch(code=code, state=StandbyBatchState.PLANNED, note=note, created_by=actor,
                         created_at=now, planned_at=now, attempts=0)
    db.add(batch)
    build_plan(db, actor, batch)
    db.flush()
    items = list(db.scalars(select(StandbyPlanItem).where(StandbyPlanItem.batch_code == code)))
    blocked = sum(item.state == StandbyItemState.BLOCKED for item in items)
    audit(db, actor, code, 'STANDBY_PLANNED', f'{len(items)} 件 · {blocked} 件排不上')
    return batch


def replan(db: DatabaseSession, actor: str, code: str):
    batch = find(db, StandbyBatch, code, lock=True)
    if batch.state not in (StandbyBatchState.PLANNED, StandbyBatchState.RETURNED):
        raise Rejected('STANDBY_REPLAN_STATE', '只有待确认或已退回的批次可以重新预排', code)
    build_plan(db, actor, batch)
    db.flush()
    items = list(db.scalars(select(StandbyPlanItem).where(StandbyPlanItem.batch_code == code)))
    blocked = sum(item.state == StandbyItemState.BLOCKED for item in items)
    audit(db, actor, code, 'STANDBY_REPLANNED', f'{len(items)} 件 · {blocked} 件排不上')
    return batch


def _conflicts(db: DatabaseSession, batch, items):
    problems = []
    for item in items:
        incident = db.get(DelayIncident, item.incident_code)
        if incident is None or incident.state != IncidentState.READY:
            problems.append(f'{item.incident_code} 已不处于待发布状态')
            continue
        if db.get(DispatchReview, item.incident_code):
            problems.append(f'{item.incident_code} 已单独发布指令')
            continue
        request = db.get(BerthRequest, incident.request_code)
        expected_start, expected_end = requirement(incident, request)
        if (item.starts_at, item.ends_at) != (expected_start, expected_end):
            problems.append(f'{item.incident_code} 的操纵时长已被修订，预排时段失效')
            continue
        assignment = db.scalar(select(TugAssignment).where(
            TugAssignment.tug_code == item.tug_code,
            TugAssignment.released_at.is_(None),
            TugAssignment.request_code != incident.request_code,
            TugAssignment.starts_at < item.ends_at,
            TugAssignment.ends_at > item.starts_at,
        ).limit(1))
        if assignment:
            problems.append(f'{item.incident_code}：预排时段被 {assignment.request_code} 的预约 '
                            f'{assignment.starts_at:%H:%M}–{assignment.ends_at:%H:%M} 抢先占用')
            continue
        hold = db.scalar(select(StandbyTugHold).where(
            StandbyTugHold.tug_code == item.tug_code,
            StandbyTugHold.batch_code != batch.code,
            StandbyTugHold.released_at.is_(None),
            StandbyTugHold.starts_at < item.ends_at,
            StandbyTugHold.ends_at > item.starts_at,
        ).limit(1))
        if hold:
            problems.append(f'{item.incident_code}：{item.tug_code} 已被批次 {hold.batch_code} '
                            f'的 {hold.incident_code} 占用')
    return problems


def publish(db: DatabaseSession, actor: str, code: str):
    """Confirm the whole batch: occupy tugs and issue every instruction, or none."""
    batch = find(db, StandbyBatch, code, lock=True)
    if batch.state == StandbyBatchState.PUBLISHED:
        return batch
    if batch.state in (StandbyBatchState.RETURNED, StandbyBatchState.SUPERSEDED):
        raise Rejected('STANDBY_REPLAN_REQUIRED', '整批已退回或作废，请重新预排后再确认', code)
    if batch.state != StandbyBatchState.PLANNED:
        raise Rejected('STANDBY_PUBLISH_STATE', '该批次当前不可确认发布', code)
    items = list(db.scalars(select(StandbyPlanItem).where(
        StandbyPlanItem.batch_code == code).order_by(StandbyPlanItem.rank)))
    if not items:
        raise Rejected('STANDBY_EMPTY', '批次中没有待命案件，请重新预排', code)
    scheduled = [item for item in items if item.state == StandbyItemState.SCHEDULED]
    if not scheduled:
        raise Rejected('STANDBY_INCOMPLETE', '本批所有案件都排不上拖轮，请调整预排后再确认', code)
    # Lock the cases first, then the competing tug rows in a stable order.
    incidents = {}
    for incident_code in sorted(item.incident_code for item in items):
        incidents[incident_code] = find(db, DelayIncident, incident_code, lock=True)
    for tug_code in sorted({item.tug_code for item in scheduled}):
        find(db, Tug, tug_code, lock=True)
    problems = _conflicts(db, batch, scheduled)
    if problems:
        # A competing booking took a pre-planned slot after the batch was built.
        # Abort before any hold or instruction is written; the caller records the
        # return in its own transaction because this one rolls back.
        raise BatchReturned('；'.join(problems)[:300], batch)
    now = utc_now()
    for item in scheduled:
        incident = incidents[item.incident_code]
        if incident.state != IncidentState.READY:
            raise Rejected('STANDBY_CASE_CHANGED', f'{incident.code} 已不处于待发布状态，请重新预排', code)
        instruction = f'待命批次 {batch.code} · {item.tug_code} {item.starts_at:%H:%M}–{item.ends_at:%H:%M}。{batch.note}'
        instruction = instruction[:300]
        db.add(DispatchReview(incident_code=incident.code, action=Action.STANDBY, instruction=instruction,
                              issued_by=actor, issued_at=now))
        incident.state = IncidentState.ACTIONED
        db.add(StandbyTugHold(batch_code=batch.code, incident_code=incident.code, tug_code=item.tug_code,
                              starts_at=item.starts_at, ends_at=item.ends_at))
        audit(db, actor, incident.code, 'ACTION_ISSUED', f'standby · {instruction}')
    batch.state = StandbyBatchState.PUBLISHED
    batch.published_by = actor
    batch.published_at = now
    batch.attempts += 1
    skipped = len(items) - len(scheduled)
    audit(db, actor, code, 'STANDBY_PUBLISHED',
          f'{len(scheduled)} 件发令' + (f' · {skipped} 件排不上未发令' if skipped else ''))
    return batch


def pending_batch_for(db: DatabaseSession, incident_code: str):
    return db.scalar(select(StandbyBatch).join(
        StandbyPlanItem, StandbyPlanItem.batch_code == StandbyBatch.code,
    ).where(
        StandbyPlanItem.incident_code == incident_code,
        StandbyBatch.state == StandbyBatchState.PLANNED,
    ).limit(1))


def release_holds(db: DatabaseSession, incident_code: str, actor: str = 'system'):
    """Free a case's standby hold once it leaves the batch (closed or re-instructed)."""
    for hold in db.scalars(select(StandbyTugHold).where(
        StandbyTugHold.incident_code == incident_code, StandbyTugHold.released_at.is_(None))):
        hold.released_at = utc_now()
        if hold.batch_code:
            audit(db, actor, hold.batch_code, 'STANDBY_HOLD_RELEASED', f'{hold.incident_code} · {hold.tug_code}')


def item_view(db: DatabaseSession, item):
    incident = db.get(DelayIncident, item.incident_code)
    request = db.get(BerthRequest, incident.request_code) if incident else None
    hold = db.scalar(select(StandbyTugHold).where(
        StandbyTugHold.incident_code == item.incident_code,
        StandbyTugHold.batch_code == item.batch_code,
    ))
    return {
        'id': item.id, 'incident_code': item.incident_code, 'rank': item.rank,
        'tug_code': item.tug_code, 'starts_at': item.starts_at, 'ends_at': item.ends_at,
        'state': item.state, 'block_reason': item.block_reason,
        'request_code': request.code if request else None,
        'vessel': request.vessel if request else None,
        'score': score_of(incident) if incident else None,
        'observed_at': incident.observed_at if incident else None,
        'hold_active': bool(hold and hold.released_at is None),
    }


def snapshot(db: DatabaseSession, batch):
    items = db.scalars(select(StandbyPlanItem).where(
        StandbyPlanItem.batch_code == batch.code).order_by(StandbyPlanItem.rank))
    return {
        'code': batch.code, 'state': batch.state, 'note': batch.note,
        'created_by': batch.created_by, 'created_at': batch.created_at, 'planned_at': batch.planned_at,
        'published_by': batch.published_by, 'published_at': batch.published_at,
        'returned_at': batch.returned_at, 'return_reason': batch.return_reason, 'attempts': batch.attempts,
        'items': [item_view(db, item) for item in items],
    }


def catalog(db: DatabaseSession):
    batches = db.scalars(select(StandbyBatch).order_by(StandbyBatch.created_at.desc()))
    return [snapshot(db, batch) for batch in batches]
