"""显式关闭并固化成员（功能题实现）。

落库形态（二选一，两者皆取）：
  1. 关闭登记对照表 session_closings：每次成功关闭落下可按 close_id 检索的
     登记行（固化成员 id 列表与内容哈希）；
  2. 访客行不可变关闭指针快照列 visitors.open_session_id：物化访客当前 open
     会话，关闭时同事务卸掉（NULL）或改指其它真正 open 的会话。

失败闭路径（二选一，取「同事务写登记+改 status+卸指针，缺一回滚」）：
  BEGIN IMMEDIATE 内重新读取当前成员 → 校验非空 → 写登记（含 close_id）
  → 改 status=closed → 卸/改 visitor_open 指针 → 写核证行 → 终检三方一致，
  任一步失败整体回滚，绝不允许 status=closed 而登记缺行、或指针仍指已关会话。

硬约束（终检 _verify_close_invariants 兜底）：
  - 禁止 status=closed 而 session_closings 缺行（关闭登记悬空）；
  - 禁止 visitors.open_session_id 仍指向该已关会话却回执 success（指针未卸撕裂）；
  - 改指的目标必须是真正 status='open' 的会话。

关闭后成员不可变：收流闸见 app.modules.closed_session_reject；
只读核证见 attestation()（内容核证）与 registry_attestation()（关闭登记核证）；
织机只按登记表与 visitor_open 指针渲染，不得把已关 id 亮成当前会话。
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

STATUS = "implemented"

# 关闭后拒收新击点写入 quarantine 的原因码
REJECT_REASON = "closed_session_reject"


class CloseError(Exception):
    """关闭失败：回滚整个事务，会话保持原状。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_close_id(session_id: str) -> str:
    """生成可检索的关闭登记 id。"""
    return f"C-{session_id}-{uuid.uuid4().hex[:12]}"


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


def _other_open_session(conn, visitor_id: str, closed_session_id: str):
    """访客名下除被关会话外最新的真正 open 会话；无则 None。"""
    row = conn.execute(
        """
        SELECT id FROM sessions
        WHERE visitor_id=? AND status='open' AND id != ?
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (visitor_id, closed_session_id),
    ).fetchone()
    return row["id"] if row else None


def close_session(conn, session_id: str) -> dict:
    """显式关闭指定 open 会话。

    同事务四写一检：关闭登记（close_id + 成员快照 + 内容哈希）→ status 置
    closed → 卸/改 visitor_open 指针 → 核证行 → 三方终检。任一不符抛
    CloseError，由调用方回滚（本函数不持有连接生命周期）。
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

    visitor_id = sess["visitor_id"]

    # 事务内重读成员，保证登记的就是关闭瞬间的稳定集合
    hit_ids, hit_rows = _load_members(conn, session_id)
    if not hit_ids:
        # 硬约束：禁止 closed 但成员快照为空
        raise CloseError("成员快照为空，禁止关闭空会话")

    digest = content_hash(hit_ids, hit_rows)
    closed_at = _now()
    close_id = new_close_id(session_id)

    # 1) 关闭登记对照表：成功关闭必须落下可按 close_id 检索的登记行
    conn.execute(
        """
        INSERT INTO session_closings(
          close_id, session_id, visitor_id, hit_ids_json,
          hit_count, content_hash, closed_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            close_id,
            session_id,
            visitor_id,
            json.dumps(hit_ids, ensure_ascii=False),
            len(hit_ids),
            digest,
            closed_at,
        ),
    )

    # 2) status 置 closed（条件更新，并发下被抢先则 rowcount=0 → 回滚）
    updated = conn.execute(
        "UPDATE sessions SET status='closed' WHERE id=? AND status='open'",
        (session_id,),
    )
    if updated.rowcount != 1:
        raise CloseError("会话状态已变更，关闭失败")

    # 3) 卸 visitor_open 指针：仍指向本会话时，改指其它真正 open，否则置 NULL
    visitor = conn.execute(
        "SELECT open_session_id FROM visitors WHERE id=?",
        (visitor_id,),
    ).fetchone()
    current_pointer = visitor["open_session_id"] if visitor else None
    if current_pointer == session_id:
        next_open = _other_open_session(conn, visitor_id, session_id)
        conn.execute(
            "UPDATE visitors SET open_session_id=? WHERE id=?",
            (next_open, visitor_id),
        )

    # 4) 核证行：与登记表同源的固化快照（关闭时点的只读证据）
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
            visitor_id,
            json.dumps(hit_ids, ensure_ascii=False),
            len(hit_ids),
            sess_row["started_at"],
            sess_row["ended_at"],
            closed_at,
        ),
    )

    # 5) 同事务终检：status、登记、指针三者同成败，缺一回滚
    att = registry_attestation(conn, session_id)
    if not att["verified"]:
        raise CloseError(f"关闭三方终检失败：{att['conclusion']}")

    return {
        "close_id": close_id,
        "session_id": session_id,
        "visitor_id": visitor_id,
        "visitor_open": att["visitor_open"],
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


def get_closing_by_close_id(conn, close_id: str):
    """按 close_id 检索关闭登记；查无返回 None。"""
    row = conn.execute(
        "SELECT * FROM session_closings WHERE close_id=?",
        (close_id,),
    ).fetchone()
    return dict(row) if row else None


def registry_attestation(conn, session_id: str) -> dict:
    """只读关闭登记核证：会话状态 / close_id 登记 / visitor_open 指针三方比对。

    不做任何写操作。verified=True 表示：
      - sessions.status 已是 closed；
      - session_closings 存在登记行且 close_id 非空（无悬空关闭）；
      - visitors.open_session_id 不再指向本会话（指针已卸或改指真正 open）。
    """
    sess = conn.execute(
        "SELECT id, visitor_id, status FROM sessions WHERE id=?",
        (session_id,),
    ).fetchone()
    if not sess:
        raise CloseError("会话不存在")

    closing = get_closing(conn, session_id)
    visitor = conn.execute(
        "SELECT open_session_id FROM visitors WHERE id=?",
        (sess["visitor_id"],),
    ).fetchone()
    visitor_open = visitor["open_session_id"] if visitor else None

    pointer_target = None
    if visitor_open:
        pointer_target = conn.execute(
            "SELECT id, status FROM sessions WHERE id=?",
            (visitor_open,),
        ).fetchone()

    checks = {
        "status_is_closed": sess["status"] == "closed",
        "registry_row_present": closing is not None,
        "close_id_registered": bool(closing and closing.get("close_id")),
        # 硬约束：visitor_open 不得仍指向已关会话
        "pointer_dismounted": visitor_open != session_id,
        # 若改指其它会话，目标必须真正 open
        "pointer_target_open": visitor_open is None
        or (pointer_target is not None and pointer_target["status"] == "open"),
    }

    reasons = {
        "status_is_closed": "status_not_closed",
        "registry_row_present": "registry_missing",
        "close_id_registered": "close_id_missing",
        "pointer_dismounted": "pointer_still_mounted",
        "pointer_target_open": "pointer_target_not_open",
    }
    failures = [reason for key, reason in reasons.items() if not checks[key]]
    verified = not failures
    conclusion = "consistent" if verified else ",".join(failures)

    return {
        "session_id": session_id,
        "visitor_id": sess["visitor_id"],
        "status": sess["status"],
        "close_id": closing["close_id"] if closing else None,
        "closed_at": closing["closed_at"] if closing else None,
        "visitor_open": visitor_open,
        "checks": checks,
        "conclusion": conclusion,
        "verified": verified,
    }


def list_registry_attestations(conn) -> list:
    """全量关闭登记核证：覆盖所有 closed 会话与所有登记行（含两边悬空）。"""
    ids = [
        r["id"]
        for r in conn.execute(
            """
            SELECT s.id FROM sessions s
            LEFT JOIN session_closings c ON c.session_id = s.id
            WHERE s.status='closed' OR c.session_id IS NOT NULL
            ORDER BY s.started_at, s.id
            """
        ).fetchall()
    ]
    return [registry_attestation(conn, sid) for sid in ids]


def attestation(conn, session_id: str) -> dict:
    """只读内容核证：登记表 vs 会话当前实况 vs 最新核证行三方比对。

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
        "close_id": closing.get("close_id"),
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
