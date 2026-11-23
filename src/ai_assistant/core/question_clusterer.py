"""
问题聚类分析工具

使用语义相似度对相似问题进行聚类
"""

import numpy as np
from typing import List, Dict, Tuple
from collections import defaultdict
from loguru import logger


class QuestionClusterer:
    """问题语义聚类器"""

    def __init__(self, similarity_threshold: float = 0.8):
        """
        初始化聚类器

        Args:
            similarity_threshold: 相似度阈值（默认 0.8）
        """
        self.similarity_threshold = similarity_threshold
        logger.info(f"问题聚类器初始化完成，相似度阈值: {similarity_threshold}")

    def cosine_similarity(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """
        计算两个向量的余弦相似度

        Args:
            vec1: 向量1
            vec2: 向量2

        Returns:
            余弦相似度 (0-1)
        """
        # 计算点积
        dot_product = np.dot(vec1, vec2)
        # 计算范数
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)

        # 避免除零
        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)

    def cluster_questions(
        self,
        questions: List[Tuple[str, int]],
        embeddings: List[np.ndarray]
    ) -> List[Dict]:
        """
        对问题进行语义聚类

        Args:
            questions: 问题列表，格式 [(question, count), ...]
            embeddings: 对应的 embedding 向量列表

        Returns:
            聚类结果，格式: [
                {
                    "representative": "代表性问题",
                    "count": 5,
                    "similar_questions": ["相似问题1", "相似问题2"]
                },
                ...
            ]
        """
        if not questions or not embeddings:
            return []

        if len(questions) != len(embeddings):
            logger.error(f"问题数量 ({len(questions)}) 与 embedding 数量 ({len(embeddings)}) 不一致")
            return []

        try:
            # 存储已分配到的问题索引
            assigned = set()
            clusters = []

            # 按出现次数降序排序
            sorted_indices = sorted(range(len(questions)), key=lambda i: questions[i][1], reverse=True)

            for i in sorted_indices:
                if i in assigned:
                    continue

                # 创建新簇
                cluster = {
                    "indices": [i],
                    "representative": questions[i][0],
                    "representative_count": questions[i][1],
                    "total_count": questions[i][1],
                    "similar_questions": []
                }

                # 查找相似问题
                for j in range(len(questions)):
                    if j == i or j in assigned:
                        continue

                    # 计算相似度
                    similarity = self.cosine_similarity(embeddings[i], embeddings[j])

                    if similarity >= self.similarity_threshold:
                        cluster["indices"].append(j)
                        cluster["similar_questions"].append(questions[j][0])
                        cluster["total_count"] += questions[j][1]
                        assigned.add(j)

                # 标记代表性问题为已分配
                assigned.add(i)

                # 添加到结果
                clusters.append({
                    "representative": cluster["representative"],
                    "count": cluster["total_count"],
                    "similar_questions": cluster["similar_questions"]
                })

            # 按总次数降序排序
            clusters.sort(key=lambda x: x["count"], reverse=True)

            logger.info(f"问题聚类完成: {len(questions)} 个问题 -> {len(clusters)} 个簇")
            return clusters

        except Exception as e:
            logger.error(f"问题聚类失败: {e}", exc_info=True)
            # 失败时返回原始数据（不聚类）
            return [
                {
                    "representative": q[0],
                    "count": q[1],
                    "similar_questions": []
                }
                for q in questions
            ]

    def select_best_representative(self, questions: List[str]) -> str:
        """
        从一组相似问题中选择最好的代表性问题

        选择标准：
        1. 长度适中（不要太长也不要太短）
        2. 语义完整
        3. 没有冗余词汇

        Args:
            questions: 问题列表

        Returns:
            代表性问题
        """
        if not questions:
            return ""

        if len(questions) == 1:
            return questions[0]

        # 简单策略：选择长度中位数的问题
        sorted_by_len = sorted(questions, key=len)
        mid_idx = len(sorted_by_len) // 2
        return sorted_by_len[mid_idx]
