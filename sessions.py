"""SQLite-backed conversation memory with bounded sessions and summaries."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from config import settings

MAX_RAW_MESSAGES = 15
MAX_SUMMARIES = 10
SESSION_TIMEOUT = timedelta(hours=3)
MAX_SESSION_MESSAGES = 40  # 20 user/assistant turns
SummaryCallback = Callable[[str], Awaitable[str]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    database_path = Path(settings.payments_db_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            birth_date TEXT,
            birth_time TEXT,
            birth_place TEXT,
            referral_source TEXT,
            referral_started_at TEXT,
            referral_inviter_id INTEGER,
            consent_accepted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER PRIMARY KEY,
            requests INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments (
            external_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            package_id TEXT NOT NULL,
            credited INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            closed_at TEXT,
            summary TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_open
            ON conversation_sessions(user_id, closed_at, last_activity_at);
        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS base_dialog_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            responses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        );
        DELETE FROM session_messages
        WHERE session_id IN (
            SELECT id FROM conversation_sessions WHERE closed_at IS NOT NULL
        );
        """
    )
    profile_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(profiles)")
    }
    for column in ("referral_source", "referral_started_at", "referral_inviter_id"):
        if column not in profile_columns:
            connection.execute(f"ALTER TABLE profiles ADD COLUMN {column} TEXT")
    return connection


def consume_base_dialog_response(user_id: int, daily_limit: int) -> int | None:
    usage_date = _now().date().isoformat()
    with _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO base_dialog_usage(user_id, usage_date) VALUES (?, ?)",
            (user_id, usage_date),
        )
        cursor = connection.execute(
            "UPDATE base_dialog_usage SET responses = responses + 1 "
            "WHERE user_id = ? AND usage_date = ? AND responses < ?",
            (user_id, usage_date, daily_limit),
        )
        if cursor.rowcount != 1:
            return None
        row = connection.execute(
            "SELECT responses FROM base_dialog_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, usage_date),
        ).fetchone()
    return int(row[0])


def base_dialog_responses_today(user_id: int) -> int:
    usage_date = _now().date().isoformat()
    with _connect() as connection:
        row = connection.execute(
            "SELECT responses FROM base_dialog_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, usage_date),
        ).fetchone()
    return int(row[0]) if row else 0


def reset_base_dialog_usage_today(user_id: int) -> None:
    usage_date = _now().date().isoformat()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM base_dialog_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, usage_date),
        )


def consent_accepted(user_id: int) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT consent_accepted FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row[0])


def accept_consent(user_id: int) -> bool:
    with _connect() as connection:
        existing = connection.execute(
            "SELECT consent_accepted FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        connection.execute(
            "INSERT INTO profiles(user_id, consent_accepted, updated_at) VALUES (?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET consent_accepted = 1, updated_at = excluded.updated_at",
            (user_id, _now().isoformat()),
        )
    return existing is None or not existing[0]


def save_referral_source(
    user_id: int, source: str, inviter_id: int | None = None
) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO profiles(user_id, referral_source, referral_started_at, "
            "referral_inviter_id, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "referral_source = COALESCE(profiles.referral_source, excluded.referral_source), "
            "referral_started_at = COALESCE(profiles.referral_started_at, excluded.referral_started_at), "
            "referral_inviter_id = COALESCE(profiles.referral_inviter_id, excluded.referral_inviter_id), "
            "updated_at = excluded.updated_at",
            (user_id, source, _now().isoformat(), inviter_id, _now().isoformat()),
        )


def get_referral_source(user_id: int) -> str | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT referral_source FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


def get_referral_inviter(user_id: int) -> int | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT referral_inviter_id FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row[0]) if row and row[0] else None


def referral_statistics() -> list[tuple[str, int]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT COALESCE(referral_source, 'direct'), COUNT(*) "
            "FROM profiles WHERE consent_accepted = 1 "
            "GROUP BY COALESCE(referral_source, 'direct') "
            "ORDER BY COUNT(*) DESC, referral_source"
        ).fetchall()
    return [(str(source), int(count)) for source, count in rows]


def save_birth_date(user_id: int, birth_date: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO profiles(user_id, birth_date, consent_accepted, updated_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET birth_date = excluded.birth_date, updated_at = excluded.updated_at",
            (user_id, birth_date, _now().isoformat()),
        )


def get_birth_date(user_id: int) -> str | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT birth_date FROM profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


def _open_session(connection: sqlite3.Connection, user_id: int) -> tuple[int, str | None]:
    row = connection.execute(
        "SELECT id, last_activity_at FROM conversation_sessions "
        "WHERE user_id = ? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return (int(row[0]), row[1]) if row else (0, None)


def _create_session(connection: sqlite3.Connection, user_id: int) -> int:
    timestamp = _now().isoformat()
    cursor = connection.execute(
        "INSERT INTO conversation_sessions(user_id, started_at, last_activity_at) VALUES (?, ?, ?)",
        (user_id, timestamp, timestamp),
    )
    return int(cursor.lastrowid)


def _create_or_reuse_session(connection: sqlite3.Connection, user_id: int) -> int:
    session_id, last_activity = _open_session(connection, user_id)
    if session_id and last_activity:
        if _now() - datetime.fromisoformat(last_activity) <= SESSION_TIMEOUT:
            count = connection.execute(
                "SELECT COUNT(*) FROM session_messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            if count < MAX_SESSION_MESSAGES:
                return session_id
        connection.execute(
            "UPDATE conversation_sessions SET closed_at = ? WHERE id = ?",
            (_now().isoformat(), session_id),
        )
    return _create_session(connection, user_id)


def record_message(user_id: int, role: str, content: str) -> int:
    with _connect() as connection:
        session_id = _create_or_reuse_session(connection, user_id)
        timestamp = _now().isoformat()
        connection.execute(
            "INSERT INTO session_messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content[:4000], timestamp),
        )
        connection.execute(
            "UPDATE conversation_sessions SET last_activity_at = ? WHERE id = ?",
            (timestamp, session_id),
        )
    return session_id


async def record_message_with_summary(
    user_id: int,
    role: str,
    content: str,
    summarize: SummaryCallback,
) -> int:
    """Record a message, summarizing an expired/full session first."""
    with _connect() as connection:
        session_id, last_activity = _open_session(connection, user_id)
        message_count = connection.execute(
            "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0] if session_id else 0
        should_close = bool(
            session_id
            and last_activity
            and (
                _now() - datetime.fromisoformat(last_activity) > SESSION_TIMEOUT
                or message_count >= MAX_SESSION_MESSAGES
            )
        )
    if should_close:
        await _close_session(user_id, session_id, summarize)
    return record_message(user_id, role, content)


def _context(connection: sqlite3.Connection, user_id: int) -> list[dict[str, str]]:
    session_id, _ = _open_session(connection, user_id)
    messages: list[dict[str, str]] = []
    if session_id:
        rows = connection.execute(
            "SELECT role, content FROM session_messages WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, MAX_RAW_MESSAGES),
        ).fetchall()
        messages.extend({"role": row[0], "content": row[1]} for row in reversed(rows))
    profile = connection.execute(
        "SELECT birth_date, birth_time, birth_place FROM profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    summaries = connection.execute(
        "SELECT summary FROM session_summaries WHERE user_id = ? ORDER BY id DESC LIMIT 3",
        (user_id,),
    ).fetchall()
    context_parts = []
    if profile and any(profile):
        context_parts.append(f"Профиль: дата рождения: {profile[0] or 'не указана'}; время: {profile[1] or 'не указано'}; место: {profile[2] or 'не указано'}.")
    if summaries:
        context_parts.append("Краткая память прошлых сессий:\n" + "\n".join(f"- {row[0]}" for row in summaries))
    if context_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(context_parts)})
    return messages


async def prepare_context(user_id: int, summarize: SummaryCallback) -> list[dict[str, str]]:
    with _connect() as connection:
        session_id, last_activity = _open_session(connection, user_id)
        stale = session_id and last_activity and _now() - datetime.fromisoformat(last_activity) > SESSION_TIMEOUT
        if stale:
            await _close_session(user_id, session_id, summarize)
        return _context(connection, user_id)


async def _close_session(user_id: int, session_id: int, summarize: SummaryCallback) -> None:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT role, content FROM session_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    if rows:
        transcript = "\n".join(f"{role}: {content}" for role, content in rows)
        summary = (await summarize(transcript)).strip()
        if summary:
            with _connect() as connection:
                connection.execute(
                    "INSERT INTO session_summaries(user_id, session_id, summary, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, session_id, summary[:1200], _now().isoformat()),
                )
                connection.execute(
                    "DELETE FROM session_summaries WHERE user_id = ? AND id NOT IN "
                    "(SELECT id FROM session_summaries WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
                    (user_id, user_id, MAX_SUMMARIES),
                )
        with _connect() as connection:
            connection.execute(
                "DELETE FROM session_messages WHERE session_id = ?", (session_id,)
            )
    with _connect() as connection:
        connection.execute(
            "UPDATE conversation_sessions SET closed_at = ? WHERE id = ?",
            (_now().isoformat(), session_id),
        )


async def close_current_session(
    user_id: int, summarize: SummaryCallback | None = None
) -> None:
    with _connect() as connection:
        session_id, _ = _open_session(connection, user_id)
    if session_id:
        if summarize is None:
            with _connect() as connection:
                connection.execute(
                    "DELETE FROM session_messages WHERE session_id = ?", (session_id,)
                )
                connection.execute(
                    "DELETE FROM session_summaries WHERE user_id = ?", (user_id,)
                )
                connection.execute(
                    "UPDATE conversation_sessions SET closed_at = ? WHERE id = ?",
                    (_now().isoformat(), session_id),
                )
        else:
            await _close_session(user_id, session_id, summarize)


def delete_user_data(user_id: int) -> None:
    """Delete conversation/profile data without resetting entitlements or quotas.

    Payment records, balances, daily usage, starter quota and active blocks are
    retained for accounting and abuse prevention. Their retention must be
    described in the privacy policy.
    """
    with _connect() as connection:
        session_ids = [row[0] for row in connection.execute(
            "SELECT id FROM conversation_sessions WHERE user_id = ?", (user_id,)
        ).fetchall()]
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            connection.execute(f"DELETE FROM session_messages WHERE session_id IN ({placeholders})", session_ids)
        connection.execute("DELETE FROM session_summaries WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM conversation_sessions WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
