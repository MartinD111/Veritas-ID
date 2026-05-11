"""
VeritasEngine – jedro za LLM sklepanje z Gemma 4 multimodalnim modelom.
Implementira ničelno-znanjsko obdelavo slik v RAM-u.
"""

import base64
import gc
import json
import logging
from typing import Any, Optional

from llama_cpp import Llama
from llama_cpp.llama_chat_format import Llava15ChatHandler

from config import settings

logger = logging.getLogger(__name__)

# Sistemski poziv – samo OCR ekstrakcija, primerjava obrazov poteka v InsightFace
_SYSTEM_PROMPT = """Si Veritas OCR sistem za branje osebnih dokumentov.
Iz slike dokumenta izvleci besedilne podatke.
OBVEZNO vrni SAMO veljavno JSON obliko – brez dodatnega besedila:
{
  "status": "approved|rejected|manual_review",
  "user_name": "<ime in priimek iz dokumenta>",
  "age_verified": true|false,
  "ocr_data": {
    "document_number": "<številka dokumenta ali null>",
    "date_of_birth": "<datum rojstva DD.MM.LLLL ali null>",
    "expiry_date": "<datum poteka DD.MM.LLLL ali null>",
    "nationality": "<državljanstvo ali null>"
  }
}

Pravila:
- status=approved: dokument je veljavne oblike in starost >= 18
- status=rejected: dokument nevelaven, ponarejen ali oseba < 18 let
- status=manual_review: kakovost slike premajhna za zanesljivo branje
- NE ocenjuj ujemanja obrazov – to opravi ločen sistem
"""

_USER_PROMPT_ID_ONLY = "Izvleci besedilne podatke iz osebnega dokumenta na sliki. Vrni JSON."


def _sniff_mime(data: bytes) -> str:
    """
    Ugotovi MIME tip slike iz magic bytov.
    Podprti formati: JPEG, PNG, WebP.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # Privzeto JPEG – model je toleranten
    logger.warning("Neznan tip slike, predpostavljam JPEG")
    return "image/jpeg"


def _to_data_uri(data: bytes) -> str:
    """Pretvori surove bajte slike v base64 data URI za LLM."""
    mime = _sniff_mime(data)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _extract_first_json(text: str) -> dict[str, Any]:
    """
    Robustno izvlečenje prvega JSON objekta iz besedila.
    Uporablja štetje oklepajev – deluje tudi če model ovije JSON v markdown.
    """
    depth = 0
    start = None

    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # Poskusi popraviti escaped narekovaje
                    cleaned = candidate.replace('\\"', '"')
                    return json.loads(cleaned)

    raise ValueError(f"Ni najden veljavni JSON v odgovoru modela: {text[:200]!r}")


def _validate_output(raw: dict[str, Any]) -> dict[str, Any]:
    """Preveri in normalizira izhod modela na pričakovano shemo."""
    allowed_statuses = {"approved", "rejected", "manual_review"}
    status = str(raw.get("status", "manual_review")).lower()
    if status not in allowed_statuses:
        status = "manual_review"

    raw_ocr = raw.get("ocr_data") or {}
    ocr_data = {
        "document_number": raw_ocr.get("document_number"),
        "date_of_birth": raw_ocr.get("date_of_birth"),
        "expiry_date": raw_ocr.get("expiry_date"),
        "nationality": raw_ocr.get("nationality"),
    }

    return {
        "status": status,
        "user_name": str(raw.get("user_name", "")).strip() or "Neznano",
        "age_verified": bool(raw.get("age_verified", False)),
        "ocr_data": ocr_data,
    }


class VeritasEngine:
    """
    Enkratna instanca za eno verifikacijsko zahtevo.
    Model se inicializira, uporabi in TAKOJ uniči – nič ni v dolgotrajnem RAM-u.
    """

    def __init__(self) -> None:
        # Preveri, da modeli obstajajo pred inicializacijo (absolutna pot glede na koren projekta)
        llm_path = settings.llm_path_absolute()
        mmproj_path = settings.mmproj_path_absolute()

        if not llm_path.is_file():
            raise FileNotFoundError(
                f"LLM model ni najden: {llm_path}. "
                "Nastavi LLM_MODEL_PATH v .env datoteki."
            )
        if not mmproj_path.is_file():
            raise FileNotFoundError(
                f"Vision projektor ni najden: {mmproj_path}. "
                "Nastavi MMPROJ_MODEL_PATH v .env datoteki."
            )

        logger.info("Inicializacija Gemma 4 multimodalnega modela...")

        # Llava15ChatHandler omogoči vision na C nivoju
        self._chat_handler = Llava15ChatHandler(
            clip_model_path=str(mmproj_path),
            verbose=False,
        )

        self._llm = Llama(
            model_path=str(llm_path),
            chat_handler=self._chat_handler,
            n_ctx=settings.llm_context_size,
            n_gpu_layers=settings.llm_gpu_layers,
            verbose=False,
        )
        logger.info("Model uspešno inicializiran")

    def verify(
        self,
        id_bytes: bytes,
        selfie_bytes: Optional[bytes] = None,
    ) -> dict[str, Any]:
        """
        Izvede verifikacijo identitete.
        id_bytes in selfie_bytes ostanejo SAMO v RAM-u.
        """
        try:
            return self._run_inference(id_bytes, selfie_bytes)
        finally:
            # Ničelno-znanjsko čiščenje – model in slike uničimo takoj po sklepanju
            self._destroy()

    def _run_inference(
        self,
        id_bytes: bytes,
        selfie_bytes: Optional[bytes] = None,
    ) -> dict[str, Any]:
        id_uri = _to_data_uri(id_bytes)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": id_uri}},
                    {"type": "text", "text": _USER_PROMPT_ID_ONLY},
                ],
            },
        ]

        logger.info("Pošiljam zahtevo modelu Gemma 4 (samo OCR)...")
        response = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=512,
            temperature=0.1,
        )

        raw_text: str = response["choices"][0]["message"]["content"]
        logger.info("Model vrnil odgovor, razčlenjujem JSON...")

        del id_uri, id_bytes, messages, response

        raw_json = _extract_first_json(raw_text)
        return _validate_output(raw_json)

    def _destroy(self) -> None:
        """Eksplicitno uniči vse llama objekte in sprosti RAM."""
        try:
            if hasattr(self, "_llm") and self._llm is not None:
                del self._llm
                self._llm = None
            if hasattr(self, "_chat_handler") and self._chat_handler is not None:
                del self._chat_handler
                self._chat_handler = None
        except Exception as exc:
            logger.warning("Napaka med čiščenjem modela: %s", exc)
        finally:
            collected = gc.collect()
            logger.info("Garbage collector pobral %d objektov po inferenci", collected)
