from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory, abort
import os
import time
import shutil
import threading
from werkzeug.utils import secure_filename
from functools import wraps
from sqlalchemy import create_engine, text, func
from sqlalchemy.orm import sessionmaker
from models import DB_PATH, OwnedChannels, Sectors, Channels, videos, logs, VideoStats

# Setup DB
engine = create_engine(DB_PATH)
Session = sessionmaker(bind=engine)
db_session = Session()

main_bp = Blueprint('main', __name__)

# --- LOGIN DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Lütfen önce giriş yapın.", "warning")
            return redirect(url_for('main.login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---

@main_bp.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Simple hardcoded auth for localhost
        if username == 'root' and password == 'toor': 
            session['logged_in'] = True
            return redirect(url_for('main.dashboard'))
        else:
            flash("Hatalı kullanıcı adı veya şifre!", "danger")
            
    return render_template('login.html')

@main_bp.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash("Çıkış yapıldı.", "info")
    return redirect(url_for('main.login'))

@main_bp.route('/dashboard')
@login_required
def dashboard():
    total_channels  = db_session.query(OwnedChannels).count()
    active_channels = db_session.query(OwnedChannels).count()
    total_videos    = db_session.query(videos).count()

    stats = {
        "total_channels":  total_channels,
        "active_channels": active_channels,
        "total_videos":    total_videos,
        "q_render":        0,
    }

    # Recent Shorts – safe getattr() guards for pre-migration NULL columns
    recent_items  = db_session.query(videos).order_by(videos.id.desc()).limit(5).all()
    recent_shorts = []
    for v in recent_items:
        vid_id    = v.id
        try:
            recent_shorts.append({
                "id":          vid_id,
                "qwen_score":  v.qwen_point,
                "file_path":   "#",
                "title":       getattr(v, 'title',        None) or f"Pending AI Analysis... #{vid_id}",
                "summary_text":getattr(v, 'summary_text', None) or "",
                "infobox_1":   getattr(v, 'infobox_1',   None) or "",
                "infobox_2":   getattr(v, 'infobox_2',   None) or "",
                "has_tts":     bool(getattr(v, 'tts_path', None)),
                "render_time_seconds": getattr(v, 'render_time_seconds', None),
            })
        except Exception:
            # Absolute fallback for unmigrated rows
            recent_shorts.append({
                "id":           vid_id,
                "qwen_score":   getattr(v, 'qwen_point', 0),
                "file_path":    "#",
                "title":        f"Video #{vid_id}",
                "summary_text": "",
                "infobox_1":    "",
                "infobox_2":    "",
                "has_tts":      False,
                "render_time_seconds": None,
            })

    return render_template('dashboard.html', stats=stats, recent_shorts=recent_shorts)

@main_bp.route('/channels', methods=['GET', 'POST'])
@login_required
def channels():
    if request.method == 'POST':
        # Add New Channel
        channel_name = request.form.get('channel_name')
        sector_id = request.form.get('sector_id')
        
        channel_mail = request.form.get('mail')
        channel_pass = request.form.get('password')
        
        # Handle Session File Upload
        js_path_val = ""
        if 'json_file' in request.files:
            file = request.files['json_file']
            if file and file.filename.endswith('.json'):
                filename = secure_filename(file.filename)
                sessions_dir = os.path.join(os.getcwd(), "sessions")
                os.makedirs(sessions_dir, exist_ok=True)
                save_path = os.path.join(sessions_dir, filename)
                file.save(save_path)
                js_path_val = save_path
        
        try:
            new_ch = OwnedChannels(
                name=channel_name or "New Channel",
                sector_id=int(sector_id) if sector_id else 1,
                js_path=js_path_val,
                level=1,
                mail=channel_mail or "unknown@mail.com",
                password=channel_pass or "unknown_pass",
                is_ypp=False,
                sum_views=0,
                m3_views=0
            )
            # Check if Sector exists
            if not db_session.query(Sectors).get(new_ch.sector_id):
                # Create default sector if none
                if db_session.query(Sectors).count() == 0:
                    db_session.add(Sectors(name="Default", title="T", description="D", tags="tag"))
                    db_session.commit()
                new_ch.sector_id = db_session.query(Sectors).first().id

            db_session.add(new_ch)
            db_session.commit()
            flash("Kanal eklendi (Mail/Pass güncellemeyi unutmayın).", "success")
        except Exception as e:
            flash(f"Hata: {e}", "danger")
            db_session.rollback()
            
        return redirect(url_for('main.channels'))

    # List Channels – pass all fields as dicts (template uses dict notation)
    channels_data = []
    all_channels  = db_session.query(OwnedChannels).all()
    for ch in all_channels:
        sec      = db_session.query(Sectors).get(ch.sector_id)
        sec_name = sec.name if sec else "Unknown"

        channels_data.append({
            "id":            ch.id,
            "name":          ch.name or f"Channel {ch.id}",
            "sector_name":   sec_name,
            "js_path":       ch.js_path or "",
            "level":         ch.level,
            "views_90d":     ch.m3_views,
            "ypp_status":    "Yes" if ch.is_ypp else "No",
            "status":        "Active",
            # Identity fields — safe getattr for pre-migration DBs
            "voice_id":      getattr(ch, 'voice_id',      None) or "af_sky",
            "theme_color":   getattr(ch, 'theme_color',   None) or "#FF0000",
            "persona":       getattr(ch, 'persona',       None) or "Analytical Expert",
            "font_style":    getattr(ch, 'font_style',    None) or "Impact",
            "accent_color_1": getattr(ch, 'accent_color_1', None) or "#1E90FF",
            "accent_color_2": getattr(ch, 'accent_color_2', None) or "#FF6D00",
            "sfx_set":       getattr(ch, 'sfx_set',       None) or "default",
        })

    # Pass sectors as dicts – template uses sec['id'] / sec['name'] notation
    sectors_list = [
        {"id": s.id, "name": s.name}
        for s in db_session.query(Sectors).all()
    ]

    return render_template('channels.html',
                           channels=channels_data,
                           sectors=sectors_list,
                           total_subs=0,
                           total_90d=0)


@main_bp.route('/add_sector', methods=['POST'])
@login_required
def add_sector():
    name = request.form.get('sector_name')
    title = request.form.get('title_template') or "Title"
    desc = request.form.get('description_template') or "Desc"
    tags = request.form.get('hashtags') or "tags"
    
    if name:
        try:
            new_sec = Sectors(name=name, title=title, description=desc, tags=tags)
            db_session.add(new_sec)
            db_session.commit()
            flash(f"Sektör eklendi: {name}", "success")
        except Exception as e:
            flash(f"Hata: {e}", "danger")
    return redirect(url_for('main.channels'))

@main_bp.route('/delete_channel/<int:id>')
@login_required
def delete_channel(id):
    try:
        ch = db_session.query(OwnedChannels).get(id)
        if ch:
            db_session.delete(ch)
            db_session.commit()
            flash("Kanal silindi.", "warning")
    except Exception as e:
        flash(f"Hata: {e}", "danger")
    return redirect(url_for('main.channels'))

@main_bp.route('/pool', methods=['GET', 'POST'])
@login_required
def pool():
    if request.method == 'POST':
        url = request.form.get('channel_url')
        sector_id = request.form.get('sector_id')

        if url:
            try:
                # Add to Channels table
                existing = db_session.query(Channels).filter_by(url=url).first()
                if not existing:
                    new_c = Channels(
                        url=url,
                        sector_id=int(sector_id) if sector_id else None
                    )
                    db_session.add(new_c)
                    db_session.commit()
                    flash("Kaynak eklendi.", "success")
                else:
                    flash("Bu kanal zaten var.", "warning")
            except Exception as e:
                flash(f"Hata: {e}", "danger")
        return redirect(url_for('main.pool'))

    # List Pool
    pool_data = []
    channels = db_session.query(Channels).all()
    for c in channels:
        # Resolve Sector Name
        sec_name = "Belirsiz"
        if c.sector_id:
            sec = db_session.query(Sectors).get(c.sector_id)
            if sec:
                sec_name = sec.name

        pool_data.append({
            "id": c.id,
            "channel_url": c.url,
            "sector_name": sec_name,
            "last_crawled_at": c.last_checked_at,
            "status": "Active" # You might want to map this to something real if needed
        })
    
    sectors_list = db_session.query(Sectors).all()
        
    return render_template('pool.html', pool_list=pool_data, sectors=sectors_list)

@main_bp.route('/api/pool_channel/<int:channel_id>/delete', methods=['POST'])
@login_required
def delete_pool_channel(channel_id):
    from models import Channels
    sess = Session()
    try:
        ch = sess.query(Channels).get(channel_id)
        if not ch:
            return jsonify({'success': False, 'error': 'Not found'}), 404
        sess.delete(ch)
        sess.commit()
        return jsonify({'success': True})
    except Exception as e:
        sess.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        sess.close()


# --- ANALYTICS MOCKS (Avoiding errors for missing tables) ---
@main_bp.route('/analytics')
@login_required
def analytics():
    import json as _json
    from sqlalchemy import func as _func

    # ── 1. Sector Stats (Aggregated) ─────────────────────────────────────────
    sector_query = db_session.query(
        Sectors.name,
        func.count(VideoStats.id).label('video_count'),
        func.sum(VideoStats.views).label('total_views')
    ).select_from(Sectors).join(OwnedChannels).join(VideoStats).group_by(Sectors.id).all()

    sector_stats = []
    for s_name, v_count, t_views in sector_query:
        sector_stats.append({
            "sector_name": s_name,
            "video_count": v_count,
            "total_revenue": (t_views or 0) / 1000 * 0.15
        })

    # ── 2. Channel list (for the YPP table) ──────────────────────────────────
    channels_query = db_session.query(OwnedChannels).all()
    channels_data  = []
    total_90d      = 0
    total_subs_all = 0

    f_sector = request.args.get('sector')
    f_level  = request.args.get('level')
    f_status = request.args.get('status')

    for ch in channels_query:
        if f_sector and f_sector != 'all' and str(ch.sector_id) != f_sector: continue
        if f_level  and f_level  != 'all' and str(ch.level)    != f_level:   continue

        sec        = db_session.query(Sectors).get(ch.sector_id)
        subs_count = getattr(ch, 'subs_count', None) or 0
        sum_views  = getattr(ch, 'sum_views',  None) or 0
        m3_views   = getattr(ch, 'm3_views',   None) or 0

        total_90d      += m3_views
        total_subs_all += subs_count

        ypp_status = "GROWING"
        if ch.is_ypp:                                   ypp_status = "MONETIZED"
        elif subs_count >= 1000 and m3_views >= 10_000_000: ypp_status = "ELIGIBLE"

        if f_status and f_status != 'all' and f_status != ypp_status: continue

        channels_data.append({
            "id":               ch.id,
            "name":             ch.name,
            "sector_name":      sec.name if sec else "Unknown",
            "img_url":          "",
            "subscriber_count": subs_count,
            "sum_views":        sum_views,
            "views_90d":        m3_views,
            "is_ypp":           bool(ch.is_ypp),
            "ypp_status":       ypp_status,
            "level":            ch.level,
            "refresh_token":    bool(ch.js_path),
        })

    sectors_ref  = db_session.query(Sectors).all()
    sectors_data = [{"id": s.id, "name": s.name} for s in sectors_ref]

    # ── 3. Per-video insight data — clean FK JOIN ─────────────────────────────
    #
    # VideoStats.videos_fk_id is an INTEGER FK to videos.id.
    # The analytics scheduler populates it via YouTube string ID match (fast)
    # with a title-match fallback (for pre-migration rows).
    # isouter=True: videos without stats yet still appear with views=0.
    try:
        vid_rows = (
            db_session.query(
                videos.id,
                videos.title,
                videos.qwen_point,
                videos.yt_video_id,
                videos.owned_channel_id,
                videos.source_channel_id,
                OwnedChannels.name.label('target_channel_name'),
                VideoStats.views,
                VideoStats.likes,
                VideoStats.comments,
                VideoStats.publish_date,
            )
            .join(OwnedChannels, OwnedChannels.id == videos.owned_channel_id, isouter=True)
            .join(VideoStats, VideoStats.videos_fk_id == videos.id, isouter=True)
            .filter(videos.is_used == True)
            .order_by(videos.id.desc())
            .limit(500)
            .all()
        )
    except Exception as _e:
        print(f"[ANALYTICS] FK JOIN failed ({_e}), falling back to no-stats mode")
        vid_rows = []

    # Build source channel id→handle lookup
    try:
        src_ch_rows = db_session.query(Channels.id, Channels.url).all()
        src_ch_map  = {
            r.id: (r.url.split("/@")[-1].split("/")[0] if "/@" in r.url else r.url)
            for r in src_ch_rows
        }
    except Exception:
        src_ch_map = {}

    video_table = []
    for row in vid_rows:
        video_table.append({
            "id":             row.id,
            "title":          row.title or "Untitled",
            "qwen_score":     row.qwen_point or 0,
            "yt_video_id":    row.yt_video_id or "",
            "source_channel": src_ch_map.get(row.source_channel_id, "Unknown"),
            "target_channel": row.target_channel_name or "—",
            "views":          row.views or 0,
            "likes":          row.likes or 0,
            "comments":       row.comments or 0,
            "upload_date":    row.publish_date or "",
        })

    # ── DEBUG log ─────────────────────────────────────────────────────────────
    _unique_views = len({v["views"] for v in video_table if v["views"] > 0})
    print(f"[ANALYTICS DEBUG] Total videos: {len(video_table)}")
    print(f"[ANALYTICS DEBUG] Unique view counts: {_unique_views}  "
          f"({'OK — FK JOIN working' if _unique_views >= len(video_table) * 0.5 or len(video_table) == 0 else 'WARNING: possible collapse'})")
    print(f"[ANALYTICS DEBUG] Avg views: "
          f"{int(sum(v['views'] for v in video_table) / max(1, len(video_table)))}")
    for _v in video_table[:3]:
        print(f"[ANALYTICS DEBUG]   id={_v['id']} yt={_v['yt_video_id'] or 'none'} "
              f"qwen={_v['qwen_score']} src={_v['source_channel']} "
              f"views={_v['views']} title={_v['title'][:40]}")

    # ── 4. Summary cards ──────────────────────────────────────────────────────
    all_scores = [v["qwen_score"] for v in video_table if v["qwen_score"]]
    all_views  = [v["views"]      for v in video_table]

    avg_score = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0
    avg_views = int(sum(all_views)  / len(all_views))       if all_views  else 0

    from collections import defaultdict
    score_views: dict = defaultdict(list)
    for v in video_table:
        score_views[v["qwen_score"]].append(v["views"])
    score_band_avg = {
        str(s): int(sum(vs) / len(vs)) if vs else 0
        for s, vs in sorted(score_views.items())
    }

    src_views: dict = defaultdict(list)
    for v in video_table:
        if v["source_channel"] != "Unknown":
            src_views[v["source_channel"]].append(v["views"])
    top_source = (max(src_views, key=lambda k: sum(src_views[k]) / len(src_views[k]))
                  if src_views else "—")

    sector_views: dict = defaultdict(list)
    for v in video_table:
        sector_views[v["target_channel"]].append(v["views"])
    top_sector = (max(sector_views, key=lambda k: sum(sector_views[k]) / len(sector_views[k]))
                  if sector_views else "—")

    summary_cards = {
        "avg_score":      avg_score,
        "avg_views":      avg_views,
        "top_source":     top_source,
        "top_sector":     top_sector,
        "score_band_avg": score_band_avg,
    }

    # ── 5. Chart datasets ─────────────────────────────────────────────────────
    # Chart 1: one point per video (correct — no collapse)
    scatter_data = [{"x": v["qwen_score"], "y": v["views"], "label": v["source_channel"]}
                    for v in video_table]

    # Chart 2: source channel avg views bar
    src_avg = {k: int(sum(vs) / len(vs)) for k, vs in src_views.items() if vs}
    bar_src_labels = list(src_avg.keys())[:20]
    bar_src_values = [src_avg[k] for k in bar_src_labels]

    # Chart 3: views over time
    date_views: dict = defaultdict(int)
    for v in video_table:
        if v["upload_date"]:
            date_views[str(v["upload_date"])[:10]] += v["views"]
    time_labels = sorted(date_views.keys())
    time_values = [date_views[d] for d in time_labels]

    # Chart 4: score distribution histogram
    hist: dict = defaultdict(int)
    for v in video_table:
        hist[v["qwen_score"]] += 1
    hist_labels = [str(i) for i in range(1, 10)]
    hist_values = [hist.get(i, 0) for i in range(1, 10)]

    charts = {
        "scatter":     _json.dumps(scatter_data),
        "bar_labels":  _json.dumps(bar_src_labels),
        "bar_values":  _json.dumps(bar_src_values),
        "time_labels": _json.dumps(time_labels),
        "time_values": _json.dumps(time_values),
        "hist_labels": _json.dumps(hist_labels),
        "hist_values": _json.dumps(hist_values),
    }

    unique_sources = sorted({v["source_channel"] for v in video_table
                             if v["source_channel"] not in ("Unknown", "")})

    return render_template('analytics.html',
                           sector_stats=sector_stats,
                           channels=channels_data,
                           sectors=sectors_data,
                           total_subs=total_subs_all,
                           total_90d=total_90d,
                           video_table=video_table,
                           summary_cards=summary_cards,
                           charts=charts,
                           unique_sources=unique_sources)


@main_bp.route('/channel/<int:id>')
@login_required
def channel_detail(id):
    ch = db_session.query(OwnedChannels).get(id)
    if not ch: return redirect(url_for('main.channels'))
    
    sec = db_session.query(Sectors).get(ch.sector_id)
    
    # Fetch Video Stats
    v_stats = db_session.query(VideoStats).filter_by(owned_channel_id=id).order_by(VideoStats.publish_date.desc()).all()
    
    uploads_data = []
    # Mock Analytics Graph Data (last 30 entries)
    analytics_data = [] 

    for v in v_stats:
        uploads_data.append({
            "upload_date": v.publish_date or "Unknown",
            "title": v.video_title or "Untitled",
            "qwen_score": v.hype_score,
            "views": v.views,
            "likes": v.likes,
            "comments": v.comments,
            "ctr": v.ctr,
            "avg_view": v.average_view_percentage,
            "youtube_uploaded_id": "#" 
        })
        
        # Simple graph data population
        analytics_data.append({
            "date": v.publish_date,
            "total_views_gained": v.views,
            "total_subs_gained": v.subs_gained,
            "estimated_revenue": (v.views / 1000) * 0.15
        })

    channel_view = {
        "id": ch.id,
        "name": ch.name,
        "sector_name": sec.name if sec else "Unknown",
        "status": "ACTIVE",
        "daily_limit": ch.level * 1,
        "refresh_token": ch.js_path,
        "youtube_channel_id": "Unknown",
    }
    
    # Slice analytics to last 30
    analytics_data = analytics_data[:30]
    
    return render_template('channel_detail.html', channel=channel_view, uploads=uploads_data, analytics=analytics_data)

# --- FILE MANAGER (Preserved) ---
@main_bp.route('/files')
@main_bp.route('/files/<path:req_path>')
@login_required
def file_manager(req_path=''):
    BASE_DIR = os.getcwd()
    abs_path = os.path.join(BASE_DIR, req_path)

    if not os.path.commonprefix([BASE_DIR, abs_path]) == BASE_DIR: return abort(403)
    if not os.path.exists(abs_path): return abort(404)

    if os.path.isfile(abs_path):
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return render_template('file_editor.html', content=content, file_path=req_path, file_name=os.path.basename(abs_path))
        except UnicodeDecodeError:
            return send_from_directory(os.path.dirname(abs_path), os.path.basename(abs_path))

    files = []
    try:
        for name in os.listdir(abs_path):
            full_path = os.path.join(abs_path, name)
            rel_path = os.path.join(req_path, name).replace("\\", "/")
            if name.startswith('.') or name == '__pycache__': continue
            is_dir = os.path.isdir(full_path)
            size = os.path.getsize(full_path) if not is_dir else 0
            mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(full_path)))
            files.append({'name': name, 'path': rel_path, 'is_dir': is_dir, 'size': size, 'mtime': mtime})
        files.sort(key=lambda x: (not x['is_dir'], x['name']))
    except PermissionError: flash("Erişim reddedildi.", "danger")

    return render_template('files.html', files=files, current_path=req_path, parent_path=os.path.dirname(req_path))

@main_bp.route('/api/system_logs')
@login_required
def get_system_logs():
    try:
        # Fetch last 50 logs
        log_entries = db_session.query(logs).order_by(logs.id.desc()).limit(50).all()
        
        data = []
        for log in log_entries:
            # Determine Level
            if log.errors:
                level = "ERROR"
                msg = log.errors
            elif "Uploaded" in (log.messages or ""):
                level = "SUCCESS"
                msg = log.messages
            else:
                level = "INFO"
                msg = log.messages or ""

            # Determine Source
            source = f"CHAN_#{log.owned_channel_id}" if log.owned_channel_id else "SYSTEM"

            data.append({
                "created_at": log.uploaded_at.strftime('%Y-%m-%d %H:%M:%S') if log.uploaded_at else "",
                "source": source,
                "level": level,
                "message": msg
            })
            
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@main_bp.route('/run_session_generator/<int:id>')
@login_required
def run_session_generator(id):
    try:
        # Launch the script in a new separate console window so it doesn't block Flask
        import subprocess
        import sys
        
        script_path = os.path.join(os.getcwd(), "get_session_file.py")
        
        # Windows: Use 'start' to open new cmd window
        # Use sys.executable to ensure we use the same python interpreter
        cmd = f'start cmd /k "{sys.executable} {script_path} --channel-id {id}"'
        subprocess.Popen(cmd, shell=True)
        
        flash("Oturum oluşturucu yeni pencerede başlatıldı. Lütfen oradan giriş yapın.", "success")
    except Exception as e:
        flash(f"Başlatılamadı: {e}", "danger")
        
    return redirect(url_for('main.channels'))

@main_bp.route('/api/trigger_processor', methods=['POST'])
@login_required
def trigger_processor():
    try:
        import subprocess
        import sys
        
        script_path = os.path.join(os.getcwd(), "sector_processor.py")
        
        # Windows: open new console so user can see progress
        cmd = f'start cmd /k "{sys.executable} {script_path}"'
        print(f"[MANUAL TRIGGER] Launching: {cmd}")
        subprocess.Popen(cmd, shell=True)
        
        return jsonify({"status": "success", "message": "Manual Processor started in new window."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@main_bp.route('/api/stats_last_updated')
@login_required
def stats_last_updated():
    """
    Returns the Unix timestamp and human-readable datetime of the last
    successful analytics update run by the 6-hour background worker.
    """
    try:
        import web.app as _app_module
        last_ts = getattr(_app_module, '_stats_last_run', 0.0)
    except Exception:
        last_ts = 0.0

    import datetime
    if last_ts > 0:
        last_dt_str = datetime.datetime.utcfromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M UTC')
    else:
        last_dt_str = "Not yet run"

    return jsonify({
        "last_run_ts":   last_ts,
        "last_run_str":  last_dt_str,
        "interval_hours": 6,
    })


@main_bp.route('/api/update_channel_identity', methods=['POST'])
@login_required
def update_channel_identity():
    """
    AJAX endpoint to save all branding & identity fields for a channel.
    Expects JSON: {
      "channel_id": int,
      "voice_id": str,       "theme_color": str,
      "persona": str,        "font_style": str,
      "accent_color_1": str, "accent_color_2": str,
      "sfx_set": str
    }
    """
    data       = request.get_json(force=True, silent=True) or {}
    channel_id = data.get('channel_id')

    if not channel_id:
        return jsonify({"status": "error", "message": "channel_id required"}), 400

    # Fresh session per request — avoids stale identity-map issues
    fresh = Session()
    try:
        ch = fresh.query(OwnedChannels).filter(OwnedChannels.id == int(channel_id)).first()
        if not ch:
            return jsonify({"status": "error", "message": f"Channel #{channel_id} not found"}), 404

        # Apply only provided (non-empty) values
        _str_fields = {
            "voice_id":      "voice_id",
            "theme_color":   "theme_color",
            "persona":       "persona",
            "font_style":    "font_style",
            "accent_color_1": "accent_color_1",
            "accent_color_2": "accent_color_2",
            "sfx_set":       "sfx_set",
        }
        for payload_key, model_attr in _str_fields.items():
            val = (data.get(payload_key) or "").strip()
            if val:
                setattr(ch, model_attr, val)

        fresh.commit()
        print(
            f"[IDENTITY] Channel #{channel_id} saved: "
            f"voice={ch.voice_id} theme={ch.theme_color} "
            f"persona='{ch.persona}' font={ch.font_style} "
            f"accent1={ch.accent_color_1} accent2={ch.accent_color_2} "
            f"sfx={ch.sfx_set}"
        )
        return jsonify({
            "status":        "ok",
            "voice_id":      ch.voice_id,
            "theme_color":   ch.theme_color,
            "persona":       ch.persona,
            "font_style":    ch.font_style,
            "accent_color_1": ch.accent_color_1,
            "accent_color_2": ch.accent_color_2,
            "sfx_set":       ch.sfx_set,
        })
    except Exception as e:
        fresh.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        fresh.close()


# ─────────────────────────────────────────────────────────────────────────────
#  FULFILLMENT API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@main_bp.route('/api/fulfillment_status')
@login_required
def fulfillment_status():
    """
    Returns per-channel 30-day pipeline inventory.
    Reads the cached snapshot from the background worker when available,
    otherwise performs a live (lightweight) DB query.

    Response:
      {
        "last_run_str": "2026-04-11 09:30 UTC",
        "channels": [
          { "channel_id": 1, "name": "Ana Kanal", "level": 2,
            "target": 60, "current": 22, "gap": 38,
            "pending": 5, "ready": 17, "status": "CRITICAL" },
          ...
        ]
      }
    """
    import datetime as _dt
    try:
        import web.app as _app_module
        snapshot = getattr(_app_module, '_fulfillment_snapshot', [])
        last_ts  = getattr(_app_module, '_fulfillment_last_run',  0.0)
    except Exception:
        snapshot = []
        last_ts  = 0.0

    # If no cached snapshot yet, run a live check (DB only, no subprocess)
    if not snapshot:
        try:
            from fulfillment_engine import get_channel_inventory, Session as _FE_Session
            sess     = _FE_Session()
            channels = sess.query(OwnedChannels).all()
            snapshot = [get_channel_inventory(sess, ch) for ch in channels]
            sess.close()
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    last_str = (
        _dt.datetime.utcfromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M UTC')
        if last_ts > 0 else "Not yet run"
    )

    return jsonify({
        "last_run_str": last_str,
        "channels":     snapshot,
    })


@main_bp.route('/api/trigger_fulfillment', methods=['POST'])
@login_required
def trigger_fulfillment():
    """
    Manually triggers a fulfillment check in a new console window.
    This mirrors the trigger_processor pattern — fire-and-forget subprocess.
    """
    try:
        import subprocess as _sp
        import sys as _sys

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "fulfillment_engine.py"
        )
        # Open a visible console so the user can monitor progress
        cmd = f'start cmd /k "{_sys.executable} {script_path}"'
        _sp.Popen(cmd, shell=True)

        return jsonify({
            "status":  "started",
            "message": "Fulfillment engine launched in new window.",
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@main_bp.route('/api/pool_exhaustion_warnings')
@login_required
def pool_exhaustion_warnings():
    """
    Returns recent SOURCE_POOL_EXHAUSTED log entries for the
    dashboard warnings panel.

    Response:
      [
        { "channel_id": 2, "sector_id": 1,
          "message": "SOURCE_POOL_EXHAUSTED: ...",
          "timestamp": "2026-04-11 08:15 UTC" },
        ...
      ]
    """
    try:
        from fulfillment_engine import get_pool_exhaustion_warnings
        warnings = get_pool_exhaustion_warnings(limit=10)
        return jsonify(warnings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@main_bp.route('/api/trigger_stats', methods=['POST'])  
def trigger_stats():
    import threading
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from update_stats import update_all_channels
    t = threading.Thread(target=update_all_channels, daemon=True)
    t.start()
    return jsonify({"status": "started"})

import json as _json

@main_bp.route('/api/cycle_status')
def cycle_status():
    try:
        with open('weekly_cycle_status.json') as f:
            return jsonify(_json.load(f))
    except:
        return jsonify({'phase': 'IDLE', 'updated_at': None})

