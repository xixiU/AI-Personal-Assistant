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
        from chromadb.utils import embedding_functions

        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self._chroma_client = chromadb.PersistentClient(path=str(self.persist_dir))

        # 使用中文 Embedding 模型
        self._embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="shibing624/text2vec-base-chinese",
        )

        self._collection = self._chroma_client.get_or_create_collection(
            name="feishu_docs",
            metadata={"hnsw:space": "cosine"},
            embedding_function=self._embedding_fn,
        )

        # BM25 索引（内存中）
        self._bm25 = None
        self._bm25_docs: List[Dict[str, str]] = []
        self._bm25_corpus: List[List[str]] = []

        logger.info(f"混合检索引擎初始化: persist_dir={persist_dir}, docs={self._collection.count()}")

    def index_documents(self, docs: List[Dict[str, str]]) -> None:
        """
        索引文档（向量 + BM25）

        大文档会被分块索引到向量数据库（提升 Embedding 质量），
        BM25 保持全文索引（精确匹配不受影响）。

        Args:
            docs: [{"token": ..., "title": ..., "path": ..., "content": ...}]
        """
        if not docs:
            return

        from rank_bm25 import BM25Okapi

        CHUNK_SIZE = 1000  # 向量索引的分块大小（字符）
        CHUNK_OVERLAP = 100  # 分块重叠

        # 向量索引：分块 upsert 到 ChromaDB
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

            if len(content) <= CHUNK_SIZE * 2:
                # 短文档：整篇索引
                ids.append(token)
                documents.append(f"{title}\n{title}\n{path}\n{content}")
                metadatas.append({"title": title, "path": path, "token": token})
            else:
                # 长文档：分块索引，每块带标题前缀
                chunks = self._split_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{token}_chunk{i}"
                    ids.append(chunk_id)
                    documents.append(f"{title}\n{path}\n{chunk}")
                    metadatas.append({"title": title, "path": path, "token": token, "chunk": i})

        if ids:
            batch_size = 5000
            for i in range(0, len(ids), batch_size):
                self._collection.upsert(
                    ids=ids[i:i+batch_size],
                    documents=documents[i:i+batch_size],
                    metadatas=metadatas[i:i+batch_size],
                )

        # BM25 索引：全文索引（不分块，精确匹配需要完整内容）
        self._bm25_docs = docs
        self._bm25_corpus = []
        for doc in docs:
            text = f"{doc.get('title', '')} {doc.get('path', '')} {doc.get('content', '')}"
            tokens = list(jieba.cut(text))
            self._bm25_corpus.append(tokens)

        self._bm25 = BM25Okapi(self._bm25_corpus)
        logger.info(f"文档索引完成: 向量={self._collection.count()}, BM25={len(self._bm25_corpus)}, 原始文档={len(docs)}")

    @staticmethod
    def _split_text(text: str, chunk_size: int, overlap: int) -> List[str]:
        """将长文本分块"""
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start = end - overlap
        return chunks

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

        # 3. RRF 融合（BM25 权重更高，因为中文场景下关键词匹配更可靠）
        fused = self._rrf_fusion(vector_results, bm25_results, k=60, bm25_weight=1.5)

        # 取 top_k
        results = fused[:top_k]

        # 详细日志
        vector_titles = [f"{d['title']}({d['score']:.3f})" for d in vector_results[:5]]
        bm25_titles = [f"{d['title']}({d['score']:.1f})" for d in bm25_results[:5]]
        result_titles = [d['title'] for d in results]
        logger.info(
            f"混合检索: query='{query[:50]}'\n"
            f"  向量Top5: {vector_titles}\n"
            f"  BM25Top5: {bm25_titles}\n"
            f"  融合结果: {result_titles}"
        )

        return results

    def _vector_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        """向量检索（分块结果自动去重回溯到原始文档）"""
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k * 3, self._collection.count()),  # 多取一些，去重后保证数量
            )

            # 去重：同一个原始文档的多个 chunk 只保留最高分
            seen_tokens = {}
            if results and results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i] if results.get("distances") else 0
                    metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
                    # 获取原始文档 token（chunk 的 metadata 中存了原始 token）
                    original_token = metadata.get("token", doc_id.split("_chunk")[0])
                    score = 1 - distance

                    if original_token not in seen_tokens or score > seen_tokens[original_token]["score"]:
                        seen_tokens[original_token] = {
                            "token": original_token,
                            "title": metadata.get("title", ""),
                            "path": metadata.get("path", ""),
                            "score": score,
                        }

            # 按分数排序
            docs = sorted(seen_tokens.values(), key=lambda x: x["score"], reverse=True)
            return docs[:top_k]
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
        bm25_weight: float = 1.5,
    ) -> List[Dict[str, Any]]:
        """
        RRF（Reciprocal Rank Fusion）融合两个检索结果

        RRF(d) = Σ weight_i/(k + rank_i(d))，k 是常数（通常 60）
        bm25_weight > 1.0 时 BM25 结果权重更高（中文场景推荐）
        """
        scores: Dict[str, float] = {}
        doc_map: Dict[str, Dict[str, Any]] = {}

        # 向量检索排名（权重 1.0）
        for rank, doc in enumerate(vector_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + 1.0 / (k + rank + 1)
            doc_map[token] = doc

        # BM25 检索排名（权重更高）
        for rank, doc in enumerate(bm25_results):
            token = doc["token"]
            scores[token] = scores.get(token, 0) + bm25_weight / (k + rank + 1)
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
