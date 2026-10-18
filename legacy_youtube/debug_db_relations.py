from models import OwnedChannels, Channels, Sectors, DB_PATH
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def check_relations():
    engine = create_engine(DB_PATH)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    print("--- DEBUG RELATIONS ---")
    
    sectors = session.query(Sectors).all()
    print(f"Total Sectors: {len(sectors)}")
    for s in sectors:
        print(f"Sector {s.id}: {s.name}")
        
    ocs = session.query(OwnedChannels).all()
    print(f"\nTotal Owned Channels: {len(ocs)}")
    for oc in ocs:
        print(f"Owned Channel {oc.id}: {oc.name} -> Sector {oc.sector_id}")
        
    scs = session.query(Channels).all()
    print(f"\nTotal Source Channels: {len(scs)}")
    for sc in scs:
        print(f"Source Channel {sc.id}: {sc.url} -> Sector {sc.sector_id}")

    if not ocs:
        print("\nWARNING: No Owned Channels found.")
    if not scs:
        print("\nWARNING: No Source Channels found.")
        
    if ocs and scs:
        linked = False
        for oc in ocs:
            sources = [s for s in scs if s.sector_id == oc.sector_id]
            if sources:
                print(f"\nPASS: Owned Channel {oc.id} has {len(sources)} source channels in Sector {oc.sector_id}.")
                linked = True
            else:
                print(f"\nFAIL: Owned Channel {oc.id} has NO source channels in Sector {oc.sector_id}.")
        
        if not linked:
            print("\nCRITICAL: No Owned Channel is linked to any Source Channel via Sector ID.")

if __name__ == "__main__":
    check_relations()
