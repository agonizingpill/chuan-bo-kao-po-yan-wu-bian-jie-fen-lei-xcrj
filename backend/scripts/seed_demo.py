from datetime import datetime, timedelta

from sqlalchemy import func, select

from src.classifier import classify, snapshot_digest
from src.database import Session
from src.incidents import report
from src.models import (
    Audit, Berth, BerthRequest, DelayClass, DelayIncident, DispatchReview, IncidentState,
    Role, StandbyBatch, StandbyPlanItem, StandbyTugHold, TideWindow, Tug, TugAssignment, User,
)
from src.planning import add_request, add_window, audit, confirm, reserve_tug
from src.schemas import IncidentInput, RequestInput, TideInput
from src.security import password_hash

DEMO_PASSWORD = 'Harbor1074!'
OBSERVED = '2026-11-21T11:25'

# Three vessels are frozen at the same observation minute, so their expected
# standby cover (observed + one manoeuvre) competes for the same tugs. A fourth
# confirmed vessel already holds TG23 across that window, which leaves the
# lowest-ranked case without a tug.
COMPETING = [
    ('BR-414', '海川 9', 'B07', 'TW-073', 16, 14, '引航员通报进港排队，拖轮仍在回转区等待。'),
    ('BR-415', '岚港 12', 'B09', 'TW-092', 12, 10, '引航席报告拖轮交接延误，申请备用拖轮待命。'),
    ('BR-416', '东晖 5', 'B12', 'TW-122', 18, 16, 'VHF 通报泊位前沿风偏大，需拖轮提前待命。'),
]


def seed():
    with Session.begin() as db:
        for username, role in [('operator', Role.OPERATOR), ('supervisor', Role.SUPERVISOR), ('viewer', Role.VIEWER)]:
            if not db.get(User, username):
                db.add(User(username=username, role=role, password_hash=password_hash(DEMO_PASSWORD)))
        for code, label in [('B07', '东七码头'), ('B09', '东九散货泊位'), ('B12', '南十二泊位')]:
            if not db.get(Berth, code):
                db.add(Berth(code=code, label=label))
        for code, label, strength in [('TG12', '海勤 12', 58), ('TG18', '海勤 18', 65), ('TG23', '远协 23', 72)]:
            if not db.get(Tug, code):
                db.add(Tug(code=code, label=label, bollard_pull_t=strength))
        for code, berth, start, end, draft, bulletin in [
            ('TW-071', 'B07', '07:45', '09:10', 11.8, 'TIDE-EAST-1121-AM.pdf'),
            ('TW-072', 'B07', '09:10', '10:45', 13.2, 'TIDE-EAST-1121-AM.pdf'),
            ('TW-091', 'B09', '08:30', '11:40', 12.5, 'TIDE-EAST-1121-AM.pdf'),
            ('TW-121', 'B12', '07:25', '12:10', 13.5, 'TIDE-EAST-1121-AM.pdf'),
            ('TW-073', 'B07', '10:00', '11:30', 13.2, 'TIDE-EAST-1121-PM.pdf'),
            ('TW-092', 'B09', '10:00', '11:30', 12.5, 'TIDE-EAST-1121-PM.pdf'),
            ('TW-093', 'B09', '10:00', '12:00', 12.5, 'TIDE-EAST-1121-PM.pdf'),
            ('TW-122', 'B12', '10:00', '11:30', 13.5, 'TIDE-EAST-1121-PM.pdf'),
        ]:
            if not db.get(TideWindow, code):
                add_window(db, 'supervisor', TideInput(code=code, berth_code=berth,
                           starts_at=f'2026-11-21T{start}', ends_at=f'2026-11-21T{end}',
                           max_draft_m=draft, bulletin=bulletin))
        db.flush()
        for code, vessel, berth, window, eta, minutes, draft in [
            ('BR-410', '海岚 27', 'B07', 'TW-071', '08:50', 45, 12.4),
            ('BR-411', '临海 308', 'B09', 'TW-091', '09:25', 55, 11.8),
            ('BR-412', '松航 16', 'B12', 'TW-121', '11:00', 45, 12.5),
            ('BR-413', '远桥 75', 'B07', 'TW-072', '09:30', 50, 11.9),
        ]:
            if db.get(BerthRequest, code):
                continue
            add_request(db, 'operator', RequestInput(code=code, vessel=vessel, berth_code=berth, window_code=window,
                        eta=f'2026-11-21T{eta}', maneuver_minutes=minutes, draft_m=draft))
            db.flush()
            if code in ('BR-411', 'BR-413'):
                reserve_tug(db, 'operator', code, 'TG12' if code == 'BR-411' else 'TG18')
                confirm(db, 'supervisor', code)
        if not db.get(DelayIncident, 'DL-413'):
            report(db, 'operator', IncidentInput(code='DL-413', request_code='BR-413',
                   radio_reference='VHF-12/0930-19', observed_at='2026-11-21T09:48', wind_kn=14,
                   tug_wait_min=12, event_note='引航员通报拖轮还在回转区，现场请示是否申请待命。'))
        _competing_cases(db)
        _concurrent_booking(db)
        db.flush()
        tables = (User, Berth, Tug, TideWindow, BerthRequest, TugAssignment, DelayIncident, DispatchReview,
                  StandbyBatch, StandbyPlanItem, StandbyTugHold, Audit)
        return {table.__tablename__: db.scalar(select(func.count()).select_from(table)) for table in tables}


def _competing_cases(db):
    """Seed competing standby demand, scored with the production classifier."""
    for index, (code, vessel, berth, window, wind, wait, note) in enumerate(COMPETING):
        if db.get(BerthRequest, code):
            continue
        add_request(db, 'operator', RequestInput(code=code, vessel=vessel, berth_code=berth, window_code=window,
                    eta='2026-11-21T10:30', maneuver_minutes=40, draft_m=11.2 + index / 10))
        db.flush()
        reserve_tug(db, 'operator', code, ('TG12', 'TG18', 'TG23')[index])
        confirm(db, 'supervisor', code)
        winter = db.get(TideWindow, window)
        request = db.get(BerthRequest, code)
        observed = datetime.fromisoformat(OBSERVED)
        features = {
            'eta_offset_min': round((observed - request.eta).total_seconds() / 60, 3),
            'tide_reserve_min': round((winter.ends_at - (request.eta + timedelta(
                minutes=request.maneuver_minutes))).total_seconds() / 60, 3),
            'wind_kn': wind, 'tug_wait_min': wait,
        }
        result = classify(features, snapshot_digest(features))
        if result['prediction'] != DelayClass.PRIORITY or result['needs_review']:
            raise RuntimeError(f'{code} 演示特征未落在优先侧，请调整风速或拖轮等候')
        incident_code = code.replace('BR-', 'DL-')
        db.add(DelayIncident(
            code=incident_code, request_code=code, radio_reference=f'VHF-12/1125-{code[-2:]}',
            observed_at=observed, event_note=note, features=features, input_digest=result['input_digest'],
            result=result, state=IncidentState.READY, decision=DelayClass.PRIORITY,
        ))
        audit(db, 'worker', incident_code, 'MODEL_CLASSIFIED',
              f"f(x)={result['score']:+.5f} · 演示班次争用样本")


def _concurrent_booking(db):
    """A confirmed vessel that already holds TG23 through the competitive window."""
    if db.get(BerthRequest, 'BR-417'):
        return
    add_request(db, 'operator', RequestInput(code='BR-417', vessel='明舟 3', berth_code='B09',
                window_code='TW-093', eta='2026-11-21T11:20', maneuver_minutes=30, draft_m=11.2))
    db.flush()
    reserve_tug(db, 'operator', 'BR-417', 'TG23')
    confirm(db, 'supervisor', 'BR-417')


if __name__ == '__main__':
    print(seed())
