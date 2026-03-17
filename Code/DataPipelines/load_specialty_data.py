import os
import csv
import io

import requests
from bs4 import BeautifulSoup
from azure.storage.blob import BlobServiceClient

from ChatHealthyMongoUtilities import ChatHealthyMongoUtilities

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
            print("Scrape failed — falling back to Haiku agent.")
            csv_url = self._agent_find_csv_url()

        print(f"Fetching CSV from: {csv_url}")
        response = requests.get(csv_url, timeout=30)
        response.raise_for_status()
        self._csv_content = response.text
        self._csv_filename = csv_url.split("/")[-1].split("?")[0] or "nucc_taxonomy.csv"
        print(f"Fetched {len(self._csv_content):,} bytes as '{self._csv_filename}'")

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
            print(f"Scrape error: {e}")
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
        print(f"Stored '{self._csv_filename}' to container '{container}'")
        return self._csv_filename

    # ------------------------------------------------------------------
    # Step 3: Load to MongoDB
    # ------------------------------------------------------------------

    def load_to_mongo(self) -> int:
        """Clear collection and load CSV rows. Returns inserted count."""
        if self._csv_content is None:
            raise RuntimeError("Call fetch_csv() before load_to_mongo()")

        mongo = ChatHealthyMongoUtilities(os.getenv("MONGO_connectionString"))
        try:
            col = mongo.getConnection()[self.db_name][self.collection_name]
            col.delete_many({})
            print(f"Cleared {self.db_name}.{self.collection_name}")

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

            print(f"Inserted {inserted:,} records into {self.db_name}.{self.collection_name}")
            return inserted
        finally:
            mongo.close()


# ------------------------------------------------------------------
# Entry point called from function_app.py dispatch
# ------------------------------------------------------------------

def run_load_specialty_data() -> dict:
    collection_fqn = os.getenv("SPECIALTY_COLLECTION", "PublicHealthData.SpecialtyMetaData")
    loader = ChatHealthyLoadSpecialtyData(collection_fqn)
    loader.fetch_csv()
    blob_name = loader.store_to_blob()
    count = loader.load_to_mongo()
    return {"blob": blob_name, "inserted": count}
