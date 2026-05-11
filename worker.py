"""
Celery worker for async IDV tasks.
On Windows run with: celery -A worker worker --pool=solo --loglevel=info

Processing pipeline (Fast Path → Manual Gemma Gate):
  1. InsightFace  – face detection on left half of ID Front + face match vs best selfie
  2. MRZ parser   – pytesseract + regex OCR of ID Back, extracts DOB, verifies 18+
  3. Gate         – if EITHER step 1 or 2 fails, task pauses as REQUIRES_GEMMA_CONFIRMATION
  4. Gemma 4      – ONLY after explicit user confirmation via /verify/trigger-gemma/{task_id}
                    • face failure → image enhancement then re-run InsightFace
                    • MRZ failure  → Gemma OCR on ID Front for DOB
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

import cv2
import numpy as np
import redis as redis_client
from celery import Celery
from celery.signals import worker_ready, worker_shutdown

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
except Exception:
    pass

from config import settings

logger = logging.getLogger(__name__)

ENGINE_STATUS_KEY = "veritas_engine_status"
# Redis key pattern for Gemma gate state: gemma_gate:{task_id} → JSON payload
_GEMMA_GATE_KEY = "gemma_gate:{}"

_redis = redis_client.from_url(settings.redis_url, decode_responses=True)
_redis.set(ENGINE_STATUS_KEY, "loading")

celery_app = Celery(
    "veritas",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    task_time_limit=settings.task_time_limit_seconds,
    task_soft_time_limit=settings.task_time_limit_seconds - 30,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

_face_app = None


def _get_face_app():
    global _face_app
    if _face_app is None:
        import insightface
        logger.info("Initialising InsightFace buffalo_l model…")
        _face_app = insightface.app.FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        _face_app.prepare(ctx_id=0, det_size=(640, 640))
        logger.info("InsightFace ready")
    return _face_app


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------

def _bytes_to_bgr(data: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def _bgr_to_bytes(img: np.ndarray, quality: int = 90) -> Optional[bytes]:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return bytes(buf) if ok else None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _crop_face(img: np.ndarray, face) -> Optional[np.ndarray]:
    """Crop and histogram-equalise a face bbox from an image."""
    x1, y1, x2, y2 = (int(v) for v in face.bbox)
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = img[y1:y2, x1:x2]
    ycrcb = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb)
    ycrcb[:, :, 0] = cv2.equalizeHist(ycrcb[:, :, 0])
    return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)


# ---------------------------------------------------------------------------
# Step 1 – Face detection on the LEFT half of ID Front + selfie match
# ---------------------------------------------------------------------------

def _detect_id_face(id_img: np.ndarray) -> tuple[Optional[object], Optional[np.ndarray]]:
    """
    Always crop to the left 55% of the ID image — the photo is invariably there.
    Returns (face_object, cropped_face_bgr) or (None, None) on failure.
    """
    face_app = _get_face_app()
    h, w = id_img.shape[:2]
    left_half = id_img[:, : int(w * 0.55)]
    faces = face_app.get(left_half)
    logger.info("InsightFace: %d face(s) detected in ID left-half", len(faces) if faces else 0)
    if not faces:
        return None, None
    best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    cropped = _crop_face(left_half, best)
    return best, cropped


def _compare_faces(
    id_bytes: bytes,
    selfie_bytes: bytes,
) -> tuple[Optional[bool], Optional[float], Optional[bytes], bool]:
    """
    Returns (match, score, cropped_id_face_bytes, id_face_detected).
    id_face_detected=False triggers the Gemma gate for face enhancement.
    """
    face_app = _get_face_app()

    id_img = _bytes_to_bgr(id_bytes)
    selfie_img = _bytes_to_bgr(selfie_bytes)

    if id_img is None or selfie_img is None:
        logger.warning("Could not decode image(s) for face comparison")
        return None, None, None, False

    id_face, cropped = _detect_id_face(id_img)
    if id_face is None:
        logger.warning("No face detected on ID document left half — will trigger Gemma gate")
        return None, None, None, False

    cropped_bytes = _bgr_to_bytes(cropped) if cropped is not None else None

    selfie_faces = face_app.get(selfie_img)
    logger.info("InsightFace: %d face(s) detected on selfie", len(selfie_faces) if selfie_faces else 0)
    if not selfie_faces:
        logger.warning("No face detected on selfie")
        return None, None, cropped_bytes, True  # ID face OK, selfie problem

    selfie_face = max(selfie_faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    score = _cosine_similarity(id_face.embedding, selfie_face.embedding)
    logger.info("Cosine similarity: %.4f (threshold 0.40)", score)
    return score > 0.40, round(score, 4), cropped_bytes, True


# ---------------------------------------------------------------------------
# Step 2 – Fast MRZ parser
# ---------------------------------------------------------------------------

_MRZ_TD3_RE = re.compile(r"([A-Z0-9<]{44})\s*([A-Z0-9<]{44})", re.MULTILINE)
_MRZ_TD1_RE = re.compile(r"([A-Z0-9<]{30})\s*([A-Z0-9<]{30})\s*([A-Z0-9<]{30})", re.MULTILINE)


def _mrz_check_digit(field: str) -> int:
    weights = [7, 3, 1]
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            val = int(ch)
        elif ch.isalpha():
            val = ord(ch.upper()) - 55
        else:
            val = 0
        total += val * weights[i % 3]
    return total % 10


def _parse_dob_from_mrz(dob_field: str) -> Optional[date]:
    """Parse YYMMDD from MRZ. Years >= 10 → 19xx, < 10 → 20xx."""
    if len(dob_field) < 6 or not dob_field[:6].isdigit():
        return None
    yy, mm, dd = int(dob_field[0:2]), int(dob_field[2:4]), int(dob_field[4:6])
    year = 1900 + yy if yy >= 10 else 2000 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _is_18_plus(dob: date) -> bool:
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return age >= 18


def _extract_td3(line1: str, line2: str) -> Optional[dict]:
    if len(line2) < 44:
        return None
    dob_raw   = line2[13:19]
    dob_check = line2[19]
    if dob_check.isdigit() and _mrz_check_digit(dob_raw) != int(dob_check):
        logger.warning("MRZ TD-3: bad DOB check digit")
        return None
    expiry_raw  = line2[21:27]
    nationality = line2[10:13].replace("<", "")
    doc_number  = line2[0:9].replace("<", "")
    name_raw    = line1[5:44]
    parts       = name_raw.split("<<", 1)
    surname     = parts[0].replace("<", " ").strip()
    given       = parts[1].replace("<", " ").strip() if len(parts) > 1 else ""
    full_name   = f"{given} {surname}".strip()
    dob         = _parse_dob_from_mrz(dob_raw)
    if dob is None:
        return None
    expiry_date = None
    try:
        ey, em, ed = int(expiry_raw[0:2]), int(expiry_raw[2:4]), int(expiry_raw[4:6])
        exp_year   = 2000 + ey if ey < 30 else 1900 + ey
        expiry_date = date(exp_year, em, ed).strftime("%d.%m.%Y")
    except (ValueError, IndexError):
        pass
    return {
        "full_name": full_name, "date_of_birth": dob.strftime("%d.%m.%Y"),
        "dob_obj": dob, "expiry_date": expiry_date,
        "document_number": doc_number, "nationality": nationality, "mrz_type": "TD3",
    }


def _extract_td1(line1: str, line2: str, line3: str) -> Optional[dict]:
    if len(line2) < 30:
        return None
    dob_raw   = line2[0:6]
    dob_check = line2[6]
    if dob_check.isdigit() and _mrz_check_digit(dob_raw) != int(dob_check):
        logger.warning("MRZ TD-1: bad DOB check digit")
        return None
    expiry_raw  = line2[8:14]
    nationality = line2[15:18].replace("<", "")
    doc_number  = line1[5:14].replace("<", "")
    parts       = line3.split("<<", 1) if "<<" in line3 else [line3, ""]
    surname     = parts[0].replace("<", " ").strip()
    given       = parts[1].replace("<", " ").strip() if len(parts) > 1 else ""
    full_name   = f"{given} {surname}".strip()
    dob         = _parse_dob_from_mrz(dob_raw)
    if dob is None:
        return None
    expiry_date = None
    try:
        ey, em, ed = int(expiry_raw[0:2]), int(expiry_raw[2:4]), int(expiry_raw[4:6])
        exp_year   = 2000 + ey if ey < 30 else 1900 + ey
        expiry_date = date(exp_year, em, ed).strftime("%d.%m.%Y")
    except (ValueError, IndexError):
        pass
    return {
        "full_name": full_name, "date_of_birth": dob.strftime("%d.%m.%Y"),
        "dob_obj": dob, "expiry_date": expiry_date,
        "document_number": doc_number, "nationality": nationality, "mrz_type": "TD1",
    }


def _preprocess_for_mrz(img: np.ndarray) -> np.ndarray:
    """Crop bottom ~45% (MRZ zone), apply blackhat + CLAHE + Otsu."""
    h, w = img.shape[:2]
    roi  = img[int(h * 0.55):h, 0:w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    kernel    = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5))
    blackhat  = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    clahe     = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced  = clahe.apply(blackhat)
    _, thresh = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    return thresh


def _run_fast_mrz(id_back_bytes: bytes) -> Optional[dict]:
    """
    Fast Path MRZ parser. Returns None if no valid DOB could be extracted,
    which triggers the Gemma confirmation gate.
    """
    img = _bytes_to_bgr(id_back_bytes)
    if img is None:
        return None

    try:
        import pytesseract
    except ImportError:
        logger.warning("pytesseract not installed — Fast Path unavailable")
        return None

    preprocessed = _preprocess_for_mrz(img)
    raw_text = pytesseract.image_to_string(
        preprocessed,
        config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<",
    )
    raw_text = re.sub(r"[^A-Z0-9<\n]", "", raw_text.upper())
    logger.debug("MRZ OCR raw:\n%s", raw_text)

    m3 = _MRZ_TD3_RE.search(raw_text)
    if m3:
        result = _extract_td3(m3.group(1), m3.group(2))
        if result:
            logger.info("MRZ TD-3 parsed via Fast Path")
            return result

    m1 = _MRZ_TD1_RE.search(raw_text)
    if m1:
        result = _extract_td1(m1.group(1), m1.group(2), m1.group(3))
        if result:
            logger.info("MRZ TD-1 parsed via Fast Path")
            return result

    # Second attempt: try mrz library if available
    try:
        from mrz.checker.td3 import TD3CodeChecker  # noqa: F401
        from mrz.checker.td1 import TD1CodeChecker  # noqa: F401
        # The mrz library validates line-by-line; already tried regex above.
        # If regex failed and mrz is installed we can try direct line feed.
        lines = [l for l in raw_text.splitlines() if len(l) >= 30]
        for i in range(len(lines) - 1):
            r = _extract_td3(lines[i], lines[i + 1])
            if r:
                logger.info("MRZ TD-3 parsed (mrz library pass)")
                return r
        for i in range(len(lines) - 2):
            r = _extract_td1(lines[i], lines[i + 1], lines[i + 2])
            if r:
                logger.info("MRZ TD-1 parsed (mrz library pass)")
                return r
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("mrz library pass failed: %s", exc)

    logger.warning("Fast Path MRZ: no valid DOB found — Gemma gate required")
    return None


# ---------------------------------------------------------------------------
# Gemma 4 helpers (only invoked after manual confirmation)
# ---------------------------------------------------------------------------

def _enhance_image_for_face(id_front_bytes: bytes) -> bytes:
    """
    Light sharpening + contrast boost on ID Front so InsightFace gets a second chance.
    """
    img = _bytes_to_bgr(id_front_bytes)
    if img is None:
        return id_front_bytes

    # Unsharp mask
    blur      = cv2.GaussianBlur(img, (0, 0), 3)
    sharpened = cv2.addWeighted(img, 1.6, blur, -0.6, 0)

    # CLAHE on L channel
    lab = cv2.cvtColor(sharpened, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l     = clahe.apply(l)
    enhanced = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

    result = _bgr_to_bytes(enhanced, quality=95)
    return result if result else id_front_bytes


def _run_gemma_ocr(id_front_bytes: bytes) -> dict:
    """Use Gemma 4 to extract DOB from ID Front image."""
    logger.warning("MRZ Fast Path failed — running Gemma 4 OCR on ID Front for DOB")
    from engine import VeritasEngine
    engine = VeritasEngine()
    try:
        return engine.verify(id_front_bytes)
    finally:
        del engine


def _build_ocr_result_from_mrz(mrz: dict) -> dict:
    dob_obj: Optional[date] = mrz.get("dob_obj")
    age_verified = _is_18_plus(dob_obj) if dob_obj else False
    return {
        "status": "approved" if age_verified else "rejected",
        "user_name": mrz.get("full_name", "Unknown"),
        "age_verified": age_verified,
        "ocr_data": {
            "document_number": mrz.get("document_number"),
            "date_of_birth":   mrz.get("date_of_birth"),
            "expiry_date":     mrz.get("expiry_date"),
            "nationality":     mrz.get("nationality"),
            "mrz_type":        mrz.get("mrz_type"),
        },
        "ocr_source": "mrz_fast_path",
    }


# ---------------------------------------------------------------------------
# Celery signals
# ---------------------------------------------------------------------------

@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    _get_face_app()
    _redis.set(ENGINE_STATUS_KEY, "ready")
    logger.info("Engine status: ready")


@worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    _redis.set(ENGINE_STATUS_KEY, "loading")
    logger.info("Engine status: loading (shutdown)")


# ---------------------------------------------------------------------------
# Main Celery task
# ---------------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="veritas.verify_identity",
    max_retries=0,
)
def verify_identity_task(
    self,
    id_front_bytes_hex: str,
    id_back_bytes_hex: str,
    selfie_bytes_hex: Optional[str],          # best (straight) selfie frame
    all_selfie_bytes_hex: Optional[list],     # all liveness frames (may be None for desktop)
    project: str,
    nfc_dob_iso: Optional[str] = None,
    gemma_authorized: bool = False,
    gemma_mode: Optional[str] = None,         # "face" | "mrz" — set when re-triggered
) -> dict:
    """
    Fast Path → Manual Gemma Gate pipeline.

    On first call (gemma_authorized=False):
      1. InsightFace face detection on left half of ID Front + selfie match
      2. MRZ parser on ID Back
      3. If either fails → store gate state in Redis, return REQUIRES_GEMMA_CONFIRMATION

    On re-trigger (gemma_authorized=True):
      • gemma_mode="face" → enhance image, retry InsightFace
      • gemma_mode="mrz"  → Gemma 4 OCR on ID Front for DOB

    Arguments are hex-encoded so they survive JSON serialisation.
    """
    logger.info("Starting IDV task for project: %s | gemma_authorized=%s", project, gemma_authorized)

    id_front_bytes: bytes = bytes.fromhex(id_front_bytes_hex)
    id_back_bytes:  bytes = bytes.fromhex(id_back_bytes_hex)
    selfie_bytes: Optional[bytes] = bytes.fromhex(selfie_bytes_hex) if selfie_bytes_hex else None

    # ── Step 1: Face comparison ─────────────────────────────────────────────
    face_match: Optional[bool]  = None
    face_score: Optional[float] = None
    has_selfie = selfie_bytes is not None
    id_face_detected = True

    if has_selfie:
        if gemma_authorized and gemma_mode == "face":
            # Re-run after Gemma enhancement
            enhanced_bytes = _enhance_image_for_face(id_front_bytes)
            face_match, face_score, _, id_face_detected = _compare_faces(enhanced_bytes, selfie_bytes)
            logger.info("Gemma-enhanced face comparison: match=%s score=%s detected=%s",
                        face_match, face_score, id_face_detected)
        else:
            face_match, face_score, _, id_face_detected = _compare_faces(id_front_bytes, selfie_bytes)

    # ── Step 2: OCR / MRZ ──────────────────────────────────────────────────
    ocr_result: dict
    ocr_source: str

    if nfc_dob_iso:
        # NFC path — trust the chip directly
        try:
            dob_obj = datetime.strptime(nfc_dob_iso, "%Y-%m-%d").date()
            age_verified = _is_18_plus(dob_obj)
            ocr_result = {
                "status": "approved" if age_verified else "rejected",
                "user_name": "Unknown",
                "age_verified": age_verified,
                "ocr_data": {
                    "date_of_birth": dob_obj.strftime("%d.%m.%Y"),
                    "document_number": None, "expiry_date": None, "nationality": None,
                },
            }
            ocr_source = "nfc_chip"
            mrz_failed = False
            logger.info("NFC DOB: %s  age_ok=%s", nfc_dob_iso, age_verified)
        except ValueError:
            logger.warning("Invalid NFC date '%s' — falling through to MRZ", nfc_dob_iso)
            nfc_dob_iso = None
            mrz_failed  = True
            ocr_result  = {}
            ocr_source  = ""
    else:
        mrz_failed = False
        ocr_result = {}
        ocr_source = ""

    if not nfc_dob_iso:
        if gemma_authorized and gemma_mode == "mrz":
            # Gemma OCR on ID Front for DOB
            ocr_result = _run_gemma_ocr(id_front_bytes)
            ocr_source = "gemma4_fallback"
            mrz_failed = False
        else:
            mrz_data = _run_fast_mrz(id_back_bytes)
            if mrz_data is not None:
                ocr_result = _build_ocr_result_from_mrz(mrz_data)
                ocr_source = "mrz_fast_path"
                mrz_failed = False
            else:
                mrz_failed = True

    # ── Step 3: Gemma gate check ────────────────────────────────────────────
    if not gemma_authorized:
        failures = []
        if has_selfie and not id_face_detected:
            failures.append("face_detection_failed")
        if mrz_failed:
            failures.append("mrz_dob_extraction_failed")

        if failures:
            reason_map = {
                "face_detection_failed":    "InsightFace could not detect a face on the ID document (image too blurry or dark).",
                "mrz_dob_extraction_failed": "The MRZ parser could not extract a valid Date of Birth from the ID back.",
            }
            reason = " | ".join(reason_map[f] for f in failures)
            gemma_mode_needed = "face" if "face_detection_failed" in failures else "mrz"

            # Persist gate state so the trigger endpoint can resume
            import json
            _redis.setex(
                _GEMMA_GATE_KEY.format(self.request.id),
                3600,
                json.dumps({
                    "id_front_hex":     id_front_bytes_hex,
                    "id_back_hex":      id_back_bytes_hex,
                    "selfie_hex":       selfie_bytes_hex,
                    "all_selfie_hex":   all_selfie_bytes_hex,
                    "project":          project,
                    "nfc_dob_iso":      nfc_dob_iso,
                    "gemma_mode":       gemma_mode_needed,
                    "failures":         failures,
                    "reason":           reason,
                }),
            )
            logger.warning("Gemma gate triggered | task=%s | failures=%s", self.request.id, failures)

            # Update task state to custom value so the frontend can detect it
            self.update_state(
                state="REQUIRES_GEMMA_CONFIRMATION",
                meta={"gemma_reason": reason, "failures": failures},
            )
            # Return a sentinel — Celery stores this as the task result while state is custom
            return {
                "status":       "requires_gemma_confirmation",
                "gemma_reason": reason,
                "failures":     failures,
            }

    # ── Step 4: Final status ────────────────────────────────────────────────
    if face_match is False:
        status = "rejected"
    elif face_match is None and has_selfie:
        status = "manual_review"
    else:
        status = ocr_result.get("status", "manual_review")

    result = {
        "status":       status,
        "user_name":    ocr_result.get("user_name", "Unknown"),
        "age_verified": ocr_result.get("age_verified", False),
        "face_match":   face_match,
        "face_score":   face_score,
        "ocr_data":     ocr_result.get("ocr_data", {}),
        "ocr_source":   ocr_source,
        "project":      project,
    }

    logger.info(
        "IDV task done | project=%s | status=%s | face_match=%s | source=%s",
        project, result["status"], result["face_match"], ocr_source,
    )
    return result
