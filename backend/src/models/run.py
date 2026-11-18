from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey
from src.config.database import Base
class CorrelationRun(Base):
    __tablename__='runs'
    id=Column(Integer,primary_key=True)
    study_id=Column(Integer,ForeignKey('studies.id'),nullable=False)
    name=Column(String(120),nullable=False)
    status=Column(String(20),default='queued')
    parameters=Column(JSON,nullable=False)
    snapshot=Column(JSON,nullable=True)
    checksum=Column(String(64),nullable=False,default='')
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    purge_task_id=Column(Integer,nullable=True)
    purged_at=Column(String(40),nullable=True)
    created_at=Column(String(40),nullable=False)
Run=CorrelationRun
