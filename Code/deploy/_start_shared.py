"""Start CHShared on HTTPS port 8002. Run as separate process."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared_services"))
os.chdir(os.path.join(os.path.dirname(__file__), "..", "shared_services"))

import uvicorn
certs_dir = sys.argv[1]

from app import app
uvicorn.run(app, host="0.0.0.0", port=8002,
            ssl_certfile=os.path.join(certs_dir, "shared.crt"),
            ssl_keyfile=os.path.join(certs_dir, "shared.key"),
            log_level="info")
