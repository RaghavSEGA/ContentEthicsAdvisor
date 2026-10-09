#!/usr/bin/env python3
"""
Content Ethics Advisor
Answers developers' content-ethics questions from the Expression Ethics Unit's
(anonymized) case history, with a panel of cultural-perspective reviewers.

Auth:     @segaamerica.com OTP via AWS SES -> HMAC-signed URL token (?t=)
Models:   Claude via AWS Bedrock
            lead reviewer (draft + final answer) · panel personas · relevance router
Cases:    from prepare_cases.py — an encrypted file in the repo (key in secrets),
          a plain local file (internal server), or optionally private S3
Prompts:  prompts.py
"""

import base64
import hashlib
import hmac
import io
import json
import re
import secrets as pysecrets
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import streamlit as st

from prompts import (BASE_PROMPT, DRAFT_ADDENDUM, PANEL_FRAME, PERSONAS, ROUTER_PROMPT,
                     SYNTHESIS_ADDENDUM)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_DOMAIN = "@segaamerica.com"
OTP_EXPIRY     = 600        # 10 minutes
TOKEN_EXPIRY   = 86400      # 1 day
MAX_ATTEMPTS   = 5

def _secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default

LEAD_MODEL_ID   = _secret("LEAD_MODEL_ID",   "us.anthropic.claude-opus-5-5")    # draft + final answer
PANEL_MODEL_ID  = _secret("PANEL_MODEL_ID",  "us.anthropic.claude-sonnet-5-5")  # persona reviewers
ROUTER_MODEL_ID = _secret("ROUTER_MODEL_ID", "us.anthropic.claude-haiku-5-5")   # picks relevant personas

LEAD_MAX_TOKENS   = 16000
PANEL_MAX_TOKENS  = 3000
ROUTER_MAX_TOKENS = 400
MAX_TOOL_ROUNDS   = 6
PANEL_MAX_WORKERS = 6
HISTORY_TURNS     = 6       # prior exchanges sent back to the lead reviewer

IMAGE_MAX_SIDE  = 1568
IMAGE_MAX_BYTES = 3_500_000
IMAGE_TYPES     = ["png", "jpg", "jpeg", "webp", "gif"]

RESET_PATTERN = re.compile(
    r"^\s*(start over|start fresh|reset|new (topic|question|conversation)|forget (that|everything)"
    r"|最初から(お願いします)?|リセット(して)?|新しい質問(です)?)\s*[.。!！]*\s*$",
    re.IGNORECASE,
)

CONCERN_STYLE = {
    "none":     ("No concern", "#3f8f6b"),
    "minor":    ("Minor",      "#b8902f"),
    "moderate": ("Moderate",   "#c7682c"),
    "serious":  ("Serious",    "#c23b3b"),
}

# ─────────────────────────────────────────────────────────────────────────────
# Page config (must be first Streamlit call)
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Content Ethics Advisor",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp, [data-testid="stAppViewContainer"] { background-color: #0d0f14; }
    [data-testid="stSidebar"]  { background-color: #11141b; border-right: 1px solid #1d2230; }
    [data-testid="stHeader"]   { background: transparent; }
    h1, h2, h3, h4 { color: #eef0f5 !important; }
    p, li, label, .stMarkdown { color: #c9ccd6; }

    .stButton > button {
        background-color: #3b5bdb; color: #fff; border: none; border-radius: 6px;
        font-weight: 600;
    }
    .stButton > button:hover { background-color: #3049b8; color: #fff; }

    .panel-card {
        border: 1px solid #232838; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px;
        background: #121621;
    }
    .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .panel-name { color: #eef0f5; font-weight: 600; }
    .chip {
        font-size: 0.75rem; font-weight: 600; padding: 2px 9px; border-radius: 999px; color: #fff;
        white-space: nowrap;
    }
    .panel-body { color: #c9ccd6; font-size: 0.9rem; margin-top: 6px; }
    .panel-body ul { margin: 4px 0 0 18px; padding: 0; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Auth — OTP via AWS SES, HMAC-signed URL token
# ─────────────────────────────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    key = st.secrets["COOKIE_SIGNING_KEY"].encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

def make_token(email: str) -> str:
    expiry = int(time.time()) + TOKEN_EXPIRY
    payload = f"{email}|{expiry}"
    raw = f"{payload}|{_sign(payload)}".encode()
    return base64.urlsafe_b64encode(raw).decode()

def verify_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        email, expiry, sig = raw.split("|")
        if not hmac.compare_digest(_sign(f"{email}|{expiry}"), sig):
            return None
        if int(expiry) < time.time():
            return None
        return email
    except Exception:
        return None

def send_otp_email(email: str, otp: str):
    import boto3
    ses = boto3.client(
        "ses",
        region_name=st.secrets["AWS_SES_REGION"],
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
    )
    ses.send_email(
        Source=st.secrets["EMAIL_FROM"],
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {"Data": "Your Content Ethics Advisor code"},
            "Body": {"Text": {"Data": f"Your one-time verification code is: {otp}\n\n"
                                      f"This code expires in {OTP_EXPIRY // 60} minutes."}},
        },
    )

def login_gate():
    """Blocks the rest of the app until a verified session/token exists."""
    query_token = st.query_params.get("t")
    if query_token:
        email = verify_token(query_token)
        if email:
            st.session_state["auth_email"] = email
            return

    if st.session_state.get("auth_email"):
        return

    st.markdown("## 🧭 Content Ethics Advisor")
    st.caption("Sign in with your Sega America email to continue.")

    step = st.session_state.get("auth_step", "email")

    if step == "email":
        with st.form("email_form"):
            email = st.text_input("Work email", placeholder="you@segaamerica.com")
            submitted = st.form_submit_button("Send code")
        if submitted:
            email = email.strip().lower()
            if not email.endswith(ALLOWED_DOMAIN):
                st.error(f"Please use an {ALLOWED_DOMAIN} address.")
            else:
                otp = f"{pysecrets.randbelow(1_000_000):06d}"
                try:
                    send_otp_email(email, otp)
                except Exception as e:
                    st.error(f"Couldn't send the code: {e}")
                    st.stop()
                st.session_state["pending_email"] = email
                st.session_state["pending_otp"] = otp
                st.session_state["otp_expires"] = time.time() + OTP_EXPIRY
                st.session_state["otp_attempts"] = 0
                st.session_state["auth_step"] = "otp"
                st.rerun()

    elif step == "otp":
        st.info(f"We sent a 6-digit code to **{st.session_state['pending_email']}**.")
        with st.form("otp_form"):
            code = st.text_input("Verification code", max_chars=6)
            c1, c2 = st.columns([1, 1])
            submitted = c1.form_submit_button("Verify")
            resend = c2.form_submit_button("Resend / use a different email")
        if resend:
            st.session_state["auth_step"] = "email"
            st.rerun()
        if submitted:
            if time.time() > st.session_state.get("otp_expires", 0):
                st.error("That code expired. Please request a new one.")
                st.session_state["auth_step"] = "email"
            elif st.session_state.get("otp_attempts", 0) >= MAX_ATTEMPTS:
                st.error("Too many attempts. Please request a new code.")
                st.session_state["auth_step"] = "email"
            elif hmac.compare_digest(code.strip(), st.session_state.get("pending_otp", "")):
                email = st.session_state["pending_email"]
                st.session_state["auth_email"] = email
                st.query_params["t"] = make_token(email)
                st.rerun()
            else:
                st.session_state["otp_attempts"] = st.session_state.get("otp_attempts", 0) + 1
                st.error("Incorrect code. Please try again.")

    st.stop()

login_gate()

# ─────────────────────────────────────────────────────────────────────────────
# Bedrock
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def get_bedrock_client():
    from anthropic import AnthropicBedrock
    return AnthropicBedrock(
        aws_region=st.secrets["AWS_BEDROCK_REGION"],
        aws_access_key=st.secrets["AWS_BEDROCK_ACCESS_KEY_ID"],
        aws_secret_key=st.secrets["AWS_BEDROCK_SECRET_ACCESS_KEY"],
    )

_CACHE_STATE = {"enabled": True}

def call_model(model, system, messages, max_tokens, tools=None, cache_system=False):
    """messages.create with optional prompt caching on the system prompt. If the model or
    region rejects cache_control, retry once without it and stop using it."""
    client = get_bedrock_client()
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    use_cache = cache_system and _CACHE_STATE["enabled"]
    kwargs["system"] = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                        if use_cache else system)
    try:
        return client.messages.create(**kwargs)
    except Exception as e:
        if use_cache and "cach" in str(e).lower():
            _CACHE_STATE["enabled"] = False
            kwargs["system"] = system
            return client.messages.create(**kwargs)
        raise

def response_text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

def parse_json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text[text.find("{"): text.rfind("}") + 1])

# ─────────────────────────────────────────────────────────────────────────────
# Case history — anonymized records only (see prepare_cases.py)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def load_case_store():
    """Load the anonymized case file. Checked in this order:
      1. CASES_S3_BUCKET set          -> private S3 object (optional)
      2. CASES_ENCRYPTION_KEY set     -> encrypted file in the repo (Streamlit Cloud)
      3. otherwise                    -> plain JSON at CASES_LOCAL_PATH (internal server)
    Returns (cases, index_text, redaction_terms) or raises."""
    here = Path(__file__).resolve().parent

    def _path(name, default):
        p = Path(_secret(name, default))
        return p if p.is_absolute() else here / p

    if _secret("CASES_S3_BUCKET"):
        import boto3
        s3 = boto3.client(
            "s3",
            region_name=_secret("CASES_S3_REGION") or st.secrets["AWS_BEDROCK_REGION"],
            aws_access_key_id=_secret("AWS_S3_ACCESS_KEY_ID") or st.secrets["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=_secret("AWS_S3_SECRET_ACCESS_KEY") or st.secrets["AWS_SECRET_ACCESS_KEY"],
        )
        raw = s3.get_object(Bucket=st.secrets["CASES_S3_BUCKET"],
                            Key=_secret("CASES_S3_KEY", "cases_sanitized.json"))["Body"].read()
    elif _secret("CASES_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet, InvalidToken
        enc = _path("CASES_ENCRYPTED_PATH", "cases_sanitized.enc").read_bytes()
        try:
            raw = Fernet(st.secrets["CASES_ENCRYPTION_KEY"].strip().encode()).decrypt(enc)
        except InvalidToken:
            raise RuntimeError("CASES_ENCRYPTION_KEY doesn't match cases_sanitized.enc — "
                               "check that the key and file come from the same prepare_cases.py run.")
    else:
        raw = _path("CASES_LOCAL_PATH", "cases_sanitized.json").read_bytes()

    data = json.loads(raw)
    cases = {c["id"]: c for c in data["cases"]}
    index_lines = [
        f"{c['id']} | {c.get('category', '')} | {'/'.join(c.get('regions') or []) or '-'} | "
        f"{c.get('verdict', '')} | {c.get('summary', '')}"
        for c in data["cases"]
    ]
    terms = sorted(set(data.get("redaction_terms", [])), key=len, reverse=True)
    return cases, "\n".join(index_lines), terms

SEARCH_FIELDS = ["category", "content_type", "issue", "reviewed_term", "reasoning",
                 "recommendation", "summary"]

def search_cases(cases: dict, query: str = "", category=None, region=None, verdict=None, limit=8):
    words = [w for w in re.findall(r"[\w'-]+", (query or "").lower()) if len(w) >= 3]
    phrase = (query or "").lower().strip()
    scored = []
    for c in cases.values():
        if category and category.lower() not in (c.get("category") or "").lower():
            continue
        if region and region not in (c.get("regions") or []):
            continue
        if verdict and verdict.lower() != (c.get("verdict") or "").lower():
            continue
        hay = " ".join(str(c.get(f) or "") for f in SEARCH_FIELDS).lower()
        score = sum(hay.count(w) for w in words)
        if phrase and len(phrase) > 4 and phrase in hay:
            score += 5
        if words and score == 0:
            continue
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    limit = max(1, min(int(limit or 8), 15))
    return {"total_matches": len(scored), "results": [c for _, c in scored[:limit]]}

CASE_TOOLS = [
    {
        "name": "search_cases",
        "description": (
            "Keyword search over the anonymized case history (records are in English). Returns full "
            "matching records. Use several searches with different keywords — the symbol or term, the "
            "theme, the content type — before concluding nothing comparable exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "English keywords, e.g. 'red cross icon' or 'rising sun flag costume'."},
                "category": {"type": "string", "description": "Optional substring filter on the category."},
                "region": {"type": "string", "enum": ["Asia", "West"], "description": "Optional region filter."},
                "verdict": {"type": "string", "description": "Optional exact verdict filter, e.g. 'Change required'."},
                "limit": {"type": "integer", "description": "Max results, default 8, max 15."},
            },
        },
    },
    {
        "name": "get_cases",
        "description": "Fetch full records by ID (from the case index), up to 10 at a time.",
        "input_schema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["ids"],
        },
    },
]

def run_case_tool(cases: dict, name: str, args: dict, seen_ids: list) -> dict:
    try:
        if name == "search_cases":
            out = search_cases(cases, **{k: v for k, v in args.items()
                                         if k in ("query", "category", "region", "verdict", "limit")})
            found = out["results"]
        elif name == "get_cases":
            found = [cases[i] for i in (args.get("ids") or [])[:10] if i in cases]
            out = {"results": found, "not_found": [i for i in args.get("ids", []) if i not in cases]}
        else:
            return {"error": f"Unknown tool {name}"}
        for c in found:
            if c["id"] not in seen_ids:
                seen_ids.append(c["id"])
        return out
    except Exception as e:
        return {"error": f"Tool failed: {e}"}

def redact(text: str, terms: list, user_text: str = "") -> str:
    """Last line of defense: mask any identifying term that slipped through, unless the
    user typed it themselves (e.g. their own title)."""
    if not text:
        return text
    for t in terms:
        if t in user_text:
            continue
        if re.fullmatch(r"[\x20-\x7e]+", t):
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", "[redacted]", text)
        else:
            text = text.replace(t, "[redacted]")
    return text

# ─────────────────────────────────────────────────────────────────────────────
# Images
# ─────────────────────────────────────────────────────────────────────────────

def prepare_image(uploaded) -> dict:
    """Downscale large uploads and return a Messages API image block."""
    from PIL import Image
    raw = uploaded.getvalue()
    img = Image.open(io.BytesIO(raw))
    if getattr(img, "is_animated", False):
        img.seek(0)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    if max(img.size) > IMAGE_MAX_SIDE or len(raw) > IMAGE_MAX_BYTES or img.format not in ("PNG", "JPEG", "WEBP", "GIF"):
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        if has_alpha:
            img.convert("RGBA").save(buf, format="PNG", optimize=True)
            media = "image/png"
        else:
            img.convert("RGB").save(buf, format="JPEG", quality=88)
            media = "image/jpeg"
        raw = buf.getvalue()
    else:
        media = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}[img.format]
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(raw).decode()}}

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline: lead draft -> router -> panel (parallel) -> synthesis
# ─────────────────────────────────────────────────────────────────────────────

def build_lead_messages(history: list, user_blocks: list) -> list:
    """Prior turns as plain text (earlier images are noted, not resent), then the new turn."""
    msgs = []
    for turn in history[-HISTORY_TURNS:]:
        user_text = turn["user_text"] or "(no text)"
        if turn.get("had_image"):
            user_text += "\n[The user attached an image with this message.]"
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": turn["answer"]})
    msgs.append({"role": "user", "content": user_blocks})
    return msgs

def run_lead_draft(cases, index_text, history, user_blocks):
    system = BASE_PROMPT + DRAFT_ADDENDUM + index_text
    messages = build_lead_messages(history, user_blocks)
    seen_ids = []
    for _ in range(MAX_TOOL_ROUNDS):
        resp = call_model(LEAD_MODEL_ID, system, messages, LEAD_MAX_TOKENS,
                          tools=CASE_TOOLS, cache_system=True)
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            return response_text(resp), seen_ids
        results = [{"type": "tool_result", "tool_use_id": tu.id,
                    "content": json.dumps(run_case_tool(cases, tu.name, tu.input, seen_ids),
                                          ensure_ascii=False)}
                   for tu in tool_uses]
        messages.append({"role": "user", "content": results})
    # Out of tool rounds: ask for the answer with what it has.
    messages.append({"role": "user", "content": "Please write your answer now using what you've found."})
    resp = call_model(LEAD_MODEL_ID, system, messages, LEAD_MAX_TOKENS, cache_system=True)
    return response_text(resp), seen_ids

def route_personas(question: str, draft: str, enabled: list) -> tuple:
    """Returns (selected_keys, reason). Counterweights are added afterwards by the caller."""
    candidates = [k for k in enabled if not PERSONAS[k]["counterweight"]]
    if not candidates:
        return [], "No perspective reviewers enabled."
    roster = "\n".join(f"- {k}: {PERSONAS[k]['description']}" for k in candidates)
    user = (f"Reviewers available:\n{roster}\n\nDeveloper's question:\n{question or '(image only)'}\n\n"
            f"Draft answer (for context):\n{draft[:2000]}")
    try:
        resp = call_model(ROUTER_MODEL_ID, ROUTER_PROMPT, [{"role": "user", "content": user}],
                          ROUTER_MAX_TOKENS)
        out = parse_json_object(response_text(resp))
        selected = [k for k in out.get("selected", []) if k in candidates]
        return selected, out.get("reason", "")
    except Exception:
        return candidates, "Relevance check failed, so every enabled reviewer was asked."

def run_panelist(key: str, question_blocks: list, context: str, draft: str, case_records: list) -> dict:
    p = PERSONAS[key]
    system = PANEL_FRAME + "\n\n" + p["brief"]
    precedent = json.dumps(case_records[:8], ensure_ascii=False) if case_records else "None found."
    content = list(question_blocks) + [{
        "type": "text",
        "text": (f"{context}"
                 f"Relevant anonymized precedent the lead advisor found:\n{precedent}\n\n"
                 f"Lead advisor's draft answer:\n{draft}\n\n"
                 "Give your review as the JSON object described."),
    }]
    try:
        resp = call_model(PANEL_MODEL_ID, system, [{"role": "user", "content": content}], PANEL_MAX_TOKENS)
        out = parse_json_object(response_text(resp))
        if out.get("concern_level") not in CONCERN_STYLE:
            out["concern_level"] = "minor"
        return {"key": key, **out}
    except Exception as e:
        return {"key": key, "error": f"{type(e).__name__}: {e}"}

def run_panel(keys, question_blocks, context, draft, case_records, on_done=None) -> list:
    results = []
    with ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as ex:
        futs = {ex.submit(run_panelist, k, question_blocks, context, draft, case_records): k for k in keys}
        for f in as_completed(futs):
            results.append(f.result())
            if on_done:
                on_done(futs[f])
    order = list(keys)
    return sorted(results, key=lambda r: order.index(r["key"]))

def needs_synthesis(panel: list) -> bool:
    return any(r.get("concern_level", "none") != "none" for r in panel if "error" not in r)

def run_synthesis(history, user_blocks, draft, panel, case_records) -> str:
    system = BASE_PROMPT + SYNTHESIS_ADDENDUM
    feedback = [{
        "reviewer": PERSONAS[r["key"]]["label"],
        **{k: v for k, v in r.items() if k not in ("key",)},
    } for r in panel if "error" not in r]
    note = {
        "type": "text",
        "text": ("Anonymized precedent you found while drafting:\n"
                 + json.dumps(case_records[:10], ensure_ascii=False)
                 + "\n\nYour draft answer:\n" + draft
                 + "\n\nPanel feedback:\n" + json.dumps(feedback, ensure_ascii=False, indent=1)
                 + "\n\nWrite the final answer to the developer's message above."),
    }
    messages = build_lead_messages(history, list(user_blocks) + [note])
    resp = call_model(LEAD_MODEL_ID, system, messages, LEAD_MAX_TOKENS)
    return response_text(resp) or draft

# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ─────────────────────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def render_panel(panel: list, router_reason: str, terms: list, user_text: str):
    if not panel:
        return
    with st.expander(f"Panel review · {len(panel)} reviewer(s)", expanded=False):
        if router_reason:
            st.caption(router_reason)
        for r in panel:
            p = PERSONAS[r["key"]]
            if "error" in r:
                st.markdown(f'<div class="panel-card"><div class="panel-name">{p["icon"]} {_esc(p["label"])}</div>'
                            f'<div class="panel-body">Review unavailable ({_esc(r["error"][:120])}).</div></div>',
                            unsafe_allow_html=True)
                continue
            label, color = CONCERN_STYLE[r["concern_level"]]
            if p["counterweight"]:
                label = {"none": "Well calibrated", "minor": "Slightly cautious",
                         "moderate": "Overcautious", "serious": "Far too cautious"}[r["concern_level"]]
            red = lambda s: _esc(redact(str(s or ""), terms, user_text))  # noqa: E731
            issues = "".join(f"<li>{red(i.get('element'))}: {red(i.get('perception'))}</li>"
                             for i in r.get("issues") or [] if isinstance(i, dict))
            changes = "".join(f"<li>{red(c)}</li>" for c in r.get("suggested_changes") or [])
            body = f"{red(r.get('headline'))}"
            if issues:
                body += f"<ul>{issues}</ul>"
            if changes:
                body += f"<div style='margin-top:6px'><b>Suggested changes</b><ul>{changes}</ul></div>"
            if r.get("on_the_draft"):
                body += f"<div style='margin-top:6px;color:#9aa0b1'>On the draft: {red(r['on_the_draft'])}</div>"
            st.markdown(
                f'<div class="panel-card"><div class="panel-head">'
                f'<span class="panel-name">{p["icon"]} {_esc(p["label"])}</span>'
                f'<span class="chip" style="background:{color}">{label}</span></div>'
                f'<div class="panel-body">{body}</div></div>',
                unsafe_allow_html=True,
            )

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

st.sidebar.markdown("### 🧭 Content Ethics Advisor")
st.sidebar.caption(f"Signed in as {st.session_state.get('auth_email', '')}")

if st.sidebar.button("New conversation", width="stretch"):
    st.session_state["history"] = []
    st.rerun()

st.sidebar.divider()
st.sidebar.markdown("#### Review panel")
panel_on = st.sidebar.toggle("Run perspective panel", value=True,
                             help="Reviewers critique the advisor's draft before you see the answer. "
                                  "Adds roughly 10–30 seconds.")
auto_select = st.sidebar.toggle("Only relevant reviewers", value=True, disabled=not panel_on,
                                help="A quick check picks which enabled reviewers apply to each question. "
                                     "Turn off to always ask every enabled reviewer.")
enabled = []
for key, p in PERSONAS.items():
    if st.sidebar.checkbox(f"{p['icon']} {p['label']}", value=p["default"], key=f"persona_{key}",
                           disabled=not panel_on, help=p["description"]):
        enabled.append(key)
show_panel = st.sidebar.toggle("Show panel notes under answers", value=True, disabled=not panel_on)

st.sidebar.divider()
st.sidebar.caption("Guidance only, not an approval. Confirm with the Expression Ethics Unit "
                   "before finalizing.")

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

try:
    CASES, INDEX_TEXT, REDACTION_TERMS = load_case_store()
except Exception as e:
    st.error(f"Couldn't load the case history: {type(e).__name__}: {e}")
    st.stop()

if "history" not in st.session_state:
    st.session_state["history"] = []
history = st.session_state["history"]

st.markdown("## Content Ethics Advisor")
st.caption("Describe the content you want checked — attach a screenshot if it helps. "
           "English or Japanese. 日本語でもご質問いただけます。")

for turn in history:
    with st.chat_message("user"):
        if turn.get("image_preview"):
            st.image(turn["image_preview"], width=320)
        if turn["user_text"]:
            st.markdown(turn["user_text"])
    with st.chat_message("assistant"):
        st.markdown(turn["answer"])
        if show_panel and panel_on:
            render_panel(turn.get("panel") or [], turn.get("router_reason", ""),
                         REDACTION_TERMS, turn["user_text"])

prompt = st.chat_input("Ask about a piece of content…", accept_file=True, file_type=IMAGE_TYPES)

if prompt:
    user_text = (prompt.text or "").strip()
    files = list(prompt.files or [])

    if not files and RESET_PATTERN.match(user_text):
        st.session_state["history"] = []
        st.toast("Started a new conversation.")
        st.rerun()

    user_blocks = []
    image_preview = None
    if files:
        try:
            user_blocks.append(prepare_image(files[0]))
            image_preview = files[0].getvalue()
        except Exception as e:
            st.error(f"Couldn't read that image: {e}")
            st.stop()
    user_blocks.append({"type": "text", "text": user_text or
                        "(The user attached an image without a message.)"})

    with st.chat_message("user"):
        if image_preview:
            st.image(image_preview, width=320)
        if user_text:
            st.markdown(user_text)

    with st.chat_message("assistant"):
        panel, router_reason = [], ""
        with st.status("Searching past cases…", expanded=False) as status:
            try:
                draft, case_ids = run_lead_draft(CASES, INDEX_TEXT, history, user_blocks)
            except Exception as e:
                status.update(label="Something went wrong", state="error")
                st.error(f"The advisor couldn't answer: {type(e).__name__}: {e}")
                st.stop()
            case_records = [CASES[i] for i in case_ids if i in CASES]
            answer = draft

            if panel_on and enabled:
                status.update(label="Choosing reviewers…")
                if auto_select:
                    keys, router_reason = route_personas(user_text, draft, enabled)
                else:
                    keys = [k for k in enabled if not PERSONAS[k]["counterweight"]]
                    router_reason = ""
                if keys:
                    keys += [k for k in enabled if PERSONAS[k]["counterweight"]]
                    done = []
                    status.update(label=f"Panel reviewing (0/{len(keys)})…")

                    def _progress(k):
                        done.append(k)
                        status.update(label=f"Panel reviewing ({len(done)}/{len(keys)})…")

                    prior = ""
                    if history:
                        prior = "Earlier in this conversation the developer asked:\n" + "\n".join(
                            f"- {t['user_text'][:300]}" for t in history[-3:]) + "\n\n"
                    panel = run_panel(keys, user_blocks, prior, draft, case_records, on_done=_progress)

                    if needs_synthesis(panel):
                        status.update(label="Finalizing the answer…")
                        try:
                            answer = run_synthesis(history, user_blocks, draft, panel, case_records)
                        except Exception:
                            answer = draft  # fall back to the draft rather than failing the turn
                elif auto_select:
                    router_reason = router_reason or "No perspective reviewers were relevant to this question."

            status.update(label="Done", state="complete")

        answer = redact(answer, REDACTION_TERMS, user_text)
        st.markdown(answer)
        if show_panel and panel_on:
            render_panel(panel, router_reason, REDACTION_TERMS, user_text)

    history.append({
        "user_text": user_text,
        "had_image": bool(image_preview),
        "image_preview": image_preview,
        "answer": answer,
        "panel": panel,
        "router_reason": router_reason,
    })
