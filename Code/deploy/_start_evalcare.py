"""Start EvaluateCare on HTTPS port 8001. Run as separate process."""
import os, sys
evalcare_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evaluate_care")
# Need parent dir so 'from evaluate_care.models' resolves
sys.path.insert(0, os.path.dirname(evalcare_dir))
sys.path.insert(0, evalcare_dir)
os.chdir(evalcare_dir)

import uvicorn
certs_dir = sys.argv[1]

from app import app
uvicorn.run(app, host="0.0.0.0", port=8001,
            ssl_certfile=os.path.join(certs_dir, "evalcare.crt"),
            ssl_keyfile=os.path.join(certs_dir, "evalcare.key"),
            log_level="info")
