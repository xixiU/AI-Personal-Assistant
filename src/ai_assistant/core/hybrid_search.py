"""n混合检索引擎

结合向量检索（语义匹配）和 BM25（关键词精确匹配），
使用 RRF（Reciprocal Rank Fusion）算法合并排序结果。
"""

import jieba
from typing import Any, Dict, List, Optional
from pathlib import Path
from loguru import logger


class HybridSearchEngine:
    """混合检索引擎：向量检索 + BM25"""

    def __init__(self, persist_dir: str = "./data/vector_db"):
        """
        Args:
            persist_dir: ChromaDB 持久化目录
        """
        import chromadb

        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self._chroma_client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection = self._chroma_client.get_or_create_collection(
            name="feishu_docs",
            metadata={"hnsw:space": "cosine"},
        )

        # BM25 索引（内存中）
        self._bm25 = None
        self._bm25_docs: List[Dict[str, str]] = []
        self._bm25_corpus: List[List[str]] = []

        logger.info(f"混合检索引擎初始化: persist_dir={persist_dir}, docs={self._collection.count()}")

    def index_documents(self, docs: List[Dict[str, str]]) -> None:
        """
        索引文档（向量 + BM25）

        Args:
            docs: [{"token": ..., "title": ..., "path": ..., "content": ...}]
        """
        if not docs:
            return

        from rank_bm25 import BM25Okapi

        # 向量索引：upsert 到 ChromaDB
        ids = []
        documents = []
        metadatas = []

        for doc in docs:
            token = doc.get("token", "")
            if not token:
                continue
            title = doc.get("title", "")
            path = doc.get("path", "")
            content = doc.get("content", "")

            ids.append(token)
            # 将标题和内容拼接，增强标题权重
            documents.append(f"{title}\n{title}\n{path}\n{content}")
            metadatas.append({"title": title, "path": path})

        if ids:
            # ChromaDB 批量 upsert（每批最多 5000）
            batch_size = 5000
            for i in range(0, len(ids), batch_size):
                self._collection.upsert(
                    ids=ids[i:i+batch_size],
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size],
                )

        # BM25 索引：中文分词后建索引
        self._bm25_docs = docs
        self._bm25_corpus = []
        for doc in docs:
            text = f"{doc.get('title', '')} {doc.get('path', '')} {doc.get('content', '')}"
            tokens = list(jieba.cut(text))
            self._bm25_corpus.append(tokens)

        self._bm25 = BM25Okapi(self._bm25_corpus)
        logger.info(f"文档索引完成: 向量={self._collection.count()}, BM25={len(self._bm25_corpus)}")

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        混合检索：向量 + BM25，RRF 融合排序

        Args:
            query: 用户查询文本
            top_k: 返回结果数量

        Returns:
            排序后的文档列表 [{"token", "title", "path", "content", "score"}]
        """
        if not self._bm25_docs:
            return []

        # 1. 向量检索
        vector_results = self._vector_search(query, top_k=top_k * 2)

        # 2. BM25 检索
        bm25_results = self._bm25_search(query, top_k=top_k * 2)

        # 3. RRF 融合
        fused = self._rrf_fusion(vector_results, bm25_results, k=60)

        # 取 top_k
        results = fused[:top_k]

        logger.info(
            f"混合检索: query='{query[:50]}', "
            f"向量命中={len(vector_results)}, BM25命中={len(bm25_results)}, "
            f"融合结果={len(results)}"
        )

        return results

    def _vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """向量检索"""
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, self._collection.count()),
            )

            docs = []
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i] if results.get("distances") else 0
                    metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
                    docs.append({
                        "token": doc_id,
                        "title": metadata.get("title", ""),
                        "path": metadata.get("path", ""),
                        "score": 1 - distance,  # cosine distance → similarity
                    })
            return docs
        except Exception as e:
            logger.warning(f"向量检索失败: {e}")
            return []

    def _bm25_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """BM25 关键词检索"""
        if not self._bm25:
            return []

        try:
            query_tokens = list(jieba.cut(query))
            scores = self._bm25.get_scores(query_tokens)

            # 获取 top_k 结果
            indexed_scores = [(i, s) for i, s in enumerate(scores) if s > 0]
            indexed_scores.sort(key=lambda x: x[1], reverse=True)

            docs = []
            for idx, score in indexed_scores[:top_k]:
                doc = self._bm25_docs[idx]
                docs.append({
                    "token": doc.get("token", ""),
                    "title": doc.get("title", ""),
                    "path": doc.get("path", ""),
                    "score": score,
                })
            return docs
        except Exception as e:
            logger.warning(f"BM25 检索失败: {e}")
            return []

    def _rrf_fusion(
        self,
        vector_results: List[Dict[str, Any]],
        bm25_results: List[Dict[str, Any]],
        k: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        RRF（Reciprocal Rank Fusion）融合两个检索结果

        RRF(d) = Σ 1/(k + rank_i(d))，k 是常数（通常 60）
        """
        scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}

        # 向量检索排名
        for rank, doc in enumerate(vector_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + 1.0 / (k + rank + 1)
            doc_map[token] = doc

        # BM25 检索排名
        for rank, doc in enumerate(bm25_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + 1.0 / (k + rank + 1)
            if token not in doc_map:
                doc_map[token] = doc

        # 按 RRF 分数排序
        sorted_tokens = sorted(scores.keys(), key=lambda t: scores[t], reverse=True)

        results = []
        for token in sorted_tokens:
            doc = doc_map[token]
            # 从 bm25_docs 中查找完整内容
            full_doc = next((d for d in self._bm25_docs if d.get("token") == token), None)
            if full_doc:
                results.append({
                    "token": token,
                    "title": full_doc.get("title", ""),
                    "path": full_doc.get("path", ""),
                    "content": full_doc.get("content", ""),
                    "score": scores[token],
                })

        return results

    def remove_documents(self, tokens: List[str]) -> None:
        """删除文档"""
        if tokens:
            self._collection.delete(ids=tokens)

    @property
    def doc_count(self) -> int:
        """已索引文档数量"""
        return self._collection.count()
