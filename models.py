import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# Veritabanı Dosyası: proje_klasoru/automation.db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = f"sqlite:///{os.path.join(BASE_DIR, 'automation.db')}"

Base = declarative_base()

# TABLO TANIMLARI

class Channels(Base):
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

class OwnedChannels(Base):
    __tablename__ = 'owned_channels'
    id = Column(Integer, primary_key=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    sector = relationship("Sectors", back_populates="owned_channels")
    name = Column(String, nullable=True)
    js_path = Column(String, nullable=True)
    level = Column(Integer, nullable=False)
    mail = Column(String, nullable=False)
    password = Column(String, nullable=False)
    user_agent = Column(String, nullable=True)
    last_uploaded_at = Column(DateTime, default=datetime.utcnow) 
    ip = Column(String, nullable=True)
    is_ypp = Column(Boolean, nullable=False)
    sum_views = Column(Integer, nullable=False)
    m3_views = Column(Integer, nullable=False)
    # Creative identity — unique per channel for 100-channel originality
    voice_id      = Column(String, nullable=True, default="af_sky")       # Kokoro voice name
    theme_color   = Column(String, nullable=True, default="#FF0000")       # Subscribe button color
    persona        = Column(String, nullable=True, default="Analytical Expert")  # Qwen persona directive
    font_style     = Column(String, nullable=True, default="Impact")        # TextClip font family
    accent_color_1 = Column(String, nullable=True, default="#1E90FF")      # Banner border + progress bar
    accent_color_2 = Column(String, nullable=True, default="#FF6D00")      # Secondary accent
    sfx_set        = Column(String, nullable=True, default="default")       # SFX pack subfolder name
    token_path     = Column(String, nullable=True)                          # OAuth2 token file for YouTube Analytics API
    # Example: 'tokens/gmail_1_token.json'  (channels 1-2 share gmail_1, 3-4 share gmail_2, etc.)
    youtube_channel_id = Column(String, nullable=True)                      # YouTube UCxxxx channel ID for Analytics API
    subs_count         = Column(Integer, default=0)                          # Live subscriber count from YouTube API

class ProcessedSourceVideos(Base):
    __tablename__ = 'processed_source_videos'
    id                = Column(Integer, primary_key=True)
    video_url         = Column(String, unique=True, nullable=False)
    published_at      = Column(DateTime, nullable=True)                    # from yt-dlp upload_date → reverse-chron sort
    processed_at      = Column(DateTime, default=datetime.utcnow)
    status            = Column(String, default="PROCESSED")                # PROCESSED, FAILED, SKIPPED, PERMANENT_DL_FAIL, etc.
    sector_id         = Column(Integer, ForeignKey('sectors.id'), nullable=True)
    source_channel_id = Column(Integer, ForeignKey('channels.id'), nullable=True)  # pool channel that produced this
    failure_count     = Column(Integer, default=0)                         # number of failed download attempts
    last_failed_at    = Column(DateTime, nullable=True)                    # timestamp of last failure
    failure_reason    = Column(String,   nullable=True)                    # last error message (truncated to 500 chars)

class videos(Base):
    __tablename__ = 'videos'
    id = Column(Integer, primary_key=True)
    sector_id = Column(Integer, ForeignKey('sectors.id'))
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=True)
    qwen_point = Column(Integer, nullable=False)
    real_point = Column(Integer, nullable=True)
    channel_id = Column(Integer, ForeignKey('channels.id'))
    # Track where this video came from (for variety check)
    source_channel_id = Column(Integer, nullable=True)
    # YouTube video ID (e.g. 'dQw4w9WgXcQ') — saved after upload
    yt_video_id = Column(String(20), nullable=True, index=True)
    # AI-generated creative metadata (populated after Qwen analysis)
    title        = Column(String, nullable=True)   # Clickbait-style title
    summary_text = Column(String, nullable=True)   # 1-sentence TTS voiceover
    cta_question = Column(String, nullable=True)   # End-of-video engagement question
    infobox_1    = Column(String, nullable=True)   # "Fact Check" judgment #1
    infobox_2    = Column(String, nullable=True)   # "Did You Know?" judgment #2
    tts_path             = Column(String, nullable=True)   # Path to generated Kokoro .wav file
    render_time_seconds  = Column(Float,  nullable=True)    # Render duration in seconds
    is_used              = Column(Boolean, default=False, nullable=True)
    # Relationship to VideoStats
    stats = relationship('VideoStats', back_populates='video',
                         foreign_keys='VideoStats.videos_fk_id')

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
    """Tracks scheduled YouTube uploads for the monthly batch system."""
    __tablename__ = 'upload_queue'
    id = Column(Integer, primary_key=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    video_id = Column(Integer, ForeignKey('videos.id'), nullable=False)
    scheduled_for = Column(DateTime, nullable=False)       # Target YouTube publish date
    status = Column(String, default='PENDING')             # PENDING, UPLOADED, FAILED
    created_at = Column(DateTime, default=datetime.utcnow)
    uploaded_at = Column(DateTime, nullable=True)
    error_message = Column(String, nullable=True)
    retry_count = Column(Integer, default=0)
    cycle_id = Column(Integer, nullable=True)              # Which monthly cycle this belongs to

class VideoStats(Base):
    __tablename__ = 'video_stats'
    id = Column(Integer, primary_key=True)
    owned_channel_id = Column(Integer, ForeignKey('owned_channels.id'), nullable=False)
    video_title = Column(String, nullable=True)
    publish_date = Column(String, nullable=True)
    views = Column(Integer, default=0)
    watch_time_hours = Column(Float, default=0.0)
    subs_gained = Column(Integer, default=0)
    subs_lost = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    unique_viewers = Column(Integer, default=0)
    new_viewers = Column(Integer, default=0)
    returning_viewers = Column(Integer, default=0)
    average_view_percentage = Column(Float, default=0.0)
    average_view_duration = Column(String, nullable=True) # "MM:SS"
    likes = Column(Integer, default=0)
    like_ratio = Column(Float, default=0.0)
    hype_score = Column(Float, default=0.0)
    comments = Column(Integer, default=0)
    impressions = Column(Integer, default=0)
    ctr = Column(Float, default=0.0)
    video_id = Column(String, nullable=True)                    # YouTube video ID string (e.g. 'dQw4w9WgXcQ') — used by analytics scheduler
    videos_fk_id = Column(Integer, ForeignKey('videos.id'), nullable=True, index=True)  # FK to our videos table
    traffic_source_shorts    = Column(Float, default=0.0)       # % views from Shorts feed
    traffic_source_search    = Column(Float, default=0.0)       # % views from Search
    traffic_source_suggested = Column(Float, default=0.0)       # % views from Suggested

    # New Columns from CSV (Turkish Headers)
    duration = Column(String, nullable=True) # "Süre"
    active_views = Column(Integer, default=0) # "Aktif izlenme"
    still_watching_percent = Column(Float, default=0.0) # "İzlemeye devam edenler (%)"
    avg_views_per_viewer = Column(Float, default=0.0) # "İzleyici başına ortalama izleme sayısı"
    casual_viewers = Column(Integer, default=0) # "Sıradan izleyiciler"
    regular_viewers = Column(Integer, default=0) # "Düzenli izleyiciler"
    hypes = Column(Integer, default=0) # "Hype"
    subscribers_net = Column(Integer, default=0) # "Aboneler" (Net Change)
    
    # Relationship back to our videos table (via videos_fk_id)
    video = relationship('videos', back_populates='stats',
                         foreign_keys=[videos_fk_id])

    # Relationship
    channel = relationship("OwnedChannels")