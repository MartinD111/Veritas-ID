import base64
import io
import json
import socket
import pathlib
import traceback
import requests
import streamlit as st

try:
    import qrcode
    _QR_AVAILABLE = True
except ImportError:
    _QR_AVAILABLE = False

BACKEND_URL = "http://localhost:8000"

# ── EU member states ──────────────────────────────────────────────────────────
EU_MEMBER_STATES = [
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
    "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
    "Spain", "Sweden",
]

COUNTRY_STATUS = {
    "EU": {"label": "European Union", "flag": "🇪🇺", "ready": True},
    "JP": {"label": "Japan",          "flag": "🇯🇵", "ready": True},
    "KR": {"label": "South Korea",    "flag": "🇰🇷", "ready": False},
    "TH": {"label": "Thailand",       "flag": "🇹🇭", "ready": False},
}

COUNTRY_READINESS_DETAIL = {
    "KR": [("PASS API", False, "Key Missing — SKT/KT/LGU+ credentials required"),
           ("Face Match", True, "InsightFace: Connected"),
           ("VAV System", True, "Internal Compute: Ready")],
    "TH": [("Laser ID", False, "API Stub — live endpoint pending"),
           ("Face Match", True, "InsightFace: Connected"),
           ("VAV System", True, "Internal Compute: Ready")],
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


_LAN_IP = _get_lan_ip()


def _get_ngrok_url() -> str | None:
    try:
        r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1.5)
        for t in r.json().get("tunnels", []):
            url = t.get("public_url", "")
            if url.startswith("https://"):
                return url.rstrip("/")
    except Exception:
        pass
    return None


def _get_stats() -> dict:
    try:
        r = requests.get(f"{BACKEND_URL}/stats", timeout=3)
        return r.json()
    except Exception:
        return {}


def _admin_headers() -> dict:
    token = st.session_state.get("admin_session_token", "")
    return {"x-admin-session": token}


def _api_get(path: str, **kwargs) -> requests.Response:
    return requests.get(f"{BACKEND_URL}{path}", headers=_admin_headers(),
                        timeout=kwargs.pop("timeout", 8), **kwargs)


def _api_post(path: str, **kwargs) -> requests.Response:
    return requests.post(f"{BACKEND_URL}{path}", headers=_admin_headers(),
                         timeout=kwargs.pop("timeout", 8), **kwargs)


CONFIG_FILE = pathlib.Path(__file__).parent / ".veritas_config.json"


def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(data: dict):
    try:
        existing = load_config()
        existing.update(data)
        CONFIG_FILE.write_text(json.dumps(existing), encoding="utf-8")
    except Exception:
        pass


_cfg = load_config()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Veritas IDV — Admin Portal",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state defaults ────────────────────────────────────────────────────
_DEFAULTS = {
    "api_key":             _cfg.get("api_key", ""),
    "dark_mode":           _cfg.get("dark_mode", False),
    "show_settings":       False,
    "qr_png_bytes":        None,
    "qr_mobile_url":       None,
    "qr_session_label":    None,
    "public_base_url":     "",
    "nav_section":         "Active Verifications",
    "selected_country":    "EU",
    "eu_member_state":     "Slovenia",
    # Admin auth
    "admin_authenticated": False,
    "admin_session_token": "",
    "admin_id":            "",
    "admin_auth_method":   "",
    # Review detail drill-down
    "review_open_task":    None,
    "review_item_data":    None,
}
for k, v in _DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

dm = st.session_state.dark_mode

# ── Theme tokens ──────────────────────────────────────────────────────────────
if dm:
    BG = "#000000"; CARD = "#1c1c1e"; CARD2 = "#2c2c2e"
    TEXT = "#f5f5f7"; MUTED = "#98989d"; INPUT_BG = "#2c2c2e"
    INPUT_HV = "#3a3a3c"; BORDER = "#3a3a3c"
    SHADOW = "0 8px 30px rgba(0,0,0,0.5)"; SHADOW_HV = "0 14px 40px rgba(0,0,0,0.7)"
    DOT_BORDER = CARD; SIDEBAR_BG = "#111111"
else:
    BG = "#f5f5f7"; CARD = "#ffffff"; CARD2 = "#f5f5f7"
    TEXT = "#1d1d1f"; MUTED = "#86868b"; INPUT_BG = "#f5f5f7"
    INPUT_HV = "#e8e8ed"; BORDER = "#e8e8ed"
    SHADOW = "0 8px 30px rgba(0,0,0,0.04)"; SHADOW_HV = "0 14px 40px rgba(0,0,0,0.08)"
    DOT_BORDER = "#ffffff"; SIDEBAR_BG = "#e8e8ed"

BLUE = "#0071e3"; BLUE_HV = "#0077ED"
GREEN = "#34c759"; RED = "#ff3b30"; ORANGE = "#ff9f0a"
PURPLE = "#af52de"

# ── SVG icons ─────────────────────────────────────────────────────────────────
def svg_shield():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="{BLUE}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>'

def svg_cpu():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="{TEXT}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2M9 2v2M15 20v2M9 20v2M2 15h2M2 9h2M20 15h2M20 9h2"/></svg>'

def svg_key():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/></svg>'

def svg_camera():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>'

def svg_lock():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="{BLUE}" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'

def svg_check_circle():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="{GREEN}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/></svg>'

def svg_x_circle():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="{RED}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6M9 9l6 6"/></svg>'

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
html, body, [class*="css"], .stApp {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif !important;
    -webkit-font-smoothing: antialiased;
    background-color: {BG} !important;
    color: {TEXT} !important;
}}
#MainMenu, footer, header {{ visibility: hidden; }}
.stDeployButton {{ display: none; }}

[data-testid="stSidebar"] {{
    background: {SIDEBAR_BG} !important;
    border-right: 1px solid {BORDER} !important;
}}
[data-testid="stSidebar"] .block-container {{ padding: 1.5rem 1rem !important; }}
[data-testid="collapsedControl"] {{ display: flex !important; }}
.block-container {{ padding-top: 1.5rem !important; padding-bottom: 5rem !important; max-width: 1200px !important; }}

/* Nav items */
.v-settings-fab, .v-theme-fab {{ position: fixed; top: 1.5rem; z-index: 999; }}
.v-settings-fab {{ right: 1.5rem; }} .v-theme-fab {{ right: 5rem; }}
.v-settings-fab button, .v-theme-fab button {{
    width: 48px !important; height: 48px !important; background: {CARD} !important;
    border-radius: 50% !important; box-shadow: {SHADOW} !important;
    display: flex !important; align-items: center !important; justify-content: center !important;
    cursor: pointer !important; border: none !important; font-size: 1.4rem !important;
    padding: 0 !important; color: {TEXT} !important;
    transition: transform 0.2s ease, box-shadow 0.2s ease !important;
}}
.v-settings-fab button:hover, .v-theme-fab button:hover {{
    transform: scale(1.08) !important; box-shadow: {SHADOW_HV} !important;
}}

/* Cards */
.v-card {{
    background: {CARD}; border-radius: 2rem; box-shadow: {SHADOW};
    padding: 2rem; transition: box-shadow 0.3s ease; margin-bottom: 1.5rem;
}}
.v-card:hover {{ box-shadow: {SHADOW_HV}; }}
.v-section-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem; }}
.v-section-head h2 {{ font-size: 1.05rem; font-weight: 600; letter-spacing: -0.02em; color: {TEXT}; margin: 0; }}

/* Status widget */
.v-status-widget {{
    background: {CARD}; border-radius: 2rem; box-shadow: {SHADOW}; padding: 2rem;
    display: flex; flex-direction: column; align-items: center; text-align: center; margin-bottom: 1.5rem;
}}
.v-status-orb {{ width: 64px; height: 64px; border-radius: 50%; background: {INPUT_BG}; display: flex; align-items: center; justify-content: center; margin-bottom: 1rem; position: relative; }}
.v-status-dot {{ position: absolute; top: 1px; right: 1px; width: 15px; height: 15px; border-radius: 50%; border: 2.5px solid {DOT_BORDER}; }}
.v-status-dot.green  {{ background: {GREEN}; }}
.v-status-dot.yellow {{ background: {ORANGE}; }}
.v-status-dot.red    {{ background: {RED}; }}
.v-status-title {{ font-size: 1.05rem; font-weight: 600; color: {TEXT}; margin-bottom: 0.25rem; }}
.v-status-sub   {{ font-size: 0.82rem; color: {MUTED}; }}

/* Buttons */
.stButton > button[kind="primary"] {{
    width: 100% !important; background: {BLUE} !important; color: white !important;
    border: none !important; border-radius: 2rem !important; padding: 1rem 1.5rem !important;
    font-size: 1rem !important; font-weight: 500 !important; cursor: pointer !important;
    transition: background 0.2s ease, transform 0.1s ease !important;
    box-shadow: 0 4px 15px rgba(0,113,227,0.3) !important;
}}
.stButton > button[kind="primary"]:hover {{ background: {BLUE_HV} !important; }}
.stButton > button[kind="primary"]:active {{ transform: scale(0.98) !important; }}
.stButton > button[kind="primary"]:disabled {{ opacity: 0.5 !important; cursor: not-allowed !important; box-shadow: none !important; }}
.stButton > button:not([kind="primary"]) {{
    background: {INPUT_BG} !important; color: {TEXT} !important; border: none !important;
    border-radius: 2rem !important; padding: 0.5rem 1rem !important;
    font-size: 0.85rem !important; font-weight: 500 !important; transition: background 0.2s !important;
}}
.stButton > button:not([kind="primary"]):hover {{ background: {INPUT_HV} !important; }}

/* Text inputs */
.stTextInput input {{
    background: {INPUT_BG} !important; border: 1px solid {BORDER} !important;
    border-radius: 1rem !important; padding: 0.85rem 1.25rem !important;
    font-size: 0.9rem !important; color: {TEXT} !important; transition: all 0.2s ease !important;
}}
.stTextInput input:focus {{
    border-color: rgba(0,113,227,0.5) !important; background: {CARD} !important;
    box-shadow: 0 0 0 3px rgba(0,113,227,0.12) !important;
}}
.stTextInput label {{ display: none !important; }}
.stTextInput input::placeholder {{ color: {MUTED} !important; opacity: 1; }}

/* Selectbox */
.stSelectbox > div > div {{
    background: {INPUT_BG} !important; border: 1px solid {BORDER} !important;
    border-radius: 1rem !important; color: {TEXT} !important;
}}

/* Metric cards */
.v-metric-card {{ background: {INPUT_BG}; border-radius: 1.5rem; padding: 1.5rem; text-align: center; }}
.v-metric-label {{ font-size: 0.72rem; font-weight: 500; color: {MUTED}; letter-spacing: 0.05em; text-transform: uppercase; margin-bottom: 0.5rem; }}
.v-metric-value       {{ font-size: 1.15rem; font-weight: 600; color: {TEXT}; }}
.v-metric-value.green {{ color: {GREEN}; }}
.v-metric-value.red   {{ color: {RED}; }}
.v-metric-value.blue  {{ color: {BLUE}; }}
.v-metric-value.orange {{ color: {ORANGE}; }}

/* Review queue cards */
.v-review-card {{
    background: {CARD}; border-radius: 1.5rem; border: 1px solid {BORDER};
    padding: 1.25rem 1.5rem; margin-bottom: .75rem;
    transition: box-shadow .2s ease;
}}
.v-review-card:hover {{ box-shadow: {SHADOW}; }}
.v-review-meta {{ font-size: .78rem; color: {MUTED}; margin-top: .2rem; }}
.v-review-reason {{ font-size: .75rem; color: {ORANGE}; font-weight: 600; margin-top: .35rem; }}
.v-ttl-badge {{
    display: inline-flex; align-items: center; gap: .3rem;
    background: rgba(255,159,10,.12); border-radius: 999px;
    padding: .2rem .6rem; font-size: .68rem; font-weight: 700; color: {ORANGE};
}}

/* Admin login card */
.v-login-wrap {{
    max-width: 420px; margin: 3rem auto 0;
    background: {CARD}; border-radius: 2rem; box-shadow: {SHADOW}; padding: 3rem 2.5rem;
    text-align: center;
}}
.v-login-icon {{ margin-bottom: 1.5rem; }}
.v-login-title {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -.04em; color: {TEXT}; margin-bottom: .3rem; }}
.v-login-sub   {{ font-size: .85rem; color: {MUTED}; margin-bottom: 2rem; }}
.v-login-label {{
    font-size: .72rem; font-weight: 600; color: {MUTED}; text-transform: uppercase;
    letter-spacing: .08em; text-align: left; margin-bottom: .35rem; margin-top: 1rem;
}}
.v-login-error {{ font-size: .8rem; color: {RED}; font-weight: 600; margin-top: .75rem; }}
.v-dev-badge {{
    display: inline-flex; align-items: center; gap: .4rem;
    background: rgba(175,82,222,.12); border: 1px solid rgba(175,82,222,.3);
    border-radius: 999px; padding: .3rem .85rem;
    font-size: .72rem; font-weight: 700; color: {PURPLE};
    margin-bottom: 1.5rem;
}}
.v-sipass-badge {{
    display: inline-flex; align-items: center; gap: .4rem;
    background: rgba(0,113,227,.08); border: 1px solid rgba(0,113,227,.25);
    border-radius: 999px; padding: .3rem .85rem;
    font-size: .72rem; font-weight: 700; color: {BLUE};
    margin-bottom: 1.5rem;
}}

/* Admin identity chip in sidebar */
.v-admin-chip {{
    display: flex; align-items: center; gap: .6rem;
    background: {CARD}; border-radius: 1rem; padding: .6rem .9rem;
    margin-bottom: .75rem;
}}
.v-admin-chip-name {{ font-size: .82rem; font-weight: 600; color: {TEXT}; }}
.v-admin-chip-sub  {{ font-size: .68rem; color: {MUTED}; }}

/* Watermark */
.v-watermark {{
    position: fixed; bottom: .75rem; right: 1.25rem;
    display: flex; align-items: center; gap: .35rem;
    font-size: .65rem; font-weight: 600; letter-spacing: .04em;
    color: {MUTED}; opacity: .55; pointer-events: none; z-index: 500;
}}

/* Settings */
.v-settings-label {{
    font-size: 0.72rem; font-weight: 600; color: {MUTED}; text-transform: uppercase;
    letter-spacing: 0.08em; margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.4rem;
}}
.v-help-text {{ font-size: 0.75rem; color: {MUTED}; margin-top: 0.4rem; }}
.v-warning   {{ font-size: 0.78rem; color: {RED}; text-align: center; margin-top: 0.5rem; font-weight: 500; }}

/* Modal */
.v-modal-overlay {{
    position: fixed; inset: 0; background: rgba(0,0,0,.45);
    backdrop-filter: blur(12px); z-index: 1000;
    display: flex; align-items: center; justify-content: center;
}}
.v-modal {{
    background: {CARD}; border-radius: 2rem; box-shadow: 0 24px 60px rgba(0,0,0,.3);
    padding: 2.5rem; width: 100%; max-width: 440px; position: relative;
}}
.v-modal-title {{ font-size: 1.5rem; font-weight: 600; letter-spacing: -.03em; color: {TEXT}; margin: 0 0 2rem; }}
.v-modal-close {{ position: absolute; top: 1.5rem; right: 1.5rem; z-index: 1001; }}
.v-modal-close button {{
    width: 32px !important; height: 32px !important; min-width: 32px !important;
    background: {INPUT_BG} !important; border-radius: 50% !important; border: none !important;
    cursor: pointer !important; color: {MUTED} !important;
    font-size: .9rem !important; padding: 0 !important;
}}

/* Readiness card */
.v-readiness-card {{ background: {CARD}; border-radius: 1.5rem; padding: 1.25rem 1.5rem; margin-bottom: .75rem; border: 1px solid {BORDER}; }}
.v-readiness-row {{ display: flex; align-items: center; gap: .6rem; font-size: .82rem; margin-bottom: .3rem; }}
.v-readiness-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.v-readiness-dot.ok  {{ background: {GREEN}; }} .v-readiness-dot.nok {{ background: {RED}; }}

hr {{ border: none; border-top: 1px solid {BORDER} !important; margin: 1.5rem 0 !important; }}
.streamlit-expanderHeader {{ background: {INPUT_BG} !important; border-radius: 1rem !important; color: {MUTED} !important; font-size: .85rem !important; }}
</style>
""", unsafe_allow_html=True)

# ── Engine status ─────────────────────────────────────────────────────────────
def _get_engine_status() -> str:
    try:
        r = requests.get(f"{BACKEND_URL}/engine-status", timeout=3)
        return r.json().get("status", "loading")
    except Exception:
        return "unreachable"

_engine_status = _get_engine_status()

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN LOGIN SCREEN
# Shown whenever the admin is not authenticated, regardless of nav section.
# ══════════════════════════════════════════════════════════════════════════════
def _render_login():
    """Full-page admin login. Dev mode: admin/admin. Prod: SI-PASS stub."""
    st.markdown(f"""
    <div class="v-login-wrap">
        <div class="v-login-icon">{svg_lock()}</div>
        <div class="v-login-title">Admin Login</div>
        <div class="v-login-sub">Veritas IDV — Secure Operations Portal</div>
    """, unsafe_allow_html=True)

    auth_mode = st.radio(
        "Authentication method",
        ["🛠  Developer Mode (admin/admin)", "🏛  SI-PASS / SIGEN-CA (production)"],
        key="login_auth_mode",
        label_visibility="collapsed",
    )
    dev_mode = auth_mode.startswith("🛠")

    if dev_mode:
        st.markdown('<div class="v-dev-badge">⚙ Developer Mode — not for production</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="v-sipass-badge">🏛 SI-PASS / SIGEN-CA — coming soon</div>', unsafe_allow_html=True)

    st.markdown('<div class="v-login-label">Username</div>', unsafe_allow_html=True)
    username = st.text_input("Username", placeholder="admin", key="login_username",
                             label_visibility="collapsed", disabled=not dev_mode)

    st.markdown('<div class="v-login-label">Password</div>', unsafe_allow_html=True)
    password = st.text_input("Password", placeholder="••••••••", type="password",
                             key="login_password", label_visibility="collapsed",
                             disabled=not dev_mode)

    if not dev_mode:
        st.info("SI-PASS digital certificate authentication is configured server-side. "
                "Contact your system administrator to enable production auth.")

    col_l, col_r = st.columns([1, 1])
    with col_r:
        login_btn = st.button("Sign In →", type="primary", key="login_btn",
                              use_container_width=True, disabled=not dev_mode)

    if login_btn:
        try:
            resp = requests.post(
                f"{BACKEND_URL}/admin/login",
                json={"username": username, "password": password, "dev_mode": True},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                st.session_state.admin_authenticated = True
                st.session_state.admin_session_token = data["session_token"]
                st.session_state.admin_id             = data["admin_id"]
                st.session_state.admin_auth_method    = data["auth_method"]
                st.rerun()
            else:
                detail = resp.json().get("detail", "Login failed.")
                st.markdown(f'<div class="v-login-error">✗ {detail}</div>', unsafe_allow_html=True)
        except requests.exceptions.ConnectionError:
            st.markdown(f'<div class="v-login-error">✗ Cannot reach backend at {BACKEND_URL}</div>',
                        unsafe_allow_html=True)
        except Exception as exc:
            st.markdown(f'<div class="v-login-error">✗ {exc}</div>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    # Watermark on login screen too
    st.markdown(f"""
    <div class="v-watermark">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
        </svg>
        Powered by Veritas ID
    </div>""", unsafe_allow_html=True)


# ── Guard: show login if not authenticated ────────────────────────────────────
if not st.session_state.admin_authenticated:
    _render_login()
    st.stop()

# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED SHELL — sidebar + nav
# ══════════════════════════════════════════════════════════════════════════════
NAV_ITEMS = [
    ("Active Verifications", "🔍"),
    ("Manual Review Queue",  "📋"),
    ("Country Management",   "🌍"),
    ("API Cost Analytics",   "📊"),
    ("Audit Log",            "📝"),
    ("System Stats",         "⚙️"),
]

with st.sidebar:
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:.75rem;padding:.5rem 0 1.25rem;border-bottom:1px solid {BORDER};margin-bottom:1rem">
        {svg_shield()}
        <div>
            <div style="font-size:1rem;font-weight:700;color:{TEXT};letter-spacing:-.03em">Veritas IDV</div>
            <div style="font-size:.72rem;color:{MUTED}">Admin Operations Portal</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Admin identity chip
    auth_icon = "⚙" if st.session_state.admin_auth_method == "dev_password" else "🏛"
    st.markdown(f"""
    <div class="v-admin-chip">
        <div style="font-size:1.4rem">{auth_icon}</div>
        <div>
            <div class="v-admin-chip-name">{st.session_state.admin_id}</div>
            <div class="v-admin-chip-sub">{st.session_state.admin_auth_method}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    for label, icon in NAV_ITEMS:
        if st.button(f"{icon}  {label}", key=f"nav_{label}", use_container_width=True):
            st.session_state.nav_section = label
            st.session_state.review_open_task = None
            st.rerun()

    st.markdown(f'<hr style="border-top:1px solid {BORDER};margin:1rem 0"/>', unsafe_allow_html=True)

    # Engine status pill
    _es = _get_engine_status()
    _dot = {"ready": GREEN, "loading": ORANGE}.get(_es, RED)
    _elabel = {"ready": "VAV System — Ready", "loading": "VAV System — Loading…"}.get(_es, "Engine — Offline")
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:.6rem;padding:.5rem .75rem;background:{CARD};border-radius:1rem;margin-bottom:.5rem">
        <div style="width:8px;height:8px;border-radius:50%;background:{_dot};flex-shrink:0"></div>
        <div style="font-size:.78rem;color:{MUTED}">{_elabel}</div>
    </div>
    """, unsafe_allow_html=True)

    theme_label = "☀️  Light Mode" if dm else "🌙  Dark Mode"
    if st.button(theme_label, key="sidebar_theme_btn", use_container_width=True):
        st.session_state.dark_mode = not st.session_state.dark_mode
        save_config({"dark_mode": st.session_state.dark_mode})
        st.rerun()

    if st.button("⚙️  Settings", key="sidebar_settings_btn", use_container_width=True):
        st.session_state.show_settings = not st.session_state.show_settings
        st.rerun()

    st.markdown(f'<hr style="border-top:1px solid {BORDER};margin:1rem 0"/>', unsafe_allow_html=True)
    if st.button("🔓  Sign Out", key="logout_btn", use_container_width=True):
        try:
            _api_post("/admin/logout")
        except Exception:
            pass
        for k in ("admin_authenticated", "admin_session_token", "admin_id", "admin_auth_method"):
            st.session_state[k] = _DEFAULTS[k]
        st.rerun()

# ── FABs ──────────────────────────────────────────────────────────────────────
st.markdown('<div class="v-theme-fab">', unsafe_allow_html=True)
if st.button("☀️" if dm else "🌙", key="theme_toggle_btn"):
    st.session_state.dark_mode = not st.session_state.dark_mode
    save_config({"dark_mode": st.session_state.dark_mode})
    st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

st.markdown('<div class="v-settings-fab">', unsafe_allow_html=True)
if st.button("⚙", key="settings_fab_btn"):
    st.session_state.show_settings = not st.session_state.show_settings
    st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

# ── Settings modal ────────────────────────────────────────────────────────────
if st.session_state.show_settings:
    st.markdown('<div class="v-modal-overlay"><div class="v-modal">', unsafe_allow_html=True)
    st.markdown('<div class="v-modal-close">', unsafe_allow_html=True)
    if st.button("✕", key="close_settings_x"):
        st.session_state.show_settings = False
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown('<div class="v-modal-title">Settings</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="v-settings-label">{svg_key()} &nbsp;API Key</div>', unsafe_allow_html=True)
    new_key = st.text_input("API Key", value=st.session_state.api_key, type="password",
                            placeholder="vrt_...", key="settings_api_key_input",
                            label_visibility="collapsed")
    st.markdown('<p class="v-help-text">Stored locally for QR session generation.</p>', unsafe_allow_html=True)
    if new_key != st.session_state.api_key:
        st.session_state.api_key = new_key
        save_config({"api_key": new_key})
    dm_label = "🌙 Dark Mode: ON" if dm else "☀️ Dark Mode: OFF"
    if st.toggle(dm_label, value=dm, key="dark_mode_toggle"):
        if not st.session_state.dark_mode:
            st.session_state.dark_mode = True; save_config({"dark_mode": True}); st.rerun()
    else:
        if st.session_state.dark_mode:
            st.session_state.dark_mode = False; save_config({"dark_mode": False}); st.rerun()
    st.markdown('</div></div>', unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align:center;padding:1.75rem 0 1.25rem">
    <div style="display:inline-flex;align-items:center;justify-content:center;background:{CARD};
                border-radius:50%;width:72px;height:72px;box-shadow:{SHADOW};margin-bottom:1rem">
        {svg_shield()}
    </div>
    <h1 style="font-size:2.2rem;font-weight:600;letter-spacing:-.04em;color:{TEXT};margin:0 0 .4rem">Veritas IDV</h1>
    <p style="font-size:.95rem;color:{MUTED};margin:0">Admin Operations Portal</p>
</div>
""", unsafe_allow_html=True)

section = st.session_state.nav_section

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — Active Verifications (QR flow)
# ══════════════════════════════════════════════════════════════════════════════
if section == "Active Verifications":
    ngrok_url = _get_ngrok_url()
    if ngrok_url:
        st.session_state.public_base_url = ngrok_url
        st.success(f"✅ ngrok tunnel: `{ngrok_url}`")
    else:
        st.warning("⚠️ ngrok not detected. Run `ngrok http 8000` and reload.")

    has_public_url = bool(ngrok_url)
    left_col, right_col = st.columns([8, 4], gap="large")

    with left_col:
        st.markdown(f"""
        <div class="v-card">
            <div class="v-section-head"><h2>Mobile QR Flow</h2>{svg_camera()}</div>
            <p style="font-size:.9rem;color:{MUTED};margin-bottom:1.25rem">
                Generate a one-time QR session. Select target country to adapt the
                mobile UI and verification logic.
            </p>
        </div>
        """, unsafe_allow_html=True)

        country_options = {"EU 🇪🇺": "EU", "Japan 🇯🇵": "JP", "South Korea 🇰🇷": "KR", "Thailand 🇹🇭": "TH"}
        sel_label = st.selectbox("Target Country", list(country_options.keys()), key="country_select_qr")
        sel_country = country_options[sel_label]
        st.session_state.selected_country = sel_country

        if sel_country == "EU":
            eu_state = st.selectbox("EU Member State", EU_MEMBER_STATES, key="eu_member_select")
            st.session_state.eu_member_state = eu_state

        gen_qr = st.button("Generate QR Code", type="primary",
                           disabled=not st.session_state.api_key.strip() or not has_public_url,
                           use_container_width=True, key="gen_qr_btn")

        if gen_qr:
            if not _QR_AVAILABLE:
                st.error("Library `qrcode` missing. Run: `pip install qrcode[pil]`")
            else:
                st.session_state.qr_png_bytes = st.session_state.qr_mobile_url = st.session_state.qr_session_label = None
                try:
                    resp = requests.post(f"{BACKEND_URL}/mobile/session",
                                         headers={"X-API-Key": st.session_state.api_key.strip()},
                                         data={"country": sel_country}, timeout=10)
                    resp.raise_for_status()
                    payload = resp.json()
                    session_id = payload["session_id"]
                    expires_in = payload.get("expires_in", 600)
                    country_confirmed = payload.get("country", sel_country)
                except requests.exceptions.ConnectionError:
                    st.error("Cannot reach `localhost:8000`. Is FastAPI running?"); st.stop()
                except Exception as exc:
                    st.error(f"Session error: {exc}"); st.stop()

                mobile_url = f"{ngrok_url.rstrip('/')}/mobile/{session_id}"
                try:
                    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                                       box_size=8, border=3)
                    qr.add_data(mobile_url); qr.make(fit=True)
                    pil_img = qr.make_image(fill_color="black", back_color="white")
                    buf = io.BytesIO(); pil_img.save(buf, format="PNG")
                    st.session_state.qr_png_bytes = buf.getvalue()
                    st.session_state.qr_mobile_url = mobile_url
                    st.session_state.qr_session_label = (
                        f"Valid {expires_in // 60} min · session: {session_id[:12]}… · {country_confirmed}")
                except Exception as exc:
                    st.warning(f"QR image error ({exc}) — use link below.")
                    st.session_state.qr_mobile_url = mobile_url
                st.rerun()

        if st.session_state.get("qr_mobile_url"):
            st.markdown("**Mobile link:**"); st.code(st.session_state.qr_mobile_url, language=None)
        if st.session_state.get("qr_png_bytes"):
            st.image(st.session_state.qr_png_bytes,
                     caption=st.session_state.get("qr_session_label") or "", width=260)
            st.info("After scanning the QR the mobile app opens in the phone browser.")
            if st.button("Clear / New Session", key="clear_qr_btn"):
                st.session_state.qr_png_bytes = st.session_state.qr_mobile_url = st.session_state.qr_session_label = None
                st.rerun()

    with right_col:
        _es = _get_engine_status()
        dot_cls = {"ready": "green", "loading": "yellow"}.get(_es, "red")
        status_txt = {"ready": "VAV System ready and active",
                      "loading": "VAV System loading into RAM…"}.get(_es, "Engine unreachable")
        st.markdown(f"""
        <div class="v-status-widget">
            <div class="v-status-orb">{svg_cpu()}<div class="v-status-dot {dot_cls}"></div></div>
            <div class="v-status-title">VAV System</div>
            <div class="v-status-sub">Veritas Advanced Verification</div>
            <div class="v-status-sub" style="margin-top:.3rem">{status_txt}</div>
        </div>
        """, unsafe_allow_html=True)
        if not st.session_state.api_key.strip():
            st.markdown(f'<p class="v-warning">API key missing. Add it in Settings (⚙).</p>',
                        unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — Manual Review Queue
# ══════════════════════════════════════════════════════════════════════════════
elif section == "Manual Review Queue":
    st.markdown(f'<div style="font-size:1.4rem;font-weight:700;color:{TEXT};margin-bottom:.4rem">📋 Manual Review Queue</div>', unsafe_allow_html=True)
    st.markdown(f'<p style="color:{MUTED};margin-bottom:1.5rem">Tasks the VAV System could not auto-resolve. Images are stored for 24 h then auto-purged (GDPR §5(1)(e)).</p>', unsafe_allow_html=True)

    # ── Detail view (drill-down) ──────────────────────────────────────────────
    if st.session_state.review_open_task:
        task_id = st.session_state.review_open_task
        item    = st.session_state.review_item_data or {}

        if st.button("← Back to Queue", key="back_to_queue"):
            st.session_state.review_open_task = None
            st.session_state.review_item_data = None
            st.rerun()

        st.markdown(f"""
        <div class="v-card">
            <div class="v-section-head">
                <h2>Review: {task_id[:16]}…</h2>
                <span style="font-size:.78rem;color:{MUTED}">{item.get('project','')} · {item.get('country','')}</span>
            </div>
            <div style="font-size:.82rem;color:{ORANGE};font-weight:600;margin-bottom:.5rem">
                ⚠ {item.get('reason','—')}
            </div>
            <div style="font-size:.75rem;color:{MUTED}">Queued: {item.get('queued_at','')[:19]}</div>
        </div>
        """, unsafe_allow_html=True)

        # Image display
        img_cols = st.columns(3)
        img_labels = [("ID Front", "id_front_b64"), ("ID Back", "id_back_b64"), ("Selfie", "selfie_b64")]
        for col, (label, key) in zip(img_cols, img_labels):
            with col:
                b64 = item.get(key)
                if b64:
                    img_bytes = base64.b64decode(b64)
                    st.image(img_bytes, caption=label, use_container_width=True)
                else:
                    st.markdown(f"""
                    <div style="background:{INPUT_BG};border-radius:1rem;height:180px;
                                display:flex;align-items:center;justify-content:center;
                                color:{MUTED};font-size:.8rem">{label}<br>(not available)</div>
                    """, unsafe_allow_html=True)

        st.markdown("---")

        # Action controls
        user_email = st.text_input("User email (for audit log)", value=item.get("user_email", ""),
                                   placeholder="user@example.com", key="review_email")
        webhook_url = st.text_input("Webhook URL (optional — notifies client app)",
                                    placeholder="https://app.tremble.com/webhooks/veritas",
                                    key="review_webhook")

        col_approve, col_reject = st.columns(2)
        with col_approve:
            if st.button("✅  Approve", type="primary", use_container_width=True, key="btn_approve"):
                try:
                    r = _api_post(f"/admin/review/{task_id}/action",
                                  json={"action": "approved", "user_email": user_email,
                                        "webhook_url": webhook_url})
                    if r.status_code == 200:
                        data = r.json()
                        st.success(f"✅ Approved. Images deleted immediately. Audit logged.")
                        if data.get("webhook"):
                            wh = data["webhook"]
                            if wh.get("ok"):
                                st.info(f"Webhook delivered → HTTP {wh['status']}")
                            else:
                                st.warning(f"Webhook failed: {wh.get('error','unknown')}")
                        st.session_state.review_open_task = None
                        st.session_state.review_item_data = None
                        st.rerun()
                    else:
                        st.error(f"API error {r.status_code}: {r.text}")
                except Exception as exc:
                    st.error(str(exc))

        with col_reject:
            if st.button("❌  Reject", use_container_width=True, key="btn_reject"):
                try:
                    r = _api_post(f"/admin/review/{task_id}/action",
                                  json={"action": "rejected", "user_email": user_email,
                                        "webhook_url": webhook_url})
                    if r.status_code == 200:
                        st.error("❌ Rejected. Images deleted immediately. Audit logged.")
                        st.session_state.review_open_task = None
                        st.session_state.review_item_data = None
                        st.rerun()
                    else:
                        st.error(f"API error {r.status_code}: {r.text}")
                except Exception as exc:
                    st.error(str(exc))

        st.markdown(f"""
        <div style="background:rgba(255,59,48,.06);border:1px solid rgba(255,59,48,.2);
                    border-radius:1rem;padding:.9rem 1.1rem;margin-top:.75rem;font-size:.75rem;color:{MUTED}">
            <strong style="color:{RED}">GDPR / ZVOP-2:</strong> Clicking Approve or Reject
            immediately executes <code>Redis DEL</code> on all stored images. The action is
            written to the append-only audit log (admin ID, timestamp, decision — no image data).
            Images are never written to disk and auto-expire after 24 h regardless.
        </div>
        """, unsafe_allow_html=True)

    # ── Queue list ────────────────────────────────────────────────────────────
    else:
        col_refresh, _ = st.columns([1, 5])
        with col_refresh:
            if st.button("🔄 Refresh Queue", key="refresh_queue"):
                st.rerun()

        try:
            resp = _api_get("/admin/review/queue")
            if resp.status_code == 401:
                st.error("Session expired — please sign in again.")
                st.session_state.admin_authenticated = False
                st.rerun()
            queue_items = resp.json() if resp.status_code == 200 else []
        except Exception as exc:
            st.error(f"Cannot reach backend: {exc}")
            queue_items = []

        if not queue_items:
            st.markdown(f"""
            <div class="v-card" style="text-align:center;padding:3rem 2rem">
                <div style="font-size:3rem;margin-bottom:1rem">✓</div>
                <div style="font-size:1.1rem;font-weight:600;color:{TEXT}">Queue is empty</div>
                <div style="font-size:.85rem;color:{MUTED};margin-top:.4rem">
                    All verifications resolved — no pending manual reviews.
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f'<div style="font-size:.85rem;color:{MUTED};margin-bottom:1rem">{len(queue_items)} item(s) pending</div>', unsafe_allow_html=True)
            for item in queue_items:
                tid = item.get("task_id", "")
                ttl = item.get("ttl_seconds", 0)
                ttl_h = ttl // 3600
                ttl_m = (ttl % 3600) // 60

                col_info, col_btn = st.columns([7, 2])
                with col_info:
                    st.markdown(f"""
                    <div class="v-review-card">
                        <div style="display:flex;align-items:center;gap:.75rem">
                            <div style="font-size:1.05rem;font-weight:600;color:{TEXT};font-family:monospace">
                                {tid[:20]}…
                            </div>
                            <span class="v-ttl-badge">⏱ {ttl_h}h {ttl_m}m left</span>
                        </div>
                        <div class="v-review-meta">
                            Project: <strong>{item.get('project','—')}</strong> ·
                            Country: <strong>{item.get('country','—')}</strong> ·
                            Queued: {(item.get('queued_at') or '')[:16]}
                        </div>
                        <div class="v-review-reason">⚠ {item.get('reason','—')}</div>
                    </div>
                    """, unsafe_allow_html=True)

                with col_btn:
                    if st.button("Open →", key=f"open_{tid}", use_container_width=True):
                        try:
                            detail_resp = _api_get(f"/admin/review/{tid}")
                            if detail_resp.status_code == 200:
                                st.session_state.review_item_data = detail_resp.json()
                                st.session_state.review_open_task = tid
                                st.rerun()
                            else:
                                st.error(f"Could not load item: {detail_resp.status_code}")
                        except Exception as exc:
                            st.error(str(exc))

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — Country Management
# ══════════════════════════════════════════════════════════════════════════════
elif section == "Country Management":
    st.markdown(f'<div style="font-size:1.4rem;font-weight:700;color:{TEXT};margin-bottom:.4rem">🌍 Country Management</div>', unsafe_allow_html=True)
    st.markdown(f'<p style="color:{MUTED};margin-bottom:1.5rem">Supported markets, readiness status, and regional verification configuration.</p>', unsafe_allow_html=True)

    st.markdown(f"""
    <div class="v-card">
        <div class="v-section-head"><h2>🇪🇺 European Union — Active</h2></div>
        <p style="font-size:.85rem;color:{MUTED};margin-bottom:1rem">
            MRZ (TD1/TD3), InsightFace biometric match, NFC chip, and VAV System fully operational.
        </p>
    </div>
    """, unsafe_allow_html=True)
    eu_state = st.selectbox("EU Member State preview", EU_MEMBER_STATES, key="eu_country_mgmt")
    st.info(f"**{eu_state}** — Standard EU eID flow. MRZ + Face + NFC + VAV System.")
    st.markdown("---")
    st.markdown(f"""
    <div class="v-card">
        <div class="v-section-head"><h2>🇯🇵 Japan — Active (My Number)</h2></div>
        <p style="font-size:.85rem;color:{MUTED}">
            VAV System uses a JP-specific prompt for マイナンバーカード.
            Converts Reiwa / Heisei / Showa eras to Gregorian dates for age verification.
        </p>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(f'<div class="v-readiness-card"><div style="font-size:.95rem;font-weight:600;color:{TEXT};margin-bottom:.5rem">🇰🇷 South Korea — Readiness Status</div>', unsafe_allow_html=True)
    for name, ok, detail in COUNTRY_READINESS_DETAIL["KR"]:
        dot = "ok" if ok else "nok"
        st.markdown(f'<div class="v-readiness-row"><div class="v-readiness-dot {dot}"></div><span style="font-weight:500">{name}</span><span style="color:{MUTED};font-size:.75rem"> — {detail}</span></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(f'<div class="v-readiness-card"><div style="font-size:.95rem;font-weight:600;color:{TEXT};margin-bottom:.5rem">🇹🇭 Thailand — Readiness Status</div>', unsafe_allow_html=True)
    for name, ok, detail in COUNTRY_READINESS_DETAIL["TH"]:
        dot = "ok" if ok else "nok"
        st.markdown(f'<div class="v-readiness-row"><div class="v-readiness-dot {dot}"></div><span style="font-weight:500">{name}</span><span style="color:{MUTED};font-size:.75rem"> — {detail}</span></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — API Cost Analytics
# ══════════════════════════════════════════════════════════════════════════════
elif section == "API Cost Analytics":
    st.markdown(f'<div style="font-size:1.4rem;font-weight:700;color:{TEXT};margin-bottom:.4rem">📊 API Cost Analytics</div>', unsafe_allow_html=True)
    st.markdown(f'<p style="color:{MUTED};margin-bottom:1.5rem">Live cost tracking. VAV System = Internal Compute (0.00 €).</p>', unsafe_allow_html=True)

    stats = _get_stats()
    if not stats or "error" in stats:
        st.warning("Cannot load stats — ensure FastAPI and Redis are running.")
    else:
        total = stats.get("total", 0); success = stats.get("success", 0)
        failed = stats.get("failed", 0); manual = stats.get("manual_review", 0)
        vav_path = stats.get("vav_path", 0); fast_path = stats.get("fast_path", 0)
        vav_rate = stats.get("vav_rate_pct", 0.0); total_cost = stats.get("total_cost_eur", 0.0)

        c1, c2, c3, c4 = st.columns(4)
        for col, label, value, cls in [
            (c1, "Total", str(total), ""),
            (c2, "Successful", str(success), "green"),
            (c3, "Failed / Review", f"{failed} / {manual}", "red" if failed else ""),
            (c4, "Total Cost", f"{total_cost:.4f} €", "blue"),
        ]:
            with col:
                st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">{label}</div><div class="v-metric-value {cls}">{value}</div></div>', unsafe_allow_html=True)

        st.markdown("---")
        cl, cr = st.columns(2)
        with cl:
            st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">Fast Path</div><div class="v-metric-value green">{fast_path}</div><div style="font-size:.72rem;color:{MUTED};margin-top:.25rem">{100 - vav_rate:.1f}% of total</div></div>', unsafe_allow_html=True)
        with cr:
            cls2 = "red" if vav_rate > 30 else "blue"
            st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">VAV System</div><div class="v-metric-value {cls2}">{vav_path}</div><div style="font-size:.72rem;color:{MUTED};margin-top:.25rem">{vav_rate:.1f}% · Internal Compute: 0.00 €</div></div>', unsafe_allow_html=True)
        st.progress(min(int(vav_rate), 100), text=f"VAV rate: {vav_rate:.1f}%")
        st.markdown("---")
        st.markdown(f'<div style="font-size:1rem;font-weight:600;color:{TEXT};margin-bottom:.75rem">Cost Model</div>', unsafe_allow_html=True)
        st.dataframe({
            "Service":     ["PASS API (Korea)", "Laser ID (Thailand)", "VAV System", "MRZ Fast Path", "NFC Chip"],
            "Type":        ["External API",     "External API",        "Internal",   "Internal",      "Device"],
            "Cost / Call": ["0.05 €",           "0.02 €",             "0.00 €",     "0.00 €",        "0.00 €"],
            "Status":      ["Stub",             "Stub",               "Active",     "Active",        "Active"],
        }, use_container_width=True, hide_index=True)
        recent = stats.get("recent_events", [])
        if recent:
            st.markdown("---")
            st.markdown(f'<div style="font-size:1rem;font-weight:600;color:{TEXT};margin-bottom:.75rem">Recent Transactions</div>', unsafe_allow_html=True)
            st.dataframe([{
                "Time": e.get("ts","")[:19].replace("T"," "), "Project": e.get("project",""),
                "Country": e.get("country",""), "Type": e.get("cost_type",""),
                "Cost €": f"{e.get('cost_eur',0):.4f}", "Status": e.get("status",""),
            } for e in recent], use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — Audit Log
# ══════════════════════════════════════════════════════════════════════════════
elif section == "Audit Log":
    st.markdown(f'<div style="font-size:1.4rem;font-weight:700;color:{TEXT};margin-bottom:.4rem">📝 Audit Log</div>', unsafe_allow_html=True)
    st.markdown(f"""
    <p style="color:{MUTED};margin-bottom:1.5rem">
        Immutable record of every manual review decision. Contains: Admin ID, User Email,
        Task ID, Timestamp, Action. <strong>No image data is ever stored</strong>
        (GDPR Art. 5 / ZVOP-2 §4).
    </p>
    """, unsafe_allow_html=True)

    limit = st.slider("Records to display", 10, 200, 50, key="audit_limit")
    if st.button("🔄 Refresh", key="refresh_audit"):
        st.rerun()

    try:
        resp = _api_get("/admin/audit-log", params={"limit": limit})
        if resp.status_code == 401:
            st.error("Session expired — please sign in again.")
            st.session_state.admin_authenticated = False; st.rerun()
        records = resp.json() if resp.status_code == 200 else []
    except Exception as exc:
        st.error(f"Cannot reach backend: {exc}"); records = []

    if not records:
        st.markdown(f"""
        <div class="v-card" style="text-align:center;padding:2.5rem">
            <div style="font-size:2rem;margin-bottom:.75rem">📋</div>
            <div style="font-weight:600;color:{TEXT}">No audit records yet</div>
            <div style="color:{MUTED};font-size:.85rem;margin-top:.3rem">
                Records appear here after admins approve or reject review items.
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f'<div style="font-size:.82rem;color:{MUTED};margin-bottom:.75rem">{len(records)} record(s)</div>', unsafe_allow_html=True)
        display = []
        for r in records:
            action = r.get("action", "")
            icon = "✅" if action == "approved" else "❌"
            display.append({
                "Time":       r.get("ts", "")[:19].replace("T", " "),
                "Admin":      r.get("admin_id", ""),
                "User Email": r.get("user_email", "—"),
                "Task ID":    r.get("task_id", "")[:16] + "…",
                "Action":     f"{icon} {action}",
                "Project":    r.get("project", ""),
                "Country":    r.get("country", ""),
            })
        st.dataframe(display, use_container_width=True, hide_index=True)

        # Download button for the full JSONL
        try:
            full_resp = _api_get("/admin/audit-log", params={"limit": 10000})
            if full_resp.status_code == 200:
                all_records = full_resp.json()
                jsonl_bytes = "\n".join(json.dumps(r) for r in reversed(all_records)).encode()
                st.download_button("⬇ Download full audit log (JSONL)",
                                   data=jsonl_bytes, file_name="veritas_audit.jsonl",
                                   mime="application/jsonl", key="download_audit")
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# SECTION — System Stats
# ══════════════════════════════════════════════════════════════════════════════
elif section == "System Stats":
    st.markdown(f'<div style="font-size:1.4rem;font-weight:700;color:{TEXT};margin-bottom:.4rem">⚙️ System Stats</div>', unsafe_allow_html=True)
    st.markdown(f'<p style="color:{MUTED};margin-bottom:1.5rem">Runtime health, engine status, and infrastructure overview.</p>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    _es = _get_engine_status()
    with c1:
        dot_cls = {"ready": "green", "loading": "yellow"}.get(_es, "red")
        lbl = {"ready": "Ready", "loading": "Loading"}.get(_es, "Unreachable")
        desc = {"ready": "VAV System loaded and active",
                "loading": "VAV System initialising…"}.get(_es, "Engine process not running")
        st.markdown(f"""
        <div class="v-status-widget">
            <div class="v-status-orb">{svg_cpu()}<div class="v-status-dot {dot_cls}"></div></div>
            <div class="v-status-title">VAV System</div>
            <div class="v-status-sub" style="font-weight:600;margin-bottom:.2rem">{lbl}</div>
            <div class="v-status-sub">{desc}</div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Refresh", key="refresh_engine"):
            st.rerun()

    with c2:
        ngrok_url = _get_ngrok_url()
        ng_cls = "green" if ngrok_url else "red"
        ng_lbl = ngrok_url or "Not detected"
        st.markdown(f"""
        <div class="v-status-widget">
            <div class="v-status-orb" style="font-size:1.6rem">🔗
                <div class="v-status-dot {ng_cls}"></div>
            </div>
            <div class="v-status-title">ngrok Tunnel</div>
            <div class="v-status-sub" style="word-break:break-all">{ng_lbl}</div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f'<div style="font-size:1rem;font-weight:600;color:{TEXT};margin-bottom:.75rem">Supported Countries</div>', unsafe_allow_html=True)
    cols = st.columns(len(COUNTRY_STATUS))
    for col, (code, info) in zip(cols, COUNTRY_STATUS.items()):
        with col:
            sc = GREEN if info["ready"] else ORANGE
            sw = "Active" if info["ready"] else "Partial"
            st.markdown(f'<div class="v-metric-card"><div style="font-size:1.8rem;margin-bottom:.4rem">{info["flag"]}</div><div class="v-metric-label">{code}</div><div class="v-metric-value" style="color:{sc}">{sw}</div></div>', unsafe_allow_html=True)

    st.markdown("---")
    try:
        health = requests.get(f"{BACKEND_URL}/health", timeout=3).json()
        api_ok = True
    except Exception:
        health = {}; api_ok = False

    st.dataframe({
        "Component":  ["FastAPI Backend", "Redis / Celery", "VAV System", "ngrok Tunnel"],
        "Status": [
            "✅ Online" if api_ok else "❌ Offline",
            "✅ Connected" if api_ok else "⚠ Unknown",
            "✅ Ready" if _es == "ready" else ("⏳ Loading" if _es == "loading" else "❌ Offline"),
            "✅ Active" if ngrok_url else "❌ Not running",
        ],
    }, use_container_width=True, hide_index=True)

# ── Persistent watermark ──────────────────────────────────────────────────────
st.markdown(f"""
<div class="v-watermark">
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
    Powered by Veritas ID
</div>""", unsafe_allow_html=True)
