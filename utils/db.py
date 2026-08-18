"""SQLite (aiosqlite) ユーティリティ — イベント都度書き込み & 再起動復元対応"""
from __future__ import annotations
import aiosqlite
import time
from pathlib import Path
import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    subject TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    paused_total INTEGER NOT NULL DEFAULT 0,
    last_pause_at INTEGER,
    is_paused INTEGER NOT NULL DEFAULT 0,
    auto_started INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, guild_id);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(user_id, guild_id, ended_at);

CREATE TABLE IF NOT EXISTS pomodoro_state (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    is_running INTEGER NOT NULL DEFAULT 0,
    is_break INTEGER NOT NULL DEFAULT 0,
    cycle INTEGER NOT NULL DEFAULT 0,
    ends_at INTEGER,
    started_at INTEGER
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    guild_id INTEGER,
    channel_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    trigger_at INTEGER NOT NULL,
    repeat_rule TEXT,
    created_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS panel_message (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_panel (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
"""

async def init_db():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

def now_ts() -> int:
    return int(time.time())

# ---------- sessions ----------
async def get_open_session(user_id: int, guild_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND guild_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (user_id, guild_id),
        ) as cur:
            return await cur.fetchone()

async def create_session(user_id: int, guild_id: int, channel_id: int | None, subject: str | None, auto_started: bool = False) -> int:
    ts = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO sessions (user_id, guild_id, channel_id, subject, started_at, created_at, auto_started) VALUES (?,?,?,?,?,?,?)",
            (user_id, guild_id, channel_id, subject, ts, ts, 1 if auto_started else 0),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore

async def end_session(session_id: int):
    ts = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        # if paused, count pause until now as paused_total as well
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        paused_total = row["paused_total"] or 0
        if row["is_paused"]:
            last_pause = row["last_pause_at"] or ts
            paused_total += ts - last_pause
        await db.execute(
            "UPDATE sessions SET ended_at=?, paused_total=?, is_paused=0, last_pause_at=NULL WHERE id=?",
            (ts, paused_total, session_id),
        )
        await db.commit()
        return paused_total

async def pause_session(session_id: int) -> bool:
    ts = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_paused, ended_at FROM sessions WHERE id=?", (session_id,)) as cur:
            row = await cur.fetchone()
        if not row or row["ended_at"] is not None or row["is_paused"]:
            return False
        await db.execute("UPDATE sessions SET is_paused=1, last_pause_at=? WHERE id=?", (ts, session_id))
        await db.commit()
        return True

async def resume_session(session_id: int) -> bool:
    ts = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT is_paused, last_pause_at, paused_total, ended_at FROM sessions WHERE id=?", (session_id,)) as cur:
            row = await cur.fetchone()
        if not row or row["ended_at"] is not None or not row["is_paused"]:
            return False
        last_pause = row["last_pause_at"] or ts
        paused_total = (row["paused_total"] or 0) + (ts - last_pause)
        await db.execute(
            "UPDATE sessions SET is_paused=0, last_pause_at=NULL, paused_total=? WHERE id=?",
            (paused_total, session_id),
        )
        await db.commit()
        return True

async def update_session_subject(session_id: int, subject: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE sessions SET subject=? WHERE id=?", (subject, session_id))
        await db.commit()

def _effective_seconds(row) -> int:
    """勉強時間(秒): (ended or now - started) - paused_total - (now - last_pause if paused)"""
    import time
    now = int(time.time())
    started = row["started_at"]
    ended = row["ended_at"] if row["ended_at"] is not None else now
    paused_total = row["paused_total"] or 0
    if row["is_paused"] and row["ended_at"] is None:
        paused_total += now - (row["last_pause_at"] or now)
    return max(0, ended - started - paused_total)

async def fetch_sessions(user_id: int, guild_id: int, since_ts: int | None = None, until_ts: int | None = None):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM sessions WHERE user_id=? AND guild_id=?"
        params: list = [user_id, guild_id]
        if since_ts is not None:
            q += " AND started_at >= ?"
            params.append(since_ts)
        if until_ts is not None:
            q += " AND started_at < ?"
            params.append(until_ts)
        q += " ORDER BY started_at ASC"
        async with db.execute(q, params) as cur:
            rows = await cur.fetchall()
            return rows

async def aggregate_user(user_id: int, guild_id: int, since_ts: int | None = None):
    rows = await fetch_sessions(user_id, guild_id, since_ts=since_ts)
    total = 0
    by_subject: dict[str, int] = {}
    for r in rows:
        # 終了済みのみ集計に含める? 計画では今日/今週/全期間 → 未終了は含めず集計が安定するため除外（表示は別）
        if r["ended_at"] is None:
            continue
        eff = _effective_seconds(r)
        total += eff
        subj = r["subject"] or "未設定"
        by_subject[subj] = by_subject.get(subj, 0) + eff
    return total, by_subject, rows

async def leaderboard(guild_id: int, since_ts: int):
    """since_ts以降の合計秒をユーザー別に集計"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND started_at >= ? AND ended_at IS NOT NULL",
            (guild_id, since_ts),
        ) as cur:
            rows = await cur.fetchall()
    totals: dict[int, int] = {}
    for r in rows:
        eff = _effective_seconds(r)
        totals[r["user_id"]] = totals.get(r["user_id"], 0) + eff
    # sort desc
    return sorted(totals.items(), key=lambda x: x[1], reverse=True)

# ---------- pomodoro ----------
async def get_pomodoro(guild_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pomodoro_state WHERE guild_id=?", (guild_id,)) as cur:
            return await cur.fetchone()

async def upsert_pomodoro(guild_id: int, **fields):
    async with aiosqlite.connect(config.DB_PATH) as db:
        # insert or update
        cols = ["guild_id"] + list(fields.keys())
        placeholders = ",".join(["?"] * len(cols))
        vals = [guild_id] + list(fields.values())
        # build update part
        updates = ", ".join([f"{k}=excluded.{k}" for k in fields.keys()])
        q = f"INSERT INTO pomodoro_state ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT(guild_id) DO UPDATE SET {updates}"
        await db.execute(q, vals)
        await db.commit()

async def clear_pomodoro(guild_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM pomodoro_state WHERE guild_id=?", (guild_id,))
        await db.commit()

# ---------- reminders ----------
async def add_reminder(user_id: int, guild_id: int | None, channel_id: int, message: str, trigger_at: int, repeat_rule: str | None = None) -> int:
    ts = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reminders (user_id, guild_id, channel_id, message, trigger_at, repeat_rule, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, guild_id, channel_id, message, trigger_at, repeat_rule, ts),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore

async def fetch_due_reminders(now: int | None = None):
    if now is None:
        now = now_ts()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM reminders WHERE active=1 AND trigger_at <= ?", (now,)) as cur:
            return await cur.fetchall()

async def deactivate_reminder(rid: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE reminders SET active=0 WHERE id=?", (rid,))
        await db.commit()

async def snooze_reminder(rid: int, delay_sec: int = 600):
    ts = now_ts() + delay_sec
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE reminders SET trigger_at=? WHERE id=?", (ts, rid))
        await db.commit()

async def reschedule_repeating(rid: int, next_trigger: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE reminders SET trigger_at=? WHERE id=?", (next_trigger, rid))
        await db.commit()

async def list_reminders(user_id: int, guild_id: int | None = None):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if guild_id is not None:
            async with db.execute("SELECT * FROM reminders WHERE user_id=? AND guild_id=? AND active=1 ORDER BY trigger_at ASC", (user_id, guild_id)) as cur:
                return await cur.fetchall()
        else:
            async with db.execute("SELECT * FROM reminders WHERE user_id=? AND active=1 ORDER BY trigger_at ASC", (user_id,)) as cur:
                return await cur.fetchall()

# ---------- panel ----------
async def set_panel(guild_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO panel_message (guild_id, channel_id, message_id) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
            (guild_id, channel_id, message_id),
        )
        await db.commit()

async def get_panel(guild_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM panel_message WHERE guild_id=?", (guild_id,)) as cur:
            return await cur.fetchone()

async def get_all_panels():
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM panel_message") as cur:
            return await cur.fetchall()

async def get_open_sessions_for_guild(guild_id: int):
    """ギルド内で現在進行中（ended_at IS NULL）の全セッションを返す"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND ended_at IS NULL ORDER BY started_at ASC",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()

# ---------- onboarding ----------
async def set_onboarding_panel(guild_id: int, channel_id: int, message_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO onboarding_panel (guild_id, channel_id, message_id) VALUES (?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
            (guild_id, channel_id, message_id),
        )
        await db.commit()

async def get_onboarding_panel(guild_id: int):
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM onboarding_panel WHERE guild_id=?", (guild_id,)) as cur:
            return await cur.fetchone()
