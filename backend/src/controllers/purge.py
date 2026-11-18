from src.services import purges
def create(db,data,user): return purges.create(db,data,user)
def list_tasks(db): return purges.list_tasks(db)
def get(db,id): return purges.detail(db,purges.require(db,id))
def retry(db,id,user): return purges.retry(db,id,user)
