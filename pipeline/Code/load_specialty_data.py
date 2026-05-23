# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

import os
import csv
import io
import logging

import requests
from bs4 import BeautifulSoup
from azure.storage.blob import BlobServiceClient
from openai import OpenAI
from pymongo import MongoClient, UpdateOne

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo

NUCC_PAGE_URL = (
    "https://www.nucc.org/index.php/code-sets-mainmenu-41/"
    "provider-taxonomy-mainmenu-40/csv-mainmenu-57"
)

EXPECTED_FIELDS = [
    "Code", "Grouping", "Classification", "Specialization",
    "Definition", "Notes", "Display Name", "Section",
]


class ChatHealthyLoadSpecialtyData:
    """
    Fetches the current NUCC provider taxonomy CSV, stores it in Azure Blob
    Storage, and loads it into MongoDB.

    Usage:
        loader = ChatHealthyLoadSpecialtyData("PublicHealthData.SpecialtyMetaData")
        loader.fetch_csv()
        loader.store_to_blob()
        loader.load_to_mongo()
    """

    def __init__(self, collection_fqn: str):
        if not collection_fqn or "." not in collection_fqn:
            raise ValueError("collection_fqn must be 'DatabaseName.CollectionName'")
        self.db_name, self.collection_name = collection_fqn.split(".", 1)
        self._csv_content: str | None = None
        self._csv_filename: str = "nucc_taxonomy.csv"

    # ------------------------------------------------------------------
    # Step 1: Fetch CSV
    # ------------------------------------------------------------------

    def fetch_csv(self) -> None:
        """Fetch the latest NUCC taxonomy CSV.

        URL discovered by AI-agent (F-102-S-003-REQ-T-006). No regex, no
        fallback constant — the agent reads the NUCC page and picks the
        latest CSV.
        """
        from source_url_discovery import find_latest_data_url

        csv_url = find_latest_data_url(
            source_name="nucc",
            page_url=NUCC_PAGE_URL,
            instructions=(
                "Find the URL of the latest NUCC Provider Taxonomy CSV file. "
                "Rules: (a) Look for the CSV file (filename ends with .csv) "
                "that contains the current taxonomy code set; the file usually "
                "has a name like `nucc_taxonomy_<version>.csv`. (b) If "
                "multiple versions are listed, return the URL of the most "
                "recent one. (c) IGNORE PDF, XLSX, and ZIP files — only the "
                "CSV is the data file we want. (d) Return only the absolute "
                "URL."
            ),
        )

        logging.info("Fetching CSV from: %s", csv_url)
        response = requests.get(csv_url, timeout=30)
        response.raise_for_status()
        self._csv_content = response.text
        self._csv_filename = csv_url.split("/")[-1].split("?")[0] or "nucc_taxonomy.csv"
        logging.info("Fetched %d bytes as '%s'", len(self._csv_content), self._csv_filename)

    # ------------------------------------------------------------------
    # Step 2: Store to Azure Blob
    # ------------------------------------------------------------------

    def store_to_blob(self) -> str:
        """Upload CSV to Azure Blob Storage. Returns blob name."""
        if self._csv_content is None:
            raise RuntimeError("Call fetch_csv() before store_to_blob()")

        conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container = os.getenv("AZURE_STORAGE_CONTAINER", "chathealthy-public-data")

        blob_client = BlobServiceClient.from_connection_string(conn_str).get_blob_client(
            container=container, blob=self._csv_filename
        )
        blob_client.upload_blob(self._csv_content.encode("utf-8"), overwrite=True)
        logging.info("Stored '%s' to container '%s'", self._csv_filename, container)
        return self._csv_filename

    # ------------------------------------------------------------------
    # Step 3: Load to MongoDB
    # ------------------------------------------------------------------

    def load_to_mongo(self) -> int:
        """Clear collection and load CSV rows. Returns inserted count."""
        if self._csv_content is None:
            raise RuntimeError("Call fetch_csv() before load_to_mongo()")

        col = _get_mongo_client()[self.db_name][self.collection_name]
        col.delete_many({})
        logging.info("Cleared %s.%s", self.db_name, self.collection_name)

        version = self._csv_filename.rsplit("_", 1)[-1].split(".")[0]

        reader = csv.DictReader(io.StringIO(self._csv_content))
        batch = []
        inserted = 0

        for record_number, row in enumerate(reader, start=1):
            doc = {field: (row.get(field) or "").strip() for field in EXPECTED_FIELDS}
            doc["version"] = version
            doc["record_number"] = record_number
            batch.append(doc)
            if len(batch) >= 128:
                inserted += len(col.insert_many(batch, ordered=False).inserted_ids)
                batch.clear()

        if batch:
            inserted += len(col.insert_many(batch, ordered=False).inserted_ids)

        logging.info("Inserted %d records into %s.%s", inserted, self.db_name, self.collection_name)
        return inserted


    # ------------------------------------------------------------------
    # Step 4: Generate and store embeddings
    # ------------------------------------------------------------------

    def generate_embeddings(self) -> int:
        """Embed each record using text-embedding-3-large and write back to MongoDB.

        Embedding text: Classification | Specialization | Display Name | Definition
        Returns number of records updated.
        """
        from embedding_worker import EMBED_MODEL, EMBED_VERSION
        openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        col = _get_mongo_client()[self.db_name][self.collection_name]
        docs = list(col.find({}, {"_id": 1, "Classification": 1, "Specialization": 1,
                                  "Display Name": 1, "Definition": 1}))
        logging.info("Generating embeddings for %d records...", len(docs))

        BATCH = 128
        updated = 0
        for i in range(0, len(docs), BATCH):
            batch = docs[i:i + BATCH]
            texts = [
                " | ".join(filter(None, [
                    d.get("Classification", ""),
                    d.get("Specialization", ""),
                    d.get("Display Name", ""),
                    d.get("Definition", ""),
                ]))
                for d in batch
            ]
            response = openai_client.embeddings.create(
                model=EMBED_MODEL,
                input=texts,
            )
            ops = [
                UpdateOne(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "embedding": item.embedding,
                        "embedding_model": EMBED_MODEL,
                        "embedding_version": EMBED_VERSION,
                    }},
                )
                for doc, item in zip(batch, response.data)
            ]
            col.bulk_write(ops, ordered=False)
            updated += len(ops)
            logging.info("Embedded %d/%d", updated, len(docs))

        logging.info("Embeddings written for %d records.", updated)
        return updated


# ------------------------------------------------------------------
# Specialty enrichment — can_prescribe and homeopathic flags from
# the proprietary NUCC classification catalog (one source of truth).
# ------------------------------------------------------------------


def enrich_specialty_flags(collection_fqn: str) -> dict:
    """Enrich SpecialtyMetaData with can_prescribe and is_homeopathic boolean flags.

    Reads the per-NUCC-code classification from the catalog blob via
    specialty_classification_catalog.load_catalog(). Field names match
    EPIC-010-F-105 — `can_prescribe` and `is_homeopathic` on every record.

    Returns counts of classified specialties.
    """
    from specialty_classification_catalog import load_catalog
    catalog = load_catalog()

    db_name, coll_name = collection_fqn.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    docs = list(coll.find({}, {"_id": 1, "Code": 1, "Display Name": 1}))
    logging.info("Enriching %d specialties from catalog (%d entries)", len(docs), len(catalog))

    ops = []
    prescribers = 0
    homeopathic = 0
    missing = 0

    for doc in docs:
        code = doc.get("Code", "")
        entry = catalog.get(code)
        if entry is None:
            missing += 1
            continue
        can_prescribe = entry["can_prescribe"]
        is_homeopathic = entry["is_homeopathic"]
        if can_prescribe:
            prescribers += 1
        if is_homeopathic:
            homeopathic += 1
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"can_prescribe": can_prescribe, "is_homeopathic": is_homeopathic}},
        ))

    if ops:
        coll.bulk_write(ops, ordered=False)
    logging.info("Enriched %d specialties: %d prescribers, %d homeopathic, %d not in catalog",
                 len(ops), prescribers, homeopathic, missing)

    return {"total": len(docs), "matched": len(ops), "prescribers": prescribers,
            "homeopathic": homeopathic, "missing_from_catalog": missing}


# ------------------------------------------------------------------
# Entry point called from function_app.py dispatch
# ------------------------------------------------------------------

def run_load_specialty_data(payload: dict = None) -> dict:
    payload = payload or {}
    _env = payload.get("env_prefix", os.getenv("ENV_PREFIX", "dev"))
    collection_fqn = os.getenv("SPECIALTY_COLLECTION", f"{_env}_PublicHealthData.SpecialtyMetaData")
    # embedding_enabled mirrors the Provider pipeline's flag. When the caller
    # passes False the Specialty pipeline skips the OpenAI embedding pass —
    # used in cheap-test runs that only need flag enrichment to land.
    embedding_enabled = bool(payload.get("embedding_enabled", True))

    loader = ChatHealthyLoadSpecialtyData(collection_fqn)
    loader.fetch_csv()
    blob_name = loader.store_to_blob()
    count = loader.load_to_mongo()

    embedded = 0
    if embedding_enabled:
        embedded = loader.generate_embeddings()
    else:
        logging.info("Specialty embeddings SKIPPED (embedding_enabled=False)")

    enrichment = enrich_specialty_flags(collection_fqn)
    return {
        "blob": blob_name,
        "inserted": count,
        "embedded": embedded,
        "embedding_enabled": embedding_enabled,
        "enrichment": enrichment,
    }


# ── Specialty Pipeline Orchestrator (F-104) ─────────────────────────────────

SPECIALTY_LABEL_RESERVE = "Step 1: Reserving cluster"
SPECIALTY_LABEL_HEALTH  = "Step 2: Checking MongoDB health"
SPECIALTY_LABEL_LOAD    = "Step 3: Loading SpecialtyMetaData"

SPECIALTY_PIPELINE_STEPS = [
    SPECIALTY_LABEL_RESERVE,
    SPECIALTY_LABEL_HEALTH,
    SPECIALTY_LABEL_LOAD,
]


from base_pipeline_orchestrator import BasePipelineOrchestrator


class SpecialtyPipelineOrchestrator(BasePipelineOrchestrator):
    """F-104 Specialty Pipeline. Steps: reserve → health → load → release.

    Reservation, IDLE wait, and try/release live in BasePipelineOrchestrator.
    This subclass owns only the specialty-specific work.
    """

    requester_name = "SpecialtyPipeline"

    def _pipeline_steps(self):
        self.context.set_custom_status(SPECIALTY_LABEL_HEALTH)
        yield self.context.call_activity("check_mongo_health_activity", self.config)

        self.context.set_custom_status(SPECIALTY_LABEL_LOAD)
        load_result = yield self.context.call_activity(
            "load_specialty_data_activity",
            {
                "env_prefix": self.config["env_prefix"],
                "embedding_enabled": self.config.get("embedding_enabled", True),
            },
        )
        return {"load": load_result}


def specialty_pipeline_orchestrator_fn(context):
    config = context.get_input() or {}
    orch = SpecialtyPipelineOrchestrator(context, config)
    result = yield from orch.run()
    return result
