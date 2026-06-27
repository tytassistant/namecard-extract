import base64
import json
import os
import re
import time
import urllib.parse
import uuid
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
import msal
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
RECORDS_DIR = BASE_DIR / "records"
STATIC_DIR = BASE_DIR / "static"
TOKENS_PATH = BASE_DIR / "helpers" / "tokens.json"

UPLOAD_DIR.mkdir(exist_ok=True)
RECORDS_DIR.mkdir(exist_ok=True)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

KNOWN_FIELDS = ["last_name", "first_name", "mobile", "direct_phone", "office_phone",
                "email", "company", "department", "title", "note"]

CONFIG_PATH = BASE_DIR / "helpers" / "namecard-extract-config.json"

load_dotenv(BASE_DIR / "helpers" / ".env")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/contacts"
GOOGLE_REDIRECT_URI = "http://localhost:8001/auth/google/callback"
MS_REDIRECT_URI = "http://localhost:8001/auth/ms/callback"
MS_SCOPES = ["Contacts.ReadWrite", "User.Read", "offline_access"]

# In-memory store for MSAL auth code flows (keyed by state param, short-lived)
_ms_auth_flows: dict = {}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {"google": {}, "ms": {}}
    with open(TOKENS_PATH) as f:
        return json.load(f)


def save_tokens(data: dict):
    with open(TOKENS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_valid_google_token() -> str:
    tokens = load_tokens()
    g = tokens.get("google", {})
    if not g.get("refresh_token"):
        raise HTTPException(status_code=401, detail="Google not connected. Visit /auth to set up.")
    if g.get("expires_at", 0) > time.time() + 300:
        return g["access_token"]
    resp = httpx.post(GOOGLE_TOKEN_URL, data={
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "refresh_token": g["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    g["access_token"] = result["access_token"]
    g["expires_at"] = time.time() + result.get("expires_in", 3600)
    tokens["google"] = g
    save_tokens(tokens)
    return g["access_token"]


def _get_msal_app(cache: Optional[msal.SerializableTokenCache] = None):
    if cache is None:
        cache = msal.SerializableTokenCache()
        ms_cache_data = load_tokens().get("ms", {}).get("cache")
        if ms_cache_data:
            cache.deserialize(ms_cache_data)
    return msal.PublicClientApplication(
        os.environ.get("MS_CLIENT_ID", ""),
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    ), cache


def get_valid_ms_token() -> str:
    app_instance, cache = _get_msal_app()
    accounts = app_instance.get_accounts()
    if not accounts:
        raise HTTPException(status_code=401, detail="Microsoft not connected. Visit /auth to set up.")
    result = app_instance.acquire_token_silent(scopes=MS_SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise HTTPException(status_code=401, detail="Microsoft token refresh failed. Visit /auth to reconnect.")
    if cache.has_state_changed:
        tokens = load_tokens()
        tokens.setdefault("ms", {})["cache"] = cache.serialize()
        save_tokens(tokens)
    return result["access_token"]


# ---------------------------------------------------------------------------
# Records helpers
# ---------------------------------------------------------------------------

def write_status(session_id: str, data: dict):
    path = RECORDS_DIR / f"status_{session_id}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def read_status(session_id: str) -> Optional[dict]:
    path = RECORDS_DIR / f"status_{session_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def read_records(session_id: str) -> Optional[dict]:
    path = RECORDS_DIR / f"records_{session_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def write_records(session_id: str, data: dict):
    path = RECORDS_DIR / f"records_{session_id}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are an expert at reading business name cards.

Analyze each image in this message carefully. Return a JSON array with exactly one element per image, in the same order as the images.
- If the image contains a business name card, return a JSON object with the extracted fields.
- If the image does NOT contain a name card (e.g. a photo, screenshot, or blank page), return null for that position.

## Output fields for name card objects (use exactly these keys)
- "last_name": family name / surname
- "first_name": given name including middle name if present
- "mobile": mobile / cell phone — look for labels M, C, Mobile, Cell, Mo
- "direct_phone": direct line — look for labels D, Dir, Direct
- "office_phone": main office number — look for labels O, Office, T, Tel
- "email": email address
- "company": company or organisation name
- "department": department and/or division
- "title": job title
- "note": if the person's name or company name on the card appears in a non-Latin script (Chinese, Japanese, Korean, etc.), put the original non-English value(s) here, separated by " / " if both are present (e.g. "田中太郎 / ABC株式会社"); otherwise leave this field out entirely

## Phone number formatting rules
1. Strip all spaces, hyphens (-), dots (.), parentheses ( ) from every phone number.
2. Prepend the country code (e.g. +852, +81, +1) with no space. If the country code is printed on the card, use it.
3. If no country code is on the card, infer it from the country in the address. If no address is present, leave the number as-is without a country code.
4. Example: '2123 4567' on a Hong Kong card → '+85221234567'

## General rules
- The array must have exactly the same number of elements as images sent — one per image, no more, no less.
- Use null (not an empty object) for any image that is not a name card.
- Omit any field that is not present or not readable on the card.
- Do not translate any text.
- Return ONLY a valid JSON array. No explanations, no markdown code blocks.

## Example (4 images, 3rd is not a name card)
[
  {"last_name":"Smith","first_name":"John","mobile":"+85298765432","office_phone":"+85221234567","email":"john@acme.com","company":"Acme Corp","title":"Sales Director"},
  {"last_name":"Tanaka","first_name":"Taro","office_phone":"+81312345678","email":"tanaka@abc.co.jp","company":"ABC株式会社","title":"部長","note":"田中太郎 / ABC株式会社"},
  null,
  {"last_name":"Lee","first_name":"David","mobile":"+85291234567","email":"david@xyz.com","company":"XYZ Ltd","title":"Manager"}
]"""


def to_name_case(s: str) -> str:
    if not s:
        return s
    return " ".join(w.capitalize() for w in s.split(" "))


def extract_cards(session_id: str, poe_api_key: str, model: str, extra_instruction: str = ""):
    upload_subdir = UPLOAD_DIR / session_id
    image_files = sorted(upload_subdir.iterdir(), key=lambda p: p.name)

    data_urls = []
    for img_path in image_files:
        if img_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            continue
        with open(img_path, "rb") as f:
            raw = f.read()
        mime = "image/jpeg" if img_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        data_urls.append(f"data:{mime};base64,{base64.b64encode(raw).decode()}")

    prompt_text = EXTRACTION_PROMPT
    if extra_instruction:
        prompt_text += f"\n\n=== EXTRA INSTRUCTION ===\n{extra_instruction}"

    message_content = [{"type": "text", "text": prompt_text}]
    for url in data_urls:
        message_content.append({"type": "image_url", "image_url": {"url": url}})

    try:
        resp = httpx.post(
            "https://api.poe.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {poe_api_key}", "Content-Type": "application/json"},
            json={"model": model, "stream": False, "messages": [{"role": "user", "content": message_content}]},
            timeout=300,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        write_status(session_id, {**read_status(session_id), "phase": "error", "error": str(exc)})
        return

    json_str = content.strip()
    m = re.search(r'\[[\s\S]*\]', json_str)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group())
        except Exception:
            parsed = None

    if not parsed or not isinstance(parsed, list):
        write_status(session_id, {**read_status(session_id), "phase": "error", "error": "Empty or invalid JSON from LLM"})
        return

    records = []
    for idx, entry in enumerate(parsed):
        if not entry or not isinstance(entry, dict):
            continue
        record = {"id": f"C{idx}_{session_id}"}
        img_path = image_files[idx] if idx < len(image_files) else None
        record["filename"] = img_path.name if img_path else ""
        record["file_path"] = str(img_path.relative_to(BASE_DIR)) if img_path else ""
        if entry.get("last_name"):
            entry["last_name"] = to_name_case(entry["last_name"])
        if entry.get("first_name"):
            entry["first_name"] = to_name_case(entry["first_name"])
        for field in KNOWN_FIELDS:
            record[field] = entry.get(field) or None
        record["synced_ms"] = False
        record["synced_google"] = False
        records.append(record)

    write_records(session_id, {
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total_uploaded": len(data_urls),
        "records": records,
    })
    write_status(session_id, {
        "phase": "complete",
        "total": len(data_urls),
        "synced_ms": False,
        "synced_google": False,
        "error": None,
    })


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/")
def home_page(request: Request):
    return templates.TemplateResponse(request, "auth.html")


@app.get("/auth")
def auth_page(request: Request):
    return templates.TemplateResponse(request, "auth.html")


@app.get("/capture")
def capture_page(request: Request):
    config = load_config()
    return templates.TemplateResponse(request, "upload.html", context={"config": config})


@app.get("/results")
def results_page(request: Request, session: str = Query(...)):
    return templates.TemplateResponse(request, "results.html", context={"session_id": session})


@app.get("/summary")
def summary_page(request: Request):
    return templates.TemplateResponse(request, "summary.html")


# ---------------------------------------------------------------------------
# OAuth: Google
# ---------------------------------------------------------------------------

@app.get("/auth/google/start")
def auth_google_start():
    if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET"):
        raise HTTPException(400, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in helpers/.env first.")
    params = {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.get("/auth/google/callback")
def auth_google_callback(code: str = Query(None), error: str = Query(None)):
    if error:
        return RedirectResponse(f"/auth?error={urllib.parse.quote(error)}")
    if not code:
        return RedirectResponse("/auth?error=no_code")
    try:
        resp = httpx.post(GOOGLE_TOKEN_URL, data={
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "code": code,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        return RedirectResponse(f"/auth?error={urllib.parse.quote(str(exc))}")
    tokens = load_tokens()
    tokens["google"] = {
        "access_token": result.get("access_token", ""),
        "refresh_token": result.get("refresh_token", ""),
        "expires_at": time.time() + result.get("expires_in", 3600),
    }
    save_tokens(tokens)
    return RedirectResponse("/auth?google=ok")


# ---------------------------------------------------------------------------
# OAuth: Microsoft
# ---------------------------------------------------------------------------

@app.get("/auth/ms/start")
def auth_ms_start():
    if not os.environ.get("MS_CLIENT_ID"):
        raise HTTPException(400, "Set MS_CLIENT_ID in helpers/.env first.")
    cache = msal.SerializableTokenCache()
    app_instance = msal.PublicClientApplication(
        os.environ.get("MS_CLIENT_ID", ""),
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    flow = app_instance.initiate_auth_code_flow(scopes=MS_SCOPES, redirect_uri=MS_REDIRECT_URI)
    _ms_auth_flows[flow["state"]] = (flow, cache)
    return RedirectResponse(flow["auth_uri"])


@app.get("/auth/ms/callback")
def auth_ms_callback(request: Request, state: str = Query(None), error: str = Query(None)):
    if error:
        return RedirectResponse(f"/auth?error={urllib.parse.quote(error)}")
    if not state or state not in _ms_auth_flows:
        return RedirectResponse("/auth?error=invalid_state")
    flow, cache = _ms_auth_flows.pop(state)
    app_instance = msal.PublicClientApplication(
        os.environ.get("MS_CLIENT_ID", ""),
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    result = app_instance.acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    if "error" in result:
        msg = urllib.parse.quote(result.get("error_description", result["error"]))
        return RedirectResponse(f"/auth?error={msg}")
    tokens = load_tokens()
    tokens["ms"] = {"cache": cache.serialize()}
    save_tokens(tokens)
    return RedirectResponse("/auth?ms=ok")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_config():
    return load_config()


@app.get("/api/auth_status")
def api_auth_status():
    tokens = load_tokens()
    google_ok = bool(tokens.get("google", {}).get("refresh_token"))
    ms_ok = False
    try:
        app_instance, _ = _get_msal_app()
        ms_ok = bool(app_instance.get_accounts())
    except Exception:
        ms_ok = False
    poe_ok = bool(os.environ.get("POE_API_KEY", ""))
    return {"google": google_ok, "ms": ms_ok, "poe_key": poe_ok}


@app.get("/api/google_labels")
def api_google_labels():
    token = get_valid_google_token()
    resp = httpx.get(
        "https://people.googleapis.com/v1/contactGroups?pageSize=50",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return [
        {"id": g["resourceName"], "name": g["name"]}
        for g in resp.json().get("contactGroups", [])
        if g.get("groupType") == "USER_CONTACT_GROUP"
    ]


@app.get("/api/ms_categories")
def api_ms_categories():
    token = get_valid_ms_token()
    resp = httpx.get(
        "https://graph.microsoft.com/v1.0/me/outlook/masterCategories",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return [{"id": c["displayName"], "name": c["displayName"]} for c in resp.json().get("value", [])]


@app.post("/api/sync_contact")
def api_sync_contact(payload: dict = Body(...)):
    session_id = payload.get("session_id")
    record_id = payload.get("record_id")
    platform = payload.get("platform")
    if platform not in ("ms", "google"):
        raise HTTPException(400, "platform must be 'ms' or 'google'")
    data = read_records(session_id)
    if not data:
        raise HTTPException(404, "Session not found")
    record = next((r for r in data["records"] if r["id"] == record_id), None)
    if not record:
        raise HTTPException(404, "Record not found")

    if platform == "ms":
        token = get_valid_ms_token()
        body: dict = {}
        if record.get("last_name"):   body["surname"] = record["last_name"]
        if record.get("first_name"):  body["givenName"] = record["first_name"]
        if record.get("email"):
            body["emailAddresses"] = [{"address": record["email"], "name": f"{record.get('first_name', '')} {record.get('last_name', '')}".strip()}]
        if record.get("mobile"):      body["mobilePhone"] = record["mobile"]
        biz = [p for p in [record.get("office_phone"), record.get("direct_phone")] if p]
        if biz:                       body["businessPhones"] = biz
        if record.get("company"):     body["companyName"] = record["company"]
        if record.get("department"):  body["department"] = record["department"]
        if record.get("title"):       body["jobTitle"] = record["title"]
        if record.get("note"):        body["personalNotes"] = record["note"]
        if payload.get("categories"): body["categories"] = payload["categories"]

        resp = httpx.post(
            "https://graph.microsoft.com/v1.0/me/contacts",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        if not resp.is_success:
            raise HTTPException(resp.status_code, f"MS Graph: {resp.text}")
        contact_id = resp.json().get("id")

        # Upload photo if available
        if contact_id and record.get("file_path"):
            img_path = BASE_DIR / record["file_path"]
            if img_path.exists():
                try:
                    httpx.put(
                        f"https://graph.microsoft.com/v1.0/me/contacts/{contact_id}/photo/$value",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                        content=img_path.read_bytes(), timeout=30,
                    )
                except Exception:
                    pass

    elif platform == "google":
        token = get_valid_google_token()
        names: dict = {}
        if record.get("first_name"): names["givenName"] = record["first_name"]
        if record.get("last_name"):  names["familyName"] = record["last_name"]
        body = {}
        if names: body["names"] = [names]
        phones = []
        if record.get("mobile"):       phones.append({"value": record["mobile"], "type": "mobile"})
        if record.get("office_phone"): phones.append({"value": record["office_phone"], "type": "work"})
        if record.get("direct_phone"): phones.append({"value": record["direct_phone"], "type": "work"})
        if phones: body["phoneNumbers"] = phones
        if record.get("email"): body["emailAddresses"] = [{"value": record["email"]}]
        org: dict = {}
        if record.get("company"):    org["name"] = record["company"]
        if record.get("department"): org["department"] = record["department"]
        if record.get("title"):      org["title"] = record["title"]
        if org: body["organizations"] = [org]
        if record.get("note"): body["biographies"] = [{"value": record["note"], "contentType": "TEXT_PLAIN"}]
        if payload.get("group_resource_names"):
            body["memberships"] = [
                {"contactGroupMembership": {"contactGroupResourceName": rn}}
                for rn in payload["group_resource_names"]
            ]

        resp = httpx.post(
            "https://people.googleapis.com/v1/people:createContact",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=30,
        )
        if not resp.is_success:
            raise HTTPException(resp.status_code, f"Google People API: {resp.text}")
        resource_name = resp.json().get("resourceName")

        # Upload photo if available
        if resource_name and record.get("file_path"):
            img_path = BASE_DIR / record["file_path"]
            if img_path.exists():
                try:
                    photo_b64 = base64.b64encode(img_path.read_bytes()).decode()
                    httpx.patch(
                        f"https://people.googleapis.com/v1/{resource_name}:updateContactPhoto",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json={"photoBytes": photo_b64}, timeout=30,
                    )
                except Exception:
                    pass

    flag = "synced_ms" if platform == "ms" else "synced_google"
    record[flag] = True
    write_records(session_id, data)
    status = read_status(session_id) or {}
    if not status.get(flag):
        status[flag] = True
        write_status(session_id, status)
    return {"ok": True}


@app.post("/upload")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    model: str = Form(...),
    extra_instruction: str = Form(""),
):
    poe_api_key = os.environ.get("POE_API_KEY", "")
    if not poe_api_key:
        raise HTTPException(status_code=500, detail="POE_API_KEY not configured on server. Add it to helpers/.env and restart.")
    session_id = str(uuid.uuid4())[:8]
    upload_subdir = UPLOAD_DIR / session_id
    upload_subdir.mkdir(parents=True, exist_ok=True)

    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    saved = 0
    for file in files:
        suffix = Path(file.filename).suffix.lower() if file.filename else ".jpg"
        if suffix not in allowed_exts:
            continue
        dest = upload_subdir / f"card_{session_id}_{saved}{suffix}"
        with open(dest, "wb") as f:
            f.write(await file.read())
        saved += 1

    if saved == 0:
        raise HTTPException(status_code=400, detail="No valid image files uploaded.")

    write_status(session_id, {
        "phase": "extracting",
        "total": saved,
        "synced_ms": False,
        "synced_google": False,
        "error": None,
    })
    background_tasks.add_task(extract_cards, session_id, poe_api_key, model, extra_instruction)
    return {"session_id": session_id}


@app.get("/api/progress")
def api_progress(session: str = Query(...)):
    status = read_status(session)
    if not status:
        raise HTTPException(status_code=404, detail="Session not found.")
    return status


@app.get("/api/get_results")
def api_get_results(session: str = Query(...)):
    data = read_records(session)
    if not data:
        raise HTTPException(status_code=404, detail="No records found for this session.")
    return data


@app.get("/api/list_sessions")
def api_list_sessions():
    sessions = []
    for status_file in RECORDS_DIR.glob("status_*.json"):
        session_id = status_file.stem.replace("status_", "")
        with open(status_file) as f:
            status = json.load(f)
        if status.get("phase") != "complete":
            continue
        records_file = RECORDS_DIR / f"records_{session_id}.json"
        total_extracted = 0
        timestamp = ""
        if records_file.exists():
            with open(records_file) as f:
                rec_data = json.load(f)
            total_extracted = len(rec_data.get("records", []))
            timestamp = rec_data.get("timestamp", "")
        sessions.append({
            "session_id": session_id,
            "timestamp": timestamp,
            "total_uploaded": status.get("total", 0),
            "total_extracted": total_extracted,
            "synced_ms": status.get("synced_ms", False),
            "synced_google": status.get("synced_google", False),
        })
    sessions.sort(key=lambda s: s["timestamp"], reverse=True)
    return sessions


@app.post("/api/update_record")
def api_update_record(payload: dict = Body(...)):
    session_id = payload.get("session_id")
    record_id = payload.get("id")
    if not session_id or not record_id:
        raise HTTPException(status_code=400, detail="session_id and id required.")
    data = read_records(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found.")
    for record in data["records"]:
        if record["id"] == record_id:
            for field in KNOWN_FIELDS:
                if field in payload:
                    record[field] = payload[field] or None
            break
    else:
        raise HTTPException(status_code=404, detail="Record not found.")
    write_records(session_id, data)
    return {"ok": True}


@app.post("/api/mark_synced")
def api_mark_synced(payload: dict = Body(...)):
    session_id = payload.get("session_id")
    record_id = payload.get("id")
    platform = payload.get("platform")
    if platform not in ("ms", "google"):
        raise HTTPException(status_code=400, detail="platform must be 'ms' or 'google'.")
    data = read_records(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found.")
    flag = "synced_ms" if platform == "ms" else "synced_google"
    for record in data["records"]:
        if record["id"] == record_id:
            record[flag] = True
            break
    else:
        raise HTTPException(status_code=404, detail="Record not found.")
    write_records(session_id, data)
    status = read_status(session_id) or {}
    if not status.get(flag):
        status[flag] = True
        write_status(session_id, status)
    return {"ok": True}


@app.post("/api/delete_session")
def api_delete_session(payload: dict = Body(...)):
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required.")
    upload_subdir = UPLOAD_DIR / session_id
    if upload_subdir.exists():
        shutil.rmtree(upload_subdir)
    for fname in [f"records_{session_id}.json", f"status_{session_id}.json"]:
        p = RECORDS_DIR / fname
        if p.exists():
            p.unlink()
    return {"ok": True}
