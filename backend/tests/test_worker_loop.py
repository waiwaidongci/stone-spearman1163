import threading,time,json
from sqlalchemy import select
from src.config.database import Session
from src.models.purge import Purge
from src import worker


def wait(predicate,timeout=8):
    end=time.time()+timeout
    while time.time()<end:
        if predicate(): return True
        time.sleep(.05)
    return False


def test_worker_loops_drain_purge_and_analysis():
    worker.redis.delete('purge-queue','analysis-queue')
    thread=threading.Thread(target=worker.purge_loop,daemon=True);thread.start()
    analysis=threading.Thread(target=worker.analysis_loop,daemon=True);analysis.start()
    time.sleep(.2)
    assert threading.active_count()>=3
    start=time.time();worker.redis.lpush('analysis-queue',0)
    while time.time()-start<2 and worker.redis.llen('analysis-queue'): time.sleep(.05)
