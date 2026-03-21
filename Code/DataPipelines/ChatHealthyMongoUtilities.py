# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

# -------------------------------------------------------------------------------
# File: MongoDBConnectionManager.py
# Author: Skip Snow
# Co-Author: GPT-5
# Copyright (c) 2025 Skip Snow. All rights reserved.
# -------------------------------------------------------------------------------
#
# WHEN TO USE THIS CLASS
# ----------------------
# Use ChatHealthyMongoUtilities in GUI / conversational UX code (Layer 1 —
# ConversationalUX / HuggingFace) where:
#   - A single request may call getConnection() multiple times across methods.
#   - An explicit ping-on-access health check is desirable to detect stale
#     connections in long-lived session processes.
#   - The connection lifecycle is managed at the object level (create, use, close).
#
# DO NOT USE in DataPipelines (Layer 2 — Azure Functions).
# Pipeline activity functions use a module-level lazy MongoClient singleton
# instead:
#
#   _mongo: MongoClient | None = None
#
#   def _get_mongo_client() -> MongoClient:
#       global _mongo
#       if _mongo is None:
#           _mongo = MongoClient(os.environ["MONGO_connectionString"])
#       return _mongo
#
# PyMongo's MongoClient is itself a connection pool. Creating one instance per
# module and never closing it is the correct pattern for Azure Functions — the
# pool persists across warm invocations on the same worker process, eliminating
# per-invocation connection overhead.
# -------------------------------------------------------------------------------

from __future__ import annotations

from typing import Optional

try:
    from pymongo.mongo_client import MongoClient
    from pymongo.errors import PyMongoError
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "pymongo is not installed in the active venv. Run: pip install pymongo"
    ) from exc


class ChatHealthyMongoUtilities:
    """
    Manages a MongoDB connection with lifecycle control.

    Behavior:
    - Constructor creates and validates a MongoClient via ping
    - getConnection() returns the existing client after ping validation
    - Raises exceptions if connection or ping fails
    - Automatically closes the client when the object is destroyed
    - Supports context-manager usage (with ...)
    """

    def __init__(self, connection_string: str) -> MongoClient:
        if not connection_string or not isinstance(connection_string, str):
            raise ValueError("A valid MongoDB connection string must be provided.")

        self._connection_string: str = connection_string
        self._client: Optional[MongoClient] = None

        self._create_and_validate_client()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _create_and_validate_client(self) -> None:
        try:
            client = MongoClient(self._connection_string)

            # Lightweight health check
            client.admin.command("ping")

            self._client = client
            print("MongoDB connection successfully established and validated.")

        except PyMongoError as e:
            raise ConnectionError(
                f"Failed to create or validate MongoDB connection: {e}"
            ) from e

    def _validate_existing_client(self) -> None:
        if self._client is None:
            raise ConnectionError("MongoDB client is not initialized.")

        try:
            self._client.admin.command("ping")
        except PyMongoError as e:
            raise ConnectionError(
                f"Existing MongoDB connection failed ping check: {e}"
            ) from e

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def getConnection(self) -> MongoClient:
        """
        Returns the active MongoClient after validating it with ping.
        """
        self._validate_existing_client()
        return self._client

    def close(self) -> None:
        """
        Explicitly closes the MongoDB client.
        """
        if self._client is not None:
            try:
                self._client.close()
                print("MongoDB connection closed.")
            finally:
                self._client = None

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------
    def __enter__(self) -> MongoClient:
        return self.getConnection()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Destructor (best-effort cleanup)
    # ----------------------------------
