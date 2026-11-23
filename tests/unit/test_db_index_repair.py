"""
数据库启动建表与索引补建的回归测试（复审用，Review-Agent 编写，不改业务代码）

在服务器仓库根目录执行（只读：所有建库都在临时目录，不碰 /app/data）：

  docker cp tests/unit/test_db_index_repair.py ai-assistant-gpu:/tmp/ && docker exec -w /tmp -e ONLINE_DB_COPY=/app/data/ai_assistant.db ai-assistant-gpu python -m pytest -p no:cacheprovider -v /tmp/test_db_index_repair.py

去掉 `-e ONLINE_DB_COPY=...` 则只跑合成库。设置后会把线上库以只读方式备份到临时目录，
再用副本额外跑一遍"补索引 / 重复启动 / 数据不丢"三项，原库文件不会被打开写入。

背景（线上运行证据，ai-assistant-gpu，SQLAlchemy 2.1.9）：
- 0869704 的 chat_history 和 feedback 声明了同名索引（idx_session_source、idx_timestamp、
  idx_user_id、idx_session_timestamp）。SQLite 的索引名在整个库内唯一，首次 create_all
  在 feedback 建索引时失败。
- 容器自动重启后 create_all 看到表已存在，跳过建表，也跳过建索引。线上 feedback 只剩
  sqlite_autoindex_feedback_1、ix_feedback_session_id、ix_feedback_user_id。
- create_all 不会给已有的表补索引，所以只改索引名修不好线上库。

验证项：
1. 线上结构的库启动后，feedback 索引补齐，模型声明的所有索引都存在
2. 重复启动不报错，索引集合稳定
3. 已有数据不丢，并能通过 ORM 读出（修复新增了列但没迁移已有表时会在这里失败）
4. 空库首次启动成功
另外两项：索引名全库唯一（PostgreSQL 同样要求）；metadata 属性改名后不再告警且列名仍是 metadata。

"启动"的定义取自 main.py：解析其中对 self.db_manager 的无参调用（当前只有 create_tables()），
按顺序执行。修复如果把补建放在别处（比如 UserManager 初始化里），test_startup_path_found 会提示。

环境变量：
  AI_ASSISTANT_SRC             src 目录，默认依次尝试 <仓库>/src、/app/src
  ONLINE_DB_COPY               线上库路径，见上
  FIXTURE_HAS_METADATA_COLUMN  合成库的 chat_history 是否带 metadata 列，默认 1。
                               线上实际列确认后按实际设置。
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def _find_src() -> Path:
    candidates = []
    if os.environ.get("AI_ASSISTANT_SRC"):
        candidates.append(Path(os.environ["AI_ASSISTANT_SRC"]))
    here = Path(__file__).resolve()
    if len(here.parents) > 2:
        candidates.append(here.parents[2] / "src")
    candidates.append(Path("/app/src"))
    for c in candidates:
        if (c / "ai_assistant" / "core" / "database.py").exists():
            return c
    raise RuntimeError(f"找不到 src 目录，已尝试: {candidates}")


SRC = _find_src()
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MAIN_PY = SRC / "ai_assistant" / "main.py"

# 不属于"启动建库"的方法，即使 main.py 里出现无参调用也不执行
_NON_STARTUP_METHODS = {"close", "drop_tables", "get_session"}


def startup_calls():
    """从 main.py 取出启动时对 self.db_manager 的无参调用，保持出现顺序、去重。"""
    text = MAIN_PY.read_text(encoding="utf-8")
    calls = []
    for name in re.findall(r"self\.db_manager\.(\w+)\(\s*\)", text):
        if name not in _NON_STARTUP_METHODS and name not in calls:
            calls.append(name)
    return calls


# 在独立子进程里模拟一次"服务启动"：每次都是全新的解释器，等同容器重启。
# 子进程只做三件事：按 main.py 的方式建 DatabaseManager 并执行启动调用；
# 用 ORM 读出每张表的所有行；输出模型声明的索引。结果以 JSON 打到 stdout 最后一行。
_STARTUP_SCRIPT = r'''
import json, sys, warnings
sys.path.insert(0, sys.argv[1])
db_path, calls = sys.argv[2], json.loads(sys.argv[3])
from ai_assistant.core.database import DatabaseManager, Base
import ai_assistant.models as models
from sqlalchemy import inspect as sa_inspect

mgr = DatabaseManager(db_type="sqlite", db_path=db_path, echo=False)
for name in calls:
    getattr(mgr, name)()

orm_rows = {}
with mgr.get_session() as s:
    for cls_name in models.__all__:
        cls = getattr(models, cls_name)
        keys = [a.key for a in sa_inspect(cls).mapper.column_attrs]
        rows = s.query(cls).all()
        for r in rows:
            for k in keys:
                getattr(r, k)
        orm_rows[cls.__tablename__] = len(rows)

declared = {}
for t in Base.metadata.sorted_tables:
    declared[t.name] = sorted(
        [ix.name, [c.name for c in ix.columns]] for ix in t.indexes
    )
mgr.close()
print(json.dumps({"orm_rows": orm_rows, "declared": declared}))
'''


def run_startup(db_path: Path, workdir: Path) -> dict:
    """执行一次启动，失败时把子进程输出原样带进断言信息。"""
    import json

    proc = subprocess.run(
        [sys.executable, "-c", _STARTUP_SCRIPT, str(SRC), str(db_path),
         json.dumps(startup_calls())],
        cwd=str(workdir), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"启动失败（rc={proc.returncode}）\n--- stdout ---\n{proc.stdout[-3000:]}"
        f"\n--- stderr ---\n{proc.stderr[-5000:]}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------- 线上结构复刻（0869704 的表结构 + 线上实际存在的索引） ----------
# 只用标准库 sqlite3 建库，不依赖被测代码。Enum 按 SQLAlchemy 默认行为存枚举名（USER/LIKE），
# DateTime 存 "YYYY-MM-DD HH:MM:SS.ffffff"，JSON 存文本。

def _online_schema(has_metadata_column: bool) -> list:
    meta_col = "metadata JSON," if has_metadata_column else ""
    return [
        """CREATE TABLE users (
            user_id VARCHAR(64) NOT NULL, username VARCHAR(64) NOT NULL,
            password_hash VARCHAR(128) NOT NULL, name VARCHAR(128),
            role VARCHAR(5) NOT NULL, feishu_open_id VARCHAR(128),
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            PRIMARY KEY (user_id))""",
        "CREATE UNIQUE INDEX ix_users_username ON users (username)",
        "CREATE UNIQUE INDEX ix_users_feishu_open_id ON users (feishu_open_id)",
        "CREATE INDEX idx_username ON users (username)",
        "CREATE INDEX idx_feishu_open_id ON users (feishu_open_id)",
        "CREATE INDEX idx_created_at ON users (created_at)",
        f"""CREATE TABLE chat_history (
            record_id VARCHAR(64) NOT NULL, timestamp DATETIME NOT NULL,
            session_id VARCHAR(128) NOT NULL, source VARCHAR(32) NOT NULL,
            query TEXT NOT NULL, answer TEXT NOT NULL, latency_ms INTEGER,
            user_id VARCHAR(128), user_name VARCHAR(128), context_key VARCHAR(128),
            chain_info JSON, message_id VARCHAR(128), {meta_col}
            created_at DATETIME NOT NULL, PRIMARY KEY (record_id))""",
        "CREATE INDEX ix_chat_history_timestamp ON chat_history (timestamp)",
        "CREATE INDEX ix_chat_history_session_id ON chat_history (session_id)",
        "CREATE INDEX ix_chat_history_source ON chat_history (source)",
        "CREATE INDEX ix_chat_history_user_id ON chat_history (user_id)",
        "CREATE INDEX ix_chat_history_context_key ON chat_history (context_key)",
        "CREATE INDEX ix_chat_history_message_id ON chat_history (message_id)",
        "CREATE INDEX idx_session_source ON chat_history (session_id, source)",
        "CREATE INDEX idx_context_key ON chat_history (context_key)",
        "CREATE INDEX idx_message_id ON chat_history (message_id)",
        "CREATE INDEX idx_user_id ON chat_history (user_id)",
        "CREATE INDEX idx_timestamp ON chat_history (timestamp)",
        "CREATE INDEX idx_session_timestamp ON chat_history (session_id, timestamp)",
        """CREATE TABLE feedback (
            feedback_id VARCHAR(64) NOT NULL, record_id VARCHAR(64) NOT NULL,
            timestamp DATETIME NOT NULL, session_id VARCHAR(128) NOT NULL,
            source VARCHAR(32) NOT NULL, feedback_type VARCHAR(7) NOT NULL,
            feedback_text TEXT, user_id VARCHAR(128), user_name VARCHAR(128),
            created_at DATETIME NOT NULL, PRIMARY KEY (feedback_id))""",
        # 线上 feedback 只有这两个显式索引（另一个是主键自动索引 sqlite_autoindex_feedback_1）
        "CREATE INDEX ix_feedback_session_id ON feedback (session_id)",
        "CREATE INDEX ix_feedback_user_id ON feedback (user_id)",
    ]


_TS = "2026-11-20 18:00:00.000000"


def build_online_fixture(path: Path, has_metadata_column: bool) -> None:
    conn = sqlite3.connect(str(path))
    try:
        for stmt in _online_schema(has_metadata_column):
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",
            ("u-1", "zhangsan", "$2b$12$" + "x" * 53, "张三", "USER", "ou_abc",
             _TS, _TS),
        )
        cols = ("record_id, timestamp, session_id, source, query, answer, latency_ms, "
                "user_id, user_name, context_key, chain_info, message_id, created_at")
        vals = ["r-1", _TS, "oc_chat", "feishu", "问题一", "回答一", 1200,
                "ou_abc", "张三", "om_root", '{"message_id": "om_1"}', "om_reply", _TS]
        if has_metadata_column:
            cols += ", metadata"
            vals.append('{"mode": "agentic", "tool_rounds": 2}')
        conn.execute(
            f"INSERT INTO chat_history ({cols}) VALUES ({','.join('?' * len(vals))})",
            vals,
        )
        conn.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("f-1", "r-1", _TS, "oc_chat", "feishu", "DISLIKE", "不准确",
             "ou_abc", "张三", _TS),
        )
        conn.commit()
    finally:
        conn.close()


def copy_online_db(src: Path, dst: Path) -> None:
    """以只读 URI 打开线上库，用 backup API 拷贝，原文件不写入。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


# ---------- 读取实际库结构 ----------

def actual_indexes(db_path: Path) -> dict:
    """{表名: {索引名: [列...]}}，不含 sqlite_autoindex_*。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        result = {}
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            result[t] = {}
            for row in conn.execute(f'PRAGMA index_list("{t}")'):
                name = row[1]
                if name.startswith("sqlite_autoindex_"):
                    continue
                cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
                result[t][name] = cols
        return result
    finally:
        conn.close()


def table_columns(db_path: Path, table: str) -> list:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


def snapshot_rows(db_path: Path) -> dict:
    """按表名取 {主键: 原有列的值}，只取 fixture 里已有的列，修复新增的列不参与比较。"""
    pks = {"users": "user_id", "chat_history": "record_id", "feedback": "feedback_id"}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        snap = {}
        for t, pk in pks.items():
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            rows = conn.execute(f'SELECT {",".join(cols)} FROM "{t}"').fetchall()
            snap[t] = {dict(zip(cols, r))[pk]: dict(zip(cols, r)) for r in rows}
        return snap
    finally:
        conn.close()


def missing_indexes(declared: dict, actual: dict) -> list:
    """模型声明了、库里没有（或列不一致）的索引。"""
    missing = []
    for table, ixs in declared.items():
        have = actual.get(table, {})
        for name, cols in ixs:
            if have.get(name) != cols:
                missing.append(f"{table}.{name}{cols} 实际={have.get(name)}")
    return missing


# ---------- 用例 ----------

def _fixture_sources():
    sources = ["synthetic"]
    if os.environ.get("ONLINE_DB_COPY"):
        sources.append("online_copy")
    return sources


@pytest.fixture(params=_fixture_sources())
def legacy_db(request, tmp_path) -> Path:
    db = tmp_path / "ai_assistant.db"
    if request.param == "synthetic":
        has_meta = os.environ.get("FIXTURE_HAS_METADATA_COLUMN", "1") != "0"
        build_online_fixture(db, has_meta)
    else:
        src = Path(os.environ["ONLINE_DB_COPY"])
        if not src.exists():
            pytest.fail(f"ONLINE_DB_COPY 指向的文件不存在: {src}")
        copy_online_db(src, db)
    return db


def test_startup_path_found():
    calls = startup_calls()
    assert calls, "main.py 里找不到对 self.db_manager 的无参调用，启动建库流程变了，需更新本测试"
    assert "create_tables" in calls or len(calls) > 1, f"启动调用: {calls}"


def test_synthetic_fixture_matches_online(tmp_path):
    """自检：合成库的 feedback 索引与线上实际一致（3 个，含主键自动索引）。"""
    db = tmp_path / "selfcheck.db"
    build_online_fixture(db, True)
    conn = sqlite3.connect(str(db))
    names = sorted(r[1] for r in conn.execute('PRAGMA index_list("feedback")'))
    conn.close()
    assert names == ["ix_feedback_session_id", "ix_feedback_user_id",
                     "sqlite_autoindex_feedback_1"]


def test_empty_db_first_start(tmp_path):
    db = tmp_path / "fresh.db"
    out = run_startup(db, tmp_path)
    missing = missing_indexes(out["declared"], actual_indexes(db))
    assert not missing, "空库首次启动后缺索引:\n" + "\n".join(missing)


def test_index_names_unique_across_tables(tmp_path):
    out = run_startup(tmp_path / "fresh.db", tmp_path)
    owner = {}
    dup = []
    for table, ixs in out["declared"].items():
        for name, _ in ixs:
            if name in owner:
                dup.append(f"{name}: {owner[name]} / {table}")
            owner[name] = table
    assert not dup, "索引名跨表重复（SQLite、PostgreSQL 都要求库内唯一）:\n" + "\n".join(dup)


def test_legacy_db_indexes_repaired(legacy_db, tmp_path):
    out = run_startup(legacy_db, tmp_path)
    missing = missing_indexes(out["declared"], actual_indexes(legacy_db))
    assert not missing, "线上结构的库启动后仍缺索引:\n" + "\n".join(missing)


def test_legacy_db_restart_idempotent(legacy_db, tmp_path):
    run_startup(legacy_db, tmp_path)
    first = actual_indexes(legacy_db)
    run_startup(legacy_db, tmp_path)
    second = actual_indexes(legacy_db)
    assert first == second, f"重复启动后索引变化:\n第一次={first}\n第二次={second}"


def test_legacy_db_data_preserved(legacy_db, tmp_path):
    before = snapshot_rows(legacy_db)
    out = run_startup(legacy_db, tmp_path)
    out = run_startup(legacy_db, tmp_path)
    after = snapshot_rows(legacy_db)
    for table, rows in before.items():
        assert set(after[table]) == set(rows), f"{table} 行丢失或多出"
        for pk, row in rows.items():
            for col, val in row.items():
                assert after[table][pk].get(col) == val, (
                    f"{table}[{pk}].{col} 变了: {val!r} -> {after[table][pk].get(col)!r}")
        assert out["orm_rows"].get(table) == len(rows), (
            f"{table} 通过 ORM 读出 {out['orm_rows'].get(table)} 行，库里有 {len(rows)} 行")


def test_metadata_attribute_renamed_keep_column(tmp_path):
    """metadata 属性改名后：导入模型不再出 SAWarning，库里列名仍是 metadata。"""
    # 只把 SAWarning 升级为错误，其他第三方库的告警不影响结论
    code = (
        f"import sys, warnings; sys.path.insert(0, {str(SRC)!r})\n"
        "from sqlalchemy.exc import SAWarning\n"
        "warnings.simplefilter('error', SAWarning)\n"
        "import ai_assistant.models\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"导入模型时有告警或报错:\n{proc.stderr[-3000:]}"
    db = tmp_path / "fresh.db"
    run_startup(db, tmp_path)
    assert "metadata" in table_columns(db, "chat_history"), (
        "chat_history 表没有 metadata 列，已有数据与 JSONL 字段对不上")
