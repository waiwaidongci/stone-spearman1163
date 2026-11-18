import os
from fastapi import HTTPException
from sqlalchemy import select
from src.models.purge import Purge
from src.models.run import Run
OPEN=('accepted','purging_db','db_purged','purging_files','files_purged','verifying')

def open_tasks(db,study_id=None):
    q=select(Purge).where(Purge.status.in_(OPEN))
    if study_id is not None: q=q.where(Purge.study_id==study_id)
    return list(db.scalars(q))

def blocked_keys(db,study_id):
    """Keys whose values are withdrawn now or were permanently purged before."""
    out=set()
    for task in db.scalars(select(Purge).where(Purge.study_id==study_id)):
        out.update(task.specimen_keys or [])
    return out

def accepted_keys(db,study_id):
    out=set()
    for task in open_tasks(db,study_id): out.update(task.specimen_keys or [])
    return out

def contains_blocked(db,study_id,snapshot):
    if not snapshot: return False
    keys=blocked_keys(db,study_id)
    return any(row.get('source_key') in keys for row in snapshot if isinstance(row,dict))

def is_purged(run):
    return run.status=='purged' or bool(run.purged_at)

def should_block(db,run):
    return is_purged(run) or run.purge_task_id is not None or contains_blocked(db,run.study_id,run.snapshot)

def assert_run_readable(db,run):
    if is_purged(run):
        raise HTTPException(410,'该研究已执行数据清除，测值与结果文件不可恢复')
    if run.purge_task_id is not None or contains_blocked(db,run.study_id,run.snapshot):
        raise HTTPException(409,'该研究涉及被撤回测值，已阻断读取、复核与下载，正在清除')

def sanitized(run):
    from src.utils.serializers import record
    out=record(run)
    out['snapshot']=None;out['result']=None;out['checksum']='';out['error']=''
    out['review']=None;out['purged']=True
    return out
