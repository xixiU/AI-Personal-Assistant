"""
Anthropic Claude Provider

使用 Anthropic API 调用 Claude 模型，支持集成飞书文档管理器。
文档内容在调用前注入 system prompt，Claude 基于文档生成回复。
"""

from typing import List, Optional
from loguru import logger
import anthropic

from ai_assistant.core.ai_provider import AIProvider
from ai_assistant.core.models import Message


class AnthropicProvider(AIProvider):
    """Anthropic Claude Provider"""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str = None,
        timeout: int = 90,
        doc_manager=None,
        local_docs: list = None,  # 保留参数兼容性，但不再使用
    ):
        """
        Args:
            api_key: Anthropic API Key
            model: 模型名称
            base_url: API 基础 URL（可选，用于代理或兼容服务）
            timeout: 请求超时时间（秒）
            doc_manager: 飞书文档管理器实例（可选）
            local_docs: 已废弃，本地文档现在由文档管理器统一管理
        """
        self.model = model
        self.timeout = timeout
        self.doc_manager = doc_manager

        # 初始化 Anthropic 客户端
        client_kwargs = {"api_key": api_key, "timeout": timeout}
        if base_url:
            client_kwargs["base_url"] = base_url
            logger.info(f"使用自定义 base_url: {base_url}")

        self.client = anthropic.Anthropic(**client_kwargs)
        logger.info(f"Anthropic Provider 初始化: model={model}, base_url={base_url or '默认'}, docs={'启用' if doc_manager else '禁用'}")

    def extract_keywords(self, query_text: str) -> List[str]:
        """
        使用 Claude 从用户查询中提取搜索关键词

        使用轻量请求，让 Claude 理解用户意图后提取关键词。
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=100,
                messages=[{"role": "user", "content": query_text}],
                system=(
                    "你是一个关键词提取器。从用户的问题中提取用于搜索文档的关键词。\n"
                    "规则：\n"
                    "1. 提取 1-3 个最核心的搜索关键词\n"
                    "2. 保留专有名词、技术术语、产品名称的完整性（如 '智慧法庭V4.0' 不要拆分）\n"
                    "3. 英文变量名、配置项保持原样（如 'note_simplify_websocket_url'）\n"
                    "4. 去除无意义的词（如 '帮我找'、'是什么'、'怎么'）\n"
                    "5. 只输出关键词，用逗号分隔，不要输出其他内容"
                ),
            )
            text = response.content[0].text.strip()
            keywords = [kw.strip() for kw in text.split(",") if kw.strip()]
            return keywords[:3]
        except Exception as e:
            logger.warning(f"Claude 关键词提取失败: {e}")
            return []

    def send_message(self, messages: List[Message], session_id: Optional[str] = None) -> str:
        """发送消息到 Claude 并获取回复"""
        try:
            # 转换消息格式
            api_messages = []
            last_user_text = ""
            for msg in messages:
                content_parts = []
                for content in msg.content:
                    if content.type == "text":
                        content_parts.append({"type": "text", "text": content.data})
                        if msg.role == "user":
                            last_user_text = content.data
                    elif content.type == "image" and isinstance(content.data, dict):
                        # 图片内容：{"data": base64, "media_type": "image/png"}
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content.data.get("media_type", "image/png"),
                                "data": content.data["data"],
                            }
                        })

                if content_parts:
                    api_messages.append({"role": msg.role, "content": content_parts})

            # 构建 system prompt
            system_parts = ["你是一个智能助手，帮助用户回答问题。你必须始终使用中文回答，包括技术术语的解释也要用中文。即使用户用英文提问，你也要用中文回答。"]

            # 如果有文档管理器，获取相关文档（已包含在线文档 + 本地文档）
            if self.doc_manager and last_user_text:
                try:
                    doc_content = self.doc_manager.get_documents_by_query(last_user_text)
                    if doc_content:
                        system_parts.append(doc_content)
                except Exception as e:
                    logger.warning(f"文档获取失败，降级到无文档模式: {e}")

            if len(system_parts) > 1:
                system_parts.append("请基于以上文档内容回答用户的问题。如果文档中没有相关信息，请如实告知。")

            system_prompt = "\n\n".join(system_parts)

            # 调用 Anthropic API
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=api_messages,
            )

            reply = response.content[0].text
            logger.info(f"Claude 回复已接收: {len(reply)} 字符, tokens={response.usage.input_tokens}+{response.usage.output_tokens}")
            return reply

        except anthropic.APITimeoutError:
            logger.error("Anthropic API 请求超时")
            raise Exception("AI 服务响应超时")
        except anthropic.APIConnectionError as e:
            logger.error(f"Anthropic API 连接失败: {e}")
            raise Exception(f"AI 服务连接失败: {str(e)}")
        except anthropic.APIStatusError as e:
            logger.error(f"Anthropic API 错误: status={e.status_code}, message={e.message}")
            logger.error(f"请求详情: base_url={self.client.base_url}, model={self.model}")
            logger.error(f"响应体: {e.response.text if hasattr(e.response, 'text') else 'N/A'}")
            raise Exception(f"AI 服务调用失败: {e.message}")

    def check_health(self) -> bool:
        """检查 Anthropic API 服务健康状态"""
        try:
            # 发送一个简单请求验证 API Key 有效
            response = self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning(f"Anthropic 健康检查失败: {e}")
            return False

    def _load_local_docs(self, query_text: str) -> str:
        """
        根据用户查询加载匹配的本地离线文档

        匹配逻辑：检查每个 local_doc 配置的 keywords，
        如果用户查询中包含任一关键词，则读取该目录下的所有文档。
        """
        import os
        matched_parts = []

        for doc_config in self.local_docs:
            path = doc_config.get("path", "")
            description = doc_config.get("description", "")
            keywords = doc_config.get("keywords", [])

            if not path:
                continue

            if not os.path.isdir(path):
                logger.warning(f"本地文档目录不存在: {path}")
                continue

            # 检查关键词是否匹配（keywords 为空则始终加载）
            query_lower = query_text.lower()
            if keywords:
                matched = any(kw.lower() in query_lower for kw in keywords)
            else:
                matched = True  # 无 keywords 配置则始终加载

            if not matched:
                continue

            # 读取目录下的文档
            logger.info(f"加载本地文档: {path} ({description})")
            doc_texts = []
            total_chars = 0
            max_chars = 50000  # 单个目录最大字符数限制
            for root, dirs, files in os.walk(path):
                for fname in sorted(files):
                    if not fname.endswith(('.txt', '.md', '.sql', '.json', '.yaml', '.yml', '.csv')):
                        continue
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            content = f.read()
                        if content.strip():
                            if total_chars + len(content) > max_chars:
                                logger.warning(f"本地文档超出大小限制，截断: {path}, 已加载 {total_chars} 字符")
                                break
                            rel_path = os.path.relpath(fpath, path)
                            doc_texts.append(f"### {rel_path}\n{content}")
                            total_chars += len(content)
                    except Exception as e:
                        logger.warning(f"读取本地文档失败: {fpath}, error={e}")
                if total_chars >= max_chars:
                    break

            if doc_texts:
                matched_parts.append(f"以下是本地离线文档（{description}）：\n\n" + "\n\n".join(doc_texts))

        return "\n\n".join(matched_parts) if matched_parts else ""
