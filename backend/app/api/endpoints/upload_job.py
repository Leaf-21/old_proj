import asyncio
import copy
import json
import os
import shutil
import threading
from typing import Any, Dict, List
from urllib import error as urllib_error
from urllib import request as urllib_request

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.models.testcase import TestCase
from app.services.analytics.stats import stats_service
from app.services.audit.auditor import ResultAuditor
from app.services.defects.clustering import defect_clusterer
from app.services.defects.extractor import defect_extractor
from app.services.ingest.service import ingest_service
from app.services.ingest.tagging import module_tagger
from app.services.report_gen.renderer import report_generator

router = APIRouter()

TOOL_ID = os.environ.get("MOOCTEST_TOOL_ID", "test-report")
MOOCTEST_JOBS_BASE_URL = os.environ.get(
    "MOOCTEST_JOBS_BASE_URL", "http://120.27.144.90:18980/api/jobs"
)


class JobStatus:
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class ExecuteJobRequest(BaseModel):
    job_id: str


job_logs: Dict[str, List[str]] = {}
job_meta: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.RLock()


def append_log(job_id: str, message: str) -> None:
    with jobs_lock:
        job_logs.setdefault(job_id, []).append(message)


def get_session_id(req: Request) -> str:
    header_id = (req.headers.get("X-Session-Id") or "").strip()
    if header_id:
        return header_id
    return (req.query_params.get("session_id") or "").strip()


def require_session(req: Request) -> str:
    session_id = get_session_id(req)
    if not session_id:
        raise HTTPException(status_code=401, detail="Missing session_id")
    return session_id


def _read_backend_json(req: urllib_request.Request, timeout: int = 5) -> Dict[str, Any]:
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=exc.code, detail=detail or str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to call Mooctest backend: {exc}")


def create_backend_job(session_id: str) -> Dict[str, Any]:
    payload = {
        "toolId": TOOL_ID,
        "enqueue": False,
    }
    backend_req = urllib_request.Request(
        MOOCTEST_JOBS_BASE_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": session_id,
        },
        method="POST",
    )
    body = _read_backend_json(backend_req)
    job_info = body.get("data") if isinstance(body, dict) else None
    if not isinstance(job_info, dict) or not job_info.get("job_id"):
        raise HTTPException(status_code=502, detail="Backend did not return job_id")
    return job_info


def get_backend_job(job_id: str, session_id: str) -> Dict[str, Any]:
    backend_req = urllib_request.Request(
        f"{MOOCTEST_JOBS_BASE_URL}/{job_id}",
        headers={"X-Session-Id": session_id},
        method="GET",
    )
    body = _read_backend_json(backend_req)
    job_info = body.get("data") if isinstance(body, dict) else None
    if not isinstance(job_info, dict):
        raise HTTPException(status_code=502, detail="Backend did not return job data")
    return job_info


def enqueue_backend_job(job_id: str, session_id: str) -> None:
    backend_req = urllib_request.Request(
        f"{MOOCTEST_JOBS_BASE_URL}/{job_id}/enqueue",
        headers={"X-Session-Id": session_id},
        method="POST",
    )
    _read_backend_json(backend_req)


def notify_backend_job_status(job_id: str, status: str) -> None:
    if not job_id or status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
        return

    backend_req = urllib_request.Request(
        f"{MOOCTEST_JOBS_BASE_URL}/{job_id}/{status}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(backend_req, timeout=5):
            pass
    except Exception as exc:
        append_log(job_id, f"同步 Spring 任务状态失败：{exc}")


def update_local_job(job_id: str, **fields: Any) -> Dict[str, Any]:
    with jobs_lock:
        job = job_meta.get(job_id, {})
        job.update(fields)
        job.setdefault("job_id", job_id)
        job.setdefault("status", JobStatus.PENDING)
        job.setdefault("report_url", None)
        job.setdefault("error", None)
        job_meta[job_id] = job
        job_logs.setdefault(job_id, [])
        return copy.deepcopy(job)


@router.post("/upload")
async def upload_file(
    req: Request,
    file: UploadFile = File(...),
):
    session_id = require_session(req)
    backend_job = create_backend_job(session_id)
    job_id = backend_job["job_id"]

    upload_dir = os.path.join("uploads", job_id)
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename or "test_results.xlsx")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    update_local_job(
        job_id,
        status=backend_job.get("status"),
        created_at=backend_job.get("created_at"),
        file_path=file_path,
        original_filename=file.filename,
        report_url=None,
        error=None,
    )
    append_log(job_id, "文件已上传，等待 Spring 后端排队调度。")

    enqueue_backend_job(job_id, session_id)
    update_local_job(job_id, status=JobStatus.QUEUED)
    append_log(job_id, "任务已提交到 Spring 后端队列。")

    return {
        "job_id": job_id,
        "status": JobStatus.QUEUED,
        "message": "文件已上传，任务已进入 Spring 后端队列。",
    }


@router.post("/execute")
async def execute_job(req: ExecuteJobRequest):
    job_id = (req.job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")

    with jobs_lock:
        if job_id not in job_meta:
            raise HTTPException(status_code=404, detail="Job not found")
        status = job_meta[job_id].get("status")
        if status in {JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}:
            return {"job_id": job_id, "status": status}
        file_path = job_meta[job_id].get("file_path")
        job_meta[job_id]["status"] = JobStatus.RUNNING

    if not file_path:
        update_local_job(job_id, status=JobStatus.FAILED, error="Uploaded file not found")
        notify_backend_job_status(job_id, JobStatus.FAILED)
        append_log(job_id, "流水线执行失败：找不到已上传文件。")
        return {"job_id": job_id, "status": JobStatus.FAILED}

    append_log(job_id, "收到 Spring 后端执行回调，开始运行本地分析流水线。")
    asyncio.create_task(run_local_pipeline(job_id, file_path))
    return {"job_id": job_id, "status": JobStatus.RUNNING}


async def run_local_pipeline(job_id: str, file_path: str) -> None:
    update_local_job(job_id, status=JobStatus.RUNNING, error=None)
    try:
        append_log(job_id, "步骤 1/6：解析 Excel 数据。")
        raw_cases = await ingest_service.parse_excel(file_path, job_id)
        cases = [TestCase(**d) for d in raw_cases]
        append_log(job_id, f"已解析 {len(cases)} 条用例。")

        append_log(job_id, "步骤 2/6：模块打标（LLM 并发）。")
        cases = await module_tagger.tag_cases_concurrently(cases)

        append_log(job_id, "步骤 3/6：结果审计（LLM 并发检查假成功）。")
        auditor = ResultAuditor()
        cases = await auditor.audit_cases_concurrently(cases)
        suspicious_cases = [c for c in cases if c.audit_status == "Flagged"]
        append_log(job_id, f"发现 {len(suspicious_cases)} 个存疑用例。")

        append_log(job_id, "步骤 4/6：计算统计数据。")
        stats = stats_service.compute_stats(cases)

        append_log(job_id, "步骤 5/6：提取缺陷事实（LLM 并发）。")
        defects = await defect_extractor.extract_defect_facts_concurrently(cases)
        append_log(job_id, f"提取了 {len(defects)} 条缺陷分析。")

        linked_defects: List[Any] = []
        for case in cases:
            if hasattr(case, "defect_analysis") and case.defect_analysis:
                case.defect_analysis.testcase = case
                linked_defects.append(case.defect_analysis)

        append_log(job_id, "步骤 6/6：缺陷聚类并生成报告。")
        clusters = await defect_clusterer.cluster_and_summarize_async(linked_defects, job_id)

        output_dir = "reports"
        os.makedirs(output_dir, exist_ok=True)
        filename = f"report_{job_id}.html"
        report_path = os.path.join(output_dir, filename)
        report_generator.render_report(
            job_id, stats, linked_defects, clusters, suspicious_cases, cases, report_path
        )

        report_url = f"/api/v1/jobs/report/{job_id}"
        update_local_job(
            job_id,
            status=JobStatus.COMPLETED,
            report_url=report_url,
            report_path=report_path,
            error=None,
        )
        append_log(job_id, f"报告已生成：{report_url}")
        append_log(job_id, "流水线执行完成。")
        notify_backend_job_status(job_id, JobStatus.COMPLETED)
    except Exception as exc:
        update_local_job(job_id, status=JobStatus.FAILED, error=str(exc))
        append_log(job_id, f"流水线执行失败：{exc}")
        notify_backend_job_status(job_id, JobStatus.FAILED)


@router.get("/status/{job_id}")
async def get_job_status(req: Request, job_id: str):
    session_id = get_session_id(req)
    backend_job = None
    if session_id:
        backend_job = get_backend_job(job_id, session_id)

    with jobs_lock:
        local_meta = copy.deepcopy(job_meta.get(job_id, {}))
        logs = copy.deepcopy(job_logs.get(job_id, []))

    if backend_job:
        update_fields = ["status", "created_at", "updated_at", "completed_at"]
        local_meta.update({field: backend_job.get(field) for field in update_fields if field in backend_job})

    if not local_meta:
        return {
            "job_id": job_id,
            "status": "unknown",
            "logs": logs,
        }

    return {
        "job_id": job_id,
        "status": local_meta.get("status"),
        "logs": logs,
        "report_url": local_meta.get("report_url"),
        "error": local_meta.get("error"),
    }


@router.get("/report/{job_id}")
async def view_report(job_id: str):
    with jobs_lock:
        report_path = job_meta.get(job_id, {}).get("report_path")

    if not report_path:
        report_path = os.path.join("reports", f"report_{job_id}.html")

    if not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(report_path, media_type="text/html")
