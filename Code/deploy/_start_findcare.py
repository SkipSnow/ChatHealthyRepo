"""Start FindCare backend on HTTPS port 7860. Run as separate process."""
import os, sys

backend_dir = os.path.join(os.path.dirname(__file__), "..", "ConversationalUX", "FindCareChat", "backend")
sys.path.insert(0, backend_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
os.chdir(backend_dir)

os.environ["EVALCARE_URL"] = "https://localhost:8001"
os.environ["SHARED_SERVICES_URL"] = "https://localhost:8002"
os.environ["ENV_PREFIX"] = "dev"

import uvicorn
certs_dir = sys.argv[1]

from main import app
uvicorn.run(app, host="0.0.0.0", port=7860,
            ssl_certfile=os.path.join(certs_dir, "findcare.crt"),
            ssl_keyfile=os.path.join(certs_dir, "findcare.key"),
            log_level="info")
