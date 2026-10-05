"""Profiles: separate sets of finances (e.g. for different people), one database file each.

The main database file is the "default" profile, so existing data is untouched.
Other profiles live in a sibling folder named after it, e.g.
``%APPDATA%\\FinOrganizer\\finorganizer-profiles\\alex.db`` or ``~/.finorganizer-profiles/alex.db``.
"""

import os
import re

from . import db

DEFAULT_ID = "default"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Profiles:
    def __init__(self, base_path=None):
        self.base_path = os.path.abspath(base_path or db.DEFAULT_DB_PATH)
        stem = os.path.splitext(self.base_path)[0]
        self.dir = stem + "-profiles"

    def path(self, profile_id):
        if profile_id == DEFAULT_ID:
            return self.base_path
        if not _ID_RE.match(profile_id or ""):
            raise ValueError("invalid profile id: %r" % profile_id)
        return os.path.join(self.dir, profile_id + ".db")

    def exists(self, profile_id):
        return profile_id == DEFAULT_ID or os.path.isfile(self.path(profile_id))

    def open(self, profile_id=DEFAULT_ID):
        if not self.exists(profile_id):
            raise LookupError("profile %r not found" % profile_id)
        return db.connect(self.path(profile_id))

    def _name(self, profile_id):
        conn = db.connect(self.path(profile_id))
        try:
            return db.get_setting(conn, "profile_name") or (
                "Default" if profile_id == DEFAULT_ID else profile_id)
        finally:
            conn.close()

    def list(self):
        ids = [DEFAULT_ID]
        if os.path.isdir(self.dir):
            ids += sorted(f[:-3] for f in os.listdir(self.dir)
                          if f.endswith(".db") and _ID_RE.match(f[:-3]))
        return [{"id": i, "name": self._name(i)} for i in ids]

    def find(self, ref):
        """Resolve a profile by id or (case-insensitive) display name."""
        for p in self.list():
            if ref in (p["id"],) or str(ref).lower() == p["name"].lower():
                return p["id"]
        raise LookupError("profile %r not found" % ref)

    def create(self, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("profile name is required")
        if any(p["name"].lower() == name.lower() for p in self.list()):
            raise ValueError("a profile named %r already exists" % name)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "profile"
        pid, n = slug, 2
        while pid == DEFAULT_ID or os.path.exists(self.path(pid)):
            pid, n = "%s-%d" % (slug, n), n + 1
        os.makedirs(self.dir, exist_ok=True)
        conn = db.connect(self.path(pid))
        db.set_setting(conn, "profile_name", name)
        conn.close()
        return pid

    def rename(self, profile_id, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("profile name is required")
        if any(p["name"].lower() == name.lower() and p["id"] != profile_id for p in self.list()):
            raise ValueError("a profile named %r already exists" % name)
        conn = self.open(profile_id)
        db.set_setting(conn, "profile_name", name)
        conn.close()

    def delete(self, profile_id):
        if profile_id == DEFAULT_ID:
            raise ValueError("the default profile can't be deleted")
        path = self.path(profile_id)
        if not os.path.isfile(path):
            raise LookupError("profile %r not found" % profile_id)
        os.remove(path)

    # The last profile used is remembered in the default database.
    def last_used(self):
        conn = db.connect(self.base_path)
        try:
            pid = db.get_setting(conn, "last_profile") or DEFAULT_ID
        finally:
            conn.close()
        return pid if self.exists(pid) else DEFAULT_ID

    def remember(self, profile_id):
        conn = db.connect(self.base_path)
        db.set_setting(conn, "last_profile", profile_id)
        conn.close()
