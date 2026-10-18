import sqlite3
import os

db_path = r'c:\Users\emre8\Desktop\utube\automation.db'

if not os.path.exists(db_path):
    print(f"Database file not found at {db_path}")
else:
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Get list of tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        for table in tables:
            table_name = table[0]
            print(f"\nTable: {table_name}")
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = cursor.fetchall()
            for col in columns:
                # cid, name, type, notnull, dflt_value, pk
                print(f"  - {col[1]} ({col[2]})")
                
        conn.close()
    except Exception as e:
        print(f"Error: {e}")
