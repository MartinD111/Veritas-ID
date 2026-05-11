"""
Celery delavec za asinhrone IDV naloge.
Na Windows zaženi z: celery -A worker worker --pool=solo --loglevel=info
"""

import logging
from typing import Optional

import redis as redis_client
from celery import Celery
from celery.signals import worker_ready, worker_shutdown

from config import settings

logger = logging.getLogger(__name__)

ENGINE_STATUS_KEY = "veritas_engine_status"

# Označi motor kot "nalaganje" takoj ob zagonu modula
_redis = redis_client.from_url(settings.redis_url, decode_responses=True)
_redis.set(ENGINE_STATUS_KEY, "loading")

# Inicializacija Celery aplikacije z Redis brokerjem in backendom
celery_app = Celery(
    "veritas",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Čas hrambe rezultatov: 1 ura (skladno z GDPR načelom minimizacije podatkov)
    result_expires=3600,
    task_time_limit=settings.task_time_limit_seconds,
    task_soft_time_limit=settings.task_time_limit_seconds - 30,
    worker_prefetch_multiplier=1,  # Ena naloga naenkrat – model je velik
    task_acks_late=True,           # Potrdi šele po uspešni obdelavi
)


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """Celery je inicializiran in čaka na naloge – motor je pripravljen."""
    _redis.set(ENGINE_STATUS_KEY, "ready")
    logger.info("Engine status: ready")


@worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    """Ponastavi status ob zaustavitvi delavca."""
    _redis.set(ENGINE_STATUS_KEY, "loading")
    logger.info("Engine status: loading (shutdown)")


@celery_app.task(
    bind=True,
    name="veritas.verify_identity",
    max_retries=0,  # Brez ponovnih poskusov – vsak klic porabi RAM
)
def verify_identity_task(
    self,
    id_bytes_hex: str,
    selfie_bytes_hex: Optional[str],
    project: str,
) -> dict:
    """
    Glavna Celery naloga za verifikacijo identitete.
    Podatki so preneseni kot hex nizi, ker Celery serializira v JSON.
    """
    # Uvoz tukaj, da delavec ne naloži modela ob zagonu
    from engine import VeritasEngine

    logger.info("Začenjam IDV nalogo za projekt: %s", project)

    # Pretvori hex nazaj v bajte (samo v RAM-u delavca)
    id_bytes: bytes = bytes.fromhex(id_bytes_hex)
    selfie_bytes: Optional[bytes] = (
        bytes.fromhex(selfie_bytes_hex) if selfie_bytes_hex else None
    )

    # Takoj izbriši hex nize – samo bajti so potrebni naprej
    del id_bytes_hex, selfie_bytes_hex

    try:
        engine = VeritasEngine()
        result = engine.verify(id_bytes, selfie_bytes)
    finally:
        # Zagotovi čiščenje tudi ob napaki
        del id_bytes
        if selfie_bytes is not None:
            del selfie_bytes

    # Dodaj ime projekta k rezultatu
    result["project"] = project
    logger.info("IDV naloga zaključena za projekt: %s, status: %s", project, result["status"])
    return result
