#!/usr/bin/env python3
"""
Database backup utility for Birthday Reminder Bot
Creates multiple types of backups for maximum safety

The database path is imported from db.py rather than hardcoded, so this
script follows DB_FILE / PRESTABLE_MODE exactly like the bot does. It
previously looked for "data.db" in the working directory while the bot
wrote to /app/data/data.db, which meant every backup silently failed
with "Main database data.db not found!".

Backups are written next to the database (inside the mounted volume on
Railway) so they survive redeploys along with the data itself.
"""

import os
import shutil
import sqlite3
import sys
from datetime import datetime

from db import DB_FILE

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), "backups")


def create_backup():
    """Creates backup of the main database.

    Produces three artifacts: a plain file copy, a consistent SQLite
    online backup, and a SQL dump. All three use Python's sqlite3
    module -- the python:*-slim image has no sqlite3 command-line
    binary, so the previous subprocess calls could not have worked
    inside the container.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not os.path.exists(DB_FILE):
        print(f"❌ Main database not found: {DB_FILE}")
        return False

    os.makedirs(BACKUP_DIR, exist_ok=True)

    try:
        # 1. Simple file copy
        shutil.copy2(DB_FILE, os.path.join(BACKUP_DIR, f"data_backup_{timestamp}.db"))

        # 2. SQLite online backup (consistent even while the bot is writing)
        source = sqlite3.connect(DB_FILE)
        try:
            target_path = os.path.join(BACKUP_DIR, f"data_sqlite_backup_{timestamp}.db")
            target = sqlite3.connect(target_path)
            try:
                source.backup(target)
            finally:
                target.close()

            # 3. SQL dump
            dump_path = os.path.join(BACKUP_DIR, f"data_dump_{timestamp}.sql")
            with open(dump_path, "w", encoding="utf-8") as f:
                for line in source.iterdump():
                    f.write(f"{line}\n")
        finally:
            source.close()

        print(f"✅ Backup created successfully: {timestamp}")
        print(f"   Location: {BACKUP_DIR}")
        return True

    except (OSError, sqlite3.Error) as e:
        print(f"❌ Backup failed: {e}")
        return False


def restore_from_backup(backup_file):
    """Restore database from backup file."""
    if not os.path.exists(backup_file):
        print(f"❌ Backup file {backup_file} not found!")
        return False

    try:
        # Create restore point first, so a bad restore is still recoverable
        if os.path.exists(DB_FILE):
            create_backup()

        os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
        shutil.copy2(backup_file, DB_FILE)
        print(f"✅ Database restored from {backup_file} to {DB_FILE}")
        return True

    except OSError as e:
        print(f"❌ Restore failed: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        if len(sys.argv) < 3:
            print("Usage: python backup_db.py restore <backup_file>")
            sys.exit(1)
        sys.exit(0 if restore_from_backup(sys.argv[2]) else 1)
    else:
        sys.exit(0 if create_backup() else 1)
