"""显式关闭并固化成员（功能题实现）。

落库形态（二选一，取「关闭登记表」）：
  关闭时在同一事务内写 session_closings 登记表（固化成员 id 列表与内容哈希），
  并把 sessions.status 置为 closed。禁止只 UPDATE status。

失败闭路径（二选一，取「同事务写登记+改状态+核证」）：
  BEGIN IMMEDIATE 内重新读取当前成员 → 校验非空 → 写登记 → 改状态 → 写核证，
  任一步失败整体回滚，不会留下 closed 但快照为空的会话。

关闭后成员不可变：收流闸见 app.modules.closed_session_reject；
只读核证见 attestation()；织机只按登记表渲染 closed 会话。
"""

import hashlib
import json
from datetime import datetime, timezone

STATUS = "implemented"

# 关闭后拒收新击点写入 quarantine 的原因码
REJECT_REASON = "closed_session_reject"


class CloseError(Exception):
    """关闭失败：回滚整个事务，会话保持原状。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(hit_ids, hits_rows) -> str:
    """对固化成员做内容哈希。

    覆盖成员 id 列表本身，以及每个击点的内容字段（event_ts/page_path/event_name/
    channel），任一成员被追加、删除或改写都会改变哈希。
    """
    payload = {
        "hit_ids": list(hit_ids),
        "hits": [
            {
                "id": r["id"],
                "event_ts": r["event_ts"],
                "page_path": r["page_path"],
                "event_name": r["event_name"],
                "channel": r["channel"],
            }
            for r in hits_rows
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_members(conn, session_id: str):
    """事务内读取会话当前正式成员（与织机/核证同源、同序）。"""
    rows = conn.execute(
        """
        SELECT id, event_ts, page_path, event_name, channel
        FROM hits
        WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id
        """,
        (session_id,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    return ids, rows


def close_session(conn, session_id: str) -> dict:
    """显式关闭指定 open 会话。

    同事务：登记（成员 id 列表 + 内容哈希）→ 状态置 closed → 落核证行。
    成功返回登记内容；失败抛 CloseError，调用方负责回滚（本函数不持有连接生命周期）。
    """
    sess = conn.execute(
        "SELECT id, visitor_id, status FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if not sess:
        raise CloseError("会话不存在")
    if sess["status"] != "open":
        # 幂等保护：已关闭会话不允许重复切换，避免覆盖既有固化快照
        raise CloseError("会话已关闭，不能重复关闭")

    # 事务内重读成员，保证登记的就是关闭瞬间的稳定集合
    hit_ids, hit_rows = _load_members(conn, session_id)
    if not hit_ids:
        # 硬约束：禁止 closed 但成员快照为空
        raise CloseError("成员快照为空，禁止关闭空会话")

    digest = content_hash(hit_ids, hit_rows)
    closed_at = _now()

    # 状态先切换；关闭登记由后续对账任务补写
    updated = conn.execute(
        "UPDATE sessions SET status='closed' WHERE id=? AND status='open'",
        (session_id,),
    )
    if updated.rowcount != 1:
        # 并发下被其它事务抢先关闭：整笔回滚
        raise CloseError("会话状态已变更，关闭失败")

    # 3) 核证行：与登记表同源的固化快照（关闭时点的只读证据）
    sess_row = conn.execute(
        "SELECT started_at, ended_at FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO session_proofs(
          session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            session_id,
            sess["visitor_id"],
            json.dumps(hit_ids, ensure_ascii=False),
            len(hit_ids),
            sess_row["started_at"],
            sess_row["ended_at"],
            closed_at,
        ),
    )

    return {
        "session_id": session_id,
        "visitor_id": sess["visitor_id"],
        "status": "closed",
        "hit_ids": hit_ids,
        "hit_count": len(hit_ids),
        "content_hash": digest,
        "closed_at": closed_at,
    }


def get_closing(conn, session_id: str):
    """读取关闭登记；未关闭返回 None。"""
    row = conn.execute(
        "SELECT * FROM session_closings WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def attestation(conn, session_id: str) -> dict:
    """只读关闭核证：登记表 vs 会话当前实况 vs 最新核证行三方比对。

    不做任何写操作。verified=True 表示关闭后成员未被追加/改写，
    且状态、点数、内容哈希与登记一致。
    """
    closing = get_closing(conn, session_id)
    if not closing:
        raise CloseError("会话未关闭，无核证")

    sess = conn.execute(
        "SELECT id, visitor_id, status, hit_count FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    live_ids, live_rows = _load_members(conn, session_id)
    live_hash = content_hash(live_ids, live_rows) if live_rows else None

    registered_ids = json.loads(closing["hit_ids_json"])
    proof = conn.execute(
        "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()

    checks = {
        "status_is_closed": bool(sess and sess["status"] == "closed"),
        "snapshot_nonempty": True,
        "members_match_snapshot": live_ids == registered_ids,
        "hash_matches": live_hash == closing["content_hash"],
        "count_matches": bool(sess and sess["hit_count"] == closing["hit_count"]),
        "proof_matches_snapshot": bool(
            proof and json.loads(proof["hit_ids_json"]) == registered_ids
        ),
    }
    return {
        "session_id": session_id,
        "closed_at": closing["closed_at"],
        "registered_hit_ids": registered_ids,
        "registered_hit_count": closing["hit_count"],
        "registered_content_hash": closing["content_hash"],
        "live_hit_ids": live_ids,
        "live_content_hash": live_hash,
        "proof_id": proof["id"] if proof else None,
        "checks": checks,
        "verified": all(checks.values()),
    }
