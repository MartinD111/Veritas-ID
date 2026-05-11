import time
import streamlit as st
import requests
from streamlit_autorefresh import st_autorefresh

BACKEND_URL = "http://localhost:8000"
POLL_INTERVAL = 2
MAX_POLLS = 60

st.set_page_config(
    page_title="Veritas IDV - Dev Portal",
    page_icon="🔐",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    /* Global font and background */
    html, body, [class*="css"] {
        font-family: 'Inter', 'Segoe UI', sans-serif;
    }

    /* Header bar */
    .portal-header {
        background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
        border-radius: 12px;
        padding: 28px 36px;
        margin-bottom: 28px;
        display: flex;
        align-items: center;
        gap: 16px;
    }
    .portal-header h1 {
        color: #f0f9ff;
        font-size: 2rem;
        font-weight: 700;
        margin: 0;
        letter-spacing: -0.5px;
    }
    .portal-header p {
        color: #94a3b8;
        font-size: 0.9rem;
        margin: 4px 0 0 0;
    }
    .badge {
        background: #0ea5e9;
        color: white;
        font-size: 0.65rem;
        font-weight: 700;
        padding: 3px 10px;
        border-radius: 999px;
        letter-spacing: 1px;
        text-transform: uppercase;
        white-space: nowrap;
    }

    /* Section cards */
    .section-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 20px 24px;
        margin-bottom: 8px;
    }
    .section-label {
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 1.2px;
        text-transform: uppercase;
        color: #64748b;
        margin-bottom: 10px;
    }

    /* Result metric cards */
    .result-grid {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 14px;
        margin-top: 16px;
    }
    .metric-card {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 18px 20px;
        text-align: center;
    }
    .metric-card .metric-label {
        font-size: 0.72rem;
        color: #64748b;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 6px;
    }
    .metric-card .metric-value {
        font-size: 1.7rem;
        font-weight: 800;
        color: #0f172a;
    }
    .metric-card .metric-value.green { color: #16a34a; }
    .metric-card .metric-value.red   { color: #dc2626; }

    /* Hide Streamlit branding */
    #MainMenu, footer { visibility: hidden; }
    .stDeployButton { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Engine status sidebar ─────────────────────────────────────────────────────
def _get_engine_status() -> str:
    try:
        r = requests.get(f"{BACKEND_URL}/engine-status", timeout=3)
        return r.json().get("status", "loading")
    except Exception:
        return "unreachable"

_engine_status = _get_engine_status()

# Auto-refresh only while engine is still loading — stops once ready
if _engine_status != "ready":
    st_autorefresh(interval=5000, limit=None, key="engine_status_refresh")

with st.sidebar:
    st.markdown("### ⚙️ Stanje sistema")
    if _engine_status == "ready":
        st.success("🟢 **Gemma 4: Aktivna**", icon=None)
    elif _engine_status == "loading":
        st.warning("🟡 **Gemma 4: Nalaganje v RAM...**", icon=None)
    else:
        st.error("🔴 **Motor ni dosegljiv**", icon=None)
    st.caption(f"Posodobljeno vsake 5 s")

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="portal-header">
    <div style="font-size:2.2rem;">🔐</div>
    <div>
        <h1>Veritas IDV &nbsp;<span class="badge">Dev Portal</span></h1>
        <p>Interno orodje za testiranje identitetne verifikacije v realnem času</p>
    </div>
</div>
""", unsafe_allow_html=True)

# ── API Key ───────────────────────────────────────────────────────────────────
st.markdown('<div class="section-label">🔑 &nbsp;API avtentikacija</div>', unsafe_allow_html=True)
api_key = st.text_input(
    "X-API-Key",
    type="password",
    placeholder="vrt_...",
    help="Vnesi veljaven API ključ za dostop do Veritas IDV strežnika.",
    label_visibility="collapsed",
)

st.divider()

# ── File inputs ───────────────────────────────────────────────────────────────
col_id, col_selfie = st.columns(2, gap="large")

with col_id:
    st.markdown('<div class="section-label">🪪 &nbsp;Osebni dokument</div>', unsafe_allow_html=True)
    id_file = st.file_uploader(
        "Naloži sliko osebnega dokumenta",
        type=["jpg", "jpeg", "png"],
        help="Podprte oblike: JPG, PNG",
        label_visibility="collapsed",
    )
    if id_file:
        st.image(id_file, caption="Predogled dokumenta", use_container_width=True)

with col_selfie:
    st.markdown('<div class="section-label">🤳 &nbsp;Liveness selfie (kamera)</div>', unsafe_allow_html=True)
    selfie_file = st.camera_input(
        "Poslikaj se za preveritev živosti",
        help="Selfie se primerja z obrazom na dokumentu.",
        label_visibility="collapsed",
    )

st.divider()

# ── Submit ────────────────────────────────────────────────────────────────────
submit_disabled = not api_key or not id_file
submit = st.button(
    "🚀 &nbsp;Zaženi verifikacijo",
    type="primary",
    disabled=submit_disabled,
    use_container_width=True,
)

if not api_key:
    st.caption("⚠️ Vnesi API ključ za nadaljevanje.")
elif not id_file:
    st.caption("⚠️ Naloži sliko osebnega dokumenta za nadaljevanje.")

# ── Verification flow ─────────────────────────────────────────────────────────
if submit:
    st.divider()

    # 1. Send to backend
    with st.status("📡 &nbsp;Pošiljam v Zero-Knowledge RAM obdelavo...", expanded=True) as status_box:
        st.write("Pripravljam multipart zahtevo...")

        headers = {"X-API-Key": api_key}
        files: dict = {"id_document": (id_file.name, id_file.getvalue(), id_file.type)}
        if selfie_file:
            files["selfie"] = ("selfie.jpg", selfie_file.getvalue(), "image/jpeg")

        try:
            resp = requests.post(
                f"{BACKEND_URL}/verify",
                headers=headers,
                files=files,
                timeout=15,
            )
        except requests.exceptions.ConnectionError:
            status_box.update(label="❌ &nbsp;Strežnik ni dosegljiv", state="error")
            st.error(
                "**Strežnik ni dosegljiv.**\n\n"
                f"Preveri, ali FastAPI teče na `{BACKEND_URL}`. "
                "Zaženi ga z `uvicorn main:app --reload`.",
                icon="🔌",
            )
            st.stop()
        except requests.exceptions.Timeout:
            status_box.update(label="❌ &nbsp;Prekoračen čas zahteve", state="error")
            st.error("Zahteva je potekla (timeout 15 s). Poskusi znova.", icon="⏱️")
            st.stop()

        if resp.status_code == 401:
            status_box.update(label="❌ &nbsp;Napaka avtentikacije", state="error")
            st.error("**Neveljaven API ključ.** Preveri vrednost `X-API-Key`.", icon="🔑")
            st.stop()
        elif resp.status_code == 422:
            status_box.update(label="❌ &nbsp;Napaka validacije", state="error")
            st.error(f"Strežnik zavrnil zahtevo (422):\n```json\n{resp.text}\n```", icon="⚠️")
            st.stop()
        elif not resp.ok:
            status_box.update(label=f"❌ &nbsp;HTTP {resp.status_code}", state="error")
            st.error(f"Nepričakovan odgovor strežnika ({resp.status_code}):\n```\n{resp.text}\n```", icon="🚫")
            st.stop()

        task_id = resp.json().get("task_id")
        if not task_id:
            status_box.update(label="❌ &nbsp;Manjkajoč task_id", state="error")
            st.error(f"Strežnik ni vrnil `task_id`:\n```json\n{resp.text}\n```")
            st.stop()

        st.write(f"✅ &nbsp;Naloga ustvarjena — `{task_id}`")
        st.write("⏳ &nbsp;Čakam na rezultat (anketiram vsake 2 s)...")

        # 2. Poll for result
        result = None
        for poll_num in range(1, MAX_POLLS + 1):
            time.sleep(POLL_INTERVAL)
            try:
                poll_resp = requests.get(
                    f"{BACKEND_URL}/verify/status/{task_id}",
                    headers=headers,
                    timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                status_box.update(label="❌ &nbsp;Napaka pri anketiranju", state="error")
                st.error(f"Napaka pri anketiranju: {exc}", icon="🔌")
                st.stop()

            if not poll_resp.ok:
                status_box.update(label=f"❌ &nbsp;HTTP {poll_resp.status_code} pri anketiranju", state="error")
                st.error(f"Anketiranje vrnilo {poll_resp.status_code}:\n```\n{poll_resp.text}\n```")
                st.stop()

            payload = poll_resp.json()
            task_status = payload.get("state", "PENDING")

            st.write(f"↻ &nbsp;[{poll_num}/{MAX_POLLS}] &nbsp;Status: `{task_status}`")

            if task_status == "SUCCESS":
                result = payload.get("result", {})
                status_box.update(label="✅ &nbsp;Verifikacija zaključena", state="complete", expanded=False)
                break
            elif task_status in ("FAILURE", "REVOKED"):
                status_box.update(label=f"❌ &nbsp;Naloga zavrnjena: {task_status}", state="error")
                st.error(
                    f"Celery naloga se je končala z **{task_status}**.\n\n"
                    f"```json\n{payload}\n```",
                    icon="💥",
                )
                st.stop()
        else:
            status_box.update(label="⏱️ &nbsp;Prekoračen čas čakanja", state="error")
            st.warning(
                f"Naloga `{task_id}` ni bila zaključena v "
                f"{MAX_POLLS * POLL_INTERVAL} sekundah. Poskusi znova ali preveri Celery delavce.",
                icon="⏱️",
            )
            st.stop()

    # 3. Display results
    st.markdown("### 📊 &nbsp;Rezultati verifikacije")

    overall_ok = result.get("status") == "approved"

    if overall_ok:
        st.success("Identiteta uspešno verificirana.", icon="✅")
    else:
        st.error("Verifikacija ni bila uspešna.", icon="❌")

    # Metric cards via Streamlit native metrics
    m1, m2, m3 = st.columns(3)

    user_name = result.get("user_name", "N/A")
    age_verified = result.get("age_verified")
    face_match = result.get("face_match")

    with m1:
        st.metric(label="👤 &nbsp;Ime in priimek", value=user_name if user_name else "—")

    with m2:
        age_label = "✔ Potrjena" if age_verified else ("✘ Ni potrjena" if age_verified is not None else "N/A")
        age_delta = "Polnoleten" if age_verified else None
        st.metric(label="🎂 &nbsp;Starost (18+)", value=age_label, delta=age_delta)

    with m3:
        face_label = "✔ Ujemanje" if face_match else ("✘ Ni ujemanja" if face_match is not None else "N/A")
        face_delta = "Obraz verificiran" if face_match else None
        st.metric(label="🧬 &nbsp;Primerjava obraza", value=face_label, delta=face_delta)

    st.markdown("#### 🗂️ &nbsp;Surovi odgovor")
    st.json({"task_id": task_id, "task_status": "SUCCESS", "task_result": result})
