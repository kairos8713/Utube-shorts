import time
import os
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import OwnedChannels, VideoStats, DB_PATH
import fetch_channel_stats

# Setup DB
engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
session = Session()

def update_all_channels():
    print("--- Starting Stats Update ---")
    channels = session.query(OwnedChannels).all()
    
    for ch in channels:
        if not ch.js_path or not os.path.exists(ch.js_path):
            print(f"[SKIP] Channel {ch.id}: No session file.")
            continue
            
        print(f"\n[UPDATE] Fetching stats for Channel {ch.id} ({ch.name})...")
        
        # Use stored UA or default
        ua = ch.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        
        # CALL FETCH SCRIPT with retry
        MAX_RETRIES = 3
        stats = None
        for attempt in range(MAX_RETRIES):
            try:
                stats = fetch_channel_stats.get_channel_stats(ch.js_path, ua)
                if stats and stats.get('views_lifetime', 0) > 0:
                    break
                elif attempt < MAX_RETRIES - 1:
                    print(f"[RETRY] Channel {ch.id} attempt {attempt+1} returned no data, retrying...")
                    time.sleep(5)
            except Exception as e:
                print(f"[RETRY] Channel {ch.id} attempt {attempt+1} error: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)

        if not stats or stats.get('views_lifetime', 0) == 0:
            print(f"[WARN] Channel {ch.id} ({ch.name}) failed after {MAX_RETRIES} attempts — keeping existing records.")
            continue

        video_recs = stats.get("video_records", [])
        print(f"  -> Found: Views={stats.get('views_lifetime')}, Videos={len(video_recs)}")

        if not video_recs:
            print(f"[SKIP] No video records returned — keeping existing records for channel {ch.id}")
            continue

        # --- 1. VideoStats Snapshot (Delete Old, Insert New) ---
        # Only delete+replace when we have real data
        try:
            session.query(VideoStats).filter_by(owned_channel_id=ch.id).delete()
            
            new_objects = []
            total_views_calc = 0
            views_90d_calc = 0
            
            # cutoff for 90d
            cutoff_date = datetime.datetime.utcnow() - datetime.timedelta(days=90)
            
            for rec in video_recs:
                # Skip placeholder / untitled rows from the analytics scraper
                video_title = rec.get("title", "") or ""
                if not video_title or video_title.strip().lower().startswith('untitled'):
                    continue

                # Parse date for calculations, but store string in DB
                p_date_obj = None
                if rec["date_str"]:
                    try:
                        p_date_obj = datetime.datetime.strptime(rec["date_str"], "%d/%m/%Y")
                    except: 
                        pass
                
                # Create Object
                v = VideoStats(
                    owned_channel_id=ch.id,
                    video_title=rec["title"],
                    publish_date=rec["date_str"], # Store String directly
                    views=rec["views"],
                    watch_time_hours=rec["watch_time"],
                    subs_gained=rec["subs"],
                    subs_lost=rec["subs_lost"],
                    impressions=rec["impressions"],
                    ctr=rec["ctr"],
                    likes=rec["likes"],
                    like_ratio=rec["like_ratio"],
                    comments=rec["comments"],
                    shares=rec["shares"],
                    unique_viewers=rec["unique_viewers"],
                    new_viewers=rec["new_viewers"],
                    returning_viewers=rec["returning_viewers"],
                    hype_score=rec["hype_score"],
                    average_view_duration=rec["avg_duration"],
                    average_view_percentage=rec["avg_percentage"],
                    # New Fields
                    duration=rec["duration"],
                    active_views=rec["active_views"],
                    still_watching_percent=rec["still_watching_percent"],
                    avg_views_per_viewer=rec["avg_views_per_viewer"],
                    casual_viewers=rec["casual_viewers"],
                    regular_viewers=rec["regular_viewers"],
                    hypes=rec["hypes"],
                    subscribers_net=rec["subscribers_net"]
                )
                new_objects.append(v)
                
                total_views_calc += rec["views"]
                
                # 90 Day Logic
                if p_date_obj and p_date_obj >= cutoff_date:
                    views_90d_calc += rec["views"]
                
            session.add_all(new_objects)
            
            # --- 2. Update Channel Totals ---
            ch.sum_views = total_views_calc
            
            if views_90d_calc > 0:
                ch.m3_views = views_90d_calc
            elif total_views_calc > 0 and views_90d_calc == 0:
                 ch.m3_views = 0
            
            # Fallback: If fetcher returned legacy keys
            if stats.get("views_lifetime", 0) > ch.sum_views:
                ch.sum_views = stats["views_lifetime"]
            
            session.commit()
            print(f"  -> Saved {len(new_objects)} video stats. Total Views: {ch.sum_views}")
            
        except Exception as e:
            print(f"  [ERROR] DB Update failed: {e}")
            session.rollback()

            
    print("\n--- Update Complete ---")

if __name__ == "__main__":
    update_all_channels()
