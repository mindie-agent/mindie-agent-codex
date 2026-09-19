"""Local, explicitly issued session leases. Discovery never creates state."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from update_lock import update_lock

LEASE_SECONDS = 24 * 60 * 60
MAX_FAILURES = 3
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def config_path():
    return (
        Path(
            os.environ.get(
                "MINDIE_AGENT_CONFIG", Path.home() / ".config/mindie-agent/codex.json"
            )
        )
        .expanduser()
        .absolute()
    )


class Inactive(ValueError):
    pass


class Sessions:
    def __init__(self, path=None):
        self.config = Path(path or config_path())
        self.path = self.config.with_suffix(".sessions.sqlite3")

    def fingerprint(self):
        raw = self.config.read_bytes()
        config = json.loads(raw)
        engine = Path(config["engine_config"]).read_bytes()
        return hashlib.sha256(raw + b"\0" + engine).hexdigest()

    def connect(self, *, create=False):
        if not self.path.exists() and not create:
            raise Inactive(
                "MindIE is inactive for this session; manual invocation required"
            )
        if create and not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=0.1)
        db.row_factory = sqlite3.Row
        if create:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS leases(
                    session TEXT PRIMARY KEY, token TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, expires REAL NOT NULL,
                    enabled INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS attempts(
                    session TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL,
                    PRIMARY KEY(session, kind, identity)
                );
            """)
        return db

    def activate(self):
        with update_lock(self.config):
            return self._activate()

    def _activate(self):
        # Native shell tools supply this value. Never guess an ID from cwd/history
        # or accept another session ID as an activation argument.
        session = os.environ.get("CODEX_THREAD_ID", "")
        if not IDENTITY.fullmatch(session):
            raise Inactive("Native CODEX_THREAD_ID required for manual activation")
        fingerprint = self.fingerprint()
        db = self.connect(create=True)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM leases WHERE session=?", (session,)
                ).fetchone()
                if (
                    row
                    and row["enabled"]
                    and row["expires"] > time.time()
                    and row["fingerprint"] == fingerprint
                    and row["failures"] < MAX_FAILURES
                ):
                    token, expires = row["token"], row["expires"]
                else:
                    token, expires = (
                        secrets.token_urlsafe(32),
                        time.time() + LEASE_SECONDS,
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO leases VALUES(?,?,?,?,1,0)",
                        (session, token, fingerprint, expires),
                    )
            return dict(
                status="active",
                mindie_session_id=session,
                mindie_activation=token,
                expires_at=expires,
            )
        finally:
            db.close()

    def _check(self, db, session, token=None):
        if not isinstance(session, str) or not IDENTITY.fullmatch(session):
            raise Inactive("Valid MindIE session identity required")
        row = db.execute("SELECT * FROM leases WHERE session=?", (session,)).fetchone()
        if (
            not row
            or not row["enabled"]
            or row["expires"] <= time.time()
            or row["fingerprint"] != self.fingerprint()
            or row["failures"] >= MAX_FAILURES
        ):
            raise Inactive(
                "MindIE session inactive, expired or paused; do not activate automatically"
            )
        if token is not None and (
            not isinstance(token, str) or not hmac.compare_digest(row["token"], token)
        ):
            raise Inactive("MindIE activation does not belong to this session")
        return row

    def check(self, session, token=None):
        db = self.connect()
        try:
            return dict(self._check(db, session, token))
        finally:
            db.close()

    def claim(self, session, kind, identity, token=None):
        """Consume before dispatch. Failure/crash/restart never replays this item."""
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                self._check(db, session, token)
                return (
                    db.execute(
                        "INSERT OR IGNORE INTO attempts VALUES(?,?,?)",
                        (session, kind, identity),
                    ).rowcount
                    == 1
                )
        finally:
            db.close()

    def finish(self, session, token, succeeded):
        db = self.connect()
        try:
            with db:
                db.execute(
                    "UPDATE leases SET failures="
                    + (
                        "CASE WHEN failures<3 THEN 0 ELSE failures END"
                        if succeeded
                        else "failures+1"
                    )
                    + " WHERE session=? AND token=?",
                    (session, token),
                )
        finally:
            db.close()

    def deactivate(self):
        session = os.environ.get("CODEX_THREAD_ID", "")
        if not IDENTITY.fullmatch(session):
            raise Inactive("Native CODEX_THREAD_ID required")
        if self.path.exists():
            db = self.connect()
            try:
                with db:
                    db.execute(
                        "UPDATE leases SET enabled=0 WHERE session=?", (session,)
                    )
            finally:
                db.close()
        return dict(status="inactive", session_id=session)
