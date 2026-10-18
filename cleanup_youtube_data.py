import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

# Import the SQLAlchemy session and models
from models import Session, logs, UploadQueue, ProcessedSourceVideos, videos, OwnedChannels, Channels, Sectors

# Order is critical: delete dependent child tables first to avoid Foreign Key constraint violations.
TABLES_IN_ORDER = [
    ("logs", logs),
    ("upload_queue", UploadQueue),
    ("processed_source_videos", ProcessedSourceVideos),
    ("videos", videos),
    ("owned_channels", OwnedChannels),
    ("channels", Channels),
    ("sectors", Sectors)
]

def get_counts(session):
    counts = {}
    for name, model in TABLES_IN_ORDER:
        try:
            c = session.query(model).count()
            counts[name] = c
        except Exception as e:
            counts[name] = f"Error: {e}"
    return counts

def print_counts(session, prefix=""):
    print(f"\n--- {prefix} ROW COUNTS ---")
    counts = get_counts(session)
    for name, c in counts.items():
        print(f"  {name:<25}: {c}")
    return counts

def run_cleanup():
    session = Session()
    try:
        print("Connecting to database and verifying current state...")
        print_counts(session, "BEFORE CLEANUP")
        
        print("\nWARNING: This will permanently DELETE ALL ROWS from the tables listed above.")
        print("This action cannot be undone.")
        
        confirm = input("\nType 'yes' to proceed with deletion: ")
        
        if confirm.strip() != "yes":
            print("\nAborted. No changes were made to the database.")
            return

        print("\nDeleting rows in dependency order (children first)...")
        for name, model in TABLES_IN_ORDER:
            # delete() issues a single bulk DELETE statement
            num_deleted = session.query(model).delete()
            print(f"  [OK] Deleted {num_deleted} rows from {name}")
            
        session.commit()
        print("\nCommit successful.")
        
        print_counts(session, "AFTER CLEANUP")
        
    except Exception as e:
        session.rollback()
        print(f"\n[!] A fatal error occurred during cleanup. Transaction rolled back.\nError details: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    run_cleanup()
