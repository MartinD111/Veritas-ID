"""
Veritas IDV – FastAPI application.
Multi-tenant API keys, image ingestion, task status, and VAV confirmation gate.
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
from worker import (
    ENGINE_STATUS_KEY, _VAV_GATE_KEY, celery_app, verify_identity_task,
    _REVIEW_ITEM_KEY, _REVIEW_QUEUE_KEY, delete_review_item,
)

# Legacy alias so any old code that imported _GEMMA_GATE_KEY still works
_GEMMA_GATE_KEY = _VAV_GATE_KEY

_STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Veritas IDV API",
    description="Multi-country identity verification service with AI.",
    version="3.0.0",
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

# Valid country codes accepted by the API
_VALID_COUNTRIES = {"EU", "KR", "TH", "JP"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup() -> None:
    init_db()
    _STATIC_DIR.mkdir(exist_ok=True)
    logger.info("Veritas IDV server started — version 3.0.0 (Global Trust Update)")


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
    vav_reason: Optional[str] = None     # present when state == REQUIRES_VAV_CONFIRMATION
    # Legacy alias surfaced for old mobile clients still polling gemma_reason
    gemma_reason: Optional[str] = None


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
    country: str = Form("EU"),
    laser_id: Optional[str] = Form(None),
) -> TaskSubmittedResponse:
    country = country.upper()
    if country not in _VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail=f"Unsupported country: {country}. Valid: {sorted(_VALID_COUNTRIES)}")

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
        country=country,
        laser_id=laser_id,
    )
    logger.info("Task %s submitted | project=%s | country=%s", task.id, project, country)
    return TaskSubmittedResponse(task_id=task.id, message="Verification in progress.")


# ---------------------------------------------------------------------------
# QR / Mobile flow
# ---------------------------------------------------------------------------

@app.post("/mobile/session", tags=["Mobile"], status_code=201)
async def create_qr_session(
    project: Annotated[str, Depends(require_api_key)],
    x_api_key: Annotated[str, Header()],
    country: str = Form("EU"),
) -> dict:
    country = country.upper()
    if country not in _VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail=f"Unsupported country: {country}.")

    session_id = secrets.token_urlsafe(24)
    _redis.setex(
        f"qr_session:{session_id}",
        _QR_SESSION_TTL,
        f"{x_api_key}|{project}|{country}",
    )
    logger.info("QR session created: %s  project=%s  country=%s", session_id, project, country)
    return {"session_id": session_id, "expires_in": _QR_SESSION_TTL, "country": country}


@app.get("/mobile/{session_id}", response_class=HTMLResponse, tags=["Mobile"], include_in_schema=False)
async def mobile_app(session_id: str) -> HTMLResponse:
    session_val: Optional[str] = _redis.get(f"qr_session:{session_id}")
    if session_val is None:
        return HTMLResponse(
            content="<h1>Session expired or invalid. Please scan a new QR code.</h1>",
            status_code=410,
        )
    # Extract country from session value (format: api_key|project|country)
    parts = session_val.split("|", 2)
    country = parts[2] if len(parts) >= 3 else "EU"

    mobile_html = _STATIC_DIR / "mobile.html"
    if not mobile_html.exists():
        return HTMLResponse(content="<h1>Mobile app not installed.</h1>", status_code=503)

    html = (
        mobile_html.read_text(encoding="utf-8")
        .replace("__SESSION_ID__", session_id)
        .replace("__COUNTRY__", country)
    )
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
    laser_id: Optional[str] = Form(None),
) -> TaskSubmittedResponse:
    """
    Accepts multiple selfie frames from the liveness flow.
    Frame index 0 is always 'straight' and used as the primary comparison frame.
    Country is read from the session (set at QR creation time).
    Session is single-use — deleted on first successful call.
    """
    session_key = f"qr_session:{session_id}"
    session_val: Optional[str] = _redis.get(session_key)
    if session_val is None:
        raise HTTPException(status_code=401, detail="Session expired.")

    parts = session_val.split("|", 2)
    _api_key = parts[0]
    project  = parts[1]
    country  = parts[2] if len(parts) >= 3 else "EU"
    _redis.delete(session_key)

    id_front_bytes = await id_front.read()
    id_back_bytes  = await id_back.read()

    if not id_front_bytes or not id_back_bytes:
        raise HTTPException(status_code=422, detail="ID images cannot be empty.")
    if not selfie_frames:
        raise HTTPException(status_code=422, detail="At least one selfie frame is required.")

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
        country=country,
        laser_id=laser_id,
    )
    logger.info(
        "Mobile task %s submitted | project=%s | country=%s | selfie_frames=%d",
        task.id, project, country, len(all_selfie_bytes),
    )
    return TaskSubmittedResponse(task_id=task.id, message="Verification in progress.")


# ---------------------------------------------------------------------------
# Task status — handles REQUIRES_VAV_CONFIRMATION state
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
    task_result = celery_app.AsyncResult(task_id)
    state = task_result.state

    if state == "SUCCESS":
        result = task_result.result or {}
        # Worker may store gate result under SUCCESS state
        if result.get("status") in ("requires_vav_confirmation", "requires_gemma_confirmation"):
            reason = result.get("vav_reason") or result.get("gemma_reason")
            return TaskStatusResponse(
                task_id=task_id,
                state="REQUIRES_VAV_CONFIRMATION",
                vav_reason=reason,
                gemma_reason=reason,  # legacy compat
            )
        return TaskStatusResponse(task_id=task_id, state=state, result=result)

    if state in ("REQUIRES_VAV_CONFIRMATION", "REQUIRES_GEMMA_CONFIRMATION"):
        meta = task_result.info or {}
        reason = meta.get("vav_reason") or meta.get("gemma_reason")
        return TaskStatusResponse(
            task_id=task_id,
            state="REQUIRES_VAV_CONFIRMATION",
            vav_reason=reason,
            gemma_reason=reason,
        )

    if state == "FAILURE":
        logger.error("Task %s failed: %s", task_id, str(task_result.result))
        return TaskStatusResponse(
            task_id=task_id, state=state,
            error="Verification failed. Please try again.",
        )

    return TaskStatusResponse(task_id=task_id, state=state)


# ---------------------------------------------------------------------------
# VAV System confirmation trigger
# (legacy /verify/trigger-gemma path kept for backwards compatibility)
# ---------------------------------------------------------------------------

async def _do_trigger_vav(task_id: str) -> TaskSubmittedResponse:
    """Shared logic for both /trigger-vav and /trigger-gemma endpoints."""
    gate_key = _VAV_GATE_KEY.format(task_id)
    raw = _redis.get(gate_key)
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail="No pending VAV gate found for this task. It may have expired (TTL 1 h).",
        )

    try:
        gate: dict = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Gate state corrupted.")

    _redis.delete(gate_key)

    vav_mode = gate.get("vav_mode") or gate.get("gemma_mode", "mrz")

    new_task = verify_identity_task.delay(
        id_front_bytes_hex=gate["id_front_hex"],
        id_back_bytes_hex=gate["id_back_hex"],
        selfie_bytes_hex=gate.get("selfie_hex"),
        all_selfie_bytes_hex=gate.get("all_selfie_hex"),
        project=gate["project"],
        nfc_dob_iso=gate.get("nfc_dob_iso"),
        vav_authorized=True,
        vav_mode=vav_mode,
        country=gate.get("country", "EU"),
        laser_id=gate.get("laser_id"),
    )

    logger.info(
        "VAV gate authorised | original_task=%s | new_task=%s | mode=%s",
        task_id, new_task.id, vav_mode,
    )
    return TaskSubmittedResponse(
        task_id=new_task.id,
        message="VAV System authorised. Poll the new task_id for results.",
    )


@app.post(
    "/verify/trigger-vav/{task_id}",
    response_model=TaskSubmittedResponse,
    status_code=202,
    tags=["IDV"],
    summary="Authorise VAV System (Veritas Advanced Verification) for a paused task",
)
async def trigger_vav(task_id: str) -> TaskSubmittedResponse:
    return await _do_trigger_vav(task_id)


@app.post(
    "/verify/trigger-gemma/{task_id}",
    response_model=TaskSubmittedResponse,
    status_code=202,
    tags=["IDV"],
    summary="[Legacy] Alias for /verify/trigger-vav — kept for backwards compatibility",
    include_in_schema=False,
)
async def trigger_gemma(task_id: str) -> TaskSubmittedResponse:
    return await _do_trigger_vav(task_id)


# ---------------------------------------------------------------------------
# Analytics / Cost stats endpoint (for dashboard)
# ---------------------------------------------------------------------------

@app.get("/stats", tags=["System"])
async def get_stats() -> dict:
    """Return aggregate verification stats and cost totals."""
    try:
        from cost_tracker import get_stats, get_recent_events
        stats = get_stats()
        stats["recent_events"] = get_recent_events(10)
        return stats
    except Exception as exc:
        logger.exception("Failed to read stats")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, tags=["System"], include_in_schema=False)
async def dashboard() -> HTMLResponse:
    dash_html = _STATIC_DIR / "dashboard.html"
    if not dash_html.exists():
        return HTMLResponse(content="<h1>Dashboard not found.</h1>", status_code=503)
    return HTMLResponse(content=dash_html.read_text(encoding="utf-8"))


@app.get("/dashboard-config", tags=["System"], include_in_schema=False)
async def dashboard_config() -> dict:
    """Return bootstrap config for the dashboard UI: API key + ngrok URL."""
    import json as _json
    import requests as _requests

    config_file = Path(__file__).parent / ".veritas_config.json"
    api_key = ""
    try:
        if config_file.exists():
            data = _json.loads(config_file.read_text(encoding="utf-8"))
            api_key = data.get("api_key", "")
    except Exception:
        pass

    ngrok_url = ""
    try:
        r = _requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5)
        for t in r.json().get("tunnels", []):
            if t.get("public_url", "").startswith("https://"):
                ngrok_url = t["public_url"].rstrip("/")
                break
    except Exception:
        pass

    return {"api_key": api_key, "ngrok_url": ngrok_url}


# ---------------------------------------------------------------------------
# Admin authentication (dev-mode + SI-PASS stub)
# ---------------------------------------------------------------------------
#
# DEV MODE:  username=admin / password=admin  →  returns a short-lived token
#            stored in Redis so the Streamlit dashboard can poll endpoints.
# PROD MODE: Replace _dev_auth with a SIGEN-CA / SI-PASS certificate check.
#            The admin_id field should be the certificate Subject CN.
#
_ADMIN_SESSION_TTL = 3600          # 1 hour
_ADMIN_SESSION_PREFIX = "admin_session:"

# Audit log — append-only JSONL, never contains image data (GDPR / ZVOP-2)
_AUDIT_LOG = Path(__file__).parent / "admin_audit.jsonl"


def _write_audit(
    admin_id: str,
    user_email: str,
    task_id: str,
    action: str,          # "approved" | "rejected"
    project: str,
    country: str,
) -> None:
    """Write one immutable audit record. Images are never included."""
    import datetime as _dt
    record = {
        "ts":         _dt.datetime.utcnow().isoformat() + "Z",
        "admin_id":   admin_id,
        "user_email": user_email,
        "task_id":    task_id,
        "action":     action,
        "project":    project,
        "country":    country,
    }
    try:
        with _AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        logger.exception("Audit write failed for task=%s", task_id)
    logger.info(
        "AUDIT | admin=%s | email=%s | task=%s | action=%s",
        admin_id, user_email, task_id, action,
    )


def _require_admin_session(x_admin_session: Annotated[str, Header()]) -> dict:
    """Validate the short-lived admin session token returned by /admin/login."""
    raw = _redis.get(f"{_ADMIN_SESSION_PREFIX}{x_admin_session}")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin session expired or invalid. Please log in again.",
        )
    return json.loads(raw)


class AdminLoginRequest(BaseModel):
    username: str
    password: str
    # Future: certificate_pem: str  (for SI-PASS / SIGEN-CA)
    dev_mode: bool = True


class AdminLoginResponse(BaseModel):
    session_token: str
    admin_id: str
    expires_in: int
    auth_method: str


@app.post(
    "/admin/login",
    response_model=AdminLoginResponse,
    tags=["Admin"],
    summary="Admin login — dev mode (user/pass) or SI-PASS cert (future)",
)
async def admin_login(body: AdminLoginRequest) -> AdminLoginResponse:
    if body.dev_mode:
        # Developer mode — simple credential check
        if body.username != "admin" or body.password != "admin":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials.",
            )
        admin_id = "dev:admin"
        auth_method = "dev_password"
    else:
        # Production path — SI-PASS / SIGEN-CA integration point
        # TODO: validate body.certificate_pem against SIGEN-CA root
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="SI-PASS certificate auth not yet configured. Set dev_mode=true.",
        )

    token = secrets.token_urlsafe(32)
    session_data = json.dumps({"admin_id": admin_id, "auth_method": auth_method})
    _redis.setex(f"{_ADMIN_SESSION_PREFIX}{token}", _ADMIN_SESSION_TTL, session_data)
    logger.info("Admin login | admin_id=%s | method=%s", admin_id, auth_method)
    return AdminLoginResponse(
        session_token=token,
        admin_id=admin_id,
        expires_in=_ADMIN_SESSION_TTL,
        auth_method=auth_method,
    )


@app.post("/admin/logout", tags=["Admin"])
async def admin_logout(
    session: Annotated[dict, Depends(_require_admin_session)],
    x_admin_session: Annotated[str, Header()],
) -> dict:
    _redis.delete(f"{_ADMIN_SESSION_PREFIX}{x_admin_session}")
    logger.info("Admin logout | admin_id=%s", session.get("admin_id"))
    return {"message": "Logged out."}


# ---------------------------------------------------------------------------
# Manual Review Queue endpoints
# ---------------------------------------------------------------------------

class ReviewActionRequest(BaseModel):
    action: str           # "approved" | "rejected"
    user_email: str = ""  # provided by the calling admin or client app
    webhook_url: str = "" # optional: client app webhook to notify on completion


@app.get(
    "/admin/review/queue",
    tags=["Admin"],
    summary="List all tasks pending manual review",
)
async def get_review_queue(
    session: Annotated[dict, Depends(_require_admin_session)],
) -> list[dict]:
    """
    Returns summary records for all items in the manual review queue.
    Images are NOT returned here — fetch a single item via /admin/review/{task_id}.
    """
    task_ids: list[str] = _redis.lrange(_REVIEW_QUEUE_KEY, 0, -1)
    items = []
    for tid in task_ids:
        raw = _redis.get(_REVIEW_ITEM_KEY.format(tid))
        if raw is None:
            # TTL expired — clean stale reference from list
            _redis.lrem(_REVIEW_QUEUE_KEY, 0, tid)
            continue
        data = json.loads(raw)
        # Return metadata only — never images in list view
        items.append({
            "task_id":    data.get("task_id"),
            "project":    data.get("project"),
            "country":    data.get("country"),
            "user_email": data.get("user_email", ""),
            "reason":     data.get("reason"),
            "queued_at":  data.get("queued_at"),
            "ttl_seconds": _redis.ttl(_REVIEW_ITEM_KEY.format(tid)),
        })
    return items


@app.get(
    "/admin/review/{task_id}",
    tags=["Admin"],
    summary="Fetch a single review item including images (base64)",
)
async def get_review_item(
    task_id: str,
    session: Annotated[dict, Depends(_require_admin_session)],
) -> dict:
    """
    Returns the full review item including base64-encoded images for display.
    Access is logged for non-repudiation.
    """
    import base64
    raw = _redis.get(_REVIEW_ITEM_KEY.format(task_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="Review item not found or TTL expired.")

    data = json.loads(raw)
    admin_id = session.get("admin_id", "unknown")
    logger.info("Admin VIEWED review item | admin=%s | task=%s", admin_id, task_id)

    def _hex_to_b64(h: Optional[str]) -> Optional[str]:
        if not h:
            return None
        return base64.b64encode(bytes.fromhex(h)).decode()

    return {
        "task_id":       data.get("task_id"),
        "project":       data.get("project"),
        "country":       data.get("country"),
        "user_email":    data.get("user_email", ""),
        "reason":        data.get("reason"),
        "queued_at":     data.get("queued_at"),
        "ttl_seconds":   _redis.ttl(_REVIEW_ITEM_KEY.format(task_id)),
        "id_front_b64":  _hex_to_b64(data.get("id_front_hex")),
        "id_back_b64":   _hex_to_b64(data.get("id_back_hex")),
        "selfie_b64":    _hex_to_b64(data.get("selfie_hex")),
    }


@app.post(
    "/admin/review/{task_id}/action",
    tags=["Admin"],
    summary="Approve or reject a manual review item — purges images immediately",
)
async def action_review_item(
    task_id: str,
    body: ReviewActionRequest,
    session: Annotated[dict, Depends(_require_admin_session)],
) -> dict:
    """
    GDPR / ZVOP-2 compliant action endpoint:
    1. Validates the action.
    2. Writes an immutable audit record (no images, no raw data).
    3. Immediately deletes images from Redis (DEL — not TTL expiry).
    4. Fires an optional webhook to the client app (e.g. Tremble).
    """
    import httpx

    if body.action not in ("approved", "rejected"):
        raise HTTPException(status_code=422, detail="action must be 'approved' or 'rejected'.")

    raw = _redis.get(_REVIEW_ITEM_KEY.format(task_id))
    if raw is None:
        raise HTTPException(status_code=404, detail="Review item not found or TTL expired.")

    data = json.loads(raw)
    admin_id  = session.get("admin_id", "unknown")
    project   = data.get("project", "")
    country   = data.get("country", "EU")
    user_email = body.user_email or data.get("user_email", "")

    # 1. Audit record (no image data ever written)
    _write_audit(
        admin_id=admin_id,
        user_email=user_email,
        task_id=task_id,
        action=body.action,
        project=project,
        country=country,
    )

    # 2. Immediately purge images from Redis — CRITICAL GDPR step
    delete_review_item(task_id)
    logger.info(
        "Review actioned | admin=%s | task=%s | action=%s | images_deleted=True",
        admin_id, task_id, body.action,
    )

    # 3. Optional webhook back to client app
    webhook_result = None
    if body.webhook_url:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    body.webhook_url,
                    json={
                        "event":      "veritas.manual_review_complete",
                        "task_id":    task_id,
                        "project":    project,
                        "action":     body.action,
                        "admin_id":   admin_id,
                        "user_email": user_email,
                        "powered_by": "Veritas ID",
                    },
                    headers={"User-Agent": "VeritasID/3.0"},
                )
            webhook_result = {"status": resp.status_code, "ok": resp.is_success}
        except Exception as exc:
            webhook_result = {"status": 0, "ok": False, "error": str(exc)}
            logger.warning("Webhook delivery failed | url=%s | error=%s", body.webhook_url, exc)

    return {
        "task_id":        task_id,
        "action":         body.action,
        "admin_id":       admin_id,
        "images_deleted": True,
        "audit_logged":   True,
        "webhook":        webhook_result,
    }


# ---------------------------------------------------------------------------
# Audit log endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/admin/audit-log",
    tags=["Admin"],
    summary="Return the last N audit records (no image data)",
)
async def get_audit_log(
    session: Annotated[dict, Depends(_require_admin_session)],
    limit: int = 50,
) -> list[dict]:
    """Returns recent audit records from the append-only JSONL file."""
    if not _AUDIT_LOG.exists():
        return []
    try:
        lines = _AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        return records[-limit:][::-1]   # newest first
    except Exception as exc:
        logger.exception("Failed to read audit log")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Health / engine status
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health_check() -> dict:
    return {"status": "ok", "service": "Veritas IDV", "version": "3.0.0"}


@app.get("/engine-status", tags=["System"])
async def engine_status() -> dict:
    try:
        val = _redis.get(ENGINE_STATUS_KEY) or "loading"
    except Exception:
        val = "loading"
    return {"status": val}
