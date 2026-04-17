"""Local UAT server — starts all 5 listeners."""
import uvicorn, threading, os, sys, ssl, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("deploy")

certs_dir = sys.argv[1]
backend_dir = sys.argv[2]
evalcare_dir = sys.argv[3]
shared_dir = sys.argv[4]
website_dir = sys.argv[5]
repo_root = sys.argv[6]

# Cert paths
ca_crt = os.path.join(certs_dir, "ca.crt")
localhost_crt = os.path.join(certs_dir, "localhost.crt")
localhost_key = os.path.join(certs_dir, "localhost.key")
findcare_crt = os.path.join(certs_dir, "findcare.crt")
findcare_key = os.path.join(certs_dir, "findcare.key")
evalcare_crt = os.path.join(certs_dir, "evalcare.crt")
evalcare_key = os.path.join(certs_dir, "evalcare.key")
shared_crt = os.path.join(certs_dir, "shared.crt")
shared_key = os.path.join(certs_dir, "shared.key")

# ── 1. HTTP redirect (port 80) ────────────────────────────────
from fastapi import FastAPI
from starlette.responses import RedirectResponse
from starlette.requests import Request

redirect_app = FastAPI()

@redirect_app.middleware("http")
async def redirect_to_https(request: Request, call_next):
    url = request.url.replace(scheme="https", port=443)
    return RedirectResponse(url=str(url), status_code=301)

def start_redirect():
    uvicorn.run(redirect_app, host="0.0.0.0", port=80, log_level="warning")

threading.Thread(target=start_redirect, daemon=True).start()
_log.info("Port 80  → HTTP redirect started")

# ── 2. FindCare backend (port 7860) ───────────────────────────
os.chdir(backend_dir)
sys.path.insert(0, backend_dir)
sys.path.insert(0, os.path.join(repo_root, "Code", "Shared"))

# Set env vars for FindCare
os.environ["EVALCARE_URL"] = "https://localhost:8001"
os.environ["SHARED_SERVICES_URL"] = "https://localhost:8002"
os.environ["ENV_PREFIX"] = "dev"

def start_findcare():
    from main import app as findcare_app
    uvicorn.run(findcare_app, host="0.0.0.0", port=7860,
                ssl_certfile=findcare_crt, ssl_keyfile=findcare_key)

threading.Thread(target=start_findcare, daemon=True).start()
_log.info("Port 7860 → FindCare backend started (HTTPS)")

# ── 3. EvaluateCare (port 8001) ───────────────────────────────
def start_evalcare():
    import importlib
    sys.path.insert(0, evalcare_dir)
    os.chdir(evalcare_dir)
    # Create SSL context with mTLS (require client cert)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(evalcare_crt, evalcare_key)
    ssl_ctx.load_verify_locations(ca_crt)
    ssl_ctx.verify_mode = ssl.CERT_OPTIONAL  # Optional for browser, required for service calls
    from app import app as evalcare_app
    uvicorn.run(evalcare_app, host="0.0.0.0", port=8001,
                ssl_certfile=evalcare_crt, ssl_keyfile=evalcare_key)

threading.Thread(target=start_evalcare, daemon=True).start()
_log.info("Port 8001 → EvaluateCare started (HTTPS)")

# ── 4. CHShared (port 8002) ───────────────────────────────────
def start_shared():
    sys.path.insert(0, shared_dir)
    os.chdir(shared_dir)
    from app import app as shared_app
    uvicorn.run(shared_app, host="0.0.0.0", port=8002,
                ssl_certfile=shared_crt, ssl_keyfile=shared_key)

threading.Thread(target=start_shared, daemon=True).start()
_log.info("Port 8002 → CHShared started (HTTPS)")

# ── 5. Website wrapper (port 443) ─────────────────────────────
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

website_app = FastAPI()

@website_app.get("/")
async def serve_website():
    return FileResponse(
        os.path.join(website_dir, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

# Serve all website static files
for fname in os.listdir(website_dir):
    fpath = os.path.join(website_dir, fname)
    if os.path.isfile(fpath) and fname != "index.html":
        # Create closure to capture fname
        def make_route(fp):
            async def serve():
                return FileResponse(fp)
            return serve
        website_app.get(f"/{fname}")(make_route(fpath))

# Proxy /health to FindCare backend for banner build info
import httpx

@website_app.get("/health")
async def proxy_health():
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get("https://localhost:7860/health")
        return resp.json()

_log.info("Port 443  → Website started (HTTPS)")
_log.info("")
_log.info("=== LOCAL UAT READY ===")
_log.info("  http://localhost → https://localhost")
_log.info("=======================")

uvicorn.run(website_app, host="0.0.0.0", port=443,
            ssl_certfile=localhost_crt, ssl_keyfile=localhost_key)
