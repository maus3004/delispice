
from fastapi import APIRouter
router = APIRouter()

@router.get("/")
def get_stats():
    return {"stats": []}
