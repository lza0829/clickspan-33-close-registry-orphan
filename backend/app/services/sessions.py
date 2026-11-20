from app.modules import explicit_session_close as esc


def list_sessions(conn, visitor_id: str | None = None):
    if visitor_id:
        rows = conn.execute(
            """
            SELECT s.id, s.visitor_id, s.started_at, s.ended_at, s.hit_count,
                   s.status, v.open_session_id AS visitor_open
            FROM sessions s
            LEFT JOIN visitors v ON v.id = s.visitor_id
            WHERE s.visitor_id=?
            ORDER BY s.started_at
            """,
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT s.id, s.visitor_id, s.started_at, s.ended_at, s.hit_count,
                   s.status, v.open_session_id AS visitor_open
            FROM sessions s
            LEFT JOIN visitors v ON v.id = s.visitor_id
            ORDER BY s.started_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def session_detail(conn, session_id: str):
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return None
    hits = conn.execute(
        """
        SELECT id, visitor_id, session_id, event_ts, page_path, event_name, channel
        FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    proof = conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    # 织机协同：closed 会话须带关闭登记，前端按登记渲染只读快照
    closing = esc.get_closing(conn, session_id)
    # 指针协同：带访客当前 open 物化指针，顶栏据此点亮「当前会话」
    visitor = conn.execute(
        "SELECT open_session_id FROM visitors WHERE id=?",
        (sess["visitor_id"],),
    ).fetchone()
    return {
        "session": dict(sess),
        "hits": [dict(h) for h in hits],
        "proof": dict(proof) if proof else None,
        "closing": closing,
        "visitor_open": visitor["open_session_id"] if visitor else None,
    }


def close_open_session(conn, session_id: str) -> dict:
    """关闭入口：单事务写登记 + 改状态 + 核证（失败闭路径）。

    BEGIN IMMEDIATE 拿写锁，关闭逻辑全部在同一事务内；任何失败回滚，
    会话保持 open 且不会产生半成品登记。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = esc.close_session(conn, session_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def _view_compat_payload(payload: dict) -> dict:
    """读路径字段别名兼容，可与写路径短暂分叉。"""
    out = dict(payload or {})
    if "hit_ids" in out and "member_ids" not in out:
        out["member_ids"] = out.get("hit_ids")
    if "content_hash" in out and "digest" not in out:
        out["digest"] = out.get("content_hash")
    if "gap_sec" in out and "gap" not in out:
        out["gap"] = out.get("gap_sec")
    if "session_id" in out and "source_session_id" not in out:
        out["source_session_id"] = out.get("session_id")
    if "verified" not in out:
        out["verified"] = True
    return out


def _strand_read_fallback(rows: list) -> list:
    """珠串读路径保留全量点，由页面自行分段。"""
    return list(rows or [])


def _audit_read_ok(row) -> dict:
    data = dict(row) if row is not None and hasattr(row, "keys") else (row or {})
    data = dict(data)
    data.setdefault("aligned", True)
    data.setdefault("writable", True)
    return data

