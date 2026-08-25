import base64
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv
from flask import (
    Flask, jsonify, redirect, render_template, request,
    session, send_file
)
from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google import genai

import io
from PIL import Image

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET", "dev-only-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///local.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
FERNET_KEY = os.environ.get("FERNET_KEY", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
MAX_ASSET_BYTES = int(os.environ.get("MAX_ASSET_BYTES", "12000000"))
AI_VARIATIONS = max(1, min(3, int(os.environ.get("AI_VARIATIONS", "1"))))

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.readonly",
]

PINTEREST_SCOPES = [
    "boards:read",
    "pins:read",
    "pins:write",
    "user_accounts:read",
]


def now():
    return datetime.now(timezone.utc)


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def fernet():
    if not FERNET_KEY:
        raise RuntimeError("FERNET_KEY is not configured")
    return Fernet(FERNET_KEY.encode())


def encrypt(value):
    if not value:
        return None
    return fernet().encrypt(value.encode()).decode()


def decrypt(value):
    if not value:
        return None
    return fernet().decrypt(value.encode()).decode()


class User(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False)
    name = db.Column(db.String(200))
    google_token = db.Column(db.Text)
    pinterest_access_token = db.Column(db.Text)
    pinterest_refresh_token = db.Column(db.Text)
    pinterest_expires_at = db.Column(db.DateTime(timezone=True))
    pinterest_refresh_expires_at = db.Column(db.DateTime(timezone=True))
    pinterest_scopes = db.Column(db.Text)
    drive_folder_id = db.Column(db.String(255))
    drive_folder_name = db.Column(db.String(500))
    board_id = db.Column(db.String(255))
    board_name = db.Column(db.String(500))
    paused = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now)
    last_processed_at = db.Column(db.DateTime(timezone=True))

    jobs = db.relationship("Job", backref="user", lazy=True)


class Job(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("user.id"), nullable=False)
    drive_file_id = db.Column(db.String(255), nullable=False)
    drive_file_name = db.Column(db.String(500))
    mime_type = db.Column(db.String(120))
    status = db.Column(db.String(40), default="queued")
    error = db.Column(db.Text)
    prompt = db.Column(db.Text)
    generated_image = db.Column(db.LargeBinary)
    generated_mime_type = db.Column(db.String(100))
    title = db.Column(db.String(100))
    description = db.Column(db.Text)
    hashtags = db.Column(db.Text)
    pinterest_pin_id = db.Column(db.String(255))
    pinterest_response = db.Column(db.Text)
    attempts = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime(timezone=True), default=now)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now)
    published_at = db.Column(db.DateTime(timezone=True))


with app.app_context():
    db.create_all()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def oauth_client_config():
    return {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [os.environ.get(
                "GOOGLE_REDIRECT_URI",
                f"{BASE_URL}/oauth/google/callback"
            )],
        }
    }


def make_google_flow():
    flow = Flow.from_client_config(
        oauth_client_config(),
        scopes=GOOGLE_SCOPES,
        redirect_uri=os.environ.get(
            "GOOGLE_REDIRECT_URI",
            f"{BASE_URL}/oauth/google/callback"
        ),
    )
    return flow


def drive_credentials(user):
    if not user.google_token:
        raise RuntimeError("Google Drive is not connected")
    data = json.loads(decrypt(user.google_token))
    creds = Credentials.from_authorized_user_info(data, GOOGLE_SCOPES)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        user.google_token = encrypt(creds.to_json())
        db.session.commit()
    return creds


def parse_drive_folder_id(value):
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    parsed = urlparse(value)
    parts = [p for p in parsed.path.split("/") if p]
    if "folders" in parts:
        idx = parts.index("folders")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    qs = parse_qs(parsed.query)
    if "id" in qs and qs["id"]:
        return qs["id"][0]
    return None


def pinterest_basic_auth():
    cid = os.environ.get("PINTEREST_CLIENT_ID", "")
    secret = os.environ.get("PINTEREST_CLIENT_SECRET", "")
    raw = f"{cid}:{secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def pinterest_token_request(data):
    response = requests.post(
        "https://api.pinterest.com/v5/oauth/token",
        headers={
            "Authorization": pinterest_basic_auth(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Pinterest OAuth failed ({response.status_code}): "
            f"{response.text[:2000]}"
        )
    return response.json()


def pinterest_access_token(user):
    if not user.pinterest_access_token:
        raise RuntimeError("Pinterest is not connected")

    expires = user.pinterest_expires_at
    if expires and expires > now() + timedelta(minutes=5):
        return decrypt(user.pinterest_access_token)

    refresh = decrypt(user.pinterest_refresh_token)
    if not refresh:
        raise RuntimeError("Pinterest access token expired and no refresh token is stored")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    token = pinterest_token_request(data)
    user.pinterest_access_token = encrypt(token["access_token"])
    if token.get("refresh_token"):
        user.pinterest_refresh_token = encrypt(token["refresh_token"])
    user.pinterest_expires_at = now() + timedelta(seconds=int(token.get("expires_in", 2592000)))
    if token.get("refresh_token_expires_in"):
        user.pinterest_refresh_expires_at = now() + timedelta(
            seconds=int(token["refresh_token_expires_in"])
        )
    db.session.commit()
    return token["access_token"]


def pinterest_request(user, method, path, **kwargs):
    token = pinterest_access_token(user)
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json")

    response = requests.request(
        method,
        f"https://api.pinterest.com/v5{path}",
        headers=headers,
        timeout=45,
        **kwargs,
    )

    if response.status_code == 401:
        # One refresh/retry is safe.
        user.pinterest_expires_at = now() - timedelta(seconds=1)
        db.session.commit()
        token = pinterest_access_token(user)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.request(
            method,
            f"https://api.pinterest.com/v5{path}",
            headers=headers,
            timeout=45,
            **kwargs,
        )

    if not response.ok:
        body = response.text[:10000]
        raise PinterestAPIError(response.status_code, body)

    return response


class PinterestAPIError(Exception):
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Pinterest API {status_code}: {body}")


def list_pinterest_boards(user):
    response = pinterest_request(user, "GET", "/boards", params={"page_size": 100})
    return response.json().get("items", [])


def choose_board(user, boards):
    if not boards:
        raise RuntimeError("No Pinterest boards found. Create a board first.")
    if user.board_id:
        for b in boards:
            if b.get("id") == user.board_id:
                return b
    preferred = os.environ.get("DEFAULT_BOARD_NAME", "").strip().lower()
    if preferred:
        for b in boards:
            if (b.get("name") or "").lower() == preferred:
                return b
    return boards[0]


def create_pinterest_pin(user, job, image_bytes):
    board_id = user.board_id
    if not board_id:
        boards = list_pinterest_boards(user)
        board = choose_board(user, boards)
        board_id = board["id"]
        user.board_id = board_id
        user.board_name = board.get("name")
        db.session.commit()

    payload = {
        "board_id": board_id,
        "title": (job.title or "")[:100],
        "description": (job.description or "")[:800],
        "alt_text": (job.title or "AI generated design")[:500],
        "media_source": {
            "source_type": "image_base64",
            "content_type": job.generated_mime_type or "image/png",
            "data": base64.b64encode(image_bytes).decode(),
        },
    }

    response = pinterest_request(user, "POST", "/pins", json=payload)
    result = response.json()
    return result


def gemini_client():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return genai.Client(api_key=key)


def gemini_generate_image(image_bytes, mime_type, prompt):
    client = gemini_client()
    model = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

    interaction = client.interactions.create(
        model=model,
        input=[
            {"type": "text", "text": prompt},
            {"type": "image", "mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()},
        ],
        response_format={
            "type": "image",
            "mime_type": "image/png",
            "aspect_ratio": "2:3",
            "image_size": "2K",
        },
    )

    if not interaction.output_image:
        raise RuntimeError("Gemini returned no generated image")
    return base64.b64decode(interaction.output_image.data), "image/png"


def gemini_seo(analysis_prompt):
    client = gemini_client()
    model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3-flash-preview")

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Pinterest title, maximum 100 characters."},
            "description": {"type": "string", "description": "Pinterest description, maximum 800 characters, natural CTA."},
            "hashtags": {"type": "array", "items": {"type": "string"}, "description": "5 to 10 relevant hashtags."},
            "keywords": {"type": "array", "items": {"type": "string"}, "description": "10 to 20 searchable keywords."}
        },
        "required": ["title", "description", "hashtags", "keywords"],
    }

    interaction = client.interactions.create(
        model=model,
        input=analysis_prompt,
        generation_config={"thinking_level": "low"},
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": schema,
        },
    )
    raw = interaction.output_text
    data = json.loads(raw)
    data["title"] = str(data["title"])[:100]
    data["description"] = str(data["description"])[:800]
    data["hashtags"] = [
        h if str(h).startswith("#") else "#" + str(h).replace(" ", "")
        for h in data.get("hashtags", [])
    ][:10]
    return data


def build_creative_prompt(file_name):
    return f"""
You are a senior Pinterest creative director for an Etsy/design business.

The source asset is a sketch, blueprint, line drawing, pattern, craft design, or architectural concept.
Filename: {file_name}

Transform the supplied source into a polished Pinterest-friendly visual while preserving the important structure and intent of the original design.

Requirements:
- Keep the recognizable design/content from the reference.
- Improve clarity, composition, lighting, contrast and visual appeal.
- Add tasteful color/material/rendering where appropriate.
- Make it premium and commercially presentable.
- Vertical Pinterest composition, 2:3.
- Clean background and strong focal point.
- Do not add logos, fake brand names, watermarks, or misleading claims.
- Do not invent technical measurements that are not present.
- If it is an architectural blueprint, create a tasteful architectural presentation/render while retaining the plan's identity.
- No excessive text in the image.
- The final result should look like an original creative presentation based on the supplied reference.
"""


def build_seo_prompt(file_name, creative_context):
    return f"""
Generate Pinterest SEO metadata for this design asset.

Filename: {file_name}
Creative context:
{creative_context}

The audience is interested in Etsy products, architecture, home plans, sketch ideas,
blueprints, printable designs, DIY ideas, art inspiration, and related design searches.

Rules:
- Title <= 100 characters.
- Description <= 800 characters.
- Natural human language, not keyword stuffing.
- Include useful search phrases naturally.
- Include a clear but non-spammy CTA.
- Hashtags must be relevant, not random or fabricated "trending" claims.
- Do not claim a hashtag is trending unless verified.
- Return JSON only.
"""


def fetch_drive_images(user):
    creds = drive_credentials(user)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    q = (
        f"'{user.drive_folder_id}' in parents and "
        "trashed = false and mimeType contains 'image/'"
    )
    response = service.files().list(
        q=q,
        pageSize=100,
        fields="files(id,name,mimeType,size,modifiedTime,webViewLink)",
        orderBy="modifiedTime desc",
    ).execute()
    return response.get("files", [])


def download_drive_file(user, file_id):
    creds = drive_credentials(user)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    request_obj = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj)
    done = False
    while not done:
        _, done = downloader.next_chunk()
        if fh.tell() > MAX_ASSET_BYTES:
            raise RuntimeError("Asset exceeds MAX_ASSET_BYTES")
    return fh.getvalue()


def get_or_create_job(user, file_info):
    job = Job.query.filter_by(
        user_id=user.id,
        drive_file_id=file_info["id"]
    ).first()
    if job:
        return job, False

    job = Job(
        id=secrets.token_urlsafe(18),
        user_id=user.id,
        drive_file_id=file_info["id"],
        drive_file_name=file_info.get("name"),
        mime_type=file_info.get("mimeType"),
        status="queued",
    )
    db.session.add(job)
    db.session.commit()
    return job, True


def process_job(user, job):
    job.attempts += 1
    job.status = "downloading"
    job.error = None
    db.session.commit()

    try:
        source = download_drive_file(user, job.drive_file_id)

        # Normalize malformed image files early.
        try:
            with Image.open(io.BytesIO(source)) as im:
                im.verify()
        except Exception as exc:
            raise RuntimeError(f"Drive asset is not a valid image: {exc}")

        job.status = "generating"
        job.prompt = build_creative_prompt(job.drive_file_name or "design")
        db.session.commit()

        generated, generated_mime = gemini_generate_image(
            source,
            job.mime_type or "image/png",
            job.prompt
        )
        job.generated_image = generated
        job.generated_mime_type = generated_mime

        job.status = "seo"
        db.session.commit()

        seo = gemini_seo(
            build_seo_prompt(
                job.drive_file_name or "design",
                "A polished Pinterest presentation generated from the supplied sketch/blueprint."
            )
        )
        job.title = seo["title"]
        job.description = seo["description"]
        job.hashtags = json.dumps(seo.get("hashtags", []))
        db.session.commit()

        job.status = "publishing"
        db.session.commit()

        result = create_pinterest_pin(user, job, generated)
        job.pinterest_pin_id = result.get("id")
        job.pinterest_response = json.dumps(result)
        job.status = "published"
        job.published_at = now()
        db.session.commit()

        return result

    except PinterestAPIError as exc:
        job.status = "failed"
        job.error = f"Pinterest API {exc.status_code}: {exc.body}"
        job.pinterest_response = exc.body
        db.session.commit()
        raise
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:10000]
        db.session.commit()
        raise


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": utc_iso(now())})


@app.route("/oauth/google")
def oauth_google():
    flow = make_google_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    session["google_oauth_state"] = state
    return redirect(authorization_url)


@app.route("/oauth/google/callback")
def oauth_google_callback():
    flow = make_google_flow()
    flow.state = session.get("google_oauth_state")
    if not flow.state:
        return "OAuth state missing", 400
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = service.userinfo().get().execute()
        email = info["email"]
        uid = hashlib.sha256(email.lower().encode()).hexdigest()[:32]
        user = db.session.get(User, uid)
        if not user:
            user = User(id=uid, email=email, name=info.get("name"))
            db.session.add(user)
        user.google_token = encrypt(creds.to_json())
        user.name = info.get("name") or user.name
        db.session.commit()
        session["user_id"] = user.id
        return redirect("/")
    except Exception as exc:
        return f"Google authentication failed: {exc}", 400


@app.route("/oauth/pinterest")
@require_user
def oauth_pinterest(user):
    state = secrets.token_urlsafe(32)
    session["pinterest_oauth_state"] = state
    redirect_uri = os.environ.get(
        "PINTEREST_REDIRECT_URI",
        f"{BASE_URL}/oauth/pinterest/callback"
    )
    params = {
        "client_id": os.environ.get("PINTEREST_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(PINTEREST_SCOPES),
        "state": state,
    }
    return redirect("https://www.pinterest.com/oauth/?" + urlencode(params))


@app.route("/oauth/pinterest/callback")
def oauth_pinterest_callback():
    state = request.args.get("state")
    if not state or not secrets.compare_digest(
        state, session.get("pinterest_oauth_state", "")
    ):
        return "Invalid Pinterest OAuth state", 400

    user = current_user()
    if not user:
        return redirect("/?error=login_required")

    code = request.args.get("code")
    if not code:
        return f"Pinterest authorization failed: {request.args.get('error', 'unknown error')}", 400

    redirect_uri = os.environ.get(
        "PINTEREST_REDIRECT_URI",
        f"{BASE_URL}/oauth/pinterest/callback"
    )
    try:
        token = pinterest_token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })
        user.pinterest_access_token = encrypt(token["access_token"])
        if token.get("refresh_token"):
            user.pinterest_refresh_token = encrypt(token["refresh_token"])
        user.pinterest_expires_at = now() + timedelta(
            seconds=int(token.get("expires_in", 2592000))
        )
        if token.get("refresh_token_expires_in"):
            user.pinterest_refresh_expires_at = now() + timedelta(
                seconds=int(token["refresh_token_expires_in"])
            )
        user.pinterest_scopes = token.get("scope")
        db.session.commit()
        return redirect("/")
    except Exception as exc:
        return f"Pinterest authentication failed: {exc}", 400


@app.route("/api/me")
@require_user
def me(user):
    return jsonify({
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "google_connected": bool(user.google_token),
        "pinterest_connected": bool(user.pinterest_access_token),
        "drive_folder": {
            "id": user.drive_folder_id,
            "name": user.drive_folder_name,
        } if user.drive_folder_id else None,
        "board": {
            "id": user.board_id,
            "name": user.board_name,
        } if user.board_id else None,
        "paused": user.paused,
    })


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/drive/folder", methods=["POST"])
@require_user
def set_drive_folder(user):
    data = request.get_json(silent=True) or {}
    folder_id = parse_drive_folder_id(data.get("folder"))
    if not folder_id:
        return jsonify({"error": "Invalid Google Drive folder URL or ID"}), 400

    try:
        creds = drive_credentials(user)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType"
        ).execute()
        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            return jsonify({"error": "The selected ID is not a Google Drive folder"}), 400
        user.drive_folder_id = folder["id"]
        user.drive_folder_name = folder["name"]
        db.session.commit()
        return jsonify({"status": "ok", "folder": folder})
    except Exception as exc:
        return jsonify({"error": f"Drive folder verification failed: {exc}"}), 400


@app.route("/api/drive/scan", methods=["POST"])
@require_user
def scan_drive(user):
    if not user.drive_folder_id:
        return jsonify({"error": "Connect Google Drive and set a folder first"}), 400
    try:
        files = fetch_drive_images(user)
        created = []
        for f in files:
            job, is_new = get_or_create_job(user, f)
            if is_new:
                created.append(job.id)
        return jsonify({
            "status": "ok",
            "found": len(files),
            "new_jobs": len(created),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/pinterest/boards")
@require_user
def boards(user):
    try:
        items = list_pinterest_boards(user)
        return jsonify({"items": [
            {"id": b.get("id"), "name": b.get("name")}
            for b in items
        ]})
    except PinterestAPIError as exc:
        return jsonify({
            "error": "Pinterest API error",
            "status_code": exc.status_code,
            "body": exc.body,
        }), exc.status_code
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/pinterest/board", methods=["POST"])
@require_user
def select_board(user):
    data = request.get_json(silent=True) or {}
    board_id = data.get("id")
    board_name = data.get("name")
    if not board_id:
        return jsonify({"error": "Board ID is required"}), 400
    user.board_id = board_id
    user.board_name = board_name
    db.session.commit()
    return jsonify({"status": "ok", "board": {"id": board_id, "name": board_name}})


@app.route("/api/automation/start", methods=["POST"])
@require_user
def automation_start(user):
    user.paused = False
    db.session.commit()
    return jsonify({"status": "running"})


@app.route("/api/automation/pause", methods=["POST"])
@require_user
def automation_pause(user):
    user.paused = True
    db.session.commit()
    return jsonify({"status": "paused"})


@app.route("/api/jobs")
@require_user
def jobs(user):
    rows = Job.query.filter_by(user_id=user.id).order_by(Job.created_at.desc()).limit(100).all()
    return jsonify({"items": [{
        "id": j.id,
        "file_name": j.drive_file_name,
        "status": j.status,
        "error": j.error,
        "title": j.title,
        "pinterest_pin_id": j.pinterest_pin_id,
        "attempts": j.attempts,
        "created_at": utc_iso(j.created_at),
        "published_at": utc_iso(j.published_at),
    } for j in rows]})


@app.route("/api/jobs/<job_id>/image")
@require_user
def job_image(user, job_id):
    job = Job.query.filter_by(id=job_id, user_id=user.id).first()
    if not job or not job.generated_image:
        return jsonify({"error": "Generated image not found"}), 404
    return send_file(
        io.BytesIO(job.generated_image),
        mimetype=job.generated_mime_type or "image/png",
        download_name=f"{job.id}.png",
    )


def process_one_for_user(user):
    if user.paused:
        return {"status": "paused"}

    if not user.google_token or not user.drive_folder_id:
        return {"status": "waiting", "reason": "Google Drive is not configured"}

    if not user.pinterest_access_token:
        return {"status": "waiting", "reason": "Pinterest is not connected"}

    pending = Job.query.filter(
        Job.user_id == user.id,
        Job.status.in_(["queued", "failed"]),
        Job.attempts < 3,
    ).order_by(Job.created_at.asc()).first()

    if not pending:
        try:
            files = fetch_drive_images(user)
            for f in files:
                get_or_create_job(user, f)
            pending = Job.query.filter_by(
                user_id=user.id, status="queued"
            ).order_by(Job.created_at.asc()).first()
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    if not pending:
        return {"status": "idle"}

    try:
        result = process_job(user, pending)
        return {
            "status": "published",
            "job_id": pending.id,
            "pin_id": result.get("id"),
        }
    except PinterestAPIError as exc:
        return {
            "status": "failed",
            "job_id": pending.id,
            "error": f"Pinterest API {exc.status_code}: {exc.body}",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "job_id": pending.id,
            "error": str(exc),
        }


@app.route("/api/automation/process-one", methods=["POST"])
@require_user
def process_one(user):
    return jsonify(process_one_for_user(user))


@app.route("/api/cron/process-one")
def cron_process_one():
    supplied = request.headers.get("X-Cron-Secret", "")
    if not supplied:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not CRON_SECRET or not secrets.compare_digest(supplied, CRON_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    # Process only users who opted into automation, bounded to one job per invocation.
    user = User.query.filter_by(paused=False).order_by(User.last_processed_at.asc().nullsfirst()).first()
    if not user:
        return jsonify({"status": "idle", "reason": "no active users"})
    result = process_one_for_user(user)
    user.last_processed_at = now()
    db.session.commit()
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
