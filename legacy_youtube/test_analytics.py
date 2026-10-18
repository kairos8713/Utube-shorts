# test_analytics.py
import sys; sys.path.insert(0, '.')
from youtube_analytics import fetch_channel_analytics
from models import DB_PATH, OwnedChannels
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(DB_PATH)
sess = sessionmaker(bind=engine)()
ch1 = sess.query(OwnedChannels).filter_by(id=1).first()

result = fetch_channel_analytics(ch1.youtube_channel_id)
for v in result.get('video_records', []):
    print(f"  {v['title'][:40]} → {v['views']} views | date: {v.get('date_str')}")