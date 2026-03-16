"""
Webhook 服务器

接收飞书事件回调
"""
import threading
from flask import Flask, request, jsonify
from loguru import logger
from typing import Optional, Callable


class WebhookServer:
    """Webhook 服务器"""

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
        self.message_handler: Optional[Callable] = None  # 消息处理回调

        # 注册路由
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

    def set_feishu_adapter(self, adapter):
        """设置飞书适配器"""
        self.feishu_adapter = adapter

    def set_message_handler(self, handler: Callable):
        """
        设置消息处理回调，事件到达时立即触发，无需等待主循环轮询

        Args:
            handler: 接受 adapter 参数的回调函数
        """
        self.message_handler = handler

    def handle_feishu_webhook(self):
        """处理飞书 webhook 回调"""
        try:
            data = request.get_json()

            if not data:
                logger.warning("Empty webhook data received")
                return jsonify({"error": "Empty data"}), 400

            logger.debug(f"Webhook event: {data.get('header', {}).get('event_type', data.get('type', 'unknown'))}")

            if self.feishu_adapter:
                response = self.feishu_adapter.handle_webhook_event(data)

                # 事件存入适配器后，立即在后台线程触发处理，避免阻塞 webhook 响应
                if self.feishu_adapter.latest_event and self.message_handler:
                    t = threading.Thread(
                        target=self.message_handler,
                        args=(self.feishu_adapter,),
                        daemon=True
                    )
                    t.start()
                logger.info(f"Webhook event resp: {response}")
                if response:
                    return jsonify(response), 200

            return jsonify({}), 200

        except Exception as e:
            logger.error(f"Error handling webhook: {e}")
            return jsonify({"error": str(e)}), 500

    def health_check(self):
        """健康检查"""
        return jsonify({"status": "ok"}), 200

    def run(self, debug: bool = False):
        """启动服务器"""
        logger.info(f"Starting webhook server on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=debug)
