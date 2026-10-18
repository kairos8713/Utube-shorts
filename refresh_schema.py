from sqlalchemy import create_engine, text
from models import Base, DB_PATH
import time

def force_refresh_videostats():
    engine = create_engine(DB_PATH)
    
    print("Attempting to DROP video_stats table...")
    with engine.connect() as conn:
        try:
            conn.execute(text("DROP TABLE video_stats"))
            conn.commit()
            print("Table dropped.")
        except Exception as e:
            print(f"Drop failed (maybe didn't exist?): {e}")

    # Give it a moment?
    time.sleep(1)
    
    print("Recreating tables from models.py...")
    Base.metadata.create_all(engine)
    print("Done.")

if __name__ == "__main__":
    force_refresh_videostats()
