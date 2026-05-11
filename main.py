"""
Veritas IDV – FastAPI aplikacija.
Zagotavlja multi-tenant API ključe, sprejem slik in preverjanje stanja nalog.
"""

import logging
from typing import Annotated, Optional

import redis as redis_client
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config import settings
from database import generate_api_key, init_db, list_api_keys, revoke_api_key, validate_api_key
from worker import ENGINE_STATUS_KEY, celery_app, verify_identity_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Veritas IDV API",
    description="On-premise storitev za preverjanje identitete z AI.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)


# ---------------------------------------------------------------------------
# Inicializacija ob zagonu
# ---------------------------------------------------------------------------

_redis = redis_client.from_url(settings.redis_url, decode_responses=True)


@app.on_event("startup")
async def startup() -> None:
    """Inicializira bazo podatkov ob zagonu strežnika."""
    init_db()
    logger.info("Veritas IDV strežnik zagnan, baza podatkov inicializirana")


# ---------------------------------------------------------------------------
# Varnostne odvisnosti
# ---------------------------------------------------------------------------

async def require_admin(x_admin_token: Annotated[str, Header()]) -> None:
    """Preveri admin žeton za zaščitene administrativne poti."""
    if x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Neveljaven admin žeton.",
        )


async def require_api_key(x_api_key: Annotated[str, Header()]) -> str:
    """
    Preveri API ključ klienta in vrne ime projekta.
    Dvigne 401, če ključ ni veljaven ali aktiven.
    """
    project = validate_api_key(x_api_key)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Neveljaven ali deaktiviran API ključ.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return project


# ---------------------------------------------------------------------------
# Pydantic sheme
# ---------------------------------------------------------------------------

class CreateKeyRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=100, description="Ime projekta")


class CreateKeyResponse(BaseModel):
    api_key: str
    project: str
    message: str


class RevokeKeyRequest(BaseModel):
    api_key: str = Field(..., description="Ključ za deaktivacijo")


class TaskSubmittedResponse(BaseModel):
    task_id: str
    message: str


class TaskStatusResponse(BaseModel):
    task_id: str
    state: str
    result: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Admin poti (zaščitene z X-Admin-Token)
# ---------------------------------------------------------------------------

@app.post(
    "/admin/api-keys",
    response_model=CreateKeyResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Admin"],
    summary="Ustvari nov API ključ za projekt",
)
async def create_api_key(
    body: CreateKeyRequest,
    _: Annotated[None, Depends(require_admin)],
) -> CreateKeyResponse:
    """Ustvari kriptografsko varen API ključ in ga shrani v bazo."""
    key = generate_api_key(body.project)
    logger.info("Ustvarjen nov API ključ za projekt: %s", body.project)
    return CreateKeyResponse(
        api_key=key,
        project=body.project,
        message="API ključ uspešno ustvarjen. Shranite ga varno – prikazan bo samo enkrat.",
    )


@app.get(
    "/admin/api-keys",
    tags=["Admin"],
    summary="Seznam vseh API ključev",
)
async def get_api_keys(
    _: Annotated[None, Depends(require_admin)],
) -> list[dict]:
    """Vrne seznam vseh API ključev z metapodatki (brez samih ključev)."""
    return list_api_keys()


@app.delete(
    "/admin/api-keys",
    tags=["Admin"],
    summary="Deaktiviraj API ključ",
)
async def delete_api_key(
    body: RevokeKeyRequest,
    _: Annotated[None, Depends(require_admin)],
) -> dict:
    """Deaktivira API ključ. Obstoječe naloge z njim niso prizadete."""
    success = revoke_api_key(body.api_key)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API ključ ni najden ali je že deaktiviran.",
        )
    return {"message": "API ključ uspešno deaktiviran."}


# ---------------------------------------------------------------------------
# Glavne IDV poti
# ---------------------------------------------------------------------------

@app.post(
    "/verify",
    response_model=TaskSubmittedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["IDV"],
    summary="Pošlji dokument v preverjanje",
)
async def submit_verification(
    project: Annotated[str, Depends(require_api_key)],
    id_document: UploadFile = File(..., description="Slika osebnega dokumenta (JPEG/PNG/WebP)"),
    selfie: Optional[UploadFile] = File(None, description="Selfie za primerjavo obraza (neobvezno)"),
) -> TaskSubmittedResponse:
    """
    Sprejme dokument (in neobvezni selfie), ju prebere v RAM in odda Celery nalogo.
    Datoteki se NIKOLI ne shranita na disk.
    """
    # Preberi bajte direktno v RAM – brez shranjevanja na disk
    id_bytes: bytes = await id_document.read()
    selfie_bytes: Optional[bytes] = await selfie.read() if selfie else None

    if len(id_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Slika dokumenta je prazna.",
        )

    # Celery serializira argumente v JSON – pretvori bajte v hex za prenos
    task = verify_identity_task.delay(
        id_bytes_hex=id_bytes.hex(),
        selfie_bytes_hex=selfie_bytes.hex() if selfie_bytes else None,
        project=project,
    )

    # Takoj uniči bajte iz tega procesa
    del id_bytes, selfie_bytes

    logger.info("Naloga %s oddana za projekt: %s", task.id, project)
    return TaskSubmittedResponse(
        task_id=task.id,
        message="Verifikacija v teku. Preverite status z /verify/status/{task_id}.",
    )


@app.get(
    "/verify/status/{task_id}",
    response_model=TaskStatusResponse,
    tags=["IDV"],
    summary="Preveri stanje naloge",
)
async def get_task_status(
    task_id: str,
    _project: Annotated[str, Depends(require_api_key)],
) -> TaskStatusResponse:
    """
    Vrne stanje Celery naloge.
    Možna stanja: PENDING, STARTED, SUCCESS, FAILURE.
    """
    task_result = celery_app.AsyncResult(task_id)
    state = task_result.state

    if state == "SUCCESS":
        return TaskStatusResponse(
            task_id=task_id,
            state=state,
            result=task_result.result,
        )
    elif state == "FAILURE":
        # Ne razkrivaj internih podrobnosti napake klientu
        logger.error("Naloga %s neuspešna: %s", task_id, str(task_result.result))
        return TaskStatusResponse(
            task_id=task_id,
            state=state,
            error="Verifikacija ni uspela. Prosimo, poskusite znova.",
        )
    else:
        # PENDING ali STARTED
        return TaskStatusResponse(task_id=task_id, state=state)


# ---------------------------------------------------------------------------
# Zdravstveni pregled
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Sistem"], summary="Zdravstveni pregled")
async def health_check() -> dict:
    """Preprost endpoint za preverjanje, ali je strežnik aktiven."""
    return {"status": "ok", "service": "Veritas IDV"}


@app.get("/engine-status", tags=["Sistem"], summary="Stanje AI motorja")
async def engine_status() -> dict:
    """Vrne stanje nalaganja Gemma modela v RAM."""
    try:
        status_val = _redis.get(ENGINE_STATUS_KEY) or "loading"
    except Exception:
        status_val = "loading"
    return {"status": status_val}
