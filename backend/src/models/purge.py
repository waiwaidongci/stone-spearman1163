from sqlalchemy import Column,Integer,String,JSON,Text,UniqueConstraint
from src.config.database import Base
class PurgeTask(Base):
    __tablename__='purge_tasks'
    __table_args__=(UniqueConstraint('study_id','key_fingerprint'),)
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,nullable=False)
    specimen_keys=Column(JSON,nullable=False)
    key_fingerprint=Column(String(64),nullable=False)
    status=Column(String(20),nullable=False,default='accepted')
    affected_run_ids=Column(JSON,nullable=False,default=list)
    created_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
    completed_at=Column(String(40),nullable=True)
    attempts=Column(Integer,nullable=False,default=0)
    removed_observations=Column(Integer,nullable=False,default=0)
    cleared_runs=Column(Integer,nullable=False,default=0)
    removed_files=Column(Integer,nullable=False,default=0)
    error=Column(Text,default='')
Purge=PurgeTask
