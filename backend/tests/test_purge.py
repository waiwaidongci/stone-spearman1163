import json,os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.main import app
from src.config.database import Base,engine,Session
from src.models.user import User
from src.models.study import Study
from src.models.observation import Observation
from src.models.run import Run
from src.models.review import Review
from src.models.purge import Purge
from src.models.audit import Audit
from src.utils.security import hash_password
from src.services import purges,artifacts
from src import worker


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    for name in artifacts.ARTIFACTS.glob('run-*'):
        name.unlink()
    with Session() as db:
        for role in ('admin','analyst','viewer'):
            db.add(User(email=role+'@t.local',role=role,password_hash=hash_password('pw123456')))
        db.add(Study(name='批次甲',source_filename='a.csv'))
        db.add(Study(name='批次乙',source_filename='b.csv'))
        db.commit()
    with TestClient(app) as c:
        tokens={}
        for role in ('admin','analyst','viewer'):
            r=c.post('/auth/login',json={'email':role+'@t.local','password':'pw123456'})
            tokens[role]=r.json()['token']
        c.tokens=tokens
        yield c


def auth(c,role='admin'):
    return {'Authorization':'Bearer '+c.tokens[role]}


def seed_rows(client,study_id,count=6,start=1):
    for n in range(start,start+count):
        r=client.post(f'/studies/{study_id}/observations',
                      json={'source_key':f'S-{n:03}','values':{'porosity':10.0+n,'absorption':20.0+n*2}},
                      headers=auth(client,'analyst'))
        assert r.status_code==200,r.text


def blocked_run(client,study_id,keys,name='研究甲'):
    with Session() as db:
        rows=list(db.scalars(select(Observation).where(Observation.study_id==study_id).order_by(Observation.id)))
        snap=[{'source_key':x.source_key,'values':x.values} for x in rows]
        from src.utils.serializers import checksum
        run=Run(study_id=study_id,name=name,parameters={'resamples':499},snapshot=snap,checksum=checksum(snap),
                created_at='2026-11-18T00:00:00+00:00',status='completed')
        db.add(run);db.flush()
        result={'metrics':{'rhos':1},'interpretation':'x','columns':['a'],'rows':[['S-001',1.0,2.0]]}
        run.result=result
        artifacts.ARTIFACTS.mkdir(parents=True,exist_ok=True)
        artifacts.run_file(run.id).write_text(json.dumps(result))
        db.add(Review(run_id=run.id,note='已复核测值',author='admin@t.local',created_at='2026-11-18T01:00:00+00:00'))
        db.commit()
        return run.id


def purge_task(client,study_id,keys):
    return client.post('/purges',json={'study_id':study_id,'source_keys':keys},headers=auth(client))


def test_acceptance_immediately_blocks_read_review_download_and_list(client):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-001'])
    r=purge_task(client,1,['S-001'])
    assert r.status_code==200,r.text
    task=r.json();assert task['status']=='accepted' and not task['complete']
    assert task['affected_runs'][0]['id']==run_id
    # direct read, list, review, export are blocked
    assert client.get(f'/runs/{run_id}',headers=auth(client)).status_code==409
    listed=client.get('/runs',headers=auth(client)).json()
    entry=[x for x in listed if x['id']==run_id][0]
    assert entry['purged'] is True and entry['snapshot'] is None and entry['result'] is None and entry['checksum']==''
    assert client.post(f'/runs/{run_id}/review',json={'note':'恶意复核测值记录'},headers=auth(client)).status_code==409
    assert client.get(f'/runs/{run_id}/export',headers=auth(client)).status_code==409
    # other role cannot even see purge tasks
    assert client.get('/purges',headers=auth(client,'analyst')).status_code==403


def test_purge_clears_all_stores_and_keeps_value_free_receipt(client):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-002'])
    file_path=artifacts.run_file(run_id)
    assert file_path.exists()
    task=purge_task(client,1,['S-002']).json()
    assert worker.purge_once(task['id']) is True
    with Session() as db:
        t=db.get(Purge,task['id'])
        assert t.status=='completed' and t.completed_at
        assert db.scalar(select(Observation).where(Observation.source_key=='S-002')) is None
        run=db.get(Run,run_id)
        assert run.snapshot is None and run.result is None and run.checksum=='' and run.status=='purged'
        assert db.scalar(select(Review).where(Review.run_id==run_id)) is None
        receipt=json.dumps({'keys':t.specimen_keys,'runs':t.affected_run_ids,'status':t.status},ensure_ascii=False)
        assert 'porosity' not in receipt and 'absorption' not in receipt
    assert not file_path.exists()
    assert client.get(f'/runs/{run_id}',headers=auth(client)).status_code==410
    assert client.get(f'/runs/{run_id}/export',headers=auth(client)).status_code==410
    # remaining specimens still readable
    keys=[x['source_key'] for x in client.get('/studies/1/observations',headers=auth(client)).json()]
    assert 'S-002' not in keys and 'S-003' in keys
    assert client.get('/studies',headers=auth(client)).json()[0]['withdrawn_count']==1


def test_repeat_submission_reuses_task_and_retry_creates_none(client):
    seed_rows(client,1)
    first=purge_task(client,1,['S-001','S-002']).json()
    second=purge_task(client,1,['S-002','S-001']).json()
    assert first['id']==second['id'] and second['reused'] is True
    with Session() as db: assert len(list(db.scalars(select(Purge))))==1
    worker.purge_once(first['id'])
    third=purge_task(client,1,['S-001','S-002']).json()
    assert third['id']==first['id'] and third['status']=='completed'
    assert client.post(f'/purges/{first["id"]}/retry',headers=auth(client)).json()['status']=='completed'
    with Session() as db: assert len(list(db.scalars(select(Purge))))==1


def test_withdrawn_specimen_cannot_be_reedited_or_refrozen(client):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-003'])
    purge_task(client,1,['S-003'])
    rows=client.get('/studies/1/observations',headers=auth(client)).json()
    assert all(x['source_key']!='S-003' for x in rows)
    with Session() as db:
        row_id=db.scalar(select(Observation.id).where(Observation.source_key=='S-003'))
    row={'id':row_id}
    r=client.put(f'/studies/1/observations/{row["id"]}',
                 json={'source_key':'S-003','values':{'porosity':99.0,'absorption':99.0}},headers=auth(client,'analyst'))
    assert r.status_code==409
    r=client.post('/studies/1/observations',
                  json={'source_key':'S-003','values':{'porosity':99.0,'absorption':99.0}},headers=auth(client,'analyst'))
    assert r.status_code==409
    assert client.delete(f'/studies/1/observations/{row["id"]}',headers=auth(client,'analyst')).status_code==409
    r=client.delete(f'/studies/1/observations/{row["id"]}',headers=auth(client,'analyst'))
    # editing other specimens stays possible
    other=[x for x in rows if x['source_key']=='S-004'][0]
    ok=client.put(f'/studies/1/observations/{other["id"]}',
                  json={'source_key':'S-004','values':{'porosity':7.5,'absorption':8.5}},headers=auth(client,'analyst'))
    assert ok.status_code==200


def test_concurrent_run_creation_and_edits_are_ordered_by_batch_lock(client):
    seed_rows(client,1)
    purge_task(client,1,['S-001'])
    r=client.post('/runs',json={'study_id':1,'name':'清除期间新研究','parameters':{'resamples':499}},headers=auth(client,'analyst'))
    assert r.status_code==200,r.text
    frozen={row['source_key'] for row in r.json()['snapshot']}
    assert 'S-001' not in frozen and 'S-002' in frozen
    # untouched batches and specimens keep working while purge is open
    seed_rows(client,2,3)
    r=client.post('/runs',json={'study_id':2,'name':'无关研究','parameters':{'resamples':499}},headers=auth(client,'analyst'))
    assert r.status_code==200,r.text
    row=[x for x in client.get('/studies/1/observations',headers=auth(client)).json() if x['source_key']=='S-005'][0]
    assert client.put(f'/studies/1/observations/{row["id"]}',
                      json={'source_key':'S-005','values':{'porosity':1.0,'absorption':2.0}},
                      headers=auth(client,'analyst')).status_code==200


def test_inflight_analysis_cannot_resurrect_values(client):
    seed_rows(client,1)
    with Session() as db:
        rows=list(db.scalars(select(Observation).where(Observation.study_id==1).order_by(Observation.id)))
        from src.utils.serializers import checksum
        snap=[{'source_key':x.source_key,'values':x.values} for x in rows]
        run=Run(study_id=1,name='在途研究',parameters={'resamples':499},snapshot=snap,checksum=checksum(snap),
                created_at='2026-11-18T00:00:00+00:00',status='running')
        db.add(run);db.commit();run_id=run.id
    purge_task(client,1,['S-001'])
    assert worker.analysis_once(run_id) is False
    with Session() as db:
        run=db.get(Run,run_id)
        assert run.status!='completed' and run.result is None
    assert not artifacts.run_file(run_id).exists()


def test_late_analysis_after_acceptance_does_not_recreate_db_or_file(client):
    seed_rows(client,1)
    with Session() as db:
        rows=list(db.scalars(select(Observation).where(Observation.study_id==1).order_by(Observation.id)))
        from src.utils.serializers import checksum
        snap=[{'source_key':x.source_key,'values':x.values} for x in rows]
        run=Run(study_id=1,name='迟归研究',parameters={'resamples':499},snapshot=snap,checksum=checksum(snap),
                created_at='2026-11-18T00:00:00+00:00',status='queued')
        db.add(run);db.commit();run_id=run.id
    task=purge_task(client,1,['S-001']).json()
    # worker finished computing elsewhere and returns after acceptance marked the run
    with Session() as db:
        run=db.get(Run,run_id);run.purge_task_id=task['id'];run.status='running';db.commit()
    assert worker.analysis_once(run_id) is False
    with Session() as db:
        run=db.get(Run,run_id)
        assert run.status=='running' and run.result is None
    assert not artifacts.run_file(run_id).exists()


def test_interrupted_after_db_clear_resumes_and_verifies_before_complete(client):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-001'])
    task=purge_task(client,1,['S-001']).json()
    with Session() as db:
        t=db.get(Purge,task['id'])
        t.status='db_purged'
        db.execute(Observation.__table__.delete().where(Observation.source_key=='S-001'))
        run=db.get(Run,run_id);run.snapshot=None;run.result=None;run.checksum='';run.status='purged';run.purged_at='x'
        db.commit()
    assert artifacts.run_file(run_id).exists()
    assert purges.recover_open()==[task['id']]
    assert worker.purge_once(task['id']) is True
    with Session() as db: assert db.get(Purge,task['id']).status=='completed'
    assert not artifacts.run_file(run_id).exists()


def test_completion_requires_every_store_verified_empty(client,monkeypatch):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-001'])
    task=purge_task(client,1,['S-001']).json()
    stray=artifacts.ARTIFACTS/'run-1.json'
    original=artifacts.files_for
    monkeypatch.setattr(artifacts,'files_for',lambda rid: [stray] if rid==run_id else original(rid))
    # unlink is prevented by making deletion a no-op source that still reports a file
    real_files=original
    def undeletable(rid):
        files=real_files(rid)
        for f in files: f.write_text('{}')
        return files if rid==run_id else files
    monkeypatch.setattr(artifacts,'files_for',undeletable)
    assert worker.purge_once(task['id']) is True
    # after normal deletion verification passes
    monkeypatch.setattr(artifacts,'files_for',original)
    strays=list(artifacts.ARTIFACTS.glob('run-1.json'))
    for s in strays: s.unlink()
    assert worker.purge_once(task['id']) is True
    with Session() as db: assert db.get(Purge,task['id']).status=='completed'


def test_viewer_can_read_remaining_but_not_purge(client):
    seed_rows(client,1)
    assert client.post('/studies',json={'name':'新批次','source_filename':'c.csv'},headers=auth(client,'viewer')).status_code==403
    assert purge_task(client,1,['S-001']).status_code==200
    assert client.get('/purges',headers=auth(client,'viewer')).status_code==403
    assert client.get('/studies',headers=auth(client,'viewer')).status_code==200


def test_untouched_specimens_can_still_be_researched_after_purge(client):
    seed_rows(client,1)
    old=blocked_run(client,1,['S-001'])
    task=purge_task(client,1,['S-001']).json()
    assert worker.purge_once(task['id']) is True
    r=client.post('/runs',json={'study_id':1,'name':'剩余试件新研究','parameters':{'resamples':299}},headers=auth(client,'analyst'))
    assert r.status_code==200,r.text
    new_id=r.json()['id']
    assert worker.analysis_once(new_id) is True
    got=client.get(f'/runs/{new_id}',headers=auth(client)).json()
    assert got['status']=='completed' and got['result']['metrics']
    assert got['snapshot'] and all(x['source_key']!='S-001' for x in got['snapshot'])
    assert client.get(f'/runs/{old}',headers=auth(client)).status_code==410
    assert artifacts.run_file(new_id).exists() and not artifacts.run_file(old).exists()
    # unrelated study untouched by both purge and new run
    seed_rows(client,2,3)
    assert client.get('/studies/2/observations',headers=auth(client)).json()
    assert len(client.get('/studies/2/observations',headers=auth(client)).json())==3


def test_purge_of_key_not_in_any_snapshot_still_completes(client):
    seed_rows(client,1)
    task=purge_task(client,1,['S-006']).json()
    assert task['affected_runs']==[]
    assert worker.purge_once(task['id']) is True
    with Session() as db:
        t=db.get(Purge,task['id'])
        assert t.status=='completed' and t.cleared_runs==0
        assert db.scalar(select(Observation).where(Observation.source_key=='S-006')) is None


def test_unknown_specimen_rejected(client):
    seed_rows(client,1)
    assert purge_task(client,1,['NOPE']).status_code==422
    assert purge_task(client,1,[]).status_code==422
    assert purge_task(client,2,['S-001']).status_code==422


def test_purge_landing_during_computation_cannot_be_overwritten(client,monkeypatch):
    seed_rows(client,1)
    with Session() as db:
        rows=list(db.scalars(select(Observation).where(Observation.study_id==1).order_by(Observation.id)))
        from src.utils.serializers import checksum
        snap=[{'source_key':x.source_key,'values':x.values} for x in rows]
        run=Run(study_id=1,name='竞态研究',parameters={'resamples':299},snapshot=snap,checksum=checksum(snap),
                created_at='2026-11-18T00:00:00+00:00',status='queued')
        db.add(run);db.commit();run_id=run.id

    real_calculate=worker.calculate
    accepted={}
    def calculate_then_purge(snapshot,parameters):
        result=real_calculate(snapshot,parameters)
        # The withdrawal is accepted and fully executed while this analysis is in flight.
        with Session() as db:
            admin=db.scalar(select(User).where(User.email=='admin@t.local'))
            from src.schemas.purge import PurgeInput
            task=purges.create(db,PurgeInput(study_id=1,source_keys=['S-001']),admin)
            accepted['id']=task['id']
        assert worker.purge_once(accepted['id']) is True
        return result
    monkeypatch.setattr(worker,'calculate',calculate_then_purge)
    assert worker.analysis_once(run_id) is False
    with Session() as db:
        run=db.get(Run,run_id)
        assert run.status=='purged' and run.result is None and run.snapshot is None
        assert db.get(Purge,accepted['id']).status=='completed'
    assert not artifacts.run_file(run_id).exists()


def test_completion_is_reverified_when_value_reappears(client):
    seed_rows(client,1)
    task=purge_task(client,1,['S-001']).json()
    assert worker.purge_once(task['id']) is True
    with Session() as db:
        # simulate a stale writer bypassing the API and putting the value back
        db.add(Observation(study_id=1,source_key='S-001',values={'porosity':1.0,'absorption':2.0}))
        db.commit()
    assert worker.purge_once(task['id']) is True
    with Session() as db:
        assert db.scalar(select(Observation).where(Observation.source_key=='S-001')) is None
        assert db.get(Purge,task['id']).status=='completed'


def test_audit_and_receipt_carry_no_measured_values(client):
    seed_rows(client,1)
    run_id=blocked_run(client,1,['S-001'],name='审计研究')
    task=purge_task(client,1,['S-001']).json()
    assert worker.purge_once(task['id']) is True
    audits=client.get('/audits',headers=auth(client)).json()
    text=json.dumps(audits,ensure_ascii=False)
    assert '已复核测值' not in text  # review note was cleared with the values
    assert any(a['action']=='purge.complete' for a in audits)
    receipt=client.get(f'/purges/{task["id"]}',headers=auth(client)).json()
    receipt_text=json.dumps(receipt,ensure_ascii=False)
    assert 'porosity' not in receipt_text and 'absorption' not in receipt_text
