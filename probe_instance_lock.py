import sys
sys.path.insert(0, r"C:\Users\hp\Documents\Codex\2026-11-10\ratpr756-gmail-com\outputs\shorts_pipeline")
import web_app
print("LOCK_HELD" if not web_app._acquire_single_instance() else "LOCK_ACQUIRED")
