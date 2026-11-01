"""Explicit human reconciliation of an unknown scheduled external outcome."""
import json
from sqlalchemy import select, update
from .jobs import jobs, get_job, render
from .store import items, transaction, seal, unseal, now


def resolve(pid,run_id,actor_id,outcome,note):
    if outcome not in ('executed','not_executed') or not isinstance(note,str) or len(note.strip())<4 or len(note)>500:
        raise ValueError('请确认执行状态，并填写至少四字的外部回执核对依据')
    note=note.strip();initial=get_job(run_id)
    if initial.get('kind') not in ('pipeline','data_workflow','business_flow') or not isinstance(initial.get('schedule_snapshot'),dict):
        raise ValueError('该运行不是可核对的自动更新任务')
    kind=initial['kind'];target_id=initial['target']
    with transaction() as c:
        target_row=c.execute(select(items.c.body).where(items.c.id==target_id,items.c.kind==kind).with_for_update()).first()
        row=c.execute(select(jobs).where(jobs.c.id==run_id).with_for_update()).mappings().first()
        if not target_row or not row or row['target']!=target_id or row['kind']!=kind:
            raise ValueError('运行所属流程已变化')
        target=json.loads(target_row[0]);run=render(row);snap=run.get('schedule_snapshot')
        if (target.get('project_id')!=pid or not isinstance(snap,dict) or snap.get('project_id')!=pid
                or snap.get('kind')!=kind or (kind=='business_flow' and target.get('actor_id')!=actor_id)):
            raise ValueError('运行不属于当前项目或使用者')
        if snap.get('legacy_unattributed') is True:
            if not target.get('schedule_generation') or target.get('schedule_actor_id')!=actor_id:
                raise ValueError('旧版运行未记录设置者；请由有工作区权限的负责人先重新设置定时，再核对外部回执。重新设置不会自动启用')
        elif snap.get('actor_id')!=actor_id:
            raise ValueError('只有该计划的设置者可以核对这次运行')
        prior=run.get('schedule_resolution')
        if isinstance(prior,dict):
            if (prior.get('actor_id')!=actor_id or prior.get('outcome')!=outcome or
                    unseal(prior.get('secret_note',''))!=note):
                raise ValueError('该运行已有不同的核对记录，请重新查看')
            return {'run_id':run_id,'resolved':True,'resume_required':True,'outcome':outcome,
                    'remaining_count':len(target.get('schedule_uncertain_runs') or ([] if not target.get('schedule_uncertain_run') else [target['schedule_uncertain_run']]))}
        if target.get('schedule_uncertain_run')!=run_id:
            raise ValueError('当前待核对的运行已变化，请刷新后核对')
        if run.get('status') not in ('failed','cancelled'):
            raise ValueError('运行尚未结束，不能记录执行结论')
        receipt={'actor_id':actor_id,'outcome':outcome,'secret_note':seal(note),'reviewed_at':now()}
        run['schedule_resolution']=receipt
        pending=target.get('schedule_uncertain_runs')
        if not isinstance(pending,list):pending=[run_id]
        pending=[value for value in pending if value!=run_id]
        target['schedule_uncertain_runs']=pending
        target['schedule_uncertain_run']=pending[0] if pending else None
        # Reconfiguration is a distinct deliberate action, so this receipt
        # never starts or replays any previously due occurrence.
        target['schedule_resume_required']=True
        field='schedule_error' if kind=='pipeline' else 'refresh_error'
        target[field]=('仍有运行 '+pending[0]+' 的外部操作状态待核对，自动更新保持停止' if pending else
                       '已记录上次运行的外部回执核对；如需继续自动更新，请重新设置定时')
        c.execute(update(jobs).where(jobs.c.id==run_id).values(body=json.dumps(run,ensure_ascii=False,default=str)))
        c.execute(update(items).where(items.c.id==target_id,items.c.kind==kind).values(body=json.dumps(target,ensure_ascii=False,default=str)))
    return {'run_id':run_id,'resolved':True,'resume_required':True,'outcome':outcome,'remaining_count':len(pending)}
