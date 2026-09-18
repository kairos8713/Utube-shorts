from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, OwnedChannels, Sectors, DB_PATH
import datetime

def add_channel():
    print("--- Add Channel to Database ---")
    name = input("Channel Name: ")
    js_path = input("Path to Session File (e.g., sessions/channel1.json): ")
    
    # Check if sector exists or create default
    engine = create_engine(DB_PATH)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    sector = session.query(Sectors).first()
    if not sector:
        print("No sectors found. Creating 'Default Sector'.")
        sector = Sectors(name="Default", title="Default", description="Desc", tags="tags")
        session.add(sector)
        session.commit()
    
    new_ch = OwnedChannels(
        sector_id=sector.id,
        name=name,
        js_path=js_path,
        level=1,
        mail="example@gmail.com",
        password="password",
        is_ypp=False,
        sum_views=0,
        m3_views=0,
        last_uploaded_at=datetime.datetime.utcnow()
    )
    
    session.add(new_ch)
    session.commit()
    print(f"Channel '{name}' added successfully with ID {new_ch.id}.")

if __name__ == "__main__":
    add_channel()
