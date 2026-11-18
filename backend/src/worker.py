import os,time,json,logging,threading
from pathlib import Path
from redis import Redis
from sqlalchemy import select
from src.config.database import Session
from src.models.run import Run
from src.models import user,study,observation,review,audit,purge  # noqa: F401
from src.services.algorithm import calculate
from src.services import artifacts,purges
from src.services.guards import contains_blocked
from src.utils.serializers import checksum

redis=Redis.from_url(os.environ['REDIS_URL'])
ARTIFACTS=artifacts.ARTIFACTS


def recover_runs():
    """Jobs interrupted after dequeue are requeued; a project runs exactly one worker."""
    with Session() as db:
        pending=list(db.scalars(select(Run).where(Run.status.in_(['queued','running']))))
    for run in pending: redis.rpush('analysis-queue',run.id)
    return [run.id for run in pending]


def analysis_once(run_id):
    with Session() as db:
        run=db.get(Run,run_id)
        if not run or run.status in ('completed','failed','purged'): return False
        if run.purge_task_id is not None or contains_blocked(db,run.study_id,run.snapshot):
            # Withdrawn input: never compute, never persist values. The purge task clears the snapshot.
            return False
        run.status='running';db.commit()
        try:
            if checksum(run.snapshot)!=run.checksum: raise ValueError('输入快照校验失败')
            result=calculate(run.snapshot,run.parameters)
        except Exception as e:
            logging.exception('analysis failed')
            db.expire_all();run=db.get(Run,run_id)
            if not run or run.status=='purged' or run.purge_task_id is not None: return False
            run.status='failed';run.error=str(e);db.commit();return False
        result['input_sha256']=run.checksum
        # The artifact lock makes the late-arriving write atomic with respect to a purge:
        # a run purged meanwhile is dropped, never resurrected.
        with artifacts.lock():
            db.expire_all();run=db.get(Run,run_id)
            if not run or run.status=='purged' or run.purge_task_id is not None or run.purged_at:
                return False
            try:
                artifacts.run_file(run_id).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False))
            except Exception:
                logging.exception('artifact write failed');run.status='failed';run.error='结果文件写入失败';db.commit();return False
            run.result=result;run.status='completed';run.error='';db.commit()
    return True


def purge_once(task_id):
    try:
        return purges.execute(int(task_id))
    except Exception as e:
        logging.exception('purge failed');purges.fail(task_id,str(e));return False


def purge_loop():
    while True:
        try:
            job=redis.blpop('purge-queue',timeout=2)
            if job: purge_once(int(job[1]))
        except Exception:
            logging.exception('purge loop error');time.sleep(2)


def analysis_loop():
    while True:
        Path('/tmp/worker-heartbeat').touch()
        job=redis.blpop('analysis-queue',timeout=2)
        if not job: continue
        try: analysis_once(int(job[1]))
        except Exception: logging.exception('analysis loop error')


def main():
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    recover_runs()
    # Unfinished purges resume from their last durable phase; replay is idempotent.
    purges.recover_open()
    threading.Thread(target=purge_loop,daemon=True).start()
    analysis_loop()


if __name__=='__main__': main()
