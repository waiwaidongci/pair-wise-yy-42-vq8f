from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
# 火场看守与复燃判定
HOTSPOT_SMOKE_STATES=['smoke', 'unknown', 'clear']; HOTSPOT_TEMP_LIMIT=80.0
HOTSPOT_REGISTER_ROLES=set(['field_commander']); HOTSPOT_REVIEW_ROLES=set(['field_commander']); HOTSPOT_CORRECT_ROLES=set(['incident_commander', 'field_commander'])
MOPUP_GATED_TARGETS=set(['controlled', 'closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def validate_smoke_state(value):
    if value not in HOTSPOT_SMOKE_STATES: raise ValidationError("smoke_state不在允许范围内")
    return value
def effective_temperature(spot):
    """降温结果未复核前不计入复燃判定，仍以登记地表温度为准。"""
    if spot.get('reviewed_by') is not None:
        return float(spot['latest_temperature'])
    return float(spot['surface_temperature'])
def pending_retest(spot):
    """降温复核时烟点必须复测为无烟，否则仍需复测。"""
    if spot.get('reviewed_by') is not None:
        return spot['latest_smoke'] != 'clear'
    return spot['smoke_state'] != 'clear'
def line_summary(hotspots):
    """汇总一条火线的最高温（按有效温度）与待复测点编号。"""
    pending=[s['ticket_no'] for s in hotspots if pending_retest(s)]
    max_temp=max((effective_temperature(s) for s in hotspots), default=None)
    return {'max_temperature': max_temp, 'pending_retest': pending}
def _unreviewed_cooling(spot):
    return spot.get('cooling_observed_by') is not None and spot.get('reviewed_by') is None
def mopup_blockers(lines):
    """任一火线存在超温热点、烟点未复测或降温结果未复核时，火场不得报已控。"""
    blockers=[]
    for line_id, hotspots in lines.items():
        summary=line_summary(hotspots)
        if summary['max_temperature'] is not None and summary['max_temperature'] > HOTSPOT_TEMP_LIMIT:
            blockers.append(f"火线{line_id}存在超过{HOTSPOT_TEMP_LIMIT:.0f}℃的热点")
        if summary['pending_retest']:
            blockers.append(f"火线{line_id}有烟点未复测：{','.join(summary['pending_retest'])}")
        unreviewed=[s['ticket_no'] for s in hotspots if _unreviewed_cooling(s)]
        if unreviewed:
            blockers.append(f"火线{line_id}降温结果未复核：{','.join(unreviewed)}")
    return blockers
def mopup_blockers_for_target(target, lines):
    return mopup_blockers(lines) if target in MOPUP_GATED_TARGETS else []
