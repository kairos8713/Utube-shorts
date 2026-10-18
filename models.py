import os
from datetime import datetime
from sqlalchemy import create_engine, event, Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from config import settings

# Veritabani: DATABASE_URL (.env) — varsayılan proje_klasoru/automation.db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = settings.DATABASE_URL

Base = declarative_base()

# TABLO TANIMLARI

class Channels(Base):
    """Source (pool) channels scraped for raw footage."""
    __tablename__ = 'channels'
    id = Column(Integer, primary_key=True)
    url = Column(String, unique=True, nullable=False)
    last_checked_at = Column(DateTime, default=datetime.utcnow)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    sector = relationship("Sectors", back_populates="channels")

class Sectors(Base):
    __tablename__ = 'sectors'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    owned_channels = relationship("OwnedChannels", back_populates="sector")
    channels = relationship("Channels", back_populates="sector")
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    tags = Column(String, nullable=False)
    voices_path = Column(String, nullable=True)
    music_slug = Column(String, nullable=True)     # bg_music/<slug>/

class OwnedChannels(Base):
    """IG accounts owned by us — one per sector rotation slot."""
    __tablename__ = 'owned_channels'
    id = Column(Integer, primary_key=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    sector = relationship("Sectors", back_populates="owned_channels")
    name = Column(String, nullable=True)
    level = Column(Integer, nullable=False)
    mail = Column(String, nullable=False)
    password = Column(String, nullable=False)
    last_uploaded_at = Column(DateTime, default=datetime.utcnow)
    # IG-specific fields
    ig_account_id = Column(String, nullable=True)         # Instagram account ID / handle
    ig_session_path = Column(String, nullable=True)       # Path to instaloader session file
    channel_pool_index = Column(Integer, default=0)       # Round-robin pool index for source rotation
    proxy = Column(String, nullable=True)                  # Proxy for IG API calls
    is_active = Column(Boolean, default=True)             # Account status
    # Creative identity — unique per channel for originality
    voice_id      = Column(String, nullable=True, default="af_sky")
    persona       = Column(String, nullable=True, default="Analytical Expert")
    font_style    = Column(String, nullable=True, default="Impact")
    accent_color_1 = Column(String, nullable=True, default="#1E90FF")
    accent_color_2 = Column(String, nullable=True, default="#FF6D00")
    sfx_set       = Column(String, nullable=True, default="default")
    # ── Instagram API with Instagram Login (migration 002) ───────────────────
    ig_user_id            = Column(String, nullable=True)
    ig_username           = Column(String, nullable=True)
    ig_access_token       = Column(String, nullable=True)   # enc:v1 Fernet ciphertext
    ig_token_expires_at   = Column(DateTime, nullable=True)
    ig_token_refreshed_at = Column(DateTime, nullable=True)
    ig_setup_completed_at = Column(DateTime, nullable=True) # entered by Emre (warm-up gate)
    status                = Column(String, default="WARMUP")  # ACTIVE|PAUSED|PAUSED_AUTO|WARMUP|DISCONNECTED
    pause_reason          = Column(String, nullable=True)
    paused_at             = Column(DateTime, nullable=True)
    daily_post_cap        = Column(Integer, default=1)
    posting_window        = Column(String, nullable=True)   # "15:00-05:00" Warsaw; NULL = default
    activated_at          = Column(DateTime, nullable=True)
    consecutive_failures  = Column(Integer, default=0)

class ProcessedSourceVideos(Base):
    __tablename__ = 'processed_source_videos'
    id                = Column(Integer, primary_key=True)
    video_url         = Column(String, unique=True, nullable=False)
    published_at      = Column(DateTime, nullable=True)
    processed_at      = Column(DateTime, default=datetime.utcnow)
    status            = Column(String, default="PROCESSED")
    sector_id         = Column(Integer, ForeignKey('sectors.id'), nullable=True)
    source_channel_id = Column(Integer, ForeignKey('channels.id'), nullable=True)
    failure_count     = Column(Integer, default=0)
    last_failed_at    = Column(DateTime, nullable=True)
    failure_reason    = Column(String,   nullable=True)

class videos(Base):
    """Rendered IG Reels — one row per produced reel."""
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=True)
    qwen_point = Column(Integer, nullable=False)
    real_point = Column(Integer, nullable=True)
    channel_id = Column(Integer, ForeignKey('channels.id'))
    source_channel_id = Column(Integer, nullable=True)     # Pool channel that produced this
    # IG Reel ID (set after upload)
    ig_reel_id = Column(String, nullable=True, index=True)
    # AI-generated creative metadata
    title        = Column(String, nullable=True)
    summary_text = Column(String, nullable=True)
    caption      = Column(String, nullable=True)           # IG caption text
    hashtags     = Column(String, nullable=True)           # Comma-separated hashtags
    tts_path             = Column(String, nullable=True)
    render_time_seconds  = Column(Float,  nullable=True)
    is_used              = Column(Boolean, default=False, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)
    # ── Instagram pipeline (migration 002) ───────────────────────────────────
    source_id       = Column(Integer, nullable=True)       # sources.id
    source_url      = Column(String, nullable=True)        # YouTube watch URL
    segment_start   = Column(Float, nullable=True)         # absolute seconds in source
    segment_end     = Column(Float, nullable=True)
    phash           = Column(String, nullable=True)        # 64-bit perceptual hash (hex)
    file_path       = Column(String, nullable=True)
    cover_path      = Column(String, nullable=True)
    cover_offset_ms = Column(Integer, nullable=True)
    transcript      = Column(String, nullable=True)
    topic           = Column(String, nullable=True)
    qc_status       = Column(String, nullable=True)        # PASS | FAIL
    qc_report       = Column(String, nullable=True)        # JSON
    cycle_id        = Column(Integer, nullable=True)
    time_sensitive  = Column(Boolean, default=False)
    clip_duration   = Column(Float, nullable=True)
    files_deleted_at = Column(DateTime, nullable=True)

class logs(Base):
    __tablename__ = 'logs'
    id = Column(Integer, primary_key=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'))
    video_id = Column(Integer, ForeignKey('videos.id'))
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    messages = Column(String, nullable=True)
    errors = Column(String, nullable=True)

class UploadQueue(Base):
    """Tracks scheduled IG Reel uploads."""
    __tablename__ = 'upload_queue'
    id = Column(Integer, primary_key=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    scheduled_for = Column(DateTime, nullable=False)
    status = Column(String, default='PENDING')              # PENDING, UPLOADED, FAILED
    created_at = Column(DateTime, default=datetime.utcnow)
    uploaded_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    cycle_id = Column(Integer, nullable=True)
    slot_source = Column(String, nullable=True)     # qwen | explore | fallback
    priority = Column(Integer, default=0)           # >0 = fast-lane (time-sensitive)
    post_id = Column(Integer, nullable=True)        # posts.id once publishing starts


# ═════════════════════════════════════════════════════════════════════════════
# Instagram-era tables (created by SQLAlchemy create_all; idempotent)
# ═════════════════════════════════════════════════════════════════════════════

class Source(Base):
    """YouTube source channel pool, fully managed by the discovery/bandit logic."""
    __tablename__ = 'sources'
    id = Column(Integer, primary_key=True)
    channel_url = Column(String, unique=True, nullable=False)
    yt_channel_id = Column(String, nullable=True)
    handle = Column(String, nullable=True)
    title = Column(String, nullable=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'), nullable=True)   # niche
    status = Column(String, default='CANDIDATE')   # CANDIDATE|TRIAL|ACTIVE|REJECTED|BLACKLISTED
    status_reason = Column(String, nullable=True)
    status_changed_at = Column(DateTime, nullable=True)
    subscribers = Column(Integer, nullable=True)
    avg_views = Column(Float, nullable=True)
    uploads_per_week = Column(Float, nullable=True)
    avg_duration_s = Column(Float, nullable=True)
    description = Column(String, nullable=True)
    recent_titles = Column(String, nullable=True)   # JSON list
    niche_fit = Column(Float, nullable=True)        # Qwen 0..1
    clip_permission = Column(Float, nullable=True)  # Qwen 0..1
    speech_ratio = Column(Float, nullable=True)     # Whisper sample
    language = Column(String, nullable=True)
    saturation = Column(Float, nullable=True)       # 0..1 — how many clip channels already cut it
    score = Column(Float, nullable=True)
    bandit_alpha = Column(Float, default=1.0)
    bandit_beta = Column(Float, default=1.0)
    clips_published = Column(Integer, default=0)
    last_video_seen = Column(String, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    legacy_channel_id = Column(Integer, nullable=True)


class SourceTrial(Base):
    __tablename__ = 'source_trials'
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    evaluate_after = Column(DateTime, nullable=True)   # last trial post published + 72 h
    evaluated_at = Column(DateTime, nullable=True)
    clips_target = Column(Integer, default=4)
    median_relative = Column(Float, nullable=True)
    result = Column(String, default='PENDING')        # PENDING|PASS|FAIL
    notes = Column(String, nullable=True)


class Post(Base):
    """One publish attempt/record per (account, video)."""
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    queue_id = Column(Integer, nullable=True)
    status = Column(String, default='PENDING')   # PENDING|CONTAINER|PUBLISHED|FAILED|UNKNOWN|SKIPPED
    ig_container_id = Column(String, nullable=True)
    ig_media_id = Column(String, nullable=True, index=True)
    permalink = Column(String, nullable=True)
    caption = Column(String, nullable=True)
    scheduled_for = Column(DateTime, nullable=True)
    slot_source = Column(String, nullable=True)
    slot_hour = Column(Integer, nullable=True)      # local (Warsaw) slot start hour
    container_created_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0)
    error_kind = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    phash = Column(String, nullable=True)
    source_id = Column(Integer, nullable=True)
    source_url = Column(String, nullable=True)
    segment_start = Column(Float, nullable=True)
    segment_end = Column(Float, nullable=True)
    is_trial = Column(Boolean, default=False)
    dry_run = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class PostMetric(Base):
    __tablename__ = 'post_metrics'
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey('posts.id'), nullable=False, index=True)
    checkpoint_h = Column(Integer, nullable=False)   # 1 | 24 | 72
    fetched_at = Column(DateTime, default=datetime.utcnow)
    views = Column(Integer, nullable=True)
    reach = Column(Integer, nullable=True)
    likes = Column(Integer, nullable=True)
    comments = Column(Integer, nullable=True)
    shares = Column(Integer, nullable=True)
    saved = Column(Integer, nullable=True)
    total_interactions = Column(Integer, nullable=True)
    avg_watch_time_ms = Column(Integer, nullable=True)
    raw_json = Column(String, nullable=True)


class AccountMetric(Base):
    __tablename__ = 'account_metrics'
    id = Column(Integer, primary_key=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False, index=True)
    day = Column(String, nullable=False)            # YYYY-MM-DD (UTC)
    followers = Column(Integer, nullable=True)
    reach = Column(Integer, nullable=True)
    online_followers_json = Column(String, nullable=True)   # {hour: count}
    audience_geo_json = Column(String, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class Comment(Base):
    __tablename__ = 'comments'
    id = Column(Integer, primary_key=True)
    ig_comment_id = Column(String, unique=True, nullable=False)
    post_id = Column(Integer, ForeignKey('posts.id'), nullable=False, index=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    username = Column(String, nullable=True)
    text = Column(String, nullable=True)
    commented_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    classification = Column(String, nullable=True)  # question|discussion|emoji_short|spam_abuse|risky
    class_reason = Column(String, nullable=True)
    status = Column(String, default='NEW')          # NEW|CLASSIFIED|REPLY_SCHEDULED|REPLIED|HIDDEN|IGNORED|RISKY|SKIPPED
    needs_attention = Column(Boolean, default=False)
    attention_resolved = Column(Boolean, default=False)


class CommentReply(Base):
    __tablename__ = 'comment_replies'
    id = Column(Integer, primary_key=True)
    comment_id = Column(Integer, ForeignKey('comments.id'), nullable=False, index=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    text = Column(String, nullable=False)
    scheduled_for = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    ig_reply_id = Column(String, nullable=True)
    status = Column(String, default='SCHEDULED')   # SCHEDULED|SENT|BLOCKED|FAILED|CANCELLED
    block_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Decision(Base):
    """Qwen decision log: every proposal, its validation and whether it was applied."""
    __tablename__ = 'decisions'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    task = Column(String, nullable=False)
    owned_channel_id = Column(Integer, nullable=True)
    input_summary = Column(String, nullable=True)
    proposed_action = Column(String, nullable=True)  # JSON
    reason = Column(String, nullable=True)
    validation = Column(String, nullable=True)       # OK | REJECTED: ...
    applied = Column(Boolean, default=False)


class CycleReport(Base):
    __tablename__ = 'cycle_reports'
    id = Column(Integer, primary_key=True)
    cycle_id = Column(Integer, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    stats_json = Column(String, nullable=True)
    narrative = Column(String, nullable=True)
    decision_summary = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = 'alerts'
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    kind = Column(String, nullable=False)
    severity = Column(String, default='WARNING')    # CRITICAL|WARNING|INFO
    message = Column(String, nullable=False)
    owned_channel_id = Column(Integer, nullable=True)
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime, nullable=True)
    emailed = Column(Boolean, default=False)


class SystemState(Base):
    """Key/value: emergency_stop, heartbeat:<service>, cycle anchors, etc."""
    __tablename__ = 'system_state'
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


# Engine & Session — several processes share this SQLite file (cycle runner,
# publisher, comment responder, panel, watchdog): WAL + busy timeout.
engine = create_engine(DB_PATH, echo=False, connect_args={"timeout": 30})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)