from publisher import YouTubePublisher
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import DB_PATH, OwnedChannels
import os
from playwright.sync_api import sync_playwright

def verify_session(channel_id):
    engine = create_engine(DB_PATH)
    Session = sessionmaker(bind=engine)
    session = Session()

    channel = session.query(OwnedChannels).get(channel_id)
    if not channel:
        print(f"Channel {channel_id} not found.")
        return

    print(f"Testing session for: {channel.mail}")
    print(f"Session Path: {channel.js_path}")

    if not os.path.exists(channel.js_path):
        print("Session file does not exist!")
        return

    print("Launching browser to manifest login status...")
    
    with sync_playwright() as p:
        # Launch with head to see
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=channel.js_path)
        page = context.new_page()

        try:
            page.goto("https://studio.youtube.com")
            page.wait_for_load_state("networkidle")
            
            # Check for current URL or specific element
            print(f"Current URL: {page.url}")
            
            if "studio.youtube.com" in page.url:
                print("SUCCESS: Logged in and reached Studio!")
            elif "accounts.google.com" in page.url:
                print("FAILURE: Redirected to Login Page. Session invalid.")
            else:
                print("UNKNOWN: Reached unexpected page.")

            # Keep open for a moment
            page.wait_for_timeout(5000)
            
        except Exception as e:
            print(f"Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    # Test for the first channel or ask user
    import sys
    if len(sys.argv) > 1:
        cid = int(sys.argv[1])
    else:
        # Default to ID 1
        cid = 1
    verify_session(cid)
