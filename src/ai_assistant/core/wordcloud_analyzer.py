"""
词云分析工具

从用户问题中提取关键词并统计词频
"""

import jieba
from typing import List, Dict
from collections import Counter
from loguru import logger


class WordCloudAnalyzer:
    """词云分析器"""

    # 停用词列表（常见无意义词汇）
    STOP_WORDS = {
        '的', '了', '是', '在', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很',
        '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这', '那', '里', '下',
        '能', '过', '么', '吗', '呢', '啊', '吧', '哦', '嗯', '哈', '呀', '噢', '咦', '哎',
        '可以', '什么', '怎么', '为什么', '如何', '怎样', '怎么样', '多少', '哪里', '哪儿',
        '请问', '您好', '你好', '谢谢', '麻烦', '帮忙', '帮我', '告诉我', '给我',
        '一下', '一些', '一点', '知道', '想', '觉得', '认为', '感觉', '发现',
        '问题', '情况', '时候', '地方', '东西', '事情', '方面', '办法', '结果',
        '这个', '那个', '这些', '那些', '这样', '那样', '如此', '这么', '那么',
        '有的', '没的', '出来', '进去', '起来', '下去', '上来', '回去', '过来',
        '以及', '或者', '还是', '但是', '不过', '然后', '接着', '于是', '因此',
        '所以', '由于', '因为', '如果', '假如', '要是', '虽然', '尽管', '即使',
        '已经', '正在', '将要', '曾经', '刚才', '现在', '以后', '之前', '之后',
        '可能', '应该', '必须', '需要', '想要', '希望', '打算', '准备', '开始',
        '使用', '进行', '得到', '变成', '成为', '作为', '关于', '对于', '根据',
        '按照', '通过', '经过', '沿着', '朝着', '向着', '往', '从', '自', '由',
        '被', '把', '将', '给', '让', '叫', '使', '令', '教', '请',
        '跟', '同', '与', '及', '以', '而', '且', '或', '又', '再',
        '才', '却', '仅', '只', '都', '还', '更', '最', '非常', '十分',
        '比较', '相当', '太', '特别', '格外', '尤其', '越', '愈', '更加',
    }

    def __init__(self):
        """初始化分析器"""
        # 加载 jieba 自定义词典（如果有）
        # jieba.load_userdict("custom_dict.txt")
        logger.info("词云分析器初始化完成")

    def extract_keywords(
        self,
        queries: List[str],
        top_n: int = 50,
        min_word_len: int = 2
    ) -> List[Dict[str, int]]:
        """
        从问题列表中提取关键词

        Args:
            queries: 问题文本列表
            top_n: 返回前 N 个高频词
            min_word_len: 最小词长（默认 2，过滤单字词）

        Returns:
            词频列表，格式: [{"word": "部署", "count": 50}, ...]
        """
        if not queries:
            return []

        try:
            # 分词并统计
            word_counts = Counter()

            for query in queries:
                if not query or not isinstance(query, str):
                    continue

                # 使用 jieba 分词
                words = jieba.cut(query.strip())

                # 过滤停用词和短词
                for word in words:
                    word = word.strip()
                    # 过滤条件：
                    # 1. 长度 >= min_word_len
                    # 2. 不在停用词表中
                    # 3. 不是纯数字
                    # 4. 不是纯标点符号
                    if (
                        len(word) >= min_word_len
                        and word not in self.STOP_WORDS
                        and not word.isdigit()
                        and not all(c in '，。！？；：""''（）【】《》、…—·' for c in word)
                    ):
                        word_counts[word] += 1

            # 获取前 N 个高频词
            top_words = word_counts.most_common(top_n)

            # 转换为字典列表
            result = [
                {"word": word, "count": count}
                for word, count in top_words
            ]

            logger.info(f"关键词提取完成: {len(queries)} 条问题 -> {len(result)} 个关键词")
            return result

        except Exception as e:
            logger.error(f"关键词提取失败: {e}", exc_info=True)
            return []

    def add_custom_words(self, words: List[str]):
        """
        添加自定义词汇到 jieba 词典

        Args:
            words: 自定义词汇列表
        """
        for word in words:
            jieba.add_word(word)
        logger.info(f"添加自定义词汇: {len(words)} 个")

    def add_stop_words(self, words: List[str]):
        """
        添加自定义停用词

        Args:
            words: 停用词列表
        """
        self.STOP_WORDS.update(words)
        logger.info(f"添加停用词: {len(words)} 个")
