from sqlalchemy import create_engine
from models import Base, DB_PATH
import os

def init_db():
    print(f"Initializing database at: {DB_PATH}")
    engine = create_engine(DB_PATH)
    Base.metadata.create_all(engine)
    print("Database tables created successfully.")

if __name__ == "__main__":
    init_db()
