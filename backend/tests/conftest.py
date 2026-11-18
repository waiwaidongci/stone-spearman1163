import os,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
_TMP=tempfile.mkdtemp(prefix='stone-test-')
os.environ['DATABASE_URL']='sqlite:///'+_TMP+'/test.db'
os.environ['REDIS_URL']='redis://localhost:6399/0'
os.environ['JWT_SECRET']='test-secret'
os.environ['ARTIFACTS_DIR']=_TMP+'/artifacts'
os.environ['SEED_DEMO']='false'
import fakeredis,redis
_SERVER=fakeredis.FakeServer()
redis.Redis.from_url=classmethod(lambda cls,*a,**k: fakeredis.FakeRedis(server=_SERVER,decode_responses=False))
