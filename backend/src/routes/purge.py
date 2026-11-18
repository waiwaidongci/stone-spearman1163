from fastapi import APIRouter,Depends
from src.config.database import get_db
from src.middlewares.auth import admin
from src.schemas.purge import PurgeInput
from src.controllers import purge as controller
router=APIRouter()
@router.get('/purges')
def list_tasks(db=Depends(get_db),user=Depends(admin)): return controller.list_tasks(db)
@router.post('/purges')
def create(data:PurgeInput,db=Depends(get_db),user=Depends(admin)): return controller.create(db,data,user)
@router.get('/purges/{id}')
def get(id:int,db=Depends(get_db),user=Depends(admin)): return controller.get(db,id)
@router.post('/purges/{id}/retry')
def retry(id:int,db=Depends(get_db),user=Depends(admin)): return controller.retry(db,id,user)
