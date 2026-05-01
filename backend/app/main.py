
from fastapi import FastAPI
from app.api.routes import stats, model, reports

app = FastAPI()

app.include_router(stats.router, prefix="/api/stats")
app.include_router(model.router, prefix="/api/model")
app.include_router(reports.router, prefix="/api/reports")

@app.get("/")
def root():
    return {"message": "API running"}
