"""关闭登记悬空 / visitor_open 指针未卸撕裂 专项测试（bugfix plant）。

本地夹具 + 临时 SQLite 库：每个用例独立 DATA_DIR/FIXTURES_DIR。
覆盖关闭入口、指针物化、写闸、织机视图与只读关闭登记核证的协同，
以及三条硬约束：
  - 禁止 status=closed 而 session_closings 缺行；
  - 禁止 visitor_open 仍指已关会话却回执 success；
  - 织机「当前会话」不得仍亮已关 id。
"""

import importlib
import json
import os
import sys
import uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    fixtures_dir = tmp_path / "fixtures"
    data_dir.mkdir()
    fixtures_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures_dir))
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        sys.modules.pop(name, None)

    db = importlib.import_module("app.db")
    seed = importlib.import_module("app.seed")
    seed.init_db()
    esc = importlib.import_module("app.modules.explicit_session_close")
    reject = importlib.import_module("app.modules.closed_session_reject")
    ingest = importlib.import_module("app.services.ingest")
    sessions = importlib.import_module("app.services.sessions")
    return {
        "db": db,
        "seed": seed,
        "esc": esc,
        "reject": reject,
        "ingest": ingest,
        "sessions": sessions,
        "fixtures_dir": fixtures_dir,
    }


def _seed_batch(fixtures_dir, hits, batch_id=None):
    payload = {
        "batch_id": batch_id or f"B-{uuid.uuid4().hex[:8]}",
        "hits": hits,
    }
    (fixtures_dir / "ingest_batch.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def _ingest(env, hits, batch_id="B-seed"):
    _seed_batch(env["fixtures_dir"], hits, batch_id=batch_id)
    report = env["ingest"].run_batch()
    assert report["ok"] is True
    return report


def _two_hits(visitor="V-9001"):
    return [
        {
            "id": f"{visitor}-T1",
            "visitor_id": visitor,
            "event_ts": "2026-11-19T08:00:00Z",
            "page_path": "/home",
            "event_name": "view",
            "visitor_label": "测试访客",
        },
        {
            "id": f"{visitor}-T2",
            "visitor_id": visitor,
            "event_ts": "2026-11-19T08:01:00Z",
            "page_path": "/cart",
            "event_name": "click",
        },
    ]


def _open_session_id(conn, visitor_id):
    return conn.execute(
        "SELECT open_session_id FROM visitors WHERE id=?", (visitor_id,)
    ).fetchone()["open_session_id"]


# ---------- 1. 关闭成功：登记可查（含 close_id）+ 指针卸下 ----------

def test_close_leaves_queryable_registry_and_dismounts_pointer(app_env):
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        # 收流后指针物化到该 open 会话
        assert _open_session_id(conn, "V-9001") == sid

        result = env["sessions"].close_open_session(conn, sid)

        # 回执带 close_id，且 close_id 可查到登记行
        assert result["close_id"]
        assert result["visitor_open"] is None
        closing = env["esc"].get_closing_by_close_id(conn, result["close_id"])
        assert closing is not None
        assert closing["session_id"] == sid
        assert json.loads(closing["hit_ids_json"]) == ["V-9001-T1", "V-9001-T2"]

        # 硬约束 1：status=closed 且登记不缺行
        assert conn.execute(
            "SELECT status FROM sessions WHERE id=?", (sid,)
        ).fetchone()["status"] == "closed"
        # 硬约束 2：visitor_open 已卸（NULL），不再指向已关会话
        assert _open_session_id(conn, "V-9001") is None

        att = env["esc"].registry_attestation(conn, sid)
        assert att["verified"] is True
        assert att["conclusion"] == "consistent"
        assert att["close_id"] == result["close_id"]
        assert att["visitor_open"] is None
        assert att["checks"]["pointer_dismounted"] is True
    finally:
        conn.close()


def test_close_repoints_pointer_to_other_real_open(app_env):
    """同访客另有 open：关闭后 visitor_open 改指它，而非置 NULL。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        first_sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        # 再造一个更晚的 open 会话（含成员，可被关闭）
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) "
            "VALUES ('S-later','V-9001','2026-11-19T08:30:00Z','2026-11-19T08:31:00Z',1,'open')"
        )
        conn.execute(
            "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel) "
            "VALUES ('L1','V-9001','S-later','2026-11-19T08:31:00Z','/z','view','primary')"
        )
        conn.execute(
            "UPDATE visitors SET open_session_id='S-later' WHERE id='V-9001'"
        )
        conn.commit()

        env["sessions"].close_open_session(conn, "S-later")

        # 指针改指仍 open 的旧会话，绝不留在已关的 S-later 上
        assert _open_session_id(conn, "V-9001") == first_sid
        att = env["esc"].registry_attestation(conn, "S-later")
        assert att["verified"] is True
        assert att["visitor_open"] == first_sid
        assert att["checks"]["pointer_target_open"] is True
    finally:
        conn.close()


# ---------- 2. 硬约束：终检失败整体回滚，无悬空/撕裂 ----------

def test_registry_insert_failure_rolls_back_status_and_pointer(app_env):
    """登记写入失败：status 与指针必须一起回滚，禁止 closed 缺登记。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    sid = conn.execute(
        "SELECT id FROM sessions WHERE visitor_id='V-9001'"
    ).fetchone()["id"]

    class FlakyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO SESSION_CLOSINGS"):
                raise RuntimeError("boom: registry write failed")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(RuntimeError):
        env["sessions"].close_open_session(FlakyConn(conn), sid)

    assert conn.execute(
        "SELECT status FROM sessions WHERE id=?", (sid,)
    ).fetchone()["status"] == "open"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM session_closings WHERE session_id=?", (sid,)
    ).fetchone()["c"] == 0
    # 指针仍在（会话仍 open），无撕裂
    assert _open_session_id(conn, "V-9001") == sid
    conn.close()


def test_invariant_check_failure_rolls_back_whole_close(app_env):
    """模拟终检发现撕裂（指针仍指已关会话）：整笔回滚，回执不得成功。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    sid = conn.execute(
        "SELECT id FROM sessions WHERE visitor_id='V-9001'"
    ).fetchone()["id"]

    real_attestation = env["esc"].registry_attestation

    def tampered_attestation(c, session_id):
        out = real_attestation(c, session_id)
        if session_id == sid:
            out["verified"] = False
            out["conclusion"] = "pointer_still_mounted"
            out["checks"]["pointer_dismounted"] = False
        return out

    env["esc"].registry_attestation = tampered_attestation
    try:
        with pytest.raises(env["esc"].CloseError):
            env["sessions"].close_open_session(conn, sid)
    finally:
        env["esc"].registry_attestation = real_attestation

    # 三方全部回滚：仍 open、无登记、无核证新增
    assert conn.execute(
        "SELECT status FROM sessions WHERE id=?", (sid,)
    ).fetchone()["status"] == "open"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM session_closings WHERE session_id=?", (sid,)
    ).fetchone()["c"] == 0
    conn.close()


# ---------- 3. 写闸：关闭后再收流不误挂已关会话；撕裂指针自愈 ----------

def test_ingest_after_close_does_not_mount_closed_session(app_env):
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        env["sessions"].close_open_session(conn, sid)
    finally:
        conn.close()

    _ingest(
        env,
        [
            {
                "id": "T3",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T09:00:00Z",
                "page_path": "/checkout",
                "event_name": "pay",
            }
        ],
        batch_id="B-after-close",
    )
    conn = env["db"].connect()
    try:
        # 新流被拒收，未挂到已关会话；指针保持 NULL，未被重新点亮
        assert conn.execute(
            "SELECT 1 FROM hits WHERE id='T3'"
        ).fetchone() is None
        assert _open_session_id(conn, "V-9001") is None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine WHERE reason=?",
            (env["reject"].REJECT_REASON,),
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_torn_pointer_routes_to_open_and_is_repaired(app_env):
    """人为制造撕裂指针（指向 closed）：闸口不得挂它，回退 open 并修指针。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        closed_sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        # 另建一个真正 open 的会话
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) "
            "VALUES ('S-open2','V-9001','2026-11-19T08:40:00Z',NULL,0,'open')"
        )
        conn.commit()
        env["sessions"].close_open_session(conn, closed_sid)
        # 关闭把指针改指 S-open2；再人为撕回已关会话
        conn.execute(
            "UPDATE visitors SET open_session_id=? WHERE id='V-9001'",
            (closed_sid,),
        )
        conn.commit()

        sid, rejected = env["reject"].route_hit(
            conn,
            {
                "id": "T7",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T09:10:00Z",
                "page_path": "/back",
                "event_name": "view",
            },
        )
        assert rejected is False
        assert sid == "S-open2"  # 绝不返回撕裂指针上的 closed 会话
    finally:
        conn.close()

    _ingest(
        env,
        [
            {
                "id": "T7",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T09:10:00Z",
                "page_path": "/back",
                "event_name": "view",
            }
        ],
        batch_id="B-repair",
    )
    conn = env["db"].connect()
    try:
        # 写闸同事务把指针修回真正 open
        assert _open_session_id(conn, "V-9001") == "S-open2"
        assert conn.execute(
            "SELECT session_id FROM hits WHERE id='T7'"
        ).fetchone()["session_id"] == "S-open2"
    finally:
        conn.close()


def test_torn_pointer_without_open_is_cleared_on_reject(app_env):
    """撕裂指针且无其它 open：拒收同时把指针卸成 NULL。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        env["sessions"].close_open_session(conn, sid)
        conn.execute(
            "UPDATE visitors SET open_session_id=? WHERE id='V-9001'", (sid,)
        )
        conn.commit()
    finally:
        conn.close()

    _ingest(
        env,
        [
            {
                "id": "T8",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T09:20:00Z",
                "page_path": "/x",
                "event_name": "ping",
            }
        ],
        batch_id="B-torn-reject",
    )
    conn = env["db"].connect()
    try:
        assert _open_session_id(conn, "V-9001") is None
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hits WHERE id='T8'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


# ---------- 4. 核证页 / 织机视图协同 ----------

def test_registry_attestation_flags_dangling_and_torn(app_env):
    """核证须能检出两类撕裂：closed 缺登记、指针仍指已关会话。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        env["sessions"].close_open_session(conn, sid)

        # 撕法一：删掉登记行（悬空关闭）
        conn.execute("DELETE FROM session_closings WHERE session_id=?", (sid,))
        conn.commit()
        att = env["esc"].registry_attestation(conn, sid)
        assert att["verified"] is False
        assert att["checks"]["registry_row_present"] is False
        assert "registry_missing" in att["conclusion"]

        # 恢复登记，再撕法二：指针挂回已关会话
        conn.execute(
            "INSERT INTO session_closings(close_id, session_id, visitor_id, hit_ids_json,"
            " hit_count, content_hash, closed_at) VALUES (?,?,?,?,?,?,?)",
            (
                "C-restored",
                sid,
                "V-9001",
                json.dumps(["V-9001-T1", "V-9001-T2"]),
                2,
                "x" * 64,
                "2026-11-19T08:05:00Z",
            ),
        )
        conn.execute(
            "UPDATE visitors SET open_session_id=? WHERE id='V-9001'", (sid,)
        )
        conn.commit()
        att = env["esc"].registry_attestation(conn, sid)
        assert att["verified"] is False
        assert att["checks"]["pointer_dismounted"] is False
        assert "pointer_still_mounted" in att["conclusion"]

        # 全量核证页同样要把异常行列出来（不静默吞掉悬空/撕裂）
        bulk = env["esc"].list_registry_attestations(conn)
        row = [r for r in bulk if r["session_id"] == sid][0]
        assert row["verified"] is False
        assert "pointer_still_mounted" in row["conclusion"]
    finally:
        conn.close()


def test_loom_views_never_light_closed_id_as_current(app_env):
    """织机数据源：列表/详情带出的 visitor_open 不得等于已关会话 id。"""
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
        env["sessions"].close_open_session(conn, sid)
    finally:
        conn.close()

    conn = env["db"].connect()
    try:
        listed = [s for s in env["sessions"].list_sessions(conn) if s["id"] == sid][0]
        assert listed["status"] == "closed"
        assert listed["visitor_open"] != sid  # 顶栏无已关 id 可亮

        detail = env["sessions"].session_detail(conn, sid)
        assert detail["closing"] is not None  # closed 必带登记
        assert detail["visitor_open"] != sid

        visitors = conn.execute(
            "SELECT open_session_id FROM visitors WHERE id='V-9001'"
        ).fetchone()
        assert visitors["open_session_id"] is None
    finally:
        conn.close()


def test_list_registry_attestations_covers_all_closed(app_env):
    env = app_env
    _ingest(env, _two_hits("V-a"))
    _ingest(env, _two_hits("V-b"), batch_id="B-seed-b")
    conn = env["db"].connect()
    try:
        sids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM sessions ORDER BY id"
            ).fetchall()
        ]
        env["sessions"].close_open_session(conn, sids[0])
    finally:
        conn.close()

    conn = env["db"].connect()
    try:
        rows = env["esc"].list_registry_attestations(conn)
        assert len(rows) == 1  # 只有一个 closed
        only = rows[0]
        assert only["verified"] is True
        assert only["conclusion"] == "consistent"
        assert only["close_id"]
    finally:
        conn.close()


# ---------- 5. HTTP：新只读核证接口 ----------

def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_http_closing_attestation_endpoints(app_env):
    env = app_env
    _ingest(env, _two_hits())
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
    finally:
        conn.close()

    client = _client()
    close_res = client.post(f"/api/sessions/{sid}/close").json()
    assert close_res["close_id"]
    assert close_res["visitor_open"] is None

    att = client.get(f"/api/sessions/{sid}/closing-attestation")
    assert att.status_code == 200
    body = att.json()
    assert body["verified"] is True
    assert body["conclusion"] == "consistent"
    assert body["close_id"] == close_res["close_id"]
    assert set(body["checks"]) == {
        "status_is_closed",
        "registry_row_present",
        "close_id_registered",
        "pointer_dismounted",
        "pointer_target_open",
    }

    listed = client.get("/api/closing-attestations")
    assert listed.status_code == 200
    assert any(r["session_id"] == sid and r["verified"] for r in listed.json())

    # 不存在的会话 / 未关闭（无登记）会话 → 404
    assert client.get("/api/sessions/S-ghost/closing-attestation").status_code == 404
    other_seed = [
        {
            "id": "O1",
            "visitor_id": "V-open",
            "event_ts": "2026-11-19T11:00:00Z",
            "page_path": "/a",
            "event_name": "view",
            "visitor_label": "未关访客",
        }
    ]
    _seed_batch(env["fixtures_dir"], other_seed, batch_id="B-open-row")
    env["ingest"].run_batch()
    conn = env["db"].connect()
    try:
        open_sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-open'"
        ).fetchone()["id"]
    finally:
        conn.close()
    assert (
        client.get(f"/api/sessions/{open_sid}/closing-attestation").status_code == 404
    )
    # 核证接口只读：重复调用不产生任何登记行
    conn = env["db"].connect()
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM session_closings"
        ).fetchone()["c"]
    finally:
        conn.close()
    client.get(f"/api/sessions/{sid}/closing-attestation")
    client.get("/api/closing-attestations")
    conn = env["db"].connect()
    try:
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM session_closings"
        ).fetchone()["c"]
        assert before == after == 1
    finally:
        conn.close()


def test_http_visitors_and_sessions_expose_pointer(app_env):
    env = app_env
    _ingest(env, _two_hits())
    client = _client()
    visitors = client.get("/api/visitors").json()
    v = [x for x in visitors if x["id"] == "V-9001"][0]
    sid = v["open_session_id"]
    assert sid  # 收流后指针已物化

    client.post(f"/api/sessions/{sid}/close")
    v_after = [x for x in client.get("/api/visitors").json() if x["id"] == "V-9001"][0]
    assert v_after["open_session_id"] is None
    s_after = [s for s in client.get("/api/sessions").json() if s["id"] == sid][0]
    assert s_after["status"] == "closed"
    assert s_after["visitor_open"] is None
