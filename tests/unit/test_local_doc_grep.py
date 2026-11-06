"""
本地文档关键词直查（utils/local_doc_grep.py）核心能力测试

只测核心：命中行带上下文、GBK、mode=index 目录不参与直查、泛词降权，
以及转为直查后向量库残留的清理（HybridSearchEngine.remove_by_token_prefix）。
接入 FeishuDocManager 的行为靠端到端冒烟验证（模块依赖 jieba/chromadb）。
"""

import sys
import types

from ai_assistant.utils.local_doc_grep import grep_local_docs, split_local_docs_by_mode


def _cfg(path, mode=None, description="SQL"):
    cfg = {"path": str(path), "description": description}
    if mode is not None:
        cfg["mode"] = mode
    return cfg


def test_grep_content_hit_with_context(tmp_path):
    """内容命中的文件返回命中行及前后 3 行，不是文件开头；没命中的文件不返回"""
    lines = [f"INSERT INTO t VALUES ({i}, 'row{i}');" for i in range(1, 201)]
    lines[99] = "INSERT INTO ifc_court_info VALUES (100, '河南省高级人民法院');"
    (tmp_path / "a.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "b.sql").write_text("CREATE TABLE other (id int);\n", encoding="utf-8")

    docs = grep_local_docs([_cfg(tmp_path)], ["ifc_court_info"])

    assert [d["title"] for d in docs] == ["[SQL] a.sql"]
    content = docs[0]["content"]
    assert "--- 第 97-103 行 ---" in content
    assert "100: INSERT INTO ifc_court_info" in content
    assert "97: " in content and "103: " in content
    assert "96: " not in content and "104: " not in content
    assert "row1'" not in content  # 不是开头盲截断


def test_grep_reads_gbk_file(tmp_path):
    """GBK 编码的 SQL 也能按中文关键词命中，返回正常解码的中文"""
    text = "-- 法院信息初始化\nINSERT INTO court VALUES (1, '河南省郑州市中级人民法院');\n"
    (tmp_path / "gbk.sql").write_bytes(text.encode("gbk"))

    docs = grep_local_docs([_cfg(tmp_path)], ["郑州市中级"])

    assert len(docs) == 1
    assert "2: INSERT INTO court VALUES (1, '河南省郑州市中级人民法院');" in docs[0]["content"]


def test_grep_ignores_index_mode_dirs(tmp_path):
    """默认 / 写错的 mode 走直查；显式 index 的目录归入索引组，直查查不到它"""
    grep_dir, index_dir, typo_dir = tmp_path / "g", tmp_path / "i", tmp_path / "t"
    for d in (grep_dir, index_dir, typo_dir):
        d.mkdir()
        (d / "x.sql").write_text("KEY_TOKEN_42\n", encoding="utf-8")
    cfgs = [
        _cfg(grep_dir, description="G"),
        _cfg(index_dir, " Index ", description="I"),
        _cfg(typo_dir, "grpe", description="T"),
    ]

    index_cfgs, grep_cfgs = split_local_docs_by_mode(cfgs)
    docs = grep_local_docs(grep_cfgs, ["KEY_TOKEN_42"])

    assert index_cfgs == [cfgs[1]]
    assert sorted(d["title"] for d in docs) == ["[G] x.sql", "[T] x.sql"]


def test_grep_generic_keyword_does_not_pull_in_unrelated_files(tmp_path):
    """泛词命中所有文件时只展开案号所在文件；案号查无时不返回只命中泛词的文件"""
    for i in range(10):
        (tmp_path / f"f{i:02d}.sql").write_text(
            "".join(f"-- 日志表 {j}\n" for j in range(50)), encoding="utf-8"
        )
    (tmp_path / "case.sql").write_text(
        "-- 日志表\nINSERT INTO case_info VALUES ('(2024)豫01民初123号');\n", encoding="utf-8"
    )

    docs = grep_local_docs([_cfg(tmp_path)], ["(2024)豫01民初123号", "日志"])
    assert [d["title"] for d in docs] == ["[SQL] case.sql"]
    assert "未单独展开" in docs[0]["content"]

    assert grep_local_docs([_cfg(tmp_path)], ["(2099)豫01民初999号", "日志"]) == []


class _FakeCollection:
    """按 chromadb 0.4 的 get/delete 形状：ids 与 metadatas 是平行列表"""

    def __init__(self, records):
        self.records = dict(records)  # id -> metadata
        self.fail_get = False

    def get(self, include=None):
        if self.fail_get:
            raise RuntimeError("boom")
        return {"ids": list(self.records), "metadatas": list(self.records.values())}

    def delete(self, ids):
        for i in ids:
            self.records.pop(i, None)


def test_remove_stale_local_vectors_keeps_online_and_index_docs(monkeypatch):
    """在线文档、仍在 index 目录的本地文档保留；已转直查的本地文档（含分块）被删；get 抛异常不外抛"""
    if "jieba" not in sys.modules:
        try:
            import jieba  # noqa: F401
        except ImportError:
            # 与 test_log_no_full_content 的占位一致：hybrid_search 导入后会缓存这个模块，
            # 后续测试走到 jieba.cut 时行为相同
            stub = types.ModuleType("jieba")
            stub.cut = lambda *a, **kw: iter(())
            stub.lcut = lambda *a, **kw: []
            monkeypatch.setitem(sys.modules, "jieba", stub)
    from ai_assistant.core.hybrid_search import HybridSearchEngine

    eng = object.__new__(HybridSearchEngine)  # 不建 chromadb 客户端
    eng._collection = _FakeCollection({
        "wikiDoc1": {"token": "wikiDoc1"},
        "local_keep": {"token": "local_keep"},
        "local_keep_chunk0": {"token": "local_keep", "chunk": 0},
        "local_old_chunk0": {"token": "local_old", "chunk": 0},
        "local_old_chunk1": {"token": "local_old", "chunk": 1},
        "no_meta": None,
    })

    assert eng.remove_by_token_prefix("local_", {"local_keep"}) == 2
    assert sorted(eng._collection.records) == [
        "local_keep", "local_keep_chunk0", "no_meta", "wikiDoc1",
    ]

    eng._collection.fail_get = True
    assert eng.remove_by_token_prefix("local_", set()) == 0
    assert "wikiDoc1" in eng._collection.records
