from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.core.config import settings
from app.api.api import api_router
from app.core.logging import get_logger
from app.db.base import Base
import json as _json
import os
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = get_logger("main")

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# Set all CORS enabled origins
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.API_V1_STR)

# ---------- Auth proxy (delegates to main Mooctest backend at 8980) ----------
MOOCTEST_AUTH_VERIFY_URL = os.environ.get(
    "MOOCTEST_AUTH_VERIFY_URL", "http://127.0.0.1:18980/api/auth/user"
)


def _get_session_id(req: Request) -> str:
    header_id = (req.headers.get("X-Session-Id") or "").strip()
    if header_id:
        return header_id
    return (req.query_params.get("session_id") or "").strip()


def _verify_session(session_id: str) -> bool:
    if not session_id:
        return False
    auth_req = urllib_request.Request(
        MOOCTEST_AUTH_VERIFY_URL,
        headers={"X-Session-Id": session_id},
        method="GET",
    )
    try:
        with urllib_request.urlopen(auth_req, timeout=3) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception:
        return False


@app.get("/api/auth/session")
def auth_session(req: Request):
    session_id = _get_session_id(req)
    if not _verify_session(session_id):
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"authenticated": True, "auth_enabled": True}


@app.get("/api/auth/user")
def auth_user(req: Request):
    session_id = _get_session_id(req)
    if not session_id:
        raise HTTPException(status_code=401, detail="No session")
    auth_req = urllib_request.Request(
        MOOCTEST_AUTH_VERIFY_URL,
        headers={"X-Session-Id": session_id},
        method="GET",
    )
    try:
        with urllib_request.urlopen(auth_req, timeout=3) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError:
        raise HTTPException(status_code=401, detail="Authentication failed")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


base_dir = os.path.dirname(os.path.dirname(__file__))
project_root = os.path.dirname(base_dir)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

reports_dir = os.path.join(project_root, "reports")
if os.path.isdir(reports_dir):
    app.mount("/reports", StaticFiles(directory=reports_dir), name="reports")


@app.get("/app", response_class=FileResponse)
def frontend_app():
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/", response_class=FileResponse)
def root():
    return FileResponse(os.path.join(static_dir, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
