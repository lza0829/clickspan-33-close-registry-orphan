import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.modules import closed_session_reject as reject_gate


def run_batch() -> dict:
    """基座装载：按夹具追加击点；超时切会话 / 脏点隔离留给功能题空桩。

    收流闸（closed_session_reject）：显式指向 closed 会话、或访客已无 open
    可挂且最新会话已关闭的击点一律隔离，不写入、不新建到旧会话——关闭后
    成员不可追加；仍有 open 会话时挂最新 open。
    """
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    hits = payload.get("hits", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        written = 0
        rejected = 0
        for hit in hits:
            vid = hit["visitor_id"]
            exists = conn.execute("SELECT 1 FROM visitors WHERE id=?", (vid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
                    (vid, hit.get("visitor_label") or vid, _now()),
                )

            # 收流闸：关闭后的会话拒收（同事务，拒收进 quarantine）
            sid, is_rejected = reject_gate.route_hit(conn, hit)
            if is_rejected:
                # 写闸协同：指针若仍撕裂地指向已关会话，同事务卸成 NULL
                conn.execute(
                    """
                    UPDATE visitors SET open_session_id = NULL
                    WHERE id = ?
                      AND open_session_id IS NOT NULL
                      AND EXISTS (
                        SELECT 1 FROM sessions s
                        WHERE s.id = visitors.open_session_id
                          AND s.status != 'open'
                      )
                    """,
                    (vid,),
                )
                reject_gate.quarantine_hit(
                    conn,
                    hit,
                    f"会话已关闭，拒收击点 {hit.get('id')}",
                )
                rejected += 1
                continue

            if sid:
                # 双保险：即便闸口放行，也绝不写 closed 会话
                row = conn.execute(
                    "SELECT status FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                if row and row["status"] != "open":
                    reject_gate.quarantine_hit(
                        conn, hit, f"会话状态 {row['status']} 不可写"
                    )
                    rejected += 1
                    continue
                # 指针物化：路由落到 open 会话但访客指针未指向它（撕裂/为空）时补齐
                conn.execute(
                    """
                    UPDATE visitors SET open_session_id = ?
                    WHERE id = ?
                      AND (open_session_id IS NULL OR open_session_id != ?)
                    """,
                    (sid, vid, sid),
                )
            else:
                sid = f"S-{vid}-{uuid.uuid4().hex[:6]}"
                conn.execute(
                    """
                    INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
                    VALUES (?,?,?,?,0,'open')
                    """,
                    (sid, vid, hit["event_ts"], hit["event_ts"]),
                )
                # 新会话即访客当前 open：物化指针，与关闭卸指同表同列
                conn.execute(
                    "UPDATE visitors SET open_session_id=? WHERE id=?",
                    (sid, vid),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hit["id"],
                    vid,
                    sid,
                    hit["event_ts"],
                    hit["page_path"],
                    hit["event_name"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                conn.execute(
                    """
                    UPDATE sessions
                    SET hit_count = hit_count + 1,
                        ended_at = CASE
                          WHEN ended_at IS NULL OR ended_at < ? THEN ?
                          ELSE ended_at END
                    WHERE id=?
                    """,
                    (hit["event_ts"], hit["event_ts"], sid),
                )
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                        (sid,),
                    ).fetchall()
                ]
                sess_row = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO session_proofs(
                      session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        sid,
                        vid,
                        json.dumps(ids, ensure_ascii=False),
                        sess_row["hit_count"],
                        sess_row["started_at"],
                        sess_row["ended_at"],
                        _now(),
                    ),
                )
        report = {
            "batch_id": batch_id,
            "written": written,
            "rejected_closed": rejected,
            "input": len(hits),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

