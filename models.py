import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, ForeignKey, DateTime, Boolean
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

# Engine & Session
engine = create_engine(DB_PATH, echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)