"""
Veritas IDV – FastAPI application.
Multi-tenant API keys, image ingestion, task status, and Gemma confirmation gate.
"""

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Annotated, List, Optional

import redis as redis_client
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import settings
from database import generate_api_key, init_db, list_api_keys, revoke_api_key, validate_api_key
from worker import ENGINE_STATUS_KEY, _GEMMA_GATE_KEY, celery_app, verify_identity_task

_STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Veritas IDV API",
    description="On-premise identity verification service with AI.",
    version="2.1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_QR_SESSION_TTL = 600  # 10 minutes

_redis = redis_client.from_url(settings.redis_url, decode_responses=True)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    init_db()
    _STATIC_DIR.mkdir(exist_ok=True)
    logger.info("Veritas IDV server started")


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

async def require_admin(x_admin_token: Annotated[str, Header()]) -> None:
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin token.")


async def require_api_key(x_api_key: Annotated[str, Header()]) -> str:
    project = validate_api_key(x_api_key)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return project


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class CreateKeyRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=100)


class CreateKeyResponse(BaseModel):
    api_key: str
    project: str
    message: str


class RevokeKeyRequest(BaseModel):
    api_key: str


class TaskSubmittedResponse(BaseModel):
    task_id: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    state: str
    result: Optional[dict] = None
    error: Optional[str] = None
    gemma_reason: Optional[str] = None   # present when state == REQUIRES_GEMMA_CONFIRMATION


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.post("/admin/api-keys", response_model=CreateKeyResponse, status_code=201, tags=["Admin"])
async def create_api_key(
    body: CreateKeyRequest,
    _: Annotated[None, Depends(require_admin)],
) -> CreateKeyResponse:
    key = generate_api_key(body.project)
    logger.info("Created API key for project: %s", body.project)
    return CreateKeyResponse(
        api_key=key, project=body.project,
        message="API key created. Save it — shown only once.",
    )


@app.get("/admin/api-keys", tags=["Admin"])
async def get_api_keys(_: Annotated[None, Depends(require_admin)]) -> list[dict]:
    return list_api_keys()


@app.delete("/admin/api-keys", tags=["Admin"])
async def delete_api_key(
    body: RevokeKeyRequest,
    _: Annotated[None, Depends(require_admin)],
) -> dict:
    if not revoke_api_key(body.api_key):
        raise HTTPException(status_code=404, detail="API key not found or already revoked.")
    return {"message": "API key revoked."}


# ---------------------------------------------------------------------------
# Desktop verify endpoint
# ---------------------------------------------------------------------------

@app.post(
    "/verify",
    response_model=TaskSubmittedResponse,
    status_code=202,
    tags=["IDV"],
    summary="Desktop: submit document for verification",
)
async def submit_verification(
    project: Annotated[str, Depends(require_api_key)],
    id_document: UploadFile = File(...),
    id_back: UploadFile = File(...),
    selfie: Optional[UploadFile] = File(None),
    nfc_dob: Optional[str] = Form(None),
) -> TaskSubmittedResponse:
    id_front_bytes = await id_document.read()
    id_back_bytes  = await id_back.read()
    selfie_bytes: Optional[bytes] = await selfie.read() if selfie else None

    if not id_front_bytes or not id_back_bytes:
        raise HTTPException(status_code=422, detail="Document image is empty.")

    task = verify_identity_task.delay(
        id_front_bytes_hex=id_front_bytes.hex(),
        id_back_bytes_hex=id_back_bytes.hex(),
        selfie_bytes_hex=selfie_bytes.hex() if selfie_bytes else None,
        all_selfie_bytes_hex=None,
        project=project,
        nfc_dob_iso=nfc_dob,
    )
    logger.info("Task %s submitted for project: %s", task.id, project)
    return TaskSubmittedResponse(task_id=task.id, message="Verification in progress.")


# ---------------------------------------------------------------------------
# QR / Mobile flow
# ---------------------------------------------------------------------------

@app.post("/mobile/session", tags=["Mobile"], status_code=201)
async def create_qr_session(
    project: Annotated[str, Depends(require_api_key)],
    x_api_key: Annotated[str, Header()],
) -> dict:
    session_id = secrets.token_urlsafe(24)
    _redis.setex(
        f"qr_session:{session_id}",
        _QR_SESSION_TTL,
        f"{x_api_key}|{project}",
    )
    logger.info("QR session created: %s  project: %s", session_id, project)
    return {"session_id": session_id, "expires_in": _QR_SESSION_TTL}


@app.get("/mobile/{session_id}", response_class=HTMLResponse, tags=["Mobile"], include_in_schema=False)
async def mobile_app(session_id: str) -> HTMLResponse:
    if not _redis.exists(f"qr_session:{session_id}"):
        return HTMLResponse(
            content="<h1>Session expired or invalid. Please scan a new QR code.</h1>",
            status_code=410,
        )
    mobile_html = _STATIC_DIR / "mobile.html"
    if not mobile_html.exists():
        return HTMLResponse(content="<h1>Mobile app not installed.</h1>", status_code=503)
    html = mobile_html.read_text(encoding="utf-8").replace("__SESSION_ID__", session_id)
    return HTMLResponse(content=html)


@app.post(
    "/mobile/verify/{session_id}",
    response_model=TaskSubmittedResponse,
    status_code=202,
    tags=["Mobile"],
    summary="Mobile: submit images (multi-selfie liveness frames supported)",
)
async def mobile_submit_verification(
    session_id: str,
    id_front: UploadFile = File(..., description="ID document front"),
    id_back: UploadFile = File(..., description="ID document back (MRZ)"),
    selfie_frames: List[UploadFile] = File(..., description="One or more liveness selfie frames"),
    nfc_dob: Optional[str] = Form(None),
) -> TaskSubmittedResponse:
    """
    Accepts multiple selfie frames from the liveness flow.
    Frame index 0 is always 'straight' and used as the primary comparison frame.
    All frames are forwarded to the worker for potential Gemma re-use.
    Session is single-use — deleted on first successful call.
    """
    session_key = f"qr_session:{session_id}"
    session_val: Optional[str] = _redis.get(session_key)
    if session_val is None:
        raise HTTPException(status_code=401, detail="Session expired.")

    _api_key, project = session_val.split("|", 1)
    _redis.delete(session_key)

    id_front_bytes = await id_front.read()
    id_back_bytes  = await id_back.read()

    if not id_front_bytes or not id_back_bytes:
        raise HTTPException(status_code=422, detail="ID images cannot be empty.")
    if not selfie_frames:
        raise HTTPException(status_code=422, detail="At least one selfie frame is required.")

    # Read all selfie frames; index 0 = straight (primary comparison frame)
    all_selfie_bytes: list[bytes] = []
    for f in selfie_frames:
        data = await f.read()
        if data:
            all_selfie_bytes.append(data)

    if not all_selfie_bytes:
        raise HTTPException(status_code=422, detail="All selfie frames were empty.")

    primary_selfie_bytes = all_selfie_bytes[0]

    task = verify_identity_task.delay(
        id_front_bytes_hex=id_front_bytes.hex(),
        id_back_bytes_hex=id_back_bytes.hex(),
        selfie_bytes_hex=primary_selfie_bytes.hex(),
        all_selfie_bytes_hex=[b.hex() for b in all_selfie_bytes],
        project=project,
        nfc_dob_iso=nfc_dob,
    )
    logger.info("Mobile task %s submitted for project: %s  selfie_frames=%d",
                task.id, project, len(all_selfie_bytes))
    return TaskSubmittedResponse(task_id=task.id, message="Verification in progress.")


# ---------------------------------------------------------------------------
# Task status — handles REQUIRES_GEMMA_CONFIRMATION state
# ---------------------------------------------------------------------------

@app.get(
    "/verify/status/{task_id}",
    response_model=TaskStatusResponse,
    tags=["IDV"],
    summary="Poll task status (no API key required — task_id is the bearer token)",
)
async def get_task_status(
    task_id: str,
    x_api_key: Annotated[Optional[str], Header()] = None,
) -> TaskStatusResponse:
    # Desktop callers supply an API key; mobile pollers supply only the task_id.
    # We don't validate the key here — the task_id is a 128-bit random UUID
    # generated by Celery and is functionally unguessable.
    task_result = celery_app.AsyncResult(task_id)
    state = task_result.state

    if state == "SUCCESS":
        result = task_result.result or {}
        # If the worker stored a gate result under SUCCESS, surface it correctly
        if result.get("status") == "requires_gemma_confirmation":
            return TaskStatusResponse(
                task_id=task_id,
                state="REQUIRES_GEMMA_CONFIRMATION",
                gemma_reason=result.get("gemma_reason"),
            )
        return TaskStatusResponse(task_id=task_id, state=state, result=result)

    if state == "REQUIRES_GEMMA_CONFIRMATION":
        meta = task_result.info or {}
        return TaskStatusResponse(
            task_id=task_id,
            state=state,
            gemma_reason=meta.get("gemma_reason"),
        )

    if state == "FAILURE":
        logger.error("Task %s failed: %s", task_id, str(task_result.result))
        return TaskStatusResponse(
            task_id=task_id, state=state,
            error="Verification failed. Please try again.",
        )

    return TaskStatusResponse(task_id=task_id, state=state)


# ---------------------------------------------------------------------------
# Gemma confirmation trigger
# ---------------------------------------------------------------------------

@app.post(
    "/verify/trigger-gemma/{task_id}",
    response_model=TaskSubmittedResponse,
    status_code=202,
    tags=["IDV"],
    summary="Authorise Gemma 4 for a paused task",
)
async def trigger_gemma(task_id: str) -> TaskSubmittedResponse:
    """
    Called by the mobile UI when the user taps "Activate Gemma 4".
    Loads the gate state from Redis and re-dispatches the task with
    gemma_authorized=True so the worker proceeds past the gate.
    No API key required — the session is already validated by the original task.
    """
    gate_key = _GEMMA_GATE_KEY.format(task_id)
    raw = _redis.get(gate_key)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail="No pending Gemma gate found for this task. It may have expired (TTL 1 h).",
        )

    try:
        gate: dict = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Gate state corrupted.")

    _redis.delete(gate_key)

    new_task = verify_identity_task.delay(
        id_front_bytes_hex=gate["id_front_hex"],
        id_back_bytes_hex=gate["id_back_hex"],
        selfie_bytes_hex=gate.get("selfie_hex"),
        all_selfie_bytes_hex=gate.get("all_selfie_hex"),
        project=gate["project"],
        nfc_dob_iso=gate.get("nfc_dob_iso"),
        gemma_authorized=True,
        gemma_mode=gate.get("gemma_mode", "mrz"),
    )

    logger.info(
        "Gemma gate authorised | original_task=%s | new_task=%s | mode=%s",
        task_id, new_task.id, gate.get("gemma_mode"),
    )
    return TaskSubmittedResponse(
        task_id=new_task.id,
        message="Gemma 4 authorised. Poll the new task_id for results.",
    )


# ---------------------------------------------------------------------------
# Health / engine status
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health_check() -> dict:
    return {"status": "ok", "service": "Veritas IDV"}


@app.get("/engine-status", tags=["System"])
async def engine_status() -> dict:
    try:
        val = _redis.get(ENGINE_STATUS_KEY) or "loading"
    except Exception:
        val = "loading"
    return {"status": val}
