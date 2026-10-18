from sqlalchemy import create_engine, inspect
import os

# Check current directory
print(f"CWD: {os.getcwd()}")

# Path to DB
db_path = "sqlite:///automation.db"
engine = create_engine(db_path)

try:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Tables in {db_path}: {tables}")
    
    if "owned_channels" in tables:
        print("owned_channels exists.")
    else:
        print("owned_channels MISSING!")
except Exception as e:
    print(f"Error inspecting DB: {e}")
