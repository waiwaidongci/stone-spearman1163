import hashlib,json,logging,os
from fastapi import HTTPException
from redis import Redis
from sqlalchemy import select,func,delete
from sqlalchemy.exc import IntegrityError
from src.config.database import Session
from src.models.purge import Purge
from src.models.run import Run
from src.models.observation import Observation
from src.models.study import Study
from src.models.review import Review
from src.models.audit import Audit
from src.services.audits import log
from src.utils.serializers import now,record
from src.services import artifacts

ACCEPTED='accepted';PURGING_DB='purging_db';DB_PURGED='db_purged';PURGING_FILES='purging_files';FILES_PURGED='files_purged';VERIFYING='verifying';COMPLETED='completed';FAILED='failed'
OPEN_STATES=(ACCEPTED,PURGING_DB,DB_PURGED,PURGING_FILES,FILES_PURGED,VERIFYING)
PROGRESS_STEPS=('accepted','database','files','verify')

def fingerprint(study_id,keys):
    payload=json.dumps({'study':study_id,'keys':sorted(keys)},sort_keys=True,ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()

def normalize_keys(keys):
    if not isinstance(keys,(list,tuple)): raise HTTPException(422,'试件编号必须为列表')
    out=[]
    for key in keys:
        if not isinstance(key,str) or not key.strip(): raise HTTPException(422,'试件编号不能为空')
        key=key.strip()
        if len(key)>100: raise HTTPException(422,'试件编号过长')
        if key not in out: out.append(key)
    if not out: raise HTTPException(422,'至少选择一条试件观测')
    if len(out)>5000: raise HTTPException(422,'单次最多清除5000个试件')
    return out

def enqueue(task_id):
    try:
        # Wakeups are cheap and execution is idempotent, so retries cannot fork work.
        Redis.from_url(os.environ['REDIS_URL']).rpush('purge-queue',int(task_id))
        return True
    except Exception:
        logging.exception('purge enqueue failed; worker recovery will retry')
        return False

def _known_keys(db,study_id):
    """Keys are known from live observations, frozen snapshots and earlier tasks,
    so re-submitting the same withdrawal is idempotent even after values are gone."""
    known=set(db.scalars(select(Observation.source_key).where(Observation.study_id==study_id)))
    for snapshot, in db.execute(select(Run.snapshot).where(Run.study_id==study_id)):
        if snapshot: known.update(row.get('source_key') for row in snapshot if isinstance(row,dict))
    for task in db.scalars(select(Purge).where(Purge.study_id==study_id)):
        known.update(task.specimen_keys or [])
    return known

def create(db,data,user):
    keys=normalize_keys(data.source_keys)
    study=db.scalar(select(Study).where(Study.id==data.study_id).with_for_update())
    if not study: raise HTTPException(404,'业务批次不存在')
    digest=fingerprint(study.id,keys)
    existing=db.scalar(select(Purge).where(Purge.study_id==study.id,Purge.key_fingerprint==digest))
    if existing:
        # One accepted withdrawal maps to one task forever; repeat submissions reuse it.
        if existing.status==FAILED:
            existing.status=ACCEPTED;existing.error='';existing.updated_at=now();db.commit();enqueue(existing.id)
        return detail(db,existing,reused=True)
    unknown=[k for k in keys if k not in _known_keys(db,study.id)]
    if unknown: raise HTTPException(422,'试件不属于该批次或从未登记：'+'、'.join(unknown[:10]))
    runs=_affected_runs(db,study.id,set(keys))
    stamp=now()
    task=Purge(study_id=study.id,specimen_keys=keys,key_fingerprint=digest,status=ACCEPTED,
               affected_run_ids=[r.id for r in runs],created_by=user.email,created_at=stamp,updated_at=stamp)
    db.add(task)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing=db.scalar(select(Purge).where(Purge.study_id==study.id,Purge.key_fingerprint==digest))
        return detail(db,existing,reused=True)
    for run in runs: run.purge_task_id=task.id
    log(db,user,'purge.accept',json.dumps({'task':task.id,'study':study.id,'keys':keys,'runs':task.affected_run_ids},ensure_ascii=False))
    db.commit()
    enqueue(task.id)
    return detail(db,task,reused=False)

def require(db,id):
    task=db.get(Purge,id)
    if not task: raise HTTPException(404,'清除任务不存在')
    return task

def retry(db,id,user):
    task=require(db,id)
    if task.status==COMPLETED: return detail(db,task)
    # Retrying rewinds the same task; it never creates a second one.
    task.status=ACCEPTED;task.error='';task.updated_at=now()
    log(db,user,'purge.retry',task.id);db.commit();enqueue(task.id)
    return detail(db,task)

def list_tasks(db):
    return [detail(db,t) for t in db.scalars(select(Purge).order_by(Purge.id.desc()))]

def detail(db,task,reused=None):
    study=db.get(Study,task.study_id)
    runs=[]
    for rid in task.affected_run_ids or []:
        run=db.get(Run,rid)
        runs.append({'id':rid,'name':run.name if run else '已删除','status':run.status if run else 'purged'})
    out=record(task)
    out['study_name']=study.name if study else ''
    out['affected_runs']=runs
    out['progress']=_progress(task)
    out['complete']=task.status==COMPLETED
    if reused is not None: out['reused']=reused
    return out

def _progress(task):
    if task.status==COMPLETED: return '已完成：数据库、历次快照与结果文件核验均为空'
    if task.status==FAILED: return '核验未通过：'+(task.error or '需要重试')
    return {
        ACCEPTED:'已受理：相关研究读取、复核与下载已阻断，等待清理',
        PURGING_DB:'清理数据库中（观测、冻结测值、衍生结果、复核记录）',
        DB_PURGED:'数据库已清，等待清理结果文件',
        PURGING_FILES:'清理结果文件中',
        FILES_PURGED:'结果文件已清，等待核验',
        VERIFYING:'核验数据库与结果文件中',
    }.get(task.status,task.status)

def _snapshot_hits(snapshot,keys):
    return bool(snapshot) and any(row.get('source_key') in keys for row in snapshot if isinstance(row,dict))

def _affected_runs(db,study_id,keys):
    return [run for run in db.scalars(select(Run).where(Run.study_id==study_id).with_for_update())
            if run.status!='purged' and run.purge_task_id is None and _snapshot_hits(run.snapshot,keys)]

def execute(task_id):
    """Idempotent purge worker. Every phase is replayable after an interruption;
    completion is only declared after both the database and the artifact volume verify empty."""
    with artifacts.lock():
        with Session() as db:
            task=db.scalar(select(Purge).where(Purge.id==task_id).with_for_update())
            if not task: return True
            keys=set(task.specimen_keys or [])
            if task.status==COMPLETED:
                # Completion is only reported after a fresh check of every store.
                # If anything reappeared, fall through and clear it again.
                if not keys or not _verify(db,task,keys,task.affected_run_ids or []):
                    return True
                task.completed_at=None;task.updated_at=now();db.commit()
            if not keys:
                task.status=FAILED;task.error='清除范围为空';task.updated_at=now();db.commit();return False
            # Serialize with concurrent edits/run creation on this batch.
            study=db.scalar(select(Study).where(Study.id==task.study_id).with_for_update())
            if not study:
                task.status=FAILED;task.error='批次不存在';task.updated_at=now();db.commit();return False
            task.attempts=(task.attempts or 0)+1;task.status=PURGING_DB;task.updated_at=now();db.commit()
            affected=list(task.affected_run_ids or [])
            # Catch runs frozen between acceptance and execution.
            for run in _affected_runs(db,study.id,keys):
                if run.id not in affected:
                    affected.append(run.id);run.purge_task_id=task.id
            task.affected_run_ids=affected
            removed=db.execute(delete(Observation).where(Observation.study_id==study.id,Observation.source_key.in_(keys))).rowcount
            cleared=0
            for rid in affected:
                run=db.scalar(select(Run).where(Run.id==rid).with_for_update())
                if not run or run.study_id!=study.id: continue
                db.execute(delete(Review).where(Review.run_id==rid))
                # Review notes may quote measured values; keep only a value-free trace.
                for note in db.scalars(select(Audit).where(Audit.action=='review.approve',Audit.detail.like(f'{rid}:%'))):
                    note.detail=f'{rid}: [复核内容随测值清除]'
                run.snapshot=None;run.result=None;run.checksum='';run.error=''
                run.status='purged';run.purged_at=now();run.purge_task_id=task.id
                cleared+=1
            task.removed_observations=max(task.removed_observations or 0,removed)
            task.cleared_runs=cleared
            task.status=DB_PURGED;task.updated_at=now();db.commit()

            task.status=PURGING_FILES;task.updated_at=now();db.commit()
            removed_files=0
            for rid in affected:
                for path in artifacts.files_for(rid):
                    try: path.unlink();removed_files+=1
                    except FileNotFoundError: pass
            task.removed_files=max(task.removed_files or 0,removed_files)
            task.status=FILES_PURGED;task.updated_at=now();db.commit()

            task.status=VERIFYING;task.updated_at=now();db.commit()
            problems=_verify(db,task,keys,affected)
            if problems:
                task.status=FAILED;task.error='；'.join(problems[:5]);task.updated_at=now();db.commit()
                return False
            task.status=COMPLETED;task.completed_at=now();task.updated_at=now();task.error=''
            log(db,'system','purge.complete',f'{task.id} study={task.study_id} keys={len(keys)} runs={len(affected)}')
            db.commit()
            return True

def _verify(db,task,keys,affected):
    """Completion requires every store to verify empty: observations, frozen snapshots,
    derived results, review notes and result files."""
    problems=[]
    left=db.scalar(select(func.count()).select_from(Observation).where(Observation.study_id==task.study_id,Observation.source_key.in_(keys)))
    if left: problems.append(f'仍有{left}条观测')
    for rid in affected:
        run=db.get(Run,rid)
        if run and (run.snapshot is not None or run.result is not None or run.checksum or run.error):
            problems.append(f'运行{rid}仍含测值或衍生结果')
        if db.scalar(select(Review.id).where(Review.run_id==rid)): problems.append(f'运行{rid}复核记录未清除')
        if artifacts.files_for(rid): problems.append(f'运行{rid}结果文件未清空')
    for run in db.scalars(select(Run).where(Run.study_id==task.study_id)):
        if run.status!='purged' and _snapshot_hits(run.snapshot,keys):
            problems.append(f'运行{run.id}快照仍含已撤回试件')
    return problems

def recover_open():
    """Re-enqueue unfinished tasks after a restart. Replays are idempotent."""
    with Session() as db:
        ids=list(db.scalars(select(Purge.id).where(Purge.status.in_(OPEN_STATES)).order_by(Purge.id)))
    for task_id in ids: enqueue(task_id)
    return ids

def fail(task_id,message):
    with Session() as db:
        task=db.scalar(select(Purge).where(Purge.id==task_id).with_for_update())
        if not task or task.status==COMPLETED: return
        task.status=FAILED;task.error=str(message)[:2000];task.updated_at=now();db.commit()
