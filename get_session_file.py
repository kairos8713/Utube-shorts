from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import OwnedChannels, DB_PATH
import os

import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-id", type=int, help="ID of the channel to login for")
    args = parser.parse_args()

    engine = create_engine(DB_PATH)
    Session = sessionmaker(bind=engine)
    session = Session()

    print("=== YouTube Session Generator ===")

    if args.channel_id:
        selected_channel = session.get(OwnedChannels, args.channel_id)
        if not selected_channel:
            print(f"Channel ID {args.channel_id} not found.")
            return
    else:
        channels = session.query(OwnedChannels).all()

        if not channels:
            print("No channels found in database.")
            return

        print("Available Channels:")
        for idx, ch in enumerate(channels):
            print(f"{idx + 1}. ID: {ch.id} | Email: {ch.mail}")

        try:
            selection = int(input("\nSelect channel number to login: ")) - 1
            if selection < 0 or selection >= len(channels):
                print("Invalid selection.")
                return
            selected_channel = channels[selection]
        except ValueError:
            print("Invalid input.")
            return

    print(f"\n--- Login Details for Channel {selected_channel.id} ---")
    print(f"Email:    {selected_channel.mail}")
    print(f"Password: {selected_channel.password}")
    print("------------------------------------------")
    
    print("------------------------------------------")
    js_path = selected_channel.js_path

    # Auto-generate path if missing
    if not js_path:
        sessions_dir = os.path.join(os.getcwd(), "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        new_path = os.path.join(sessions_dir, f"channel_{selected_channel.id}_session.json")
        selected_channel.js_path = new_path
        session.commit()
        js_path = new_path
        
    os.makedirs(os.path.dirname(os.path.abspath(js_path)), exist_ok=True)

    print("\n[INSTRUCTION] Opening Browser...")
    print("1. Enter your email/password in the browser.")
    print("2. If Google says 'Browser not secure', try waiting or using a differnet method.")
    print("3. After success, come back here and press ENTER.")

    import sys
    import subprocess
    print(f"[DEBUG] Python Executable: {sys.executable}")
    # print(f"[DEBUG] Sys Path: {sys.path}")

    try:
        # Based on dir() output, it seems 'stealth' is the function or class available
        from playwright_stealth import stealth
    except ImportError:
        print("[WARNING] playwright-stealth missing. Attempting auto-install...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright-stealth"])
            from playwright_stealth import stealth
            print("[INFO] Auto-install successful.")
        except Exception as e:
            print(f"[CRITICAL] Could not install playwright-stealth: {e}")
            return

    try:
        with sync_playwright() as p:
            print("[INFO] Launching Chromium (bundled)...")
            browser = p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled', '--start-maximized']
            )
            context = browser.new_context(no_viewport=True)
            
            # Apply stealth
            # Check if stealth is a function or class
            if callable(stealth):
                stealth(context)
            else:
                print("[WARNING] 'stealth' is not callable? trying manual injection fallback just in case.")
            
            page = context.new_page()

            page.goto("https://accounts.google.com/ServiceLogin?service=youtube", timeout=60000)
            
            try:
                page.fill('input[type="email"]', selected_channel.mail)
            except:
                pass

            input("\n>>> PRESS ENTER AFTER LOGIN <<<")
            
            context.storage_state(path=js_path)
            print(f"\n[SUCCESS] Session saved to: {js_path}")
            browser.close()

    except Exception as e:
        print("\n[CRITICAL ERROR] Could not launch or connect to browser.")
        traceback.print_exc()
        print("\nPossible fix: Ensure 'playwright install chromium' was run.")

if __name__ == "__main__":
    main()
