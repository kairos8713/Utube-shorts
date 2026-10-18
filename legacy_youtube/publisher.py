import os
import time
import json
import datetime as dt
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from typing import Optional

class YouTubePublisher:
    def __init__(self, headless: bool = False):
        self.headless = headless

    def upload_video(self,
                     video_path: str,
                     title: str,
                     description: str,
                     tags: str,
                     js_path: str,
                     user_agent: str,
                     schedule_time: Optional[str] = None,
                     schedule_datetime: Optional[dt.datetime] = None):
        """
        Uploads a video to YouTube using Playwright.

        Args:
            video_path: Absolute path to the video file.
            title: Video title.
            description: Video description.
            tags: Comma-separated tags.
            js_path: Path to the storageState.json file.
            user_agent: User agent string to use.
            schedule_time: Legacy string param (unused).
            schedule_datetime: If provided, video will be scheduled for this datetime
                               instead of being published immediately.
        """
        if not os.path.exists(video_path):
            print(f"[ERROR] Video file not found: {video_path}")
            return False

        if not os.path.exists(js_path):
            print(f"[ERROR] Session file (js_path) not found: {js_path}")
            return False

        mode = f"Zamanlanmış ({schedule_datetime.strftime('%Y-%m-%d %H:%M')})" if schedule_datetime else "Anında Yayın"
        print(f"[PUBLISH] Başlatılıyor: {title} [{mode}]")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ]
            )

            try:
                context = browser.new_context(
                    storage_state=js_path,
                    user_agent=user_agent,
                    viewport={'width': 1280, 'height': 720},
                    locale='en-US'
                )
            except Exception as e:
                print(f"[ERROR] Storage state yüklenemedi: {e}")
                browser.close()
                return False

            page = context.new_page()

            # YouTube Studio'ya git
            try:
                page.goto("https://studio.youtube.com", timeout=60000)
                page.wait_for_load_state("networkidle")
            except PlaywrightTimeoutError:
                print("[ERROR] YouTube Studio yüklenemedi. Çerezler geçersiz olabilir.")
                browser.close()
                return False

            if "google.com/signin" in page.url:
                print("[ERROR] Giriş yapılmamış. Oturum dosyası süresi dolmuş olabilir.")
                browser.close()
                return False

            try:
                first = page.locator("body > div > div.buttons > a")
                first.click()
                time.sleep(3)
                # Upload butonunu bul ve tıkla
                create_button = page.locator("#upload-icon")
                if not create_button.is_visible():
                    create_button = page.locator("text=Create").first
                if not create_button.is_visible():
                    create_button = page.locator("text=Oluştur").first
                create_button.click()

                # Dosyayı seç
                print("[PUBLISH] Dosya seçiliyor...")
                page.wait_for_selector("input[type='file']", state="attached", timeout=20000)
                page.set_input_files("input[type='file']", video_path)
                print("[PUBLISH] Video yükleniyor...")
                time.sleep(3)

                # Başlık doldur
                try:
                    # Clean title — remove sector suffixes
                    clean_title = title
                    for suffix in [" | Riskli", " | riskli", "| Riskli", "| riskli"]:
                        clean_title = clean_title.replace(suffix, "").strip()
                    title = clean_title
                    print(f"[PUBLISH] Clean title: {title}")
                    title_field = page.locator("#textbox").first
                    title_field.click()
                    title_field.select_text()
                    title_field.fill(title)
                except Exception as e:
                    print(f"[WARN] Başlık doldurulamadı: {e}")

                # Açıklama doldur
                try:
                    if description:
                        desc_field = page.locator("#textbox").nth(1)
                        if desc_field.is_visible():
                            desc_field.click()
                            desc_field.select_text()
                            desc_field.fill(description)
                except Exception as e:
                    print(f"[WARN] Açıklama doldurulamadı: {e}")

                # İleri -> Video Elements
                page.locator("#next-button").click()
                time.sleep(2)

                # İleri -> Checks
                page.locator("#next-button").click()
                time.sleep(2)

                # İleri -> Visibility (Son adım)
                page.locator("#next-button").click()
                time.sleep(3)

                print("[PUBLISH] Görünürlük adımına ulaşıldı...")
                schedule_div = page.locator("#second-container > div.early-access-header.style-scope.ytcp-video-visibility-select")
                schedule_div.click()
                time.sleep(3)
                second = page.locator("#publish-from-private-non-sponsor-selector > div > ytcp-datetime-picker > div > div.date-timezone-container.style-scope.ytcp-datetime-picker")
                second.click()
                time.sleep(3)

                TR_MONTHS = {
                    1: 'Oca', 2: 'Şub', 3: 'Mar', 4: 'Nis',
                    5: 'May', 6: 'Haz', 7: 'Tem', 8: 'Ağu',
                    9: 'Eyl', 10: 'Eki', 11: 'Kas', 12: 'Ara'
                }

                date_str = f"{schedule_datetime.day} {TR_MONTHS[schedule_datetime.month]} {schedule_datetime.year}"

                date_field = page.locator("#input-2 > input")
                date_field.fill(date_str)
                time.sleep(3)
                page.keyboard.press("Escape")

                time_field = page.locator("#input-1 > input")
                time_field.fill(schedule_datetime.strftime('%H:%M'))
                time.sleep(3)

                # Done / Zamanla butonuna bas
                time.sleep(2)
                done_btn = page.locator("#done-button")
                done_btn.wait_for(state="visible", timeout=15000)
                done_btn.click()
                print("[PUBLISH] ✓ İşlem tamamlandı!")

                # Kapanma dialogunu bekle
                try:
                    page.wait_for_selector("#close-icon-button", timeout=60000)
                    close_btn = page.locator("#close-icon-button")
                    if close_btn.is_visible():
                        close_btn.click()
                except Exception:
                    print("[WARN] Kapat butonu bulunamadı.")

            except Exception as e:
                print(f"[ERROR] Yükleme sırasında hata: {e}")
                try:
                    page.screenshot(path="upload_error.png")
                except Exception:
                    pass
                browser.close()
                return False

            context.close()
            browser.close()
            # NOTE: Playwright-based upload cannot capture the YouTube video ID
            # directly — YouTube Studio doesn't expose it in the DOM after upload.
            # The yt_video_id field on the videos row gets populated automatically
            # on the next analytics sync (every 30 min) via youtube_analytics.py,
            # which matches by title and stamps videos.yt_video_id = the API-returned ID.
            return True

    def _set_schedule(self, page, schedule_datetime: dt.datetime) -> bool:
        """
        YouTube Studio görünürlük adımında 'Zamanlanmış' seçeneğini seçer
        ve tarih/saat alanlarını doldurur.
        Returns True if successful.
        """
        try:
            import datetime as _dt
            if schedule_datetime <= _dt.datetime.utcnow():
                print(f"[WARN] Schedule date in past: {schedule_datetime} — moving to tomorrow")
                schedule_datetime = (_dt.datetime.utcnow() + _dt.timedelta(days=1)).replace(
                    hour=15, minute=0, second=0, microsecond=0
                )
            # "Zamanlanmış" radio butonunu bul ve seç
            schedule_selectors = [
                "tp-yt-paper-radio-button[name='SCHEDULE']",
                "ytcp-icon-button[aria-label*='zamanla']",
                "ytcp-icon-button[aria-label*='Schedule']",
            ]
            schedule_selected = False
            for sel in schedule_selectors:
                try:
                    btn = page.locator(sel)
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        schedule_selected = True
                        break
                except Exception:
                    continue

            if not schedule_selected:
                # Metin bazlı arama
                for text in ["Zamanlanmış", "Schedule for", "Schedule"]:
                    try:
                        btn = page.get_by_text(text, exact=False)
                        if btn.first.is_visible(timeout=2000):
                            btn.first.click()
                            schedule_selected = True
                            break
                    except Exception:
                        continue

            if not schedule_selected:
                print("[WARN] Zamanlama seçeneği bulunamadı")
                return False

            time.sleep(1.5)

            # Tarih formatı (en-US locale ile): MM/DD/YYYY
            date_str = schedule_datetime.strftime("%m/%d/%Y")
            # Saat formatı: HH:MM AM/PM
            time_str = schedule_datetime.strftime("%I:%M %p")

            print(f"[SCHEDULE] Tarih: {date_str}, Saat: {time_str}")

            # Tarih alanını doldur
            date_filled = False
            date_selectors = [
                "#datepicker-trigger input",
                "input[placeholder*='date']",
                "input[placeholder*='Date']",
                "ytcp-date-picker input",
            ]
            for sel in date_selectors:
                try:
                    date_input = page.locator(sel).first
                    if date_input.is_visible(timeout=2000):
                        date_input.triple_click()
                        date_input.fill(date_str)
                        date_input.press("Tab")
                        date_filled = True
                        break
                except Exception:
                    continue

            if not date_filled:
                print("[WARN] Tarih alanı doldurulamadı")

            time.sleep(0.8)

            # Saat alanını doldur
            time_filled = False
            time_selectors = [
                "#time-of-day-picker input",
                "ytcp-time-of-day-picker input",
                "input[placeholder*='time']",
                "input[placeholder*='Time']",
            ]
            for sel in time_selectors:
                try:
                    time_input = page.locator(sel).first
                    if time_input.is_visible(timeout=2000):
                        time_input.triple_click()
                        time_input.fill(time_str)
                        time_input.press("Tab")
                        time_filled = True
                        break
                except Exception:
                    continue

            if not time_filled:
                print("[WARN] Saat alanı doldurulamadı")

            time.sleep(0.8)
            return True

        except Exception as e:
            print(f"[WARN] _set_schedule hatası: {e}")
            return False

    def get_channel_stats(self, js_path: str, user_agent: str):
        """
        Scrapes YouTube Studio for:
        - Total Subscribers
        - Views (Last 28 days from Dashboard Summary)
        - Lifetime Views (from Analytics or estimate)
        """
        if not os.path.exists(js_path):
            print(f"[ERROR] Session file not found: {js_path}")
            return None
            
        print("[STATS] Fetching stats...")
        stats = {"subs": 0, "views_28d": 0, "views_lifetime": 0}
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            
            try:
                context = browser.new_context(storage_state=js_path, user_agent=user_agent)
                page = context.new_page()
                
                # 1. Dashboard (For Subs & 28d Views)
                page.goto("https://studio.youtube.com", timeout=60000)
                page.wait_for_load_state("networkidle")
                
                # Check Login
                if "google.com/signin" in page.url:
                    print("[ERROR] Session expired.")
                    browser.close()
                    return None
                
                # --- SCRAPE DASHBOARD ---
                # Subscribers
                # Usually in a card 'Current subscribers' -> value
                # Locator strategy: Look for text "Current subscribers" then find the number nearby
                try:
                    # Generic strategy: Get all text and parse? Or specific ID.
                    # ID often: #metric-value-big or class style-scope ytcp-analytics-card
                    
                    # Try to find the "Channel analytics" card
                    # Inside that, "Current subscribers" -> number
                    
                    # Fallback: Just dump text of the analytics card
                    card = page.locator("ytcp-analytics-card").first
                    if card.is_visible():
                        text_content = card.inner_text()
                        # Parse simple integers from known patterns
                        # "Current subscribers\n1,234"
                        lines = text_content.split('\n')
                        for i, line in enumerate(lines):
                            if "subscribers" in line.lower() and i+1 < len(lines):
                                try:
                                    stats["subs"] = int(lines[i+1].replace(',', '').replace('.', ''))
                                except: pass
                            
                            if "views" in line.lower() and i+1 < len(lines):
                                try:
                                    # "Views\n1.5K" -> need parsing logic for K/M?
                                    # For now assume full number or simple parse
                                    val_str = lines[i+1]
                                    multiplier = 1
                                    if 'K' in val_str: multiplier = 1000; val_str = val_str.replace('K','')
                                    elif 'M' in val_str: multiplier = 1000000; val_str = val_str.replace('M','')
                                    stats["views_28d"] = int(float(val_str.replace(',', '')) * multiplier)
                                except: pass
                except Exception as e:
                    print(f"[WARN] Dashboard scrape error: {e}")
                    
                # 2. Analytics (For Lifetime Views)
                # https://studio.youtube.com/channel/CHANNEL_ID/analytics/tab-overview/period-lifetime
                # accessing via URL is tricky if we don't know channel ID, but usually studio redirects correctly if we omit it?
                # No, studio url is /channel/[ID]/...
                # We can get ID from current URL
                
                current_url = page.url
                # https://studio.youtube.com/channel/UCxxxxxxxx
                if "/channel/" in current_url:
                    chan_id = current_url.split("/channel/")[1].split("/")[0]
                    analytics_url = f"https://studio.youtube.com/channel/{chan_id}/analytics/tab-overview/period-lifetime"
                    page.goto(analytics_url, timeout=60000)
                    page.wait_for_load_state("networkidle")
                    time.sleep(2) # Wait for animation
                    
                    # The main metric card usually shows the total views
                    # Look for "Views" big number
                    try:
                        # Metric card again
                        main_metric = page.locator(".total-value").first
                        if not main_metric.is_visible():
                             main_metric = page.locator("#metric-value").first
                        
                        if main_metric.is_visible():
                            val_str = main_metric.inner_text()
                            multiplier = 1
                            if 'K' in val_str: multiplier = 1000; val_str = val_str.replace('K','')
                            elif 'M' in val_str: multiplier = 1000000; val_str = val_str.replace('M','')
                            stats["views_lifetime"] = int(float(val_str.replace(',', '').replace('.','')) * multiplier)
                    except: pass
                
            except Exception as e:
                print(f"[ERROR] Stats fetch failed: {e}")
            finally:
                browser.close()
                
        print(f"[STATS] Result: {stats}")
        return stats

if __name__ == "__main__":
    # Test Run
    print("running test publisher...")
    
    # 1. Create a dummy video file if needed
    import uuid
    dummy_path = os.path.abspath("test_video.mp4")
    if not os.path.exists(dummy_path):
        with open(dummy_path, "wb") as f:
            f.write(b"0" * 1024 * 1024) # 1MB dummy file, might fail youtube processing but enough for UI test
    
    # 2. Load config
    # We need a session file.
    session_file = os.path.join("sessions", "channel_1_session.json")
    if not os.path.exists(session_file):
        print(f"Session file not found: {session_file}")
    else:
        pub = YouTubePublisher(headless=False)
        # Test Upload
        # pub.upload_video(
        #     video_path=dummy_path,
        #     title=f"Test Upload {uuid.uuid4().hex[:6]}",
        #     description="Automated Test",
        #     tags="test",
        #     js_path=os.path.abspath(session_file),
        #     user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        # )
        
        # Test Stats
        pub.get_channel_stats(os.path.abspath(session_file), "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
