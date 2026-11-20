import json

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  -- 访客当前 open 会话指针（落库形态：访客行关闭指针快照列）。
  -- NULL 表示该访客名下无当前 open；关闭会话时同事务卸掉或改指真正 open。
  open_session_id TEXT REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_closings (
  -- 关闭登记对照表：每次成功关闭必须在此落下可按 close_id 检索的登记行，
  -- 与 sessions.status=closed、visitors.open_session_id 卸指同事务同成败。
  close_id TEXT NOT NULL UNIQUE,
  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  closed_at TEXT NOT NULL
);
"""


def _column_names(conn, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn):
    """幂等迁移：为已存在的旧库补关闭指针列、close_id 列并回填。"""
    visitor_cols = _column_names(conn, "visitors")
    if "open_session_id" not in visitor_cols:
        conn.execute("ALTER TABLE visitors ADD COLUMN open_session_id TEXT")

    closing_cols = _column_names(conn, "session_closings")
    if "close_id" not in closing_cols:
        conn.execute("ALTER TABLE session_closings ADD COLUMN close_id TEXT")

    # 回填访客当前 open 指针：每个访客取最新 open 会话（与收流闸同序）
    conn.execute(
        """
        UPDATE visitors
        SET open_session_id = (
          SELECT s.id FROM sessions s
          WHERE s.visitor_id = visitors.id AND s.status = 'open'
          ORDER BY s.started_at DESC, s.id DESC LIMIT 1
        )
        WHERE open_session_id IS NULL
          AND EXISTS (
            SELECT 1 FROM sessions s
            WHERE s.visitor_id = visitors.id AND s.status = 'open'
          )
        """
    )
    # 回填历史登记行的 close_id（旧库可能已有无 close_id 的关闭登记）
    rows = conn.execute(
        "SELECT session_id FROM session_closings WHERE close_id IS NULL"
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE session_closings SET close_id=? WHERE session_id=?",
            (f"C-{r['session_id']}", r["session_id"]),
        )


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at, open_session_id) VALUES (?,?,?,NULL)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 基座：按访客整段落进一个 open 会话（超时切分留给功能题空桩）
    for visitor_id, group in _group_hits(data.get("hits", [])).items():
        group = sorted(group, key=lambda h: (h["event_ts"], h["id"]))
        sid = f"S-{visitor_id}-1"
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) VALUES (?,?,?,?,?,?)",
            (
                sid,
                visitor_id,
                group[0]["event_ts"],
                group[-1]["event_ts"],
                len(group),
                "open",
            ),
        )
        # 指针物化：访客当前 open 指向该基座会话
        conn.execute(
            "UPDATE visitors SET open_session_id=? WHERE id=?", (sid, visitor_id)
        )
        hit_ids = []
        for h in group:
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    h["id"],
                    visitor_id,
                    sid,
                    h["event_ts"],
                    h["page_path"],
                    h["event_name"],
                    h.get("channel", "primary"),
                ),
            )
            hit_ids.append(h["id"])
        conn.execute(
            """
            INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            """,
            (
                sid,
                visitor_id,
                json.dumps(hit_ids, ensure_ascii=False),
                len(hit_ids),
                group[0]["event_ts"],
                group[-1]["event_ts"],
            ),
        )


def _group_hits(hits):
    out = {}
    for h in hits:
        out.setdefault(h["visitor_id"], []).append(h)
    return out


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800
