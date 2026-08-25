# AI Pinterest Agent (Academy Edition)

A multi-user academy project: each student connects their own Google Drive and Pinterest account. The backend uses the academy's Gemini API key to turn sketches/blueprints into Pinterest-ready images, generate SEO metadata, and publish Pins through Pinterest's official OAuth/API.

## Architecture

Google OAuth + Drive -> Flask API -> Gemini image/text -> Pinterest OAuth/API -> Pin

- One Pinterest Developer App owned by the academy.
- Students use ordinary Pinterest accounts; they do NOT create developer apps.
- One Gemini API key stays server-side.
- OAuth tokens are encrypted at rest.
- Drive file IDs are tracked to avoid duplicate processing.
- Vercel deployment is supported for the web/API layer.
- Vercel Cron can call one bounded job per invocation. For large queues, use a durable worker/queue later.

## Current model defaults

- Text: `gemini-3-flash-preview` (override with `GEMINI_TEXT_MODEL`)
- Image: `gemini-3.1-flash-image` (override with `GEMINI_IMAGE_MODEL`)

Google currently documents Gemini native image generation/editing through Nano Banana models. Imagen models are deprecated/shut down on Aug 17, 2026, so this project does not depend on Imagen.

## Important Pinterest limitation

This project deliberately does NOT read `_pinterest_sess`, CSRF cookies, or call undocumented internal Pinterest endpoints. It uses Pinterest OAuth 2.0 and the official `/v5/pins` API.

Pinterest's current docs say Authorization Code is the flow for apps serving multiple independent users. One academy-owned app can therefore authorize many normal student Pinterest accounts.

## Local setup

1. Create a Pinterest Developer app under the academy's Pinterest Business account.
2. Add a redirect URI:
   `http://localhost:5000/oauth/pinterest/callback`
3. Create a Google Cloud OAuth Web Application.
4. Add:
   `http://localhost:5000/oauth/google/callback`
5. Enable Google Drive API.
6. Copy `.env.example` to `.env`.
7. Install:
   `pip install -r requirements.txt`
8. Run:
   `python app.py`
9. Open `http://localhost:5000`.

## Production/Vercel

Set all `.env.example` variables in Vercel Project Settings -> Environment Variables.

Set production callback URLs, for example:
- `https://YOUR-DOMAIN.vercel.app/oauth/google/callback`
- `https://YOUR-DOMAIN.vercel.app/oauth/pinterest/callback`

Update both Google and Pinterest app configurations with the exact production URLs.

For a public GitHub repository:
- NEVER commit `.env`
- NEVER commit OAuth tokens
- NEVER commit `client_secret.json`
- NEVER commit service-account JSON
- NEVER put `GEMINI_API_KEY` in frontend JavaScript

## Queue behavior

The dashboard's Start button runs a bounded `process-one` request. It does not hold an HTTP request open for an infinite worker. A Vercel Cron can call `/api/cron/process-one` every minute with `CRON_SECRET`.

For a classroom/MVP, this is adequate. For hundreds of users or high-volume image generation, move jobs to Redis/Cloud Tasks/Celery/RQ and object storage.

## AI output

Each processed asset produces:

{
  "status": "published",
  "action": "publish_pin",
  "prompt": "...",
  "generated_image": "/api/jobs/<id>/image",
  "seo": {
    "title": "...",
    "description": "...",
    "hashtags": ["..."]
  },
  "pinterest_post_result": {
    "status": "published",
    "pin_id": "..."
  }
}

## Known weaknesses / production upgrades

1. Vercel serverless is not a persistent worker. Use a queue for high volume.
2. Generated images are stored in the database as binary for simplicity. Use S3/R2/GCS for production scale.
3. SQLite is for local development only. Use Postgres in production.
4. Google OAuth/Drive scopes may require Google verification depending on your publishing/use case.
5. Pinterest app access/approval is controlled by Pinterest. The code cannot bypass it.
6. AI generation costs are per request. Rate-limit students and add quotas before opening it broadly.
7. Add an admin dashboard, per-user quotas, moderation, and job retry controls before production at academy scale.
8. Never ask students for Pinterest passwords or session cookies.
