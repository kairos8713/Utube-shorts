import os
import sys
import glob
import subprocess
import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)

from models import DB_PATH, OwnedChannels, Sectors, Channels

app = Flask(__name__, template_folder='ig_templates', static_folder='ig_static')
app.secret_key = 'ig_dashboard_secret_todo_auth' # TODO: Add real auth for RPi deployment

engine = create_engine(DB_PATH, echo=False)
Session = sessionmaker(bind=engine)

PENDING_BASE = os.path.join(ROOT_DIR, "pending")
PUBLISHED_BASE = os.path.join(ROOT_DIR, "published")
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

def get_video_info(filepath):
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    duration = 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0:
            import json
            duration = float(json.loads(r.stdout)["format"]["duration"])
    except:
        pass
    return {"name": os.path.basename(filepath), "size_mb": round(size_mb, 2), "duration": round(duration, 1)}

def get_account_file_stats(account_id):
    pending_dir = os.path.join(PENDING_BASE, str(account_id))
    published_dir = os.path.join(PUBLISHED_BASE, str(account_id))
    
    pending = []
    if os.path.isdir(pending_dir):
        pending = [f for f in os.listdir(pending_dir) if f.endswith('.mp4')]
        
    published = []
    last_pub = None
    if os.path.isdir(published_dir):
        published = [f for f in os.listdir(published_dir) if f.endswith('.mp4')]
        if published:
            latest = max([os.path.join(published_dir, f) for f in published], key=os.path.getctime)
            last_pub = datetime.datetime.fromtimestamp(os.path.getctime(latest)).strftime("%Y-%m-%d %H:%M")
            
    return len(pending), len(published), last_pub

@app.route('/')
def dashboard():
    session = Session()
    accounts = session.query(OwnedChannels).all()
    
    total_accs = len(accounts)
    active_accs = sum(1 for a in accounts if a.is_active)
    total_pend = 0
    total_pub = 0
    
    acc_data = []
    for a in accounts:
        p_c, pub_c, last_p = get_account_file_stats(a.id)
        total_pend += p_c
        total_pub += pub_c
        acc_data.append({
            "id": a.id,
            "ig_account_id": a.ig_account_id,
            "accent": a.accent_color_1,
            "is_active": a.is_active,
            "pending": p_c,
            "published": pub_c,
            "last_published": last_p
        })
        
    session.close()
    return render_template('dashboard.html', accounts=acc_data, 
                           stats={"total": total_accs, "active": active_accs, "pending": total_pend, "published": total_pub})

@app.route('/accounts/new', methods=['GET', 'POST'])
def add_account():
    if request.method == 'POST':
        ig_id = request.form.get('ig_account_id', '')
        mail = request.form.get('mail', '')
        password = request.form.get('password', '')
        urls = request.form.get('channel_pool', '').strip().split('\n')
        accent = request.form.get('accent_color_1', '#1E90FF')
        is_active = request.form.get('is_active') == 'on'
        
        session = Session()
        
        new_sector = Sectors(name=f"IG_{ig_id}", title=ig_id, description="IG automated pool", tags="ig")
        session.add(new_sector)
        session.flush()
        
        for url in urls:
            u = url.strip()
            if u:
                ch = Channels(url=u, sector_id=new_sector.id)
                session.add(ch)
                
        new_acc = OwnedChannels(
            ig_account_id=ig_id,
            name=ig_id,
            sector_id=new_sector.id,
            accent_color_1=accent,
            is_active=is_active,
            mail=mail,
            password=password,
            level=1
        )
        session.add(new_acc)
        session.commit()
        session.close()
        return redirect(url_for('dashboard'))
        
    return render_template('add_account.html', account=None, urls="")

@app.route('/accounts/<int:acc_id>', methods=['GET', 'POST'])
def account_detail(acc_id):
    session = Session()
    account = session.query(OwnedChannels).filter_by(id=acc_id).first()
    if not account:
        session.close()
        return "Account not found", 404
        
    if request.method == 'POST':
        account.ig_account_id = request.form.get('ig_account_id', '')
        account.name = account.ig_account_id
        account.mail = request.form.get('mail', '')
        account.password = request.form.get('password', '')
        account.accent_color_1 = request.form.get('accent_color_1', '#1E90FF')
        account.is_active = request.form.get('is_active') == 'on'
        
        urls = request.form.get('channel_pool', '').strip().split('\n')
        urls = [u.strip() for u in urls if u.strip()]
        
        existing_channels = session.query(Channels).filter_by(sector_id=account.sector_id).all()
        existing_urls = {c.url for c in existing_channels}
        
        for c in existing_channels:
            if c.url not in urls:
                session.delete(c)
        for u in urls:
            if u not in existing_urls:
                session.add(Channels(url=u, sector_id=account.sector_id))
                
        session.commit()
        return redirect(url_for('account_detail', acc_id=acc_id))
        
    channels = session.query(Channels).filter_by(sector_id=account.sector_id).all()
    urls_str = "\n".join([c.url for c in channels])
    session.close()
    
    pending_dir = os.path.join(PENDING_BASE, str(acc_id))
    published_dir = os.path.join(PUBLISHED_BASE, str(acc_id))
    
    pending_files = []
    if os.path.isdir(pending_dir):
        for f in os.listdir(pending_dir):
            if f.endswith('.mp4'):
                pending_files.append(get_video_info(os.path.join(pending_dir, f)))
                
    published_files = []
    if os.path.isdir(published_dir):
        for f in os.listdir(published_dir):
            if f.endswith('.mp4'):
                published_files.append(get_video_info(os.path.join(published_dir, f)))
                
    return render_template('account_detail.html', account=account, urls=urls_str, pending=pending_files, published=published_files)

@app.route('/accounts/<int:acc_id>/delete_pending/<filename>', methods=['POST'])
def delete_pending(acc_id, filename):
    safe_name = os.path.basename(filename)
    mp4_path = os.path.join(PENDING_BASE, str(acc_id), safe_name)
    txt_path = mp4_path.replace('.mp4', '.txt')
    
    if os.path.exists(mp4_path):
        os.remove(mp4_path)
    if os.path.exists(txt_path):
        os.remove(txt_path)
        
    return redirect(url_for('account_detail', acc_id=acc_id))

def get_latest_log_summary(prefix):
    logs = glob.glob(os.path.join(LOGS_DIR, f"{prefix}*.log"))
    if not logs:
        return None
    latest = max(logs, key=os.path.getctime)
    
    try:
        with open(latest, 'r', encoding='utf-8') as f:
            content = f.read()
            if "=== SUMMARY ===" in content:
                summary = content.split("=== SUMMARY ===")[-1].strip()
                return {"file": os.path.basename(latest), "summary": summary}
            else:
                lines = content.splitlines()[-15:]
                return {"file": os.path.basename(latest), "summary": "\n".join(lines)}
    except:
        return {"file": os.path.basename(latest), "summary": "Error reading log file."}

@app.route('/status')
def status():
    daily_log = get_latest_log_summary("daily_production_")
    publish_log = get_latest_log_summary("publish_on_connect_")
    return render_template('status.html', daily=daily_log, publish=publish_log)

if __name__ == '__main__':
    app.run(port=5001, host='0.0.0.0', debug=True)
