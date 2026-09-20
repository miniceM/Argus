import os

# Ensure tests default to test DB mode unless explicitly overridden
os.environ.setdefault("ARGUS_DB_MODE", "test")
