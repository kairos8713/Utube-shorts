import os
import time
from playwright.sync_api import sync_playwright

def get_channel_stats(js_path: str, user_agent: str = None):
    """
    Scrapes YouTube Studio for:
    - Total Subscribers
    - Views (Last 28 days from Dashboard Summary)
    - Lifetime Views (from Analytics or estimate)
    """
    if not os.path.exists(js_path):
        print(f"[ERROR] Session file not found: {js_path}")
        return None
        
    print(f"[STATS] Fetching stats using {os.path.basename(js_path)}...")
    stats = {"subs": 0, "views_28d": 0, "views_lifetime": 0}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
        )
        
        try:
            # Use default UA if none provided
            if not user_agent:
                user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                
            context = browser.new_context(storage_state=js_path, user_agent=user_agent)
            page = context.new_page()
            
            # 1. Dashboard (For Subs & 28d Views)
            print("[STATS] detailed log: navigating to studio.youtube.com")
            try:
                page.goto("https://studio.youtube.com", timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                if "ERR_ABORTED" in str(e) or "frame was detached" in str(e).lower():
                    print(f"[WARN] Navigation aborted (likely redirect). Continuing... ({e})")
                else:
                    raise
            
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception as e:
                print(f"[WARN] networkidle wait timed out or failed: {e}")
            
            # Check Login
            if "google.com/signin" in page.url:
                print("[ERROR] Session expired. Please re-login.")
                browser.close()
                return None
            
            # --- SCRAPE DASHBOARD ---
            print("[STATS] detailed log: scanning dashboard cards...")
            
            # Strategy: Look for the metrics card usually on the right or top left
            # We will use text-based search to be more robust against css class changes
            
            # SUBSCRIBERS
            # Search for "Current subscribers"
            try:
                # Find element containing text "Current subscribers"
                # The number is usually in a sibling or child container.
                # Let's try grabbing the entire analytics card text.
                card = page.locator("ytcp-analytics-card").first
                if card.is_visible():
                    all_text = card.inner_text()
                    # print(f"[DEBUG] Card Text:\n{all_text}")
                    
                    lines = [l.strip() for l in all_text.split('\n') if l.strip()]
                    
                    for i, line in enumerate(lines):
                        # LOOKING FOR SUBSCRIBERS
                        if "subscribers" in line.lower():
                            # The number should be nearby (usually next line)
                            # Example: "Current subscribers" -> "123"
                            if i + 1 < len(lines):
                                val_str = lines[i+1]
                                # Clean string
                                val_clean = val_str.replace(',', '').replace('.', '')
                                if val_clean.isdigit():
                                    stats["subs"] = int(val_clean)
                                    print(f"  [FOUND] Subscribers: {stats['subs']}")
                                    
                        # LOOKING FOR 28D VIEWS
                        # Example: "Views", "1.2K" (often with an arrow or percent)
                        if line.lower() == "views":
                             if i + 1 < len(lines):
                                val_str = lines[i+1]
                                # Check if it's a number (handles K/M)
                                if any(c.isdigit() for c in val_str):
                                    multiplier = 1
                                    clean_val = val_str.upper().replace(',','')
                                    if 'K' in clean_val: multiplier = 1000; clean_val = clean_val.replace('K','')
                                    elif 'M' in clean_val: multiplier = 1000000; clean_val = clean_val.replace('M','')
                                    elif 'B' in clean_val: multiplier = 1000000000; clean_val = clean_val.replace('B','')
                                    
                                    try:
                                        stats["views_28d"] = int(float(clean_val) * multiplier)
                                        print(f"  [FOUND] 28d Views: {stats['views_28d']}")
                                    except: pass

            except Exception as e:
                print(f"[WARN] Dashboard parse error: {e}")
                
            # 2. Analytics (For Lifetime Views)
            # Need to go to Analytics tab -> Lifetime
            
            current_url = page.url
            if "/channel/" in current_url:
                chan_id = current_url.split("/channel/")[1].split("/")[0]
                # Construct Analytics URL
                # "analytics/tab-overview/period-lifetime"
                # Construct complex URL, injecting dynamic channel ID twice (entity_id and path)
                # We replace the hardcoded ID with {chan_id}
                analytics_url = (
                    f"https://studio.youtube.com/channel/{chan_id}/analytics/tab-overview/period-default/explore"
                    f"?entity_type=CHANNEL&entity_id={chan_id}&time_period=lifetime"
                    "&explore_type=TABLE_AND_CHART&chart_type=LINE_CHART&metric=ENGAGED_VIEWS"
                    "&granularity=DAY&t_metrics=ENGAGED_VIEWS&t_metrics=AVERAGE_WATCH_TIME&t_metrics=AVERAGE_WATCH_PERCENTAGE"
                    "&t_metrics=VIDEO_COUNT_NEW&t_metrics=VIDEO_COUNT_FIRST_PUBLISHED&t_metrics=SHORTS_FEED_IMPRESSIONS_VTR"
                    "&t_metrics=ESTIMATED_UNIQUE_VIEWERS&t_metrics=AVERAGE_VIEWS_PER_VIEWER&t_metrics=RECENT_VIEWERS"
                    "&t_metrics=RETURNING_VIEWERS&t_metrics=OCCASIONAL_VIEWERS&t_metrics=FREQUENT_VIEWERS&t_metrics=HYPES"
                    "&t_metrics=HYPE_POINTS&t_metrics=SUBSCRIBERS_GAINED&t_metrics=SUBSCRIBERS_LOST&t_metrics=RATINGS_LIKES"
                    "&t_metrics=LIKES_PER_LIKES_PLUS_DISLIKES_PERCENT&t_metrics=SHARINGS&t_metrics=COMMENTS&t_metrics=EXTERNAL_VIEWS"
                    "&t_metrics=EXTERNAL_WATCH_TIME&t_metrics=SUBSCRIBERS_NET_CHANGE&t_metrics=TOTAL_ESTIMATED_EARNINGS"
                    "&t_metrics=VIDEO_THUMBNAIL_IMPRESSIONS&t_metrics=VIDEO_THUMBNAIL_IMPRESSIONS_VTR"
                    "&dimension=VIDEO&o_column=ENGAGED_VIEWS&o_direction=ANALYTICS_ORDER_DIRECTION_DESC"
                )
                print(f"[STATS] detailed log: navigating to analytics (likely ZIP export): {analytics_url}")
                
                try:
                    page.goto(analytics_url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    if "ERR_ABORTED" in str(e) or "frame was detached" in str(e).lower():
                        print(f"[WARN] Analytics navigation aborted (likely redirect). Continuing... ({e})")
                    else:
                        raise
                        
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception as e:
                    print(f"[WARN] Analytics networkidle wait timed out or failed: {e}")
                    
                time.sleep(5) # Allow rendering
                
                try:
                    # Click Export Icon
                    print("[STATS] detailed log: looking for export button...")
                    # Try all possible selectors for export button (TR + EN aria-label)
                    export_btn = page.locator(
                        "button[aria-label*='dışa aktar'], "
                        "button[aria-label*='Export'], "
                        "ytcp-icon-button[aria-label*='dışa aktar'], "
                        "ytcp-icon-button[aria-label*='Export'], "
                        "#export-button"
                    ).first


                    try:
                        export_btn.scroll_into_view_if_needed()
                        page.wait_for_timeout(500)
                        export_btn.click(force=True)
                        print("[STATS] Export clicked.")
                    except Exception as click_err:
                        print(f"[WARN] Export button click failed: {click_err}")
                        raise

                    page.wait_for_timeout(1500)  # wait for dropdown menu to open

                    # Click CSV option — force=True bypasses tp-yt-iron-overlay-backdrop
                    with page.expect_download(timeout=60000) as download_info:
                        page.locator("#text-item-1").first.click(force=True)

                    download = download_info.value

                    # Save to temp
                    downloads_dir = os.path.join(os.getcwd(), "downloads")
                    os.makedirs(downloads_dir, exist_ok=True)

                    # Use suggested filename to handle extension properly (.zip or .csv)
                    suggested_name = download.suggested_filename
                    save_path = os.path.join(downloads_dir, suggested_name)
                    download.save_as(save_path)
                    print(f"  [DOWNLOAD] Saved to: {save_path}")

                    # HANDLE ZIP vs CSV
                    import zipfile
                    import csv

                    csv_file_path = save_path

                    if save_path.lower().endswith('.zip'):
                        print("  [ZIP] extracting...")
                        with zipfile.ZipFile(save_path, 'r') as zip_ref:
                            zip_ref.extractall(downloads_dir)
                            extracted = zip_ref.namelist()
                            csv_files = [f for f in extracted if f.lower().endswith('.csv')]
                            if csv_files:
                                csv_file_path = os.path.join(downloads_dir, csv_files[0])
                                print(f"  [ZIP] Found CSV: {csv_files[0]}")

                    # Parse CSV
                    if os.path.exists(csv_file_path) and csv_file_path.lower().endswith('.csv'):
                        with open(csv_file_path, 'r', encoding='utf-8-sig') as f:
                            reader = csv.DictReader(f)
                            total_views_csv = 0
                            video_records = []
                            rows = list(reader)
                            if rows:
                                keys = list(rows[0].keys())
                                print(f"  [CSV] Columns: {keys[:5]}...")

                                # Key Mapping (Flexible for EN/TR)
                                k_title      = next((k for k in keys if "başlığı" in k.lower() or "title" in k.lower()), None)
                                k_date       = next((k for k in keys if "tarihi" in k.lower() or "publish time" in k.lower()), None)
                                k_views      = next((k for k in keys if k.strip() == "Görüntüleme" or k.strip() == "Views"), None)
                                k_watch      = next((k for k in keys if "saat" in k.lower() or "hours" in k.lower()), None)
                                k_subs       = next((k for k in keys if "kazanılan abone" in k.lower() or "subscribers gained" in k.lower()), None)
                                k_subs_lost  = next((k for k in keys if "kaybedilen abone" in k.lower() or "subscribers lost" in k.lower()), None)
                                k_imp        = next((k for k in keys if "gösterimler" in k.lower() or "impressions" in k.lower()), None)
                                k_ctr        = next((k for k in keys if "tıklama oranı" in k.lower() or "click-through" in k.lower()), None)
                                k_likes      = next((k for k in keys if "beğenenler" in k.lower() or k.lower() == "likes"), None)
                                k_like_ratio = next((k for k in keys if "oranı" in k.lower() and "beğenme" in k.lower()), None)
                                k_comments   = next((k for k in keys if "yorum" in k.lower() or "comments" in k.lower()), None)
                                k_shares     = next((k for k in keys if "paylaşım" in k.lower() or "shares" in k.lower()), None)
                                k_unique     = next((k for k in keys if "benzersiz" in k.lower() or "unique" in k.lower()), None)
                                k_new        = next((k for k in keys if "yeni izleyici" in k.lower() or "new viewers" in k.lower()), None)
                                k_returning  = next((k for k in keys if "geri gelen" in k.lower() or "returning" in k.lower()), None)
                                k_avg_dur    = next((k for k in keys if "ortalama görüntüleme süresi" in k.lower() or "average view duration" in k.lower()), None)
                                k_avg_pct    = next((k for k in keys if "yüzdesi" in k.lower() or "percentage" in k.lower()), None)
                                k_hype       = next((k for k in keys if "hype" in k.lower() and "puan" in k.lower()), None)
                                k_duration         = next((k for k in keys if "süre" in k.lower() or "duration" in k.lower()), None)
                                k_active_views     = next((k for k in keys if "aktif" in k.lower() and "izlenme" in k.lower()), None)
                                k_still_watching   = next((k for k in keys if "devam edenler" in k.lower()), None)
                                k_avg_per_viewer   = next((k for k in keys if "izleyici başına" in k.lower()), None)
                                k_casual           = next((k for k in keys if "sıradan" in k.lower()), None)
                                k_regular          = next((k for k in keys if "düzenli" in k.lower()), None)
                                k_hypes_count      = next((k for k in keys if k.lower() == "hype"), None)
                                k_subs_net         = next((k for k in keys if k.lower() == "aboneler" or k.lower() == "subscribers"), None)

                                if k_title and k_views:
                                    print(f"  [CSV] Mapping: Title='{k_title}', Views='{k_views}'")

                                    months_map = {
                                        'Oca': 1, 'Şub': 2, 'Mar': 3, 'Nis': 4, 'May': 5, 'Haz': 6,
                                        'Tem': 7, 'Ağu': 8, 'Eyl': 9, 'Eki': 10, 'Kas': 11, 'Ara': 12,
                                        'Jan': 1, 'Feb': 2, 'Apr': 4, 'Jun': 6, 'Jul': 7, 'Aug': 8,
                                        'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
                                    }

                                    def parse_date_flexible(d_str):
                                        if not d_str: return None
                                        try:
                                            d_clean = d_str.replace(",", "")
                                            parts = d_clean.split()
                                            day, month, year = 0, 0, 0
                                            if len(parts) == 3 and parts[0].isdigit():
                                                day = int(parts[0]); month_str = parts[1]; year = int(parts[2])
                                                month = months_map.get(month_str[:3], 1)
                                            elif len(parts) == 3 and parts[1].isdigit():
                                                month_str = parts[0]; day = int(parts[1]); year = int(parts[2])
                                                month = months_map.get(month_str[:3], 1)
                                            if day > 0:
                                                return f"{day:02d}/{month:02d}/{year}"
                                        except:
                                            return None
                                        return d_str

                                    def clean_num(val, is_float=False):
                                        if not val: return 0.0 if is_float else 0
                                        v = val.replace('%', '').replace('.', '').replace(',', '.')
                                        try:
                                            return float(v) if is_float else int(float(v))
                                        except:
                                            return 0

                                    for row in rows:
                                        try:
                                            r_title = row.get(k_title, "Unknown")
                                            if r_title.lower() in ("toplam", "total"): continue
                                            r_date_raw = row.get(k_date, "")
                                            p_date     = parse_date_flexible(r_date_raw)
                                            r_views        = clean_num(row.get(k_views, "0"))
                                            r_watch        = clean_num(row.get(k_watch, "0"), True)
                                            r_subs         = clean_num(row.get(k_subs, "0"))
                                            r_subs_lost    = clean_num(row.get(k_subs_lost, "0"))
                                            r_imp          = clean_num(row.get(k_imp, "0"))
                                            r_ctr          = clean_num(row.get(k_ctr, "0"), True)
                                            r_likes        = clean_num(row.get(k_likes, "0"))
                                            r_comments     = clean_num(row.get(k_comments, "0"))
                                            r_shares       = clean_num(row.get(k_shares, "0"))
                                            r_unique       = clean_num(row.get(k_unique, "0"))
                                            r_new          = clean_num(row.get(k_new, "0"))
                                            r_returning    = clean_num(row.get(k_returning, "0"))
                                            r_hype         = clean_num(row.get(k_hype, "0"), True)
                                            r_like_ratio   = clean_num(row.get(k_like_ratio, "0"), True)
                                            r_avg_dur      = row.get(k_avg_dur, "0:00")
                                            r_avg_pct      = clean_num(row.get(k_avg_pct, "0"), True)
                                            r_dur              = row.get(k_duration, "0:00")
                                            r_active_views     = clean_num(row.get(k_active_views, "0"))
                                            r_still_watching   = clean_num(row.get(k_still_watching, "0"), True)
                                            r_avg_per_viewer   = clean_num(row.get(k_avg_per_viewer, "0"), True)
                                            r_casual           = clean_num(row.get(k_casual, "0"))
                                            r_regular          = clean_num(row.get(k_regular, "0"))
                                            r_hypes_count      = clean_num(row.get(k_hypes_count, "0"))
                                            r_subs_net         = clean_num(row.get(k_subs_net, "0"))
                                            video_records.append({
                                                "title": r_title, "date_str": p_date,
                                                "views": r_views, "watch_time": r_watch,
                                                "subs": r_subs, "subs_lost": r_subs_lost,
                                                "impressions": r_imp, "ctr": r_ctr,
                                                "likes": r_likes, "like_ratio": r_like_ratio,
                                                "comments": r_comments, "shares": r_shares,
                                                "unique_viewers": r_unique, "new_viewers": r_new,
                                                "returning_viewers": r_returning, "hype_score": r_hype,
                                                "avg_duration": r_avg_dur, "avg_percentage": r_avg_pct,
                                                "duration": r_dur, "active_views": r_active_views,
                                                "still_watching_percent": r_still_watching,
                                                "avg_views_per_viewer": r_avg_per_viewer,
                                                "casual_viewers": r_casual, "regular_viewers": r_regular,
                                                "hypes": r_hypes_count, "subscribers_net": r_subs_net,
                                            })
                                            total_views_csv += r_views
                                        except Exception as e:
                                            pass

                                    stats["views_lifetime"] = total_views_csv
                                    stats["video_records"]  = video_records
                                    print(f"  [FOUND] Lifetime Views (CSV Sum): {total_views_csv}")
                                    print(f"  [FOUND] Processed {len(video_records)} videos.")
                                else:
                                    print(f"[WARN] Could not identify Title/Views columns. Found: {keys}")


                except Exception as e:
                    print(f"[WARN] Export download failed: {e}")

        except Exception as e:
            print(f"[ERROR] Stats fetch critical fail: {e}")
        finally:
            browser.close()
            
    print(f"[STATS] Final Result: {stats}")
    return stats

if __name__ == "__main__":
    # Self-test if run directly
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="Path to session json")
    args = parser.parse_args()
    
    if args.json:
        get_channel_stats(args.json)
    else:
        print("Run with --json [codepath] to test.")
