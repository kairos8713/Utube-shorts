# get_channel_ids.py
import os, glob
from playwright.sync_api import sync_playwright

sessions = sorted(glob.glob('sessions/channel_*_session.json'))

results = {}
for session_file in sessions:
    ch_num = session_file.split('channel_')[1].split('_session')[0]
    print(f"[CH{ch_num}] Opening session...")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(storage_state=session_file)
            page = context.new_page()
            page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
            
            # Sayfanın channel URL'e redirect etmesini bekle
            try:
                page.wait_for_url("**/channel/**", timeout=15000)
            except:
                pass
            
            url = page.url
            
            # URL'de yoksa JS'den almayı dene
            if '/channel/' not in url:
                try:
                    channel_id = page.evaluate("""
                        () => {
                            const meta = document.querySelector('meta[name="google-signin-client_id"]');
                            const link = document.querySelector('a[href*="/channel/"]');
                            if (link) {
                                const match = link.href.match(/\\/channel\\/(UC[^/]+)/);
                                if (match) return match[1];
                            }
                            const match = window.location.href.match(/\\/channel\\/(UC[^/]+)/);
                            if (match) return match[1];
                            return null;
                        }
                    """)
                    if channel_id:
                        url = f"https://studio.youtube.com/channel/{channel_id}"
                except:
                    pass
            
            if '/channel/' in url:
                channel_id = url.split('/channel/')[1].split('/')[0]
                results[int(ch_num)] = channel_id
                print(f"  -> Ch{ch_num}: {channel_id}")
            else:
                # Son çare: analytics sayfasına git
                try:
                    page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(3000)
                    url2 = page.url
                    if '/channel/' in url2:
                        channel_id = url2.split('/channel/')[1].split('/')[0]
                        results[int(ch_num)] = channel_id
                        print(f"  -> Ch{ch_num}: {channel_id} (from analytics)")
                    else:
                        print(f"  -> Ch{ch_num}: FAILED, URL={url2}")
                except Exception as e2:
                    print(f"  -> Ch{ch_num}: FAILED, {e2}")
            
            browser.close()
    except Exception as e:
        print(f"  -> Error: {e}")

# DB'ye yaz
if results:
    import sqlite3
    con = sqlite3.connect('automation.db')
    cur = con.cursor()
    for ch_id, yt_id in results.items():
        cur.execute("UPDATE owned_channels SET youtube_channel_id=? WHERE id=?", (yt_id, ch_id))
        print(f"Saved: Ch{ch_id} → {yt_id}")
    con.commit()
    con.close()
    print(f"\nDone! {len(results)} channels updated.")
else:
    print("No channels found!")