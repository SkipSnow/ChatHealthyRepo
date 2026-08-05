"""This file violates Rule-004: direct MongoClient instantiation."""
from pymongo import MongoClient

def get_provider(npi: str):
    """Get a provider by NPI — violates Rule-004."""
    client = MongoClient("mongodb://atlas-cluster")
    db = client["prod_PublicHealthData"]
    collection = db["Provider"]
    return collection.find_one({"npi": npi})
