#!/usr/bin/env python3
# Copyright (c) 2026 Skip Snow. All rights reserved.
# Local HTTPS server for admin content on port 443.
# Uses self-signed cert from certs/ directory.
# Usage: python local_admin_server.py
# Requires: run as admin (port 443 needs elevated privileges on Windows)

import http.server
import os
import ssl
import sys

PORT = 443
DIRECTORY = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Website")
CERT_DIR = os.path.join(os.path.dirname(__file__), "certs")
CERTFILE = os.path.join(CERT_DIR, "localhost.crt")
KEYFILE = os.path.join(CERT_DIR, "localhost.key")

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

if __name__ == "__main__":
    if not os.path.exists(CERTFILE):
        print(f"ERROR: Certificate not found at {CERTFILE}")
        print("Run: openssl req -x509 -newkey rsa:2048 -keyout localhost.key -out localhost.crt -days 365 -nodes -subj /CN=localhost")
        sys.exit(1)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERTFILE, KEYFILE)

    print(f"Serving {os.path.abspath(DIRECTORY)} on https://localhost:{PORT}")
    print("NOTE: Browser will warn about self-signed cert — click Advanced > Proceed")
    with http.server.HTTPServer(("", PORT), Handler) as httpd:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
