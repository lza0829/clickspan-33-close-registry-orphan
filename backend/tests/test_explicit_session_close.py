"""explicit_session_close 功能题测试。

本地夹具 + 临时 SQLite 库：每个用例独立 DATA_DIR/FIXTURES_DIR，
覆盖关闭入口、收流闸、只读核证、织机协同四条链路与全部硬约束。
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

RELOADED = (
    "app.db",
    "app.seed",
    "app.modules.explicit_session_close",
    "app.modules.closed_session_reject",
    "app.services.ingest",
    "app.services.sessions",
)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """每个用例一套独立 SQLite 库与夹具目录。"""
    data_dir = tmp_path / "data"
    fixtures_dir = tmp_path / "fixtures"
    data_dir.mkdir()
    fixtures_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(fixtures_dir))
    # 整体清掉 app 子模块，保证 db.py 在导入期读到的新环境变量生效，
    # 路由/main 也会重新绑定新的 connect（HTTP 用例的库隔离）。
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


def _ingest_two_hits(env):
    """收流两击点，落到同一访客的同一个 open 会话。"""
    hits = [
        {
            "id": "T1",
            "visitor_id": "V-9001",
            "event_ts": "2026-11-19T08:00:00Z",
            "page_path": "/home",
            "event_name": "view",
            "visitor_label": "测试访客",
        },
        {
            "id": "T2",
            "visitor_id": "V-9001",
            "event_ts": "2026-11-19T08:01:00Z",
            "page_path": "/cart",
            "event_name": "click",
        },
    ]
    _seed_batch(env["fixtures_dir"], hits, batch_id="B-seed")
    report = env["ingest"].run_batch()
    assert report["ok"] is True
    conn = env["db"].connect()
    try:
        sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-9001'"
        ).fetchone()["id"]
    finally:
        conn.close()
    return sid, hits


# ---------- 1. 关闭入口：登记 + 状态 + 核证同事务 ----------

def test_close_writes_registry_status_and_proof(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        result = env["sessions"].close_open_session(conn, sid)
        assert result["status"] == "closed"
        assert result["hit_ids"] == ["T1", "T2"]
        assert len(result["content_hash"]) == 64

        status = conn.execute(
            "SELECT status FROM sessions WHERE id=?", (sid,)
        ).fetchone()["status"]
        assert status == "closed"

        closing = conn.execute(
            "SELECT * FROM session_closings WHERE session_id=?", (sid,)
        ).fetchone()
        assert closing is not None
        assert json.loads(closing["hit_ids_json"]) == ["T1", "T2"]
        assert closing["hit_count"] == 2
        assert closing["content_hash"] == result["content_hash"]

        proof = conn.execute(
            "SELECT * FROM session_proofs WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        assert json.loads(proof["hit_ids_json"]) == ["T1", "T2"]
    finally:
        conn.close()


def test_close_is_idempotent_reject_and_keeps_snapshot(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
        first_hash = env["esc"].get_closing(conn, sid)["content_hash"]
        with pytest.raises(env["esc"].CloseError):
            env["sessions"].close_open_session(conn, sid)
        # 重复关闭不得覆盖固化快照
        assert env["esc"].get_closing(conn, sid)["content_hash"] == first_hash
    finally:
        conn.close()


def test_close_unknown_session_raises(app_env):
    env = app_env
    conn = env["db"].connect()
    try:
        with pytest.raises(env["esc"].CloseError):
            env["sessions"].close_open_session(conn, "S-nope")
    finally:
        conn.close()


# ---------- 2. 硬约束：空快照禁闭；失败整体回滚 ----------

def test_close_empty_session_forbidden_and_rolls_back(app_env):
    env = app_env
    conn = env["db"].connect()
    try:
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES ('V-empty','空','2026-11-19T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) "
            "VALUES ('S-empty','V-empty','2026-11-19T00:00:00Z',NULL,0,'open')"
        )
        conn.commit()
        with pytest.raises(env["esc"].CloseError):
            env["sessions"].close_open_session(conn, "S-empty")

        row = conn.execute(
            "SELECT status FROM sessions WHERE id='S-empty'"
        ).fetchone()
        assert row["status"] == "open"  # 没有半成品 closed
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM session_closings"
        ).fetchone()["c"] == 0  # 没有半成品登记
    finally:
        conn.close()


def test_close_failure_leaves_no_partial_writes(app_env):
    """模拟核证行写入失败：登记与状态切换必须一起回滚。"""
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()

    class FlakyConn:
        """代理：仅在写核证行时抛错，其余透传（sqlite3 方法不可实例级替换）。"""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO SESSION_PROOFS"):
                raise RuntimeError("boom: proof write failed")
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    flaky = FlakyConn(conn)
    with pytest.raises(RuntimeError):
        env["sessions"].close_open_session(flaky, sid)

    row = conn.execute(
        "SELECT status FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row["status"] == "open"
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM session_closings WHERE session_id=?",
        (sid,),
    ).fetchone()["c"] == 0
    conn.close()


# ---------- 3. 收流闸：关闭后拒收、不追加、不改旧成员 ----------

def test_ingest_rejects_hits_after_close(app_env):
    env = app_env
    sid, original = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
    finally:
        conn.close()

    _seed_batch(
        env["fixtures_dir"],
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
    report = env["ingest"].run_batch()
    assert report["ok"] is True
    assert report["report"]["rejected_closed"] == 1
    assert report["report"]["written"] == 0

    conn = env["db"].connect()
    try:
        # 旧成员原样不动
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                (sid,),
            ).fetchall()
        ]
        assert ids == ["T1", "T2"]
        hit_count = conn.execute(
            "SELECT hit_count, status FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        assert hit_count["hit_count"] == 2
        assert hit_count["status"] == "closed"
        # 拒收落搁置区
        q = conn.execute(
            "SELECT reason, member_json FROM quarantine WHERE reason=?",
            (env["reject"].REJECT_REASON,),
        ).fetchall()
        assert len(q) == 1
        assert json.loads(q[0]["member_json"])["hit"]["id"] == "T3"
        # 不允许把新击点偷偷挂到关闭会话
        assert conn.execute(
            "SELECT 1 FROM hits WHERE id='T3' AND session_id=?", (sid,)
        ).fetchone() is None
    finally:
        conn.close()


def test_ingest_explicit_closed_session_id_rejected(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
    finally:
        conn.close()

    _seed_batch(
        env["fixtures_dir"],
        [
            {
                "id": "T4",
                "visitor_id": "V-9001",
                "session_id": sid,  # 显式指向已关闭会话
                "event_ts": "2026-11-19T09:05:00Z",
                "page_path": "/x",
                "event_name": "ping",
            }
        ],
        batch_id="B-explicit-closed",
    )
    report = env["ingest"].run_batch()
    assert report["report"]["rejected_closed"] == 1
    conn = env["db"].connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM hits WHERE id='T4'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_ingest_open_session_still_accepts_hits(app_env):
    """闸口不能误伤 open 会话。"""
    env = app_env
    sid, _ = _ingest_two_hits(env)
    _seed_batch(
        env["fixtures_dir"],
        [
            {
                "id": "T5",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T08:02:00Z",
                "page_path": "/pay",
                "event_name": "view",
            }
        ],
        batch_id="B-open-ok",
    )
    report = env["ingest"].run_batch()
    assert report["report"]["written"] == 1
    assert report["report"]["rejected_closed"] == 0
    conn = env["db"].connect()
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                (sid,),
            ).fetchall()
        ]
        assert ids == ["T1", "T2", "T5"]
    finally:
        conn.close()


def test_ingest_falls_back_to_older_open_when_latest_closed(app_env):
    """最新会话已关闭、但同访客还有旧 open：挂旧 open，不误拒收。"""
    env = app_env
    old_sid, _ = _ingest_two_hits(env)  # 旧 open 会话
    conn = env["db"].connect()
    try:
        # 造一个更晚的会话（含一个成员），随后关闭它
        conn.execute(
            "INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status) "
            "VALUES ('S-later','V-9001','2026-11-19T08:30:00Z','2026-11-19T08:31:00Z',1,'open')"
        )
        conn.execute(
            "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel) "
            "VALUES ('L1','V-9001','S-later','2026-11-19T08:31:00Z','/z','view','primary')"
        )
        conn.commit()
        env["sessions"].close_open_session(conn, "S-later")
    finally:
        conn.close()

    _seed_batch(
        env["fixtures_dir"],
        [
            {
                "id": "T6",
                "visitor_id": "V-9001",
                "event_ts": "2026-11-19T09:00:00Z",
                "page_path": "/back",
                "event_name": "view",
            }
        ],
        batch_id="B-fallback-open",
    )
    report = env["ingest"].run_batch()
    assert report["report"]["written"] == 1
    assert report["report"]["rejected_closed"] == 0
    conn = env["db"].connect()
    try:
        # 新击点挂到仍 open 的旧会话；已关闭的 S-later 成员不动
        assert conn.execute(
            "SELECT session_id FROM hits WHERE id='T6'"
        ).fetchone()["session_id"] == old_sid
        assert [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM hits WHERE session_id='S-later' ORDER BY id"
            ).fetchall()
        ] == ["L1"]
    finally:
        conn.close()


# ---------- 4. 只读核证：正常通过 / 篡改可检出 ----------

def test_attestation_verified_after_close(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
        att = env["esc"].attestation(conn, sid)
        assert att["verified"] is True
        assert att["registered_hit_ids"] == ["T1", "T2"]
        assert att["live_hit_ids"] == ["T1", "T2"]
        assert att["live_content_hash"] == att["registered_content_hash"]
        assert all(att["checks"].values())
    finally:
        conn.close()


def test_attestation_detects_appended_member(app_env):
    """模拟绕过闸口的追加：核证必须判负（织机不可再按 open 渲染可写）。"""
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
        conn.execute(
            "INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel) "
            "VALUES ('TAMPER','V-9001',?,'2026-11-19T10:00:00Z','/evil','hack','primary')",
            (sid,),
        )
        conn.execute(
            "UPDATE sessions SET hit_count=hit_count+1 WHERE id=?", (sid,)
        )
        conn.commit()
        att = env["esc"].attestation(conn, sid)
        assert att["verified"] is False
        assert att["checks"]["members_match_snapshot"] is False
        assert att["checks"]["hash_matches"] is False
        assert att["checks"]["count_matches"] is False
    finally:
        conn.close()


def test_attestation_detects_content_rewrite(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
        conn.execute(
            "UPDATE hits SET event_name='rewritten' WHERE id='T1'"
        )
        conn.commit()
        att = env["esc"].attestation(conn, sid)
        assert att["verified"] is False
        assert att["checks"]["hash_matches"] is False
        # 成员 id 列表没变，但内容哈希变了
        assert att["checks"]["members_match_snapshot"] is True
    finally:
        conn.close()


def test_attestation_is_read_only(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        env["sessions"].close_open_session(conn, sid)
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM session_closings"
        ).fetchone()["c"]
        env["esc"].attestation(conn, sid)
        env["esc"].attestation(conn, sid)
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM session_closings"
        ).fetchone()["c"]
        assert before == after == 1
    finally:
        conn.close()


def test_attestation_unknown_open_session(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    conn = env["db"].connect()
    try:
        with pytest.raises(env["esc"].CloseError):
            env["esc"].attestation(conn, sid)
    finally:
        conn.close()


# ---------- 5. HTTP 层：入口与只读核证接口 ----------

def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_http_close_and_attestation_flow(app_env):
    env = app_env
    sid, _ = _ingest_two_hits(env)
    client = _client()

    res = client.post(f"/api/sessions/{sid}/close")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "closed"
    assert body["hit_ids"] == ["T1", "T2"]

    # 重复关闭 → 409，不产生第二份登记
    dup = client.post(f"/api/sessions/{sid}/close")
    assert dup.status_code == 409

    att = client.get(f"/api/sessions/{sid}/attestation")
    assert att.status_code == 200
    assert att.json()["verified"] is True

    # 未关闭会话核证 → 404
    other_seed = [
        {
            "id": "O1",
            "visitor_id": "V-other",
            "event_ts": "2026-11-19T11:00:00Z",
            "page_path": "/a",
            "event_name": "view",
            "visitor_label": "另一访客",
        }
    ]
    _seed_batch(env["fixtures_dir"], other_seed, batch_id="B-other")
    env["ingest"].run_batch()
    conn = env["db"].connect()
    try:
        other_sid = conn.execute(
            "SELECT id FROM sessions WHERE visitor_id='V-other'"
        ).fetchone()["id"]
    finally:
        conn.close()
    assert client.get(f"/api/sessions/{other_sid}/attestation").status_code == 404

    # 关闭不存在的会话 → 404
    assert client.post("/api/sessions/S-ghost/close").status_code == 404


def test_http_detail_carries_closing_registry(app_env):
    """织机协同：closed 会话详情必须带关闭登记，前端才能只读渲染。"""
    env = app_env
    sid, _ = _ingest_two_hits(env)
    client = _client()
    client.post(f"/api/sessions/{sid}/close")

    detail = client.get(f"/api/sessions/{sid}").json()
    assert detail["session"]["status"] == "closed"
    assert detail["closing"] is not None
    assert json.loads(detail["closing"]["hit_ids_json"]) == ["T1", "T2"]
    assert len(detail["closing"]["content_hash"]) == 64
