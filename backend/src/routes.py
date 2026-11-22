from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from . import incidents, planning, standby
from .database import Session
from .models import Audit, Berth, BerthRequest, DelayIncident, DispatchReview, Role, StandbyBatch, StandbyBatchState, StandbyPlanItem, TideWindow, Tug, TugAssignment, User
from .schemas import AssignTug, CloseInput, DispatchInput, IncidentInput, Login, MarginDecision, RequestInput, Schedule, StandbyBatchInput, TideInput
from .security import current_user, permitted, token_for, valid_password

router = APIRouter()
write = Depends(permitted(Role.OPERATOR, Role.SUPERVISOR))
supervise = Depends(permitted(Role.SUPERVISOR))
read = Depends(current_user)


def row(model):
    return {column.key: getattr(model, column.key) for column in model.__table__.columns}


@router.post('/auth/login')
def login(data: Login):
    with Session.begin() as db:
        user = db.get(User, data.username)
        if not user or not valid_password(data.password, user.password_hash):
            raise HTTPException(401, '账号或口令错误')
        planning.audit(db, user.username, user.username, 'LOGIN', '交班会话已登录')
        return {'access_token': token_for(user), 'username': user.username, 'role': user.role}


@router.get('/watch')
def watch(user: User = read):
    with Session() as db:
        planned = standby.pending_incident_codes(db)
        requests = []
        for request in db.scalars(select(BerthRequest).order_by(BerthRequest.eta)):
            window = planning.require_window(db, request.window_code, request.berth_code, request.code)
            reserve, warning = planning.tide_clearance(request, window)
            reservation = planning.active_tug(db, request.code)
            requests.append({**row(request), 'ends_at': planning.ends(request), 'tide_reserve_min': reserve,
                             'tide_warning': warning, 'tug_code': reservation.tug_code if reservation else None})
        return {
            'berths': [row(item) for item in db.scalars(select(Berth).order_by(Berth.code))],
            'tugs': [row(item) for item in db.scalars(select(Tug).order_by(Tug.code))],
            'windows': [row(item) for item in db.scalars(select(TideWindow).order_by(TideWindow.starts_at))],
            'requests': requests,
            'assignments': [row(item) for item in db.scalars(select(TugAssignment).order_by(TugAssignment.starts_at))],
            'incidents': [row(item) for item in db.scalars(select(DelayIncident).order_by(DelayIncident.observed_at.desc()))],
            'reviews': [row(item) for item in db.scalars(select(DispatchReview).order_by(DispatchReview.issued_at.desc()))],
            'standby': standby.catalog(db),
            'standby_ready': [{'code': item.code, 'request_code': item.request_code, 'score': standby.score_of(item),
                               'observed_at': item.observed_at, 'decision': item.decision,
                               'planned': item.code in planned}
                              for item in standby.ordered_candidates(db)],
            'standby_waiting': standby.waiting_cases(db),
        }


@router.get('/history/{reference}')
def history(reference: str, user: User = read):
    with Session() as db:
        case = db.get(DelayIncident, reference)
        references = [reference, case.request_code] if case else [reference]
        batch = db.get(StandbyBatch, reference)
        if batch:
            references.extend(item.incident_code for item in db.scalars(select(StandbyPlanItem).where(
                StandbyPlanItem.batch_code == reference)))
        events = db.scalars(select(Audit).where(Audit.reference.in_(references)).order_by(Audit.id.desc()).limit(60))
        return [{**row(event), 'port_time': event.event_at + timedelta(hours=8)} for event in events]


@router.post('/tide-windows', status_code=201)
def create_window(data: TideInput, user: User = supervise):
    with Session.begin() as db:
        return row(planning.add_window(db, user.username, data))


@router.post('/berth-requests', status_code=201)
def create_request(data: RequestInput, user: User = write):
    with Session.begin() as db:
        return row(planning.add_request(db, user.username, data))


@router.put('/berth-requests/{code}/schedule')
def revise_request(code: str, data: Schedule, user: User = write):
    with Session.begin() as db:
        return row(planning.revise(db, user.username, code, data))


@router.put('/berth-requests/{code}/tug')
def assign_tug(code: str, data: AssignTug, user: User = write):
    with Session.begin() as db:
        reservation = planning.reserve_tug(db, user.username, code, data.tug_code)
        db.flush()
        return row(reservation)


@router.post('/berth-requests/{code}/confirm')
def confirm_request(code: str, user: User = supervise):
    with Session.begin() as db:
        return row(planning.confirm(db, user.username, code))


@router.post('/delay-incidents', status_code=202)
def create_incident(data: IncidentInput, user: User = write):
    with Session.begin() as db:
        incident = incidents.report(db, user.username, data)
        db.flush()
        output = row(incident)
    incidents.notify(incident.code)
    return output


@router.post('/delay-incidents/{code}/retry', status_code=202)
def retry_incident(code: str, user: User = supervise):
    with Session.begin() as db:
        output = row(incidents.retry(db, user.username, code))
    incidents.notify(code)
    return output


@router.post('/delay-incidents/{code}/margin-review')
def review_incident(code: str, data: MarginDecision, user: User = supervise):
    with Session.begin() as db:
        return row(incidents.review_margin(db, user.username, code, data))


@router.post('/delay-incidents/{code}/dispatch', status_code=201)
def issue_action(code: str, data: DispatchInput, user: User = supervise):
    with Session.begin() as db:
        return row(incidents.dispatch(db, user.username, code, data))


@router.post('/delay-incidents/{code}/close')
def close_review(code: str, data: CloseInput, user: User = supervise):
    with Session.begin() as db:
        return row(incidents.close(db, user.username, code, data))


@router.get('/standby-batches')
def standby_batches(user: User = read):
    with Session() as db:
        return standby.catalog(db)


@router.post('/standby-batches', status_code=201)
def plan_standby(data: StandbyBatchInput, user: User = supervise):
    with Session.begin() as db:
        batch = standby.create_batch(db, user.username, data.code, data.note)
        db.flush()
        return standby.snapshot(db, batch)


@router.post('/standby-batches/{code}/replan')
def replan_standby(code: str, user: User = supervise):
    with Session.begin() as db:
        batch = standby.replan(db, user.username, code)
        db.flush()
        return standby.snapshot(db, batch)


@router.post('/standby-batches/{code}/publish')
def publish_standby(code: str, user: User = supervise):
    try:
        with Session.begin() as db:
            batch = standby.publish(db, user.username, code)
            db.flush()
            return standby.snapshot(db, batch)
    except standby.BatchReturned as exc:
        # The publish transaction rolled back; record the return on its own.
        with Session.begin() as db:
            batch = db.get(StandbyBatch, code, with_for_update=True)
            if batch and batch.state == StandbyBatchState.PLANNED:
                batch.state = StandbyBatchState.RETURNED
                batch.returned_at = planning.utc_now()
                batch.return_reason = exc.message
                batch.attempts += 1
                planning.audit(db, user.username, code, 'STANDBY_RETURNED', exc.message)
        raise HTTPException(409, exc.message)
