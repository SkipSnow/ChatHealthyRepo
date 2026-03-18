"""
Pytest configuration for Website tests.
Starts a local HTTP server serving the Website/ directory so Playwright
can load pages via http:// (required for iframe src to resolve correctly).
"""
import functools
import http.server
import os
import threading

import pytest

PORT = 8765
WEBSITE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="session", autouse=True)
def http_server():
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=WEBSITE_DIR,
    )
    server = http.server.HTTPServer(("localhost", PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield
    server.shutdown()


@pytest.fixture(scope="session")
def base_url():
    return f"http://localhost:{PORT}"
