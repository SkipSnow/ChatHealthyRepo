"""Test file to verify Rule-004 blocks MongoClient() instantiation."""
from pymongo import MongoClient

def violate_rule():
    """This violates Rule-004: direct MongoClient instantiation."""
    return MongoClient("mongodb://localhost")
