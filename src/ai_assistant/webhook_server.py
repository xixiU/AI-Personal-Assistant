"""
Webhook 服务器

接收飞书事件回调，将事件放入队列由线程池并发处理
"""
import queue
from flask import Flask, request, jsonify, send_from_directory
from loguru import logger
from typing import Optional
import os


class WebhookServer:
    """Webhook 服务器（生产者角色，只负责接收事件并放入队列）"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        """
        初始化 Webhook 服务器

        Args:
            host: 监听地址
            port: 监听端口
        """
        self.app = Flask(__name__)
        self.host = host
        self.port = port
        self.feishu_adapter = None
        self.event_queue: Optional[queue.Queue] = None  # 事件队列（由外部注入）
        self.server = None  # 用于存储 waitress 服务器实例

        # 静态文件目录（src/ai_assistant/static/）
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")

        # 注册路由
        self.app.add_url_rule(
            "/",
            "index",
            self.serve_index,
            methods=["GET"]
        )

        self.app.add_url_rule(
            "/webhook/feishu",
            "feishu_webhook",
            self.handle_feishu_webhook,
            methods=["POST"]
        )

        self.app.add_url_rule(
            "/health",
            "health_check",
            self.health_check,
            methods=["GET"]
        )

        self.app.add_url_rule(
            "/api/chat",
            "chat",
            self.handle_chat,
            methods=["POST"]
        )

        # 用于存储 AI Provider 和 Context Manager 的引用
        self.ai_provider = None
        self.context_manager = None

    def serve_index(self):
        """返回首页"""
        try:
            return send_from_directory(self.static_dir, "index.html")
        except Exception as e:
            logger.error(f"Error serving index.html: {e}")
            return jsonify({"error": "index.html not found"}), 404

    def set_feishu_adapter(self, adapter):
        """设置飞书适配器"""
        self.feishu_adapter = adapter

    def set_event_queue(self, event_queue: queue.Queue):
        """
        设置事件队列

        Args:
            event_queue: 事件队列，webhook 收到事件后放入队列
        """
        self.event_queue = event_queue

    def set_ai_components(self, ai_provider, context_manager):
        """
        设置 AI 组件（用于 Web 聊天接口）

        Args:
            ai_provider: AI Provider 实例
            context_manager: Context Manager 实例
        """
        self.ai_provider = ai_provider
        self.context_manager = context_manager

    def handle_feishu_webhook(self):
        """
        处理飞书 webhook 回调：立即返回 200，将原始事件放入队列异步处理

        飞书要求 3 秒内返回 HTTP 200，否则会认为推送失败。
        因此这里只做最基本的验证，立即返回 200，所有业务逻辑在后台处理。
        """
        from ai_assistant.core.trace_context import with_new_trace_id

        trace_id = with_new_trace_id()

        try:
            data = request.get_json()

            if not data:
                logger.warning("Empty webhook data received")
                return jsonify({"error": "Empty data"}), 400

            # 记录接收到的事件（加密数据无法读取详细信息）
            logger.info(f"📨 Webhook received, data : {data}")

            # 如果是加密数据，先解密以判断事件类型（URL 验证必须同步返回，不能走异步队列）
            decrypted_data = data
            if "encrypt" in data and self.feishu_adapter and self.feishu_adapter.encrypt_key:
                try:
                    decrypted_data = self.feishu_adapter._decrypt(data["encrypt"])
                    logger.info(f"📨 Decrypted preview: type={decrypted_data.get('type')}, "
                                f"event_type={decrypted_data.get('header', {}).get('event_type')}")
                except Exception as e:
                    logger.error(f"Failed to decrypt webhook for type check: {e}")

            # URL 验证请求需要立即返回 challenge（不放入队列）
            if decrypted_data.get("type") == "url_verification":
                challenge = decrypted_data.get("challenge", "")
                logger.info(f"✅ URL verification: returning challenge={challenge}")
                return jsonify({"challenge": challenge}), 200

            # 将原始事件数据放入队列，由后台线程异步处理
            if self.event_queue:
                try:
                    self.event_queue.put_nowait({
                        "trace_id": trace_id,
                        "adapter": self.feishu_adapter,
                        "raw_data": data  # 存储原始数据，不预处理
                    })
                    logger.info(f"✅ Event enqueued, queue size: {self.event_queue.qsize()}/{self.event_queue.maxsize}")
                except queue.Full:
                    logger.error(f"❌ Event queue full (size={self.event_queue.maxsize}), dropping event")
                    # 即使队列满了，也要返回 200，避免飞书重试

            # 立即返回 200，确保在 3 秒内响应
            return jsonify({}), 200

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            # 即使出错也返回 200，避免飞书重试
            return jsonify({}), 200

    def health_check(self):
        """健康检查"""
        status = {
            "status": "ok",
            "queue_size": self.event_queue.qsize() if self.event_queue else 0
        }
        return jsonify(status), 200

    def handle_chat(self):
        """
        处理 Web 聊天请求

        请求格式：
        {
            "message": "用户消息",
            "session_id": "会话ID（可选）",
            "image": {              // 可选
                "data": "base64...",
                "media_type": "image/png"
            }
        }

        响应格式：
        {
            "reply": "AI 回复",
            "session_id": "会话ID"
        }
        """
        from ai_assistant.core.models import Message, Content
        from datetime import datetime

        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Empty request"}), 400

            user_text = data.get("message", "")
            session_id = data.get("session_id", "web-default")
            image_data = data.get("image")  # {"data": base64, "media_type": "image/png"}

            if not user_text and not image_data:
                return jsonify({"error": "Missing message or image"}), 400

            if not self.ai_provider or not self.context_manager:
                return jsonify({"error": "AI components not initialized"}), 500

            # 构建用户消息内容
            contents = []
            if image_data:
                contents.append(Content(type="image", data=image_data))
            if user_text:
                contents.append(Content(type="text", data=user_text))

            user_message = Message(
                role="user",
                content=contents,
                timestamp=datetime.now()
            )

            # 添加到上下文
            self.context_manager.add_message(session_id, user_message)

            # 获取上下文消息
            context_messages = self.context_manager.get_context(session_id)

            # 调用 AI 生成回复
            reply = self.ai_provider.call(context_messages, session_id=session_id)

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now()
            )
            self.context_manager.add_message(session_id, ai_message)

            return jsonify({
                "reply": reply,
                "session_id": session_id
            }), 200

        except Exception as e:
            logger.error(f"Error handling chat request: {e}")
            return jsonify({"error": str(e)}), 500

    def run(self, debug: bool = False):
        """启动服务器（开发模式，使用 Flask 内置服务器）"""
        logger.info(f"Starting webhook server on {self.host}:{self.port} (development mode)")
        self.app.run(host=self.host, port=self.port, debug=debug)

    def run_production(self):
        """启动服务器（生产模式，使用 waitress）"""
        try:
            from waitress import serve
            logger.info(f"Starting webhook server on {self.host}:{self.port} (production mode with waitress)")
            # waitress 是阻塞调用，会一直运行直到被停止
            serve(self.app, host=self.host, port=self.port, threads=4)
        except ImportError:
            logger.warning("waitress not installed, falling back to Flask development server")
            logger.warning("Install waitress with: pip install waitress")
            self.run(debug=False)
        except Exception as e:
            logger.error(f"Error starting production server: {e}", exc_info=True)

    def shutdown(self):
        """停止服务器"""
        # waitress 没有提供优雅停止的 API，只能通过线程退出来停止
        # 这里主要是为了日志记录
        logger.info("Webhook server shutdown requested")
