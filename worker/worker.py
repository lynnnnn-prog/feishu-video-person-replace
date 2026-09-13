from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
from pipeline import run_pipeline
DATA_DIR = Path(os.getenv("WORKER_DATA_DIR", "./data")).expanduser()
if not DATA_DIR.is_absolute():
    DATA_DIR = (BASE_DIR / DATA_DIR).resolve()
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
MAX_DOWNLOAD = int(os.getenv("WORKER_MAX_DOWNLOAD_MB", "500")) * 1024 * 1024
DOWNLOAD_ATTEMPTS = max(1, int(os.getenv("WORKER_DOWNLOAD_ATTEMPTS", "5")))
DOWNLOAD_RETRY_BASE_SECONDS = max(
    0.5, float(os.getenv("WORKER_DOWNLOAD_RETRY_BASE_SECONDS", "2"))
)
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-worker")
STATE_LOCK = threading.Lock()

app = FastAPI(title="Huanlian Video Worker", version="1.0.0")


class JobRequest(BaseModel):
    source_url: HttpUrl
    reference_image_urls: list[HttpUrl] = Field(min_length=1, max_length=6)
    segment_seconds: float = Field(default=8.0, ge=3.0, le=9.0)
    prompt: str | None = Field(default=None, max_length=5000)
    callback_url: HttpUrl | None = None
    external_id: str | None = Field(default=None, max_length=200)
    feishu_access_token: str | None = None
    generate_audio: bool = False


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_key(x_worker_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("WORKER_API_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "WORKER_API_KEY is not configured")
    if not x_worker_key or not secrets.compare_digest(x_worker_key, expected):
        raise HTTPException(401, "Invalid worker key")


def state_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "state.json"


def read_state(job_id: str) -> dict[str, Any]:
    path = state_path(job_id)
    if not path.is_file():
        raise HTTPException(404, "Job not found")
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(job_id: str, **updates: Any) -> dict[str, Any]:
    with STATE_LOCK:
        path = state_path(job_id)
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        current.update(updates)
        current["updated_at"] = now()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return current


def safe_suffix(url: str, fallback: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".mp4", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp"} else fallback


async def download(url: str, target: Path, token: str | None = None) -> None:
    timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
    host = (urlparse(url).hostname or "").lower()
    is_feishu = any(host == domain or host.endswith('.' + domain)
                    for domain in ('feishu.cn', 'larksuite.com'))
    # A row may contain a public external URL. Never send its host our tenant token.
    headers = {"Authorization": f"Bearer {token}"} if token and is_feishu else {}
    temporary = target.with_suffix(f"{target.suffix}.part")

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        total = 0
        temporary.unlink(missing_ok=True)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length", "0") or 0)
                    if declared > MAX_DOWNLOAD:
                        raise ValueError(
                            f"Download exceeds {MAX_DOWNLOAD // 1024 // 1024} MB"
                        )
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_raw(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_DOWNLOAD:
                                raise ValueError(
                                    f"Download exceeds {MAX_DOWNLOAD // 1024 // 1024} MB"
                                )
                            handle.write(chunk)

            if total == 0:
                raise IOError("Downloaded file is empty")
            if declared and total != declared:
                raise IOError(
                    f"Incomplete download: received {total} bytes, expected {declared}"
                )

            temporary.replace(target)
            return
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            retryable_status = (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
            )
            retryable = isinstance(exc, (httpx.TransportError, IOError)) or retryable_status
            detail = str(exc).strip() or repr(exc)

            if not retryable or attempt == DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"Download failed after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {detail}"
                ) from exc

            await asyncio.sleep(
                min(DOWNLOAD_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 20)
            )

    raise RuntimeError("Download failed unexpectedly")


async def notify(callback_url: str | None, payload: dict[str, Any]) -> None:
    if not callback_url:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.post(callback_url, json=payload)
    except Exception:
        pass


async def execute_job(job_id: str, request: JobRequest) -> None:
    job_dir = JOBS_DIR / job_id
    try:
        write_state(job_id, status="downloading", message="正在下载视频和参考图")
        source = job_dir / f"source{safe_suffix(str(request.source_url), '.mp4')}"
        await download(str(request.source_url), source, request.feishu_access_token)
        refs_dir = job_dir / "references"
        refs_dir.mkdir(exist_ok=True)
        refs: list[Path] = []
        for index, url in enumerate(request.reference_image_urls, 1):
            target = refs_dir / f"reference_{index:02d}{safe_suffix(str(url), '.jpg')}"
            await download(str(url), target, request.feishu_access_token)
            refs.append(target)

        write_state(job_id, status="processing", message="正在调用 Gemini 处理视频")

        def progress(index: int, total: int, stage: str) -> None:
            write_state(
                job_id,
                status="processing",
                current_segment=index,
                total_segments=total,
                message=f"视频分段 {index}/{total}: {stage}",
            )

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            EXECUTOR,
            lambda: asyncio.run(run_pipeline(
                source, refs, job_dir, request.segment_seconds, request.prompt, progress,
                generate_audio=request.generate_audio,
            )),
        )
        state = write_state(
            job_id,
            status="completed",
            message="处理完成",
            result_path=str(result),
            result_endpoint=f"/v1/jobs/{job_id}/result",
            completed_at=now(),
        )

        await notify(
            str(request.callback_url) if request.callback_url else None,
            state,
        )
    except Exception as exc:
        (job_dir / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        error_message = str(exc).strip() or f"{type(exc).__name__}: {exc!r}"
        state = write_state(job_id, status="failed", message=error_message, failed_at=now())
        await notify(str(request.callback_url) if request.callback_url else None, state)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "worker_key_configured": bool(os.getenv("WORKER_API_KEY", "").strip()),
    }


@app.post("/jobs", dependencies=[Depends(require_key)], status_code=202)
@app.post("/v1/jobs", dependencies=[Depends(require_key)], status_code=202)
async def create_job(payload: JobRequest) -> dict[str, Any]:
    job_id = f"job_{uuid.uuid4().hex}"
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "request.json").write_text(
        payload.model_dump_json(indent=2, exclude={"feishu_access_token"}) + "\n", encoding="utf-8"
    )

    state = write_state(
        job_id,
        external_id=payload.external_id,
        status="queued",
        message="任务已进入队列",
        created_at=now(),
    )
    state["job_id"] = job_id
    asyncio.create_task(execute_job(job_id, payload))
    return state


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_key)])
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    state = read_state(job_id)
    if state.get("status") == "completed":
        state["output_url"] = str(request.url_for("get_result", job_id=job_id))
    return state


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_key)])
@app.get("/v1/jobs/{job_id}/result", dependencies=[Depends(require_key)])
def get_result(job_id: str) -> FileResponse:
    state = read_state(job_id)
    if state.get("status") != "completed":
        raise HTTPException(409, f"Job is {state.get('status')}")
    result = JOBS_DIR / job_id / "result.mp4"
    if not result.is_file():
        raise HTTPException(404, "Result file not found")
    return FileResponse(result, media_type="video/mp4", filename=f"{job_id}.mp4")


if __name__ == "__main__":
    uvicorn.run(
        "worker:app",
        host=os.getenv("WORKER_HOST", "127.0.0.1"),
        port=int(os.getenv("WORKER_PORT", "8080")),
        reload=False,
    )
