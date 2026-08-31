"""
Webhook 服务器

接收飞书事件回调，将事件放入队列由线程池并发处理
"""
import queue
from flask import Flask, request, jsonify, send_from_directory
from loguru import logger
from typing import Optional
import os
from ai_assistant.core.feedback_manager import FeedbackManager


class WebhookServer:
    """Webhook 服务器（生产者角色，只负责接收事件并放入队列）"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, web_frontend_enabled: bool = False):
        """
        初始化 Webhook 服务器

        Args:
            host: 监听地址
            port: 监听端口
            web_frontend_enabled: 是否启用自有前端 Web（首页 / Web 聊天 / Web 反馈）。
                默认关闭，因当前前端无鉴权。关闭时不注册这些路由，使其真正不可访问；
                飞书 webhook 与健康检查始终可用。
        """
        # 前端禁用时传 static_folder=None，连 Flask 内置的 /static/<path> 静态路由
        # 也不挂载，避免 static/ 目录下的 index.html、js/css 等被直接 URL 访问。
        if web_frontend_enabled:
            self.app = Flask(__name__)
        else:
            self.app = Flask(__name__, static_folder=None)
        self.host = host
        self.port = port
        self.web_frontend_enabled = web_frontend_enabled
        self.feishu_adapter = None
        self.event_queue: Optional[queue.Queue] = None  # 事件队列（由外部注入）
        self.server = None  # 用于存储 waitress 服务器实例

        # 静态文件目录（src/ai_assistant/static/）
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")

        # 初始化 FeedbackManager
        self.feedback_manager = FeedbackManager()

        # ===== 始终注册：飞书 webhook 与健康检查（不受前端开关影响）=====
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
            "/webhook/feishu/card",
            "feishu_card",
            self.handle_feishu_card,
            methods=["POST"]
        )

        # ===== 按开关注册：自有前端 Web 路由（无鉴权，默认关闭）=====
        if self.web_frontend_enabled:
            self.app.add_url_rule(
                "/",
                "index",
                self.serve_index,
                methods=["GET"]
            )

            self.app.add_url_rule(
                "/api/chat",
                "chat",
                self.handle_chat,
                methods=["POST"]
            )

            self.app.add_url_rule(
                "/api/feedback",
                "feedback",
                self.handle_feedback,
                methods=["POST"]
            )
            logger.info("自有前端 Web 已启用（首页 / Web 聊天 / Web 反馈接口）")
        else:
            logger.info("自有前端 Web 已禁用（无鉴权，默认关闭）；仅飞书 webhook 与 /health 可用")

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
                    # 解密失败也要返回 200 避免飞书重试
                    return jsonify({}), 200

            # URL 验证请求需要立即返回 challenge（不放入队列）
            if decrypted_data.get("type") == "url_verification":
                challenge = decrypted_data.get("challenge", "")
                logger.info(f"✅ URL verification: returning challenge={challenge}")
                return jsonify({"challenge": challenge}), 200

            # 卡片交互事件需要同步处理并返回响应体（不能异步）
            event_type = decrypted_data.get("header", {}).get("event_type", "")
            if event_type == "card.action.trigger":
                logger.info(f"📝 Card action trigger, processing synchronously")
                if self.feishu_adapter:
                    response_body = self.feishu_adapter.process_card_action(decrypted_data) or {}
                    logger.info(f"Card action response: {response_body}")
                    return jsonify(response_body), 200
                else:
                    logger.error("Feishu adapter not set")
                    return jsonify({}), 200

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
            "session_id": "会话ID",
            "record_id": "对话历史记录主键（用于关联反馈）"
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

            # 调用 AI 生成回复（返回回复文本 + chat_history 记录主键）
            reply, record_id, metadata = self.ai_provider.call(context_messages, session_id=session_id, source="web")

            # 将 AI 回复添加到上下文
            ai_message = Message(
                role="assistant",
                content=[Content(type="text", data=reply)],
                timestamp=datetime.now()
            )
            self.context_manager.add_message(session_id, ai_message)

            return jsonify({
                "reply": reply,
                "session_id": session_id,
                "record_id": record_id
            }), 200

        except Exception as e:
            logger.error(f"Error handling chat request: {e}")
            return jsonify({"error": str(e)}), 500

    def handle_feishu_card(self):
        """
        处理飞书卡片交互事件（按钮点击）

        处理 card.action.trigger 事件，提取反馈信息并调用 feishu_bot 的处理方法
        """
        try:
            data = request.get_json()
            if not data:
                logger.warning("Empty card action data received")
                return jsonify({}), 200

            logger.info(f"📨 Card action received: {data}")

            # 如果配置了加密，先解密
            if self.feishu_adapter and self.feishu_adapter.encrypt_key and "encrypt" in data:
                try:
                    data = self.feishu_adapter._decrypt(data["encrypt"])
                    logger.info("Card action decrypted successfully")
                except Exception as e:
                    logger.error(f"Failed to decrypt card action: {e}")
                    return jsonify({}), 200

            # 验证事件类型
            event_type = data.get("header", {}).get("event_type", "")
            if event_type != "card.action.trigger":
                logger.warning(f"Unexpected event type: {event_type}")
                return jsonify({}), 200

            # 调用 feishu_bot 处理卡片交互，拿到响应体（toast 或表单卡片）
            if self.feishu_adapter:
                response_body = self.feishu_adapter.process_card_action(data) or {}
            else:
                logger.error("Feishu adapter not set")
                response_body = {}

            # 返回响应体给飞书（toast 提示 / 表单卡片 / 空 dict）
            return jsonify(response_body), 200

        except Exception as e:
            logger.error(f"Error handling card action: {e}", exc_info=True)
            return jsonify({}), 200

    def handle_feedback(self):
        """
        处理 Web 端反馈提交

        请求格式：
        {
            "session_id": "xxx",
            "record_id": "chat_history 记录主键",
            "feedback_type": "like|dislike",
            "feedback_text": "用户反馈文字（可选）"
        }

        响应格式：
        {
            "success": true,
            "feedback_id": "uuid"
        }
        """
        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Empty request"}), 400

            # 提取必填字段
            session_id = data.get("session_id")
            record_id = data.get("record_id")
            feedback_type = data.get("feedback_type")

            # 验证必填字段
            required_fields = {
                "session_id": session_id,
                "record_id": record_id,
                "feedback_type": feedback_type,
            }

            for field_name, field_value in required_fields.items():
                if not field_value:
                    return jsonify({"error": f"Missing required field: {field_name}"}), 400

            # 验证 feedback_type 枚举值
            if feedback_type not in ["like", "dislike"]:
                return jsonify({"error": f"Invalid feedback_type: {feedback_type}, must be 'like' or 'dislike'"}), 400

            # 提取可选字段
            feedback_text = data.get("feedback_text")

            # 调用 FeedbackManager 保存反馈
            feedback_id = self.feedback_manager.save_feedback(
                record_id=record_id,
                session_id=session_id,
                source="web",
                feedback_type=feedback_type,
                feedback_text=feedback_text if feedback_text else None
            )

            logger.info(f"Web 反馈提交: session={session_id}, type={feedback_type}, feedback_id={feedback_id}")

            return jsonify({
                "success": True,
                "feedback_id": feedback_id
            }), 200

        except ValueError as e:
            # FeedbackManager 参数验证失败
            logger.warning(f"反馈参数验证失败: {e}")
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            # 其他异常
            logger.error(f"处理反馈请求失败: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

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
