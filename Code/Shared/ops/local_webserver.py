#!/usr/bin/env python3
# Copyright (c) 2026 Skip Snow. All rights reserved.
# Local static web server for Website/ on port 80.
# Usage: python local_webserver.py
# Requires: run as admin (port 80 needs elevated privileges on Windows)

import http.server
import os
import sys

PORT = 80
DIRECTORY = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Website")

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

if __name__ == "__main__":
    print(f"WARNING: This HTTP server is for bootstrap only. Use Caddy (start_local.bat) for HTTPS.")
    print(f"Serving {os.path.abspath(DIRECTORY)} on http://localhost:{PORT}")
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
