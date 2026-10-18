from models import Channels, Sectors, DB_PATH
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def fix_sectors():
    engine = create_engine(DB_PATH)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    print("--- FIX SECTORS ---")
    
    # Get Default Sector
    sector = session.query(Sectors).first()
    if not sector:
        print("No sector found! (Run add_channel.py first?)")
        return
        
    print(f"Using Sector: {sector.id} ({sector.name})")
    
    # Find channels with None sector
    channels = session.query(Channels).filter(Channels.sector_id == None).all()
    
    if not channels:
        print("No unassigned channels found.")
    
    for ch in channels:
        print(f"Assigning Channel {ch.id} ({ch.url}) to Sector {sector.id}")
        ch.sector_id = sector.id
        
    session.commit()
    print("Done.")

if __name__ == "__main__":
    fix_sectors()
