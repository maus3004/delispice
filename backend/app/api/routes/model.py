
from fastapi import APIRouter
router = APIRouter()

@router.get("/")
def run_model():
    return {"prediction": 0}
