# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

import os
import csv
import io

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
        """Fetch current NUCC taxonomy CSV. Scrapes page first, falls back to Haiku."""
        csv_url = self._scrape_csv_url()
        if not csv_url:
            logging.warning("Scrape failed — falling back to Haiku agent.")
            csv_url = self._agent_find_csv_url()

        logging.info("Fetching CSV from: %s", csv_url)
        response = requests.get(csv_url, timeout=30)
        response.raise_for_status()
        self._csv_content = response.text
        self._csv_filename = csv_url.split("/")[-1].split("?")[0] or "nucc_taxonomy.csv"
        logging.info("Fetched %d bytes as '%s'", len(self._csv_content), self._csv_filename)

    def _scrape_csv_url(self) -> str | None:
        try:
            response = requests.get(NUCC_PAGE_URL, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".csv"):
                    return href if href.startswith("http") else "https://www.nucc.org" + href
        except Exception as e:
            logging.warning("Scrape error: %s", e)
        return None

    def _agent_find_csv_url(self) -> str:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
        page_html = requests.get(NUCC_PAGE_URL, timeout=15).text[:8000]
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    "Find the direct download URL for the current NUCC provider taxonomy "
                    "CSV file from this HTML page. Return only the URL, nothing else.\n\n"
                    + page_html
                ),
            }],
        )
        url = message.content[0].text.strip()
        if not url.startswith("http"):
            raise ValueError(f"Agent returned invalid URL: {url}")
        return url

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
        """Embed each record using text-embedding-3-small and write back to MongoDB.

        Embedding text: Classification | Specialization | Display Name | Definition
        Returns number of records updated.
        """
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
                model="text-embedding-3-small",
                input=texts,
            )
            ops = [
                UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": item.embedding}})
                for doc, item in zip(batch, response.data)
            ]
            col.bulk_write(ops, ordered=False)
            updated += len(ops)
            logging.info("Embedded %d/%d", updated, len(docs))

        logging.info("Embeddings written for %d records.", updated)
        return updated


# ------------------------------------------------------------------
# Entry point called from function_app.py dispatch
# ------------------------------------------------------------------

def run_load_specialty_data(payload: dict = None) -> dict:
    collection_fqn = os.getenv("SPECIALTY_COLLECTION", "PublicHealthData.SpecialtyMetaData")
    loader = ChatHealthyLoadSpecialtyData(collection_fqn)
    loader.fetch_csv()
    blob_name = loader.store_to_blob()
    count = loader.load_to_mongo()
    embedded = loader.generate_embeddings()
    return {"blob": blob_name, "inserted": count, "embedded": embedded}
