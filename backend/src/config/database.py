import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
class Base(DeclarativeBase):
    pass
def _engine_args(url):
    return {'connect_args':{'check_same_thread':False}} if url.startswith('sqlite') else {}
engine = create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True, **_engine_args(os.environ['DATABASE_URL']))
Session = sessionmaker(bind=engine, expire_on_commit=False)
def get_db():
    with Session() as db:
        yield db
