from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
HOTSPOT_ROLES=set(['field_commander'])
SMOKE_STATUSES=['smoking', 'clear']
HOTSPOT_TEMP_LIMIT=80.0
MOPUP_STATE='contained'; CONTROLLED_STATE='controlled'
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
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
def effective_surface_temp(hotspot):
    # 降温观测经另一名巡线员复核后以复核值为准，原登记记录保留但不参与最高温
    if hotspot.get('cooling_temp') is not None and hotspot.get('review_by'):
        return float(hotspot['cooling_temp'])
    return float(hotspot['surface_temp'])
def smoke_retest_pending(hotspot):
    # 登记/更正时仍有烟，且尚无复测结果的点为待复测点
    return hotspot.get('smoke_status')=='smoking' and not hotspot.get('smoke_retest_status')
def mop_up_blockers(hotspots):
    blockers=[]
    over_lines=sorted({h['fireline'] for h in hotspots if effective_surface_temp(h)>HOTSPOT_TEMP_LIMIT})
    if over_lines:
        blockers.append("火线{}仍有超过80℃的热点".format("、".join(over_lines)))
    pending=[h['ticket_no'] for h in hotspots if smoke_retest_pending(h)]
    if pending:
        blockers.append("现场单号{}的烟点尚未复测".format("、".join(sorted(pending))))
    unreviewed=[h['ticket_no'] for h in hotspots if h.get('cooling_temp') is not None and not h.get('review_by')]
    if unreviewed:
        blockers.append("现场单号{}的降温结果尚未复核".format("、".join(sorted(unreviewed))))
    return blockers
def guard_blockers(target,hotspots):
    # 看守阶段宣布控制前：超温点、待复测烟点、未复核降温任一存在，火场停在原状态
    return mop_up_blockers(hotspots) if target==CONTROLLED_STATE else []
def fireline_summary(hotspots):
    lines={}
    for h in hotspots:
        line=lines.setdefault(h['fireline'], {'fireline':h['fireline'],'max_temp':None,'pending_retest':0,'pending_retest_tickets':[],'hotspot_count':0})
        temp=effective_surface_temp(h)
        line['max_temp']=temp if line['max_temp'] is None else max(line['max_temp'],temp)
        line['hotspot_count']+=1
        if smoke_retest_pending(h):
            line['pending_retest']+=1
            line['pending_retest_tickets'].append(h['ticket_no'])
    return [lines[key] for key in sorted(lines)]
