# test_stats_single.py
import sys, os
sys.path.insert(0, '.')
os.chdir(r'C:\Users\emre8\Desktop\utube')

from fetch_channel_stats import fetch_stats_for_channel
from models import DB_PATH, OwnedChannels
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
sess = Session()
ch = sess.query(OwnedChannels).get(1)
sess.close()

result = fetch_stats_for_channel(ch, headless=False)
print("Sonuç:", result)