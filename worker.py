"""
Celery worker for async IDV tasks.
On Windows run with: celery -A worker worker --pool=solo --loglevel=info

Processing pipeline (Fast Path → Manual VAV Gate):
  1. InsightFace  – face detection on left half of ID Front + face match vs best selfie
  2. MRZ parser   – pytesseract + regex OCR of ID Back, extracts DOB, verifies 18+
  3. Gate         – if EITHER step 1 or 2 fails, task pauses as REQUIRES_VAV_CONFIRMATION
  4. VAV System   – ONLY after explicit user confirmation via /verify/trigger-vav/{task_id}
                    • face failure → image enhancement then re-run InsightFace
                    • MRZ failure  → VAV System OCR on ID Front for DOB

Country-specific extensions:
  KR (South Korea) – PASS API stub; prioritises phone-based identity data
  TH (Thailand)    – Laser ID format validation + API stub
  JP (Japan)       – VAV prompt handles My Number card; Japanese era → Gregorian conversion
"""

import json
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
# Redis key pattern for VAV gate state: vav_gate:{task_id} → JSON payload
_VAV_GATE_KEY = "vav_gate:{}"
# Legacy alias so main.py import doesn't break during transition
_GEMMA_GATE_KEY = _VAV_GATE_KEY

# ── Manual Review Queue ───────────────────────────────────────────────────────
# Redis key pattern for admin review payload: review_item:{task_id} → JSON
# TTL: 24 hours — images are purged automatically if never actioned (GDPR §5(1)(e))
_REVIEW_ITEM_KEY = "review_item:{}"
_REVIEW_QUEUE_KEY = "review_queue"        # Redis list of task_ids awaiting admin action
_REVIEW_IMAGE_TTL = 86_400               # 24 h in seconds


def push_review_item(
    task_id: str,
    project: str,
    country: str,
    user_email: str,
    reason: str,
    id_front_hex: str,
    id_back_hex: str,
    selfie_hex: Optional[str],
) -> None:
    """
    Store the minimum data needed for manual admin review in Redis with a 24 h TTL.
    Images are stored as hex strings — never written to disk — and are deleted the
    moment an admin actions the item (approve / reject) or TTL expires.
    """
    payload = json.dumps({
        "task_id":      task_id,
        "project":      project,
        "country":      country,
        "user_email":   user_email,
        "reason":       reason,
        "id_front_hex": id_front_hex,
        "id_back_hex":  id_back_hex,
        "selfie_hex":   selfie_hex,
        "queued_at":    datetime.utcnow().isoformat(),
    })
    pipe = _redis.pipeline()
    pipe.setex(_REVIEW_ITEM_KEY.format(task_id), _REVIEW_IMAGE_TTL, payload)
    # Prepend to queue list so newest items appear first
    pipe.lpush(_REVIEW_QUEUE_KEY, task_id)
    pipe.execute()
    logger.info("Review item queued | task=%s | project=%s", task_id, project)


def delete_review_item(task_id: str) -> None:
    """
    Immediately purge review images from Redis — called after admin action.
    Also removes the task_id from the queue list.
    """
    pipe = _redis.pipeline()
    pipe.delete(_REVIEW_ITEM_KEY.format(task_id))
    pipe.lrem(_REVIEW_QUEUE_KEY, 0, task_id)
    pipe.execute()
    logger.info("Review item purged | task=%s", task_id)

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
    id_face_detected=False triggers the VAV gate for face enhancement.
    """
    face_app = _get_face_app()

    id_img = _bytes_to_bgr(id_bytes)
    selfie_img = _bytes_to_bgr(selfie_bytes)

    if id_img is None or selfie_img is None:
        logger.warning("Could not decode image(s) for face comparison")
        return None, None, None, False

    id_face, cropped = _detect_id_face(id_img)
    if id_face is None:
        logger.warning("No face detected on ID document left half — will trigger VAV gate")
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
    """Parse YYMMDD from MRZ with dynamic pivot year.

    If yy > current two-digit year → person born in 19xx (must be adult).
    Otherwise → 20xx. This correctly handles e.g. yy=15 as 2015, not 1915.
    """
    if len(dob_field) < 6 or not dob_field[:6].isdigit():
        return None
    yy, mm, dd = int(dob_field[0:2]), int(dob_field[2:4]), int(dob_field[4:6])
    current_yy = date.today().year % 100
    year = 1900 + yy if yy > current_yy else 2000 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _is_18_plus(dob: date) -> bool:
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return age >= 18


def _heal_dob_field(dob_raw: str, expected_check: int) -> Optional[str]:
    """Iteratively replace OCR-confused letters with digits until checksum matches.

    Common Tesseract confusions: O↔0, S↔5, I↔1, Z↔2, B↔8, G↔6, T↔1.
    """
    SUBSTITUTIONS = {"O": "0", "S": "5", "I": "1", "Z": "2", "B": "8", "G": "6", "T": "1"}
    candidates = [dob_raw]
    seen: set[str] = {dob_raw}
    for candidate in candidates:
        for idx, ch in enumerate(candidate):
            if ch in SUBSTITUTIONS:
                healed = candidate[:idx] + SUBSTITUTIONS[ch] + candidate[idx + 1:]
                if healed not in seen:
                    seen.add(healed)
                    if _mrz_check_digit(healed) == expected_check:
                        logger.info("MRZ self-heal: '%s' → '%s'", dob_raw, healed)
                        return healed
                    candidates.append(healed)
    return None


def _extract_td3(line1: str, line2: str) -> Optional[dict]:
    if len(line2) < 44:
        return None
    dob_raw   = line2[13:19]
    dob_check = line2[19]
    if dob_check.isdigit():
        if _mrz_check_digit(dob_raw) != int(dob_check):
            healed = _heal_dob_field(dob_raw, int(dob_check))
            if healed:
                dob_raw = healed
            else:
                logger.warning("MRZ TD-3: bad DOB check digit — self-heal failed")
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
    if dob_check.isdigit():
        if _mrz_check_digit(dob_raw) != int(dob_check):
            healed = _heal_dob_field(dob_raw, int(dob_check))
            if healed:
                dob_raw = healed
            else:
                logger.warning("MRZ TD-1: bad DOB check digit — self-heal failed")
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


def _retesseract_dob_segment(preprocessed: np.ndarray, dob_raw: str, pytesseract) -> str:
    """Re-OCR only the 6-char DOB slice with digits-only whitelist."""
    if dob_raw.isdigit():
        return dob_raw

    h, w = preprocessed.shape[:2]
    char_w = w // 44
    for dob_start_char, line_len in [(13, 44), (0, 30)]:
        cw = w // line_len
        x1 = dob_start_char * cw
        x2 = x1 + 6 * cw
        x1, x2 = max(0, x1), min(w, x2)
        strip = preprocessed[:, x1:x2]
        if strip.size == 0:
            continue
        raw = pytesseract.image_to_string(
            strip,
            config="--psm 7 -c tessedit_char_whitelist=0123456789",
        )
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 6:
            candidate = digits[:6]
            logger.info("Digit-only re-OCR for DOB: '%s' → '%s'", dob_raw, candidate)
            return candidate
    return dob_raw


def _run_fast_mrz(id_back_bytes: bytes) -> Optional[dict]:
    """
    Fast Path MRZ parser. Returns None if no valid DOB could be extracted,
    which triggers the VAV confirmation gate.
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

    try:
        from mrz.checker.td3 import TD3CodeChecker  # noqa: F401
        from mrz.checker.td1 import TD1CodeChecker  # noqa: F401
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

    lines = [l for l in raw_text.splitlines() if len(l) >= 30]
    for i in range(len(lines) - 1):
        l2 = lines[i + 1]
        if len(l2) >= 44:
            raw_dob = l2[13:19]
            if not raw_dob.isdigit():
                fixed = _retesseract_dob_segment(preprocessed, raw_dob, pytesseract)
                l2_fixed = l2[:13] + fixed + l2[19:]
                r = _extract_td3(lines[i], l2_fixed)
                if r:
                    logger.info("MRZ TD-3 parsed via digit-only DOB re-pass")
                    return r
    for i in range(len(lines) - 2):
        l2 = lines[i + 1]
        if len(l2) >= 30:
            raw_dob = l2[0:6]
            if not raw_dob.isdigit():
                fixed = _retesseract_dob_segment(preprocessed, raw_dob, pytesseract)
                l2_fixed = fixed + l2[6:]
                r = _extract_td1(lines[i], l2_fixed, lines[i + 2])
                if r:
                    logger.info("MRZ TD-1 parsed via digit-only DOB re-pass")
                    return r

    logger.warning("Fast Path MRZ: no valid DOB found — VAV gate required")
    return None


# ---------------------------------------------------------------------------
# VAV System helpers (Veritas Advanced Verification System)
# Only invoked after manual confirmation
# ---------------------------------------------------------------------------

def _enhance_image_for_face(id_front_bytes: bytes) -> bytes:
    """Light sharpening + contrast boost on ID Front so InsightFace gets a second chance."""
    img = _bytes_to_bgr(id_front_bytes)
    if img is None:
        return id_front_bytes

    blur      = cv2.GaussianBlur(img, (0, 0), 3)
    sharpened = cv2.addWeighted(img, 1.6, blur, -0.6, 0)

    lab = cv2.cvtColor(sharpened, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l     = clahe.apply(l)
    enhanced = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

    result = _bgr_to_bytes(enhanced, quality=95)
    return result if result else id_front_bytes


def _run_vav_ocr(id_front_bytes: bytes, country: str = "EU") -> dict:
    """
    Use the VAV System to extract DOB from ID Front image.
    For Japan (JP): the prompt instructs the model to handle My Number cards
    and convert Japanese eras (Reiwa/Heisei/Showa) to Gregorian dates.
    Cost: 0.00€ (Internal Compute).
    """
    if country == "JP":
        logger.warning("MRZ Fast Path failed — running VAV System (JP/My Number) on ID Front")
    else:
        logger.warning("MRZ Fast Path failed — running VAV System focused DOB extraction on ID Front")

    from engine import VeritasEngine
    engine = VeritasEngine()
    try:
        # Pass country so the engine can adapt its prompt
        if hasattr(engine, "verify_dob_country"):
            return engine.verify_dob_country(id_front_bytes, country=country)
        return engine.verify_dob(id_front_bytes)
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
# Country-specific verification helpers
# ---------------------------------------------------------------------------

# --- South Korea: PASS API stub ---
def _run_pass_api_stub(project: str, task_id: str) -> dict:
    """
    Placeholder for the Korean PASS API (통신사 본인인증).
    In production replace with a signed HTTP call to PASS (SKT/KT/LGU+).
    Returns a dict with the same shape as ocr_result.
    Logs a fixed cost per call.
    """
    logger.info("KR: PASS API stub invoked for project=%s task=%s", project, task_id)
    try:
        from cost_tracker import log_transaction
        log_transaction(
            task_id=task_id, project=project, country="KR",
            cost_type="pass_api", status="success",
            extra={"note": "PASS API stub — live integration pending"},
        )
    except Exception:
        pass
    return {
        "status": "manual_review",
        "user_name": "Unknown",
        "age_verified": False,
        "ocr_data": {"pass_api": "stub_ok"},
        "ocr_source": "pass_api_stub",
    }


# --- Thailand: Laser ID format validator ---
_LASER_ID_RE = re.compile(r"^[A-Z]{2}\d{7}[A-Z0-9]{2}$")


def _validate_laser_id(laser_id: str) -> bool:
    """
    Thai Laser ID format: 2 uppercase letters + 7 digits + 2 alphanumeric chars.
    Example: AB1234567C8
    """
    return bool(_LASER_ID_RE.match(laser_id.strip().upper()))


def _run_laser_id_stub(laser_id: str, project: str, task_id: str) -> dict:
    """Validate Laser ID format and call the API stub. Logs cost per call."""
    valid = _validate_laser_id(laser_id)
    logger.info("TH: Laser ID validation for project=%s valid=%s", project, valid)
    try:
        from cost_tracker import log_transaction
        log_transaction(
            task_id=task_id, project=project, country="TH",
            cost_type="laser_id",
            status="success" if valid else "failed",
            extra={"laser_id_valid": valid},
        )
    except Exception:
        pass
    return {
        "laser_id_valid": valid,
        "laser_id_source": "format_validation",
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
    selfie_bytes_hex: Optional[str],
    all_selfie_bytes_hex: Optional[list],
    project: str,
    nfc_dob_iso: Optional[str] = None,
    vav_authorized: bool = False,
    vav_mode: Optional[str] = None,       # "face" | "mrz"
    country: str = "EU",
    laser_id: Optional[str] = None,       # Thailand Laser ID string
    # Legacy aliases from old API callers
    gemma_authorized: bool = False,
    gemma_mode: Optional[str] = None,
) -> dict:
    """
    Fast Path → Manual VAV Gate pipeline.

    On first call (vav_authorized=False):
      1. Country-specific pre-checks (PASS for KR, Laser ID for TH)
      2. InsightFace face detection on left half of ID Front + selfie match
      3. MRZ parser on ID Back
      4. If either step 2-3 fails → store gate state in Redis, return REQUIRES_VAV_CONFIRMATION

    On re-trigger (vav_authorized=True):
      • vav_mode="face" → enhance image, retry InsightFace
      • vav_mode="mrz"  → VAV System OCR on ID Front for DOB (JP-aware)

    Arguments are hex-encoded so they survive JSON serialisation.
    """
    # Support legacy field names from old API callers
    if gemma_authorized and not vav_authorized:
        vav_authorized = gemma_authorized
    if gemma_mode and not vav_mode:
        vav_mode = gemma_mode

    logger.info(
        "Starting IDV task | project=%s | country=%s | vav_authorized=%s",
        project, country, vav_authorized,
    )

    task_id = self.request.id

    id_front_bytes: bytes = bytes.fromhex(id_front_bytes_hex)
    id_back_bytes:  bytes = bytes.fromhex(id_back_bytes_hex)
    selfie_bytes: Optional[bytes] = bytes.fromhex(selfie_bytes_hex) if selfie_bytes_hex else None

    # ── Country pre-checks ──────────────────────────────────────────────────

    country_extra: dict = {}

    if country == "KR" and not vav_authorized:
        # PASS API stub — logs its own cost
        pass_result = _run_pass_api_stub(project, task_id)
        country_extra["pass_api"] = pass_result.get("ocr_data", {})

    if country == "TH" and laser_id:
        laser_result = _run_laser_id_stub(laser_id, project, task_id)
        country_extra["laser_id"] = laser_result

    # ── Step 1: Face comparison ─────────────────────────────────────────────
    face_match: Optional[bool]  = None
    face_score: Optional[float] = None
    has_selfie = selfie_bytes is not None
    id_face_detected = True

    if has_selfie:
        if vav_authorized and vav_mode == "face":
            enhanced_bytes = _enhance_image_for_face(id_front_bytes)
            face_match, face_score, _, id_face_detected = _compare_faces(enhanced_bytes, selfie_bytes)
            logger.info(
                "VAV-enhanced face comparison: match=%s score=%s detected=%s",
                face_match, face_score, id_face_detected,
            )
        else:
            face_match, face_score, _, id_face_detected = _compare_faces(id_front_bytes, selfie_bytes)

    # ── Step 2: OCR / MRZ ──────────────────────────────────────────────────
    ocr_result: dict
    ocr_source: str
    cost_type_used: str = "mrz_fast_path"

    if nfc_dob_iso:
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
            cost_type_used = "nfc"
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
        if vav_authorized and vav_mode == "mrz":
            ocr_result = _run_vav_ocr(id_front_bytes, country=country)
            ocr_source = "vav_system"
            cost_type_used = "vav_system"
            mrz_failed = False
        else:
            mrz_data = _run_fast_mrz(id_back_bytes)
            if mrz_data is not None:
                ocr_result = _build_ocr_result_from_mrz(mrz_data)
                ocr_source = "mrz_fast_path"
                cost_type_used = "mrz_fast_path"
                mrz_failed = False
            else:
                mrz_failed = True

    # ── Step 3: VAV gate check ──────────────────────────────────────────────
    if not vav_authorized:
        failures = []
        if has_selfie and not id_face_detected:
            failures.append("face_detection_failed")
        if mrz_failed:
            failures.append("mrz_dob_extraction_failed")

        if failures:
            reason_map = {
                "face_detection_failed":     "InsightFace could not detect a face on the ID document (image too blurry or dark).",
                "mrz_dob_extraction_failed": "The MRZ parser could not extract a valid Date of Birth from the ID back.",
            }
            reason = " | ".join(reason_map[f] for f in failures)
            vav_mode_needed = "face" if "face_detection_failed" in failures else "mrz"

            _redis.setex(
                _VAV_GATE_KEY.format(task_id),
                3600,
                json.dumps({
                    "id_front_hex":   id_front_bytes_hex,
                    "id_back_hex":    id_back_bytes_hex,
                    "selfie_hex":     selfie_bytes_hex,
                    "all_selfie_hex": all_selfie_bytes_hex,
                    "project":        project,
                    "nfc_dob_iso":    nfc_dob_iso,
                    "vav_mode":       vav_mode_needed,
                    "failures":       failures,
                    "reason":         reason,
                    "country":        country,
                    "laser_id":       laser_id,
                }),
            )
            logger.warning("VAV gate triggered | task=%s | failures=%s", task_id, failures)

            # Push images into the admin manual review queue (24 h TTL, GDPR-safe)
            # user_email is unknown at worker level — admin can see project + task_id
            push_review_item(
                task_id=task_id,
                project=project,
                country=country,
                user_email="",          # populated by client webhook if available
                reason=reason,
                id_front_hex=id_front_bytes_hex,
                id_back_hex=id_back_bytes_hex,
                selfie_hex=selfie_bytes_hex,
            )

            self.update_state(
                state="REQUIRES_VAV_CONFIRMATION",
                meta={"vav_reason": reason, "failures": failures},
            )
            return {
                "status":     "requires_vav_confirmation",
                "vav_reason": reason,
                "failures":   failures,
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
        "country":      country,
        **({k: v for k, v in country_extra.items()} if country_extra else {}),
    }

    # ── Cost tracking ───────────────────────────────────────────────────────
    try:
        from cost_tracker import log_transaction
        log_transaction(
            task_id=task_id,
            project=project,
            country=country,
            cost_type=cost_type_used,  # type: ignore[arg-type]
            status=status if status in ("success", "failed", "manual_review") else "success",
            extra={"ocr_source": ocr_source, "face_match": face_match},
        )
    except Exception:
        logger.exception("Failed to log cost transaction for task %s", task_id)

    logger.info(
        "IDV task done | project=%s | country=%s | status=%s | face_match=%s | source=%s",
        project, country, result["status"], result["face_match"], ocr_source,
    )
    return result
