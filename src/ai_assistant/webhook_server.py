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

    def handle_feishu_webhook(self):
        """处理飞书 webhook 回调：解析事件并放入队列"""
        from ai_assistant.core.trace_context import with_new_trace_id

        trace_id = with_new_trace_id()

        try:
            data = request.get_json()

            if not data:
                logger.warning("Empty webhook data received")
                return jsonify({"error": "Empty data"}), 400

            logger.info(f"📨 Webhook received event_type: {data.get('header', {}).get('event_type', data.get('type', 'unknown'))}")

            if self.feishu_adapter:
                response = self.feishu_adapter.handle_webhook_event(data)

                # 将事件放入队列，由消费线程处理
                if self.feishu_adapter.latest_event and self.event_queue:
                    pending_event = self.feishu_adapter.latest_event
                    self.feishu_adapter.latest_event = None  # 立即清空

                    try:
                        self.event_queue.put_nowait({
                            "trace_id": trace_id,
                            "adapter": self.feishu_adapter,
                            "event": pending_event
                        })
                        logger.info(f"✅ Event enqueued, queue size: {self.event_queue.qsize()}/{self.event_queue.maxsize}")
                    except queue.Full:
                        logger.error(f"❌ Event queue full (size={self.event_queue.maxsize}), dropping event")

                if response:
                    return jsonify(response), 200

            return jsonify({}), 200

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            return jsonify({"error": str(e)}), 500

    def health_check(self):
        """健康检查"""
        status = {
            "status": "ok",
            "queue_size": self.event_queue.qsize() if self.event_queue else 0
        }
        return jsonify(status), 200

    def run(self, debug: bool = False):
        """启动服务器"""
        logger.info(f"Starting webhook server on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=debug)
