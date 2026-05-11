import io
import json
import socket
import time
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


def _get_lan_ip() -> str:
    """Return the machine's LAN IPv4 (e.g. 192.168.x.x), never 127.0.0.1."""
    try:
        # Connect to an external address – no data is sent, just resolves routing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # Fallback: iterate all interfaces
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ":" not in ip:
                return ip
    except Exception:
        pass
    return "127.0.0.1"


_LAN_IP = _get_lan_ip()
POLL_INTERVAL = 2
MAX_POLLS = 60
CONFIG_FILE = pathlib.Path(__file__).parent / ".veritas_config.json"

# ── Persistent config ─────────────────────────────────────────────────────────
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
    page_title="Veritas IDV - Dev Portal",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Session state ─────────────────────────────────────────────────────────────
if "api_key" not in st.session_state:
    st.session_state.api_key = _cfg.get("api_key", "")
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = _cfg.get("dark_mode", False)
if "show_settings" not in st.session_state:
    st.session_state.show_settings = False
if "result" not in st.session_state:
    st.session_state.result = None
if "task_id" not in st.session_state:
    st.session_state.task_id = None
if "qr_png_bytes" not in st.session_state:
    st.session_state.qr_png_bytes = None
if "qr_mobile_url" not in st.session_state:
    st.session_state.qr_mobile_url = None
if "qr_session_label" not in st.session_state:
    st.session_state.qr_session_label = None
if "public_base_url" not in st.session_state:
    st.session_state.public_base_url = _cfg.get("public_base_url", "")

dm = st.session_state.dark_mode

# ── Theme tokens ──────────────────────────────────────────────────────────────
if dm:
    BG        = "#000000"
    CARD      = "#1c1c1e"
    CARD2     = "#2c2c2e"
    TEXT      = "#f5f5f7"
    MUTED     = "#98989d"
    INPUT_BG  = "#2c2c2e"
    INPUT_HV  = "#3a3a3c"
    BORDER    = "#3a3a3c"
    SHADOW    = "0 8px 30px rgba(0,0,0,0.5)"
    SHADOW_HV = "0 14px 40px rgba(0,0,0,0.7)"
    DOT_BORDER = CARD
else:
    BG        = "#f5f5f7"
    CARD      = "#ffffff"
    CARD2     = "#f5f5f7"
    TEXT      = "#1d1d1f"
    MUTED     = "#86868b"
    INPUT_BG  = "#f5f5f7"
    INPUT_HV  = "#e8e8ed"
    BORDER    = "#e8e8ed"
    SHADOW    = "0 8px 30px rgba(0,0,0,0.04)"
    SHADOW_HV = "0 14px 40px rgba(0,0,0,0.08)"
    DOT_BORDER = "#ffffff"

BLUE      = "#0071e3"
BLUE_HV   = "#0077ED"
GREEN     = "#34c759"
RED       = "#ff3b30"
ORANGE    = "#ff9f0a"

# ── SVG icons (inline Lucide) ─────────────────────────────────────────────────
def svg_shield():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="{BLUE}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>'

def svg_id_card():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="5" rx="2"/><circle cx="8" cy="12" r="2"/><path d="M13 11h4M13 15h3"/></svg>'

def svg_camera():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/></svg>'

def svg_cpu():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="{TEXT}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2M9 2v2M15 20v2M9 20v2M2 15h2M2 9h2M20 15h2M20 9h2"/></svg>'

def svg_settings():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="{TEXT}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>'

def svg_key():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/></svg>'

def svg_moon():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>'

def svg_file_badge():
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M8 21v-1a4 4 0 0 1 8 0v1"/></svg>'

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
html, body, [class*="css"], .stApp {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Helvetica, Arial, sans-serif !important;
    -webkit-font-smoothing: antialiased;
    background-color: {BG} !important;
    color: {TEXT} !important;
}}

#MainMenu, footer, header {{ visibility: hidden; }}
.stDeployButton {{ display: none; }}
[data-testid="stSidebar"] {{ display: none; }}
[data-testid="collapsedControl"] {{ display: none; }}

.block-container {{
    padding-top: 1.5rem !important;
    padding-bottom: 5rem !important;
    max-width: 1100px !important;
}}

/* ── Settings button fixed top-right ── */
.v-settings-fab {{
    position: fixed;
    top: 1.5rem;
    right: 1.5rem;
    z-index: 999;
    width: 48px;
    height: 48px;
    background: {CARD};
    border-radius: 50%;
    box-shadow: {SHADOW};
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    border: none;
    outline: none;
}}
.v-settings-fab:hover {{ transform: scale(1.08); box-shadow: {SHADOW_HV}; }}

/* ── Modal overlay ── */
.v-modal-overlay {{
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.45);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
}}
.v-modal {{
    background: {CARD};
    border-radius: 2rem;
    box-shadow: 0 24px 60px rgba(0,0,0,0.3);
    padding: 2.5rem;
    width: 100%;
    max-width: 440px;
    position: relative;
}}
.v-modal-title {{
    font-size: 1.5rem;
    font-weight: 600;
    letter-spacing: -0.03em;
    color: {TEXT};
    margin: 0 0 2rem 0;
}}
.v-modal-close {{
    position: absolute;
    top: 1.5rem;
    right: 1.5rem;
    width: 32px;
    height: 32px;
    background: {INPUT_BG};
    border-radius: 50%;
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    color: {MUTED};
    font-size: 1rem;
    transition: background 0.2s;
}}
.v-modal-close:hover {{ background: {INPUT_HV}; }}

/* Dark mode toggle row */
.v-toggle-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: 1.5rem;
    border-top: 1px solid {BORDER};
    margin-top: 1.5rem;
}}
.v-toggle-left {{
    display: flex;
    align-items: center;
    gap: 0.75rem;
}}
.v-toggle-icon {{
    width: 36px;
    height: 36px;
    background: {INPUT_BG};
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
}}
.v-toggle-label {{ font-size: 0.95rem; font-weight: 500; color: {TEXT}; }}
.v-toggle-sub   {{ font-size: 0.75rem; color: {MUTED}; }}

/* iOS-style toggle */
.v-ios-toggle {{
    width: 51px;
    height: 31px;
    background: {"#34c759" if dm else "#e5e5ea"};
    border-radius: 999px;
    position: relative;
    cursor: pointer;
    transition: background 0.3s ease;
    flex-shrink: 0;
    border: none;
    outline: none;
}}
.v-ios-knob {{
    position: absolute;
    top: 2px;
    left: {"22px" if dm else "2px"};
    width: 27px;
    height: 27px;
    background: white;
    border-radius: 50%;
    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
    transition: left 0.3s cubic-bezier(0.25,1,0.5,1);
}}

/* ── Header ── */
.v-header {{
    text-align: center;
    padding: 3rem 0 2rem 0;
}}
.v-icon-wrap {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: {CARD};
    border-radius: 50%;
    width: 72px;
    height: 72px;
    box-shadow: {SHADOW};
    margin-bottom: 1.25rem;
}}
.v-header h1 {{
    font-size: 2.8rem;
    font-weight: 600;
    letter-spacing: -0.04em;
    color: {TEXT};
    margin: 0 0 0.5rem 0;
}}
.v-header p {{
    font-size: 1.1rem;
    color: {MUTED};
    font-weight: 500;
    margin: 0;
}}

/* ── Cards ── */
.v-card {{
    background: {CARD};
    border-radius: 2rem;
    box-shadow: {SHADOW};
    padding: 2rem;
    transition: box-shadow 0.3s ease;
    margin-bottom: 1.5rem;
}}
.v-card:hover {{ box-shadow: {SHADOW_HV}; }}

.v-section-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.25rem;
}}
.v-section-head h2 {{
    font-size: 1.05rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    color: {TEXT};
    margin: 0;
}}

/* ── Upload zones ── */
.v-upload-zone {{
    background: {INPUT_BG};
    border-radius: 1.5rem;
    height: 240px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background 0.2s ease, transform 0.2s ease;
    text-align: center;
    padding: 1.5rem;
    margin-bottom: 0.75rem;
}}
.v-upload-zone:hover {{ background: {INPUT_HV}; transform: scale(0.99); }}
.v-upload-title {{ font-size: 0.95rem; font-weight: 500; color: {TEXT}; margin-top: 0.75rem; }}
.v-upload-sub   {{ font-size: 0.8rem; color: {MUTED}; margin-top: 0.25rem; }}

/* ── Status widget ── */
.v-status-widget {{
    background: {CARD};
    border-radius: 2rem;
    box-shadow: {SHADOW};
    padding: 2rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    margin-bottom: 1.5rem;
}}
.v-status-orb {{
    width: 64px;
    height: 64px;
    border-radius: 50%;
    background: {INPUT_BG};
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 1rem;
    position: relative;
}}
.v-status-dot {{
    position: absolute;
    top: 1px; right: 1px;
    width: 15px; height: 15px;
    border-radius: 50%;
    border: 2.5px solid {DOT_BORDER};
}}
.v-status-dot.green  {{ background: {GREEN}; }}
.v-status-dot.yellow {{ background: {ORANGE}; }}
.v-status-dot.red    {{ background: {RED}; }}
.v-status-title {{ font-size: 1.05rem; font-weight: 600; color: {TEXT}; margin-bottom: 0.25rem; }}
.v-status-sub   {{ font-size: 0.82rem; color: {MUTED}; }}

/* ── Primary button ── */
.stButton > button[kind="primary"] {{
    width: 100% !important;
    background: {BLUE} !important;
    color: white !important;
    border: none !important;
    border-radius: 2rem !important;
    padding: 1rem 1.5rem !important;
    font-size: 1rem !important;
    font-weight: 500 !important;
    letter-spacing: -0.01em !important;
    cursor: pointer !important;
    transition: background 0.2s ease, transform 0.1s ease !important;
    box-shadow: 0 4px 15px rgba(0,113,227,0.3) !important;
}}
.stButton > button[kind="primary"]:hover {{ background: {BLUE_HV} !important; }}
.stButton > button[kind="primary"]:active {{ transform: scale(0.98) !important; }}
.stButton > button[kind="primary"]:disabled {{
    opacity: 0.5 !important;
    cursor: not-allowed !important;
    box-shadow: none !important;
}}

/* ── Secondary / icon buttons ── */
.stButton > button:not([kind="primary"]) {{
    background: {INPUT_BG} !important;
    color: {TEXT} !important;
    border: none !important;
    border-radius: 2rem !important;
    padding: 0.5rem 1rem !important;
    font-size: 0.85rem !important;
    font-weight: 500 !important;
    transition: background 0.2s !important;
}}
.stButton > button:not([kind="primary"]):hover {{ background: {INPUT_HV} !important; }}

/* ── Text input ── */
.stTextInput input {{
    background: {INPUT_BG} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 1rem !important;
    padding: 0.85rem 1.25rem !important;
    font-size: 0.9rem !important;
    font-family: "SF Mono", "Fira Code", monospace !important;
    color: {TEXT} !important;
    transition: all 0.2s ease !important;
}}
.stTextInput input:focus {{
    border-color: rgba(0,113,227,0.5) !important;
    box-shadow: 0 0 0 3px rgba(0,113,227,0.12) !important;
    outline: none !important;
    background: {CARD} !important;
}}
.stTextInput label {{ display: none !important; }}
.stTextInput input::placeholder {{ color: {MUTED} !important; opacity: 1; }}

/* ── Settings label ── */
.v-settings-label {{
    font-size: 0.72rem;
    font-weight: 600;
    color: {MUTED};
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.5rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
}}
.v-help-text {{ font-size: 0.75rem; color: {MUTED}; margin-top: 0.4rem; }}
.v-warning    {{ font-size: 0.78rem; color: {RED}; text-align: center; margin-top: 0.5rem; font-weight: 500; }}

/* ── Result card ── */
.v-result-card {{
    background: {CARD};
    border-radius: 2rem;
    box-shadow: {SHADOW};
    padding: 3rem;
    margin-top: 2rem;
}}
.v-result-header {{ text-align: center; margin-bottom: 2rem; }}
.v-result-icon {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 64px; height: 64px;
    border-radius: 50%;
    font-size: 1.8rem;
    margin-bottom: 1rem;
}}
.v-result-icon.success {{ background: rgba(52,199,89,0.12); color: {GREEN}; }}
.v-result-icon.error   {{ background: rgba(255,59,48,0.12);  color: {RED}; }}
.v-result-icon.manual  {{ background: rgba(255,159,10,0.12); color: {ORANGE}; }}
.v-result-header h2 {{
    font-size: 1.9rem;
    font-weight: 600;
    letter-spacing: -0.03em;
    margin: 0 0 0.4rem 0;
    color: {TEXT};
}}
.v-result-header p {{ font-size: 0.95rem; color: {MUTED}; margin: 0; }}

.v-metric-card {{
    background: {INPUT_BG};
    border-radius: 1.5rem;
    padding: 1.5rem;
    text-align: center;
}}
.v-metric-label {{
    font-size: 0.72rem;
    font-weight: 500;
    color: {MUTED};
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 0.5rem;
}}
.v-metric-value          {{ font-size: 1.15rem; font-weight: 600; color: {TEXT}; }}
.v-metric-value.green    {{ color: {GREEN}; }}
.v-metric-value.red      {{ color: {RED}; }}

/* ── Loading ── */
.v-loading {{
    background: {CARD};
    border-radius: 2rem;
    box-shadow: {SHADOW};
    padding: 2.5rem;
    text-align: center;
}}
.v-loading-title {{ font-size: 1rem; font-weight: 600; color: {TEXT}; margin-bottom: 0.25rem; }}
.v-loading-sub   {{ font-size: 0.78rem; color: {MUTED}; font-family: monospace; }}
.v-progress-track {{
    width: 100%;
    height: 3px;
    background: {INPUT_BG};
    border-radius: 999px;
    margin-top: 1.5rem;
    overflow: hidden;
}}

/* ── Expander ── */
.streamlit-expanderHeader {{
    background: {INPUT_BG} !important;
    border-radius: 1rem !important;
    color: {MUTED} !important;
    font-size: 0.85rem !important;
}}

/* ── Divider ── */
hr {{ border: none; border-top: 1px solid {BORDER} !important; margin: 1.5rem 0 !important; }}
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
# autorefresh removed – it caused constant screen flickering.
# Engine status updates on manual page reload or after a verification completes.

# ── Settings FAB (fixed top-right via HTML) ───────────────────────────────────
st.markdown(f"""
<div class="v-settings-fab" onclick="window.location.href='?settings=1'" title="Nastavitve" id="settings-fab">
    {svg_settings()}
</div>
""", unsafe_allow_html=True)

# Detect settings open via a Streamlit button hidden off-screen (actual toggle)
# We use a real st.button for interactivity, styled to look like the FAB
_, fab_col = st.columns([20, 1])
with fab_col:
    if st.button("⚙", key="settings_fab_btn", help="Nastavitve"):
        st.session_state.show_settings = not st.session_state.show_settings
        st.rerun()

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="v-header">
    <div class="v-icon-wrap">{svg_shield()}</div>
    <h1>Veritas IDV</h1>
    <p>Interno orodje za varno in hitro preverjanje identitete.</p>
</div>
""", unsafe_allow_html=True)

# ── Settings modal ─────────────────────────────────────────────────────────────
if st.session_state.show_settings:
    st.markdown(f"""
    <div class="v-modal-overlay">
        <div class="v-modal">
            <div class="v-modal-title">Nastavitve</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    with st.container():
        st.markdown(f"""
        <div style="
            background:{CARD};
            border-radius:2rem;
            box-shadow:0 24px 60px rgba(0,0,0,0.25);
            padding:2.5rem;
            max-width:440px;
            margin:0 auto 2rem auto;
            position:relative;
        ">
            <div style="font-size:1.5rem;font-weight:600;letter-spacing:-0.03em;color:{TEXT};margin-bottom:2rem;">Nastavitve</div>
            <div class="v-settings-label">{svg_key()} &nbsp;API Avtentikacija</div>
        </div>
        """, unsafe_allow_html=True)

        _, center_col, _ = st.columns([1, 4, 1])
        with center_col:
            close_col, _ = st.columns([1, 6])
            with close_col:
                if st.button("✕ Zapri", key="close_settings"):
                    st.session_state.show_settings = False
                    st.rerun()

            st.markdown(f'<div class="v-settings-label">{svg_key()} &nbsp;API Avtentikacija</div>', unsafe_allow_html=True)

            new_key = st.text_input(
                "API Key",
                value=st.session_state.api_key,
                type="password",
                placeholder="vrt_...",
                key="settings_api_key_input",
            )
            st.markdown(f'<p class="v-help-text">Ključ se shranjuje lokalno in se uporablja za avtentikacijo na Veritas omrežje.</p>', unsafe_allow_html=True)

            if new_key != st.session_state.api_key:
                st.session_state.api_key = new_key
                save_config({"api_key": new_key})

            st.markdown(f"""
            <div class="v-toggle-row">
                <div class="v-toggle-left">
                    <div class="v-toggle-icon">{svg_moon()}</div>
                    <div>
                        <div class="v-toggle-label">Temni način</div>
                        <div class="v-toggle-sub">Prilagodi videz vmesnika</div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)

            dm_label = "🌙 Temni način: VKLOPLJEN" if dm else "☀️ Temni način: IZKLOPLJEN"
            if st.toggle(dm_label, value=dm, key="dark_mode_toggle"):
                if not st.session_state.dark_mode:
                    st.session_state.dark_mode = True
                    save_config({"dark_mode": True})
                    st.rerun()
            else:
                if st.session_state.dark_mode:
                    st.session_state.dark_mode = False
                    save_config({"dark_mode": False})
                    st.rerun()

    st.divider()

# ── Main layout ───────────────────────────────────────────────────────────────
left_col, right_col = st.columns([8, 4], gap="large")

with left_col:
    tab_desktop, tab_mobile = st.tabs(["💻  Desktop Upload", "📱  Mobile QR Flow"])

    # ── Desktop upload tab ────────────────────────────────────────────────
    with tab_desktop:
        st.markdown(f"""
        <div class="v-card">
            <div class="v-section-head">
                <h2>Identifikacijski podatki</h2>
                {svg_file_badge()}
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:1rem;">
                <div class="v-upload-zone">
                    {svg_id_card()}
                    <div class="v-upload-title">Dokument – Spredaj</div>
                    <div class="v-upload-sub">Naloži sliko (JPG, PNG)</div>
                </div>
                <div class="v-upload-zone">
                    {svg_id_card()}
                    <div class="v-upload-title">Dokument – Zadaj (MRZ)</div>
                    <div class="v-upload-sub">Naloži sliko (JPG, PNG)</div>
                </div>
                <div class="v-upload-zone">
                    {svg_camera()}
                    <div class="v-upload-title">Selfie obraz</div>
                    <div class="v-upload-sub">Za primerjavo (Opcijsko)</div>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        up_col1, up_col2, up_col3 = st.columns(3, gap="medium")
        with up_col1:
            id_file = st.file_uploader(
                "Dokument – Spredaj",
                type=["jpg", "jpeg", "png", "webp"],
                label_visibility="collapsed",
                key="id_uploader",
            )
            if id_file:
                st.image(id_file, use_container_width=True)

        with up_col2:
            id_back_file = st.file_uploader(
                "Dokument – Zadaj",
                type=["jpg", "jpeg", "png", "webp"],
                label_visibility="collapsed",
                key="id_back_uploader",
            )
            if id_back_file:
                st.image(id_back_file, use_container_width=True)

        with up_col3:
            selfie_file = st.camera_input(
                "Selfie",
                label_visibility="collapsed",
                key="selfie_input",
            )

    # ── Mobile QR tab ─────────────────────────────────────────────────────
    with tab_mobile:
        st.markdown(f"""
        <div class="v-card">
            <div class="v-section-head">
                <h2>Mobilni tok z QR kodo</h2>
                {svg_camera()}
            </div>
            <p style="font-size:.9rem;color:{MUTED};margin-bottom:1.25rem;">
                Ustvari enkratno QR sejo. Stranka skenira QR s telefonom,
                naredi selfie, fotografira obe strani dokumenta in po želji
                prebere NFC čip.
            </p>
        </div>
        """, unsafe_allow_html=True)

        # Auto-detected LAN IP – user can still override
        default_url = f"http://{_LAN_IP}:8000"
        mobile_server = st.text_input(
            "URL strežnika (dostopen s telefona – samodejno zaznano)",
            value=st.session_state.get("mobile_server_url", default_url),
            placeholder=default_url,
            key="mobile_server_url_input",
        )
        if mobile_server != st.session_state.get("mobile_server_url", ""):
            st.session_state["mobile_server_url"] = mobile_server
            save_config({"mobile_server_url": mobile_server})

        st.caption(f"Zaznani LAN IP: `{_LAN_IP}` — FastAPI mora teči z `--host 0.0.0.0 --port 8000`")

        public_url = st.text_input(
            "Public Base URL (e.g. ngrok)",
            value=st.session_state.public_base_url,
            placeholder="https://a1b2-c3d4.ngrok-free.app",
            key="public_base_url_input",
            help="If set, the QR code will point to this URL instead of the LAN IP. Leave empty to use the LAN address above.",
        )
        if public_url != st.session_state.public_base_url:
            st.session_state.public_base_url = public_url
            save_config({"public_base_url": public_url})
        has_public_url = bool(public_url.strip())
        if has_public_url:
            st.caption(f"QR will use: `{public_url.rstrip('/')}`")
        else:
            st.warning(
                "⚠️ **Public URL required.** Camera access needs HTTPS. "
                "Enter your ngrok URL above before generating the QR code.",
                icon=None,
            )

        gen_qr = st.button(
            "Generiraj QR kodo",
            type="primary",
            disabled=not st.session_state.api_key.strip() or not has_public_url,
            use_container_width=True,
            key="gen_qr_btn",
        )

        if gen_qr:
            if not _QR_AVAILABLE:
                st.error("Knjižnica `qrcode` ni nameščena. Zaženi: `pip install qrcode[pil]`")
            else:
                # Clear any previous QR from session state
                st.session_state.qr_png_bytes    = None
                st.session_state.qr_mobile_url   = None
                st.session_state.qr_session_label = None

                # Step 1: get session_id from backend
                try:
                    qr_resp = requests.post(
                        f"{BACKEND_URL}/mobile/session",
                        headers={"X-API-Key": st.session_state.api_key.strip()},
                        timeout=10,
                    )
                    qr_resp.raise_for_status()
                    payload    = qr_resp.json()
                    session_id = payload["session_id"]
                    expires_in = payload.get("expires_in", 600)
                except requests.exceptions.ConnectionError:
                    st.error("Strežnik ni dosegljiv na `localhost:8000`. Preveri, ali FastAPI teče.")
                    st.stop()
                except Exception as exc:
                    st.error(f"Napaka pri ustvarjanju seje: {exc}")
                    st.error(traceback.format_exc())
                    st.stop()

                _base = st.session_state.public_base_url.strip().rstrip('/') or mobile_server.rstrip('/')
                mobile_url = f"{_base}/mobile/{session_id}"

                # Step 2: render QR into bytes and store in session_state
                try:
                    qr = qrcode.QRCode(
                        version=None,
                        error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=8,
                        border=3,
                    )
                    qr.add_data(mobile_url)
                    qr.make(fit=True)
                    pil_img = qr.make_image(fill_color="black", back_color="white")
                    buf = io.BytesIO()
                    pil_img.save(buf, format="PNG")
                    st.session_state.qr_png_bytes     = buf.getvalue()
                    st.session_state.qr_mobile_url    = mobile_url
                    st.session_state.qr_session_label = f"Velja {expires_in // 60} min · seja: {session_id[:12]}…"
                except Exception as exc:
                    st.warning(f"QR slika ni bila ustvarjena ({exc}) — uporabi spodnjo povezavo.")
                    st.error(traceback.format_exc())
                    st.session_state.qr_mobile_url = mobile_url
                # Rerun immediately so the render block executes on a clean pass
                st.rerun()

        # Render QR from session_state – always visible, survives reruns
        if st.session_state.get("qr_mobile_url"):
            st.markdown("**Mobilna povezava** (kopiraj ali skeniraj QR):")
            st.code(st.session_state.qr_mobile_url, language=None)
        if st.session_state.get("qr_png_bytes"):
            st.image(
                st.session_state.qr_png_bytes,
                caption=st.session_state.get("qr_session_label") or "",
                width=260,
            )
            st.info("Po skeniranju QR se mobilna aplikacija odpre v brskalniku telefona.")
            if st.button("Počisti / nova seja", key="clear_qr_btn"):
                st.session_state.qr_png_bytes     = None
                st.session_state.qr_mobile_url    = None
                st.session_state.qr_session_label = None
                st.rerun()

    # Desktop tab sets these; mobile tab leaves them None so submit is skipped
    if "id_file" not in dir():
        id_file = None
    if "id_back_file" not in dir():
        id_back_file = None
    if "selfie_file" not in dir():
        selfie_file = None

with right_col:
    # Engine status
    if _engine_status == "ready":
        dot_class   = "green"
        status_text = "AI motor je pripravljen in aktiven"
    elif _engine_status == "loading":
        dot_class   = "yellow"
        status_text = "Gemma 4 se nalaga v RAM..."
    else:
        dot_class   = "red"
        status_text = "Motor ni dosegljiv"

    st.markdown(f"""
    <div class="v-status-widget">
        <div class="v-status-orb">
            {svg_cpu()}
            <div class="v-status-dot {dot_class}"></div>
        </div>
        <div class="v-status-title">Gemma 4.0</div>
        <div class="v-status-sub">{status_text}</div>
    </div>
    """, unsafe_allow_html=True)

    # Inline API key entry if missing
    if not st.session_state.api_key:
        st.markdown(f'<div class="v-settings-label">{svg_key()} &nbsp;API Ključ</div>', unsafe_allow_html=True)
        inline_key = st.text_input(
            "API Key inline",
            value="",
            type="password",
            placeholder="vrt_...",
            label_visibility="collapsed",
            key="inline_api_key",
        )
        if inline_key:
            st.session_state.api_key = inline_key
            save_config({"api_key": inline_key})
            st.rerun()

    has_key  = bool(st.session_state.api_key.strip())
    has_doc  = id_file is not None
    has_back = id_back_file is not None

    submit = st.button(
        "🔒  Zaženi verifikacijo",
        type="primary",
        disabled=(not has_key or not has_doc or not has_back),
        use_container_width=True,
        key="verify_btn",
    )

    if not has_key:
        st.markdown(f'<p class="v-warning">Manjka API ključ. Dodajte ga zgoraj ali v nastavitvah (⚙).</p>', unsafe_allow_html=True)
    elif not has_doc or not has_back:
        st.markdown(f'<p class="v-warning">Naloži sprednji in zadnji del dokumenta za nadaljevanje.</p>', unsafe_allow_html=True)

# ── Verification flow ─────────────────────────────────────────────────────────
if submit:
    st.session_state.result = None
    st.session_state.task_id = None

    prog = st.empty()

    def _progress(icon: str, title: str, sub: str, pct: int):
        prog.markdown(f"""
        <div class="v-loading">
            <div style="font-size:2rem;margin-bottom:0.75rem">{icon}</div>
            <div class="v-loading-title">{title}</div>
            <div class="v-loading-sub">{sub}</div>
            <div class="v-progress-track">
                <div style="width:{pct}%;height:100%;background:{BLUE};border-radius:999px;transition:width 0.8s cubic-bezier(0.65,0,0.35,1)"></div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    _progress("📡", "Kriptiranje in prenos...", "Priprava multipart zahteve", 15)

    headers = {"X-API-Key": st.session_state.api_key.strip()}
    files: dict = {
        "id_document": (id_file.name, id_file.getvalue(), id_file.type),
        "id_back": (id_back_file.name, id_back_file.getvalue(), id_back_file.type),
    }
    if selfie_file:
        files["selfie"] = ("selfie.jpg", selfie_file.getvalue(), "image/jpeg")

    try:
        resp = requests.post(f"{BACKEND_URL}/verify", headers=headers, files=files, timeout=15)
    except requests.exceptions.ConnectionError:
        prog.error("**Strežnik ni dosegljiv.** Preveri, ali FastAPI teče na `localhost:8000`.", icon="🔌")
        st.stop()
    except requests.exceptions.Timeout:
        prog.error("Zahteva je potekla (timeout 15 s). Poskusi znova.", icon="⏱️")
        st.stop()

    if resp.status_code == 401:
        prog.error("**Neveljaven API ključ.** Preveri vrednost v nastavitvah.", icon="🔑")
        st.stop()
    elif not resp.ok:
        prog.error(f"Napaka strežnika ({resp.status_code}):\n```\n{resp.text}\n```")
        st.stop()

    task_id = resp.json().get("task_id")
    if not task_id:
        prog.error(f"Strežnik ni vrnil `task_id`:\n```json\n{resp.text}\n```")
        st.stop()

    st.session_state.task_id = task_id
    _progress("🧠", "Analiza dokumenta...", f"Gemma 4 procesira sliko v RAM-u · task: {task_id[:8]}…", 45)

    result = None
    for poll_num in range(1, MAX_POLLS + 1):
        time.sleep(POLL_INTERVAL)
        try:
            poll_resp = requests.get(f"{BACKEND_URL}/verify/status/{task_id}", headers=headers, timeout=10)
        except requests.exceptions.RequestException as exc:
            prog.error(f"Napaka pri anketiranju: {exc}", icon="🔌")
            st.stop()

        if not poll_resp.ok:
            prog.error(f"Anketiranje vrnilo {poll_resp.status_code}:\n```\n{poll_resp.text}\n```")
            st.stop()

        payload = poll_resp.json()
        task_status = payload.get("state", "PENDING")
        pct = min(45 + poll_num * 2, 90)
        _progress("🔍", "Preverjanje varnostnih elementov...", f"[{poll_num}/{MAX_POLLS}] Status: {task_status}", pct)

        if task_status == "SUCCESS":
            result = payload.get("result", {})
            break
        elif task_status in ("FAILURE", "REVOKED"):
            prog.error(f"Naloga se je končala z **{task_status}**.\n```json\n{payload}\n```", icon="💥")
            st.stop()
    else:
        prog.warning(f"Naloga `{task_id}` ni bila zaključena v {MAX_POLLS * POLL_INTERVAL} s. Poskusi znova.")
        st.stop()

    _progress("✅", "Verifikacija zaključena", "Rezultati so pripravljeni", 100)
    time.sleep(0.4)
    prog.empty()
    st.session_state.result = result

# ── Results ───────────────────────────────────────────────────────────────────
if st.session_state.result:
    result  = st.session_state.result
    task_id = st.session_state.task_id
    status  = result.get("status")

    if status == "approved":
        cls, ico, title, sub = "success", "✓", "Identiteta potrjena", "Varnostni pregledi so bili uspešno zaključeni."
    elif status == "manual_review":
        cls, ico, title, sub = "manual", "⚠", "Ročni pregled potreben", "Avtomatska verifikacija ni bila zaključena."
    else:
        cls, ico, title, sub = "error", "✕", "Verifikacija zavrnjena", "Identitete ni bilo mogoče potrditi."

    st.markdown(f"""
    <div class="v-result-card">
        <div class="v-result-header">
            <div class="v-result-icon {cls}">{ico}</div>
            <h2>{title}</h2>
            <p>{sub}</p>
        </div>
    </div>
    """, unsafe_allow_html=True)

    user_name    = result.get("user_name") or "—"
    age_verified = result.get("age_verified")
    face_match   = result.get("face_match")

    def _bool_html(val, true_text, false_text):
        if val is True:
            return f'<div class="v-metric-value green">{true_text}</div>'
        elif val is False:
            return f'<div class="v-metric-value red">{false_text}</div>'
        return f'<div class="v-metric-value">N/A</div>'

    m1, m2, m3 = st.columns(3, gap="medium")
    with m1:
        st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">Ime in priimek</div><div class="v-metric-value">{user_name}</div></div>', unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">Polnoletnost (18+)</div>{_bool_html(age_verified, "Potrjena", "Ni potrjena")}</div>', unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div class="v-metric-card"><div class="v-metric-label">Ujemanje obraza</div>{_bool_html(face_match, "Potrjeno", "Ni ujemanja")}</div>', unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    with st.expander("Pokaži surove podatke (JSON)"):
        st.json({"task_id": task_id, "task_status": "SUCCESS", "task_result": result})
