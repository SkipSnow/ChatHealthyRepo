"""Start website wrapper + HTTP redirect. Run as separate process."""
import os, sys, threading
import uvicorn
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse, FileResponse
from starlette.requests import Request

certs_dir = sys.argv[1]
website_dir = sys.argv[2]

localhost_crt = os.path.join(certs_dir, "localhost.crt")
localhost_key = os.path.join(certs_dir, "localhost.key")

# ── HTTP redirect (port 80) ──────────────────────────────────
redirect_app = FastAPI()

@redirect_app.middleware("http")
async def redirect_to_https(request: Request, call_next):
    url = request.url.replace(scheme="https", port=443)
    return RedirectResponse(url=str(url), status_code=301)

threading.Thread(
    target=lambda: uvicorn.run(redirect_app, host="0.0.0.0", port=80, log_level="warning"),
    daemon=True,
).start()

# ── Website (port 443) ───────────────────────────────────────
website_app = FastAPI()

# Force no-cache on ALL responses — never let the browser serve stale content
@website_app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@website_app.get("/")
async def serve_index():
    return FileResponse(os.path.join(website_dir, "index.html"))

# Serve all website files
for fname in os.listdir(website_dir):
    fpath = os.path.join(website_dir, fname)
    if os.path.isfile(fpath) and fname != "index.html":
        def make_route(fp):
            async def serve():
                return FileResponse(fp)
            return serve
        website_app.get(f"/{fname}")(make_route(fpath))

@website_app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.get("https://localhost:7860/health", timeout=10)
            return resp.json()
        except Exception:
            return {"status": "waiting", "note": "FindCare not ready yet"}

uvicorn.run(website_app, host="0.0.0.0", port=443,
            ssl_certfile=localhost_crt, ssl_keyfile=localhost_key)
