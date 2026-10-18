import sys, os
sys.path.insert(0, r'C:\Users\emre8\Desktop\utube')
os.chdir(r'C:\Users\emre8\Desktop\utube')

from sector_processor import execute_upload_queue, session
from models import UploadQueue

cycle_ids = [r[0] for r in session.query(UploadQueue.cycle_id).distinct().all()]
print(f"Found {len(cycle_ids)} cycle(s)")

for cycle_id in cycle_ids:
    print(f"\n--- Uploading cycle {cycle_id} ---")
    success, fail = execute_upload_queue(cycle_id)
    print(f"Cycle {cycle_id}: {success} ok, {fail} failed")