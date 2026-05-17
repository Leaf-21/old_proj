from fastapi import APIRouter
from app.api.endpoints import upload_job

api_router = APIRouter()
api_router.include_router(upload_job.router, prefix="/jobs", tags=["jobs"])
