import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

print("Initializing database...")
sys.path.insert(0, SRC_DIR)
from state_manager import initialize_db
initialize_db()

print("Setup complete.")
print("")
print("Next step: run this to authorize Google account:")
print(f"  python {os.path.join(SRC_DIR, 'main.py')}")
