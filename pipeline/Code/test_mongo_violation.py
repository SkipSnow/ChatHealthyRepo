"""Test file to verify Rule-004 enforcement catches violations."""
import os

def test_direct_env_read():
    """This violates Rule-004-REQ-B-012: direct MONGO_* env var read."""
    connection_string = os.environ.get("MONGO_connectionString")
    return connection_string

def test_subscript_access():
    """This also violates Rule-004-REQ-B-012: subscript access."""
    connection_string = os.environ["MONGO_FRONTEND_connectionString"]
    return connection_string
