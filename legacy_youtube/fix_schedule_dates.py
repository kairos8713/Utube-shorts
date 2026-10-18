"""
fix_schedule_dates.py

Her kanal için PENDING olan 7 videonun schedule tarihlerini sıfırlar.
Başlangıç tarihi: 2026-07-30, saat 15:00
Her kanal için 7 video, 1'er gün arayla planlanır.
"""

import sqlite3
import datetime
import sys

DB_PATH = 'automation.db'

def fix_dates():
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # ----- Önce mevcut durumu göster -----
    cur.execute("""
        SELECT uq.id, uq.owned_channel_id, oc.name, uq.scheduled_for, uq.status
        FROM   upload_queue uq
        LEFT JOIN owned_channels oc ON oc.id = uq.owned_channel_id
        WHERE  uq.status = 'PENDING'
        ORDER  BY uq.owned_channel_id, uq.scheduled_for
    """)
    rows = cur.fetchall()
    print(f"[INFO] Toplam PENDING kayıt: {len(rows)}")
    print()
    for r in rows:
        print(f"  id={r[0]:4d}  ch={r[1]:2d} ({r[2]})  scheduled={r[3]}")

    if not rows:
        print("[WARN] Güncellenecek PENDING kayıt yok.")
        conn.close()
        return

    # ----- Başlangıç tarihi: 2026-07-30 15:00 -----
    base_date = datetime.datetime(2026, 7, 30, 15, 0, 0)

    # Kanal başına sayaç tutarak sıraya göre 1'er gün arttır
    channel_day = {}   # ch_id -> kaçıncı gün (0-indexed)
    updated = []

    for (uid, ch_id, ch_name, sched, status) in rows:
        if ch_id not in channel_day:
            channel_day[ch_id] = 0

        new_dt = base_date + datetime.timedelta(days=channel_day[ch_id])
        channel_day[ch_id] += 1

        cur.execute(
            "UPDATE upload_queue SET scheduled_for = ? WHERE id = ?",
            (new_dt.strftime('%Y-%m-%d %H:%M:%S'), uid)
        )
        updated.append((uid, ch_id, ch_name, new_dt))

    conn.commit()
    conn.close()

    print()
    print("=" * 60)
    print(f"[OK] {len(updated)} kayıt güncellendi:")
    print("=" * 60)
    for (uid, ch_id, ch_name, new_dt) in updated:
        print(f"  id={uid:4d}  ch={ch_id:2d} ({ch_name})  → {new_dt.strftime('%Y-%m-%d %H:%M')}")

    print()
    print(f"[DONE] İlk tarih : {base_date.strftime('%Y-%m-%d %H:%M')}")
    last = base_date + datetime.timedelta(days=max(channel_day.values()) - 1)
    print(f"[DONE] Son tarih  : {last.strftime('%Y-%m-%d %H:%M')} (her kanal kendi başlangıcından itibaren)")

if __name__ == '__main__':
    fix_dates()
