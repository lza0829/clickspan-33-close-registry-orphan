"""已关闭会话拒收新击点（功能题实现）。

收流闸：收流时若击点显式指向 closed 会话，或其访客已无 open 会话可挂
（最新会话已关闭），不得写入或新建到已关闭会话——一律隔离进 quarantine，
保证关闭后旧成员不被追加。

关闭登记与核证在 app.modules.explicit_session_close。
"""

import json
from datetime import datetime, timezone

STATUS = "implemented"

REJECT_REASON = "closed_session_reject"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_hit(conn, hit: dict):
    """决定击点的目标会话。

    返回 (session_id, rejected)：
      - 显式带 session_id 且该会话 closed → 拒收（隔离）
      - 显式带 session_id 且 open → 挂该会话
      - 未带 session_id：访客仍有 open 会话则挂最新 open（与基座同源）；
        无 open 且最新会话已 closed → 拒收，不新建到旧会话；
        访客无任何会话 → 交由收流服务新建 open 会话
    """
    vid = hit["visitor_id"]
    explicit_sid = hit.get("session_id")

    if explicit_sid:
        sess = conn.execute(
            "SELECT id, status FROM sessions WHERE id=?",
            (explicit_sid,),
        ).fetchone()
        if sess and sess["status"] == "closed":
            return None, True
        return explicit_sid, False

    latest_open = conn.execute(
        """
        SELECT id FROM sessions
        WHERE visitor_id=? AND status='open'
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (vid,),
    ).fetchone()
    if latest_open:
        return latest_open["id"], False

    latest = conn.execute(
        """
        SELECT id, status FROM sessions
        WHERE visitor_id=?
        ORDER BY started_at DESC, id DESC LIMIT 1
        """,
        (vid,),
    ).fetchone()
    if latest and latest["status"] == "closed":
        return None, True
    return None, False  # 无会话：由收流服务新建 open 会话


def quarantine_hit(conn, hit: dict, detail: str) -> None:
    """把被拒收的击点写入搁置区。"""
    conn.execute(
        """
        INSERT INTO quarantine(visitor_id, reason, member_json, created_at)
        VALUES (?,?,?,?)
        """,
        (
            hit.get("visitor_id"),
            REJECT_REASON,
            json.dumps({"hit": hit, "detail": detail}, ensure_ascii=False),
            _now(),
        ),
    )
