"""Back up one profile's data to a single file, and restore it again.

A backup is a plain SQLite copy of the profile's database, so it opens in the
desktop app, the browser version and the iPhone web app alike. That makes it
the way to move your data between devices.
"""

import os
import sqlite3
import tempfile

from . import db

REQUIRED_TABLES = {"accounts", "categories", "transactions"}
SQLITE_HEADER = b"SQLite format 3\x00"


def db_path(conn):
    """The file behind an open connection ('' for in-memory databases)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return row[2] or ""
    return ""


def snapshot(conn):
    """A consistent copy of the open database, as bytes."""
    conn.commit()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


def check(path):
    """Raise ValueError unless ``path`` is a readable FinOrganizer database."""
    with open(path, "rb") as f:
        if f.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError("that file isn't a FinOrganizer backup")
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not REQUIRED_TABLES <= tables:
            raise ValueError("that file isn't a FinOrganizer backup")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("that backup file is damaged")
    except sqlite3.DatabaseError:
        raise ValueError("that backup file is damaged")
    finally:
        conn.close()


def restore(conn, data):
    """Replace the database behind ``conn`` with the backup ``data``.

    The backup is fully checked first; nothing changes if it's invalid. The data
    it replaces is kept next to it as ``<name>.before-restore``. Returns a new
    connection to the restored database (``conn`` is closed).
    """
    path = db_path(conn)
    if not path:
        raise ValueError("this database can't be restored into")
    if not data:
        raise ValueError("the backup file is empty")
    tmp = path + ".restore"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        check(tmp)
        db.connect(tmp).close()  # bring an older backup up to the current format
        conn.commit()
        conn.close()
        os.replace(path, path + ".before-restore")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return db.connect(path)


def summary(conn):
    """What's in a database, for confirmation messages."""
    count = lambda t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    return {"accounts": count("accounts"), "transactions": count("transactions")}
