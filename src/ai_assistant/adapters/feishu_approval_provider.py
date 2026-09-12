"""
飞书审批提供者实现

基于飞书私聊消息卡片实现运维操作审批流程
"""

import json
import uuid
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from loguru import logger

from ai_assistant.core.approval_provider import (
    ApprovalProvider,
    ApprovalRequest,
    ApprovalResponse,
)
from ai_assistant.core.operations_models import OperationRisk


class FeishuApprovalProvider(ApprovalProvider):
    """飞书审批提供者

    通过飞书私聊发送审批卡片给管理员，支持多人审批（一人同意即可执行）
    """

    def __init__(self, feishu_bot, approvers: List[Dict[str, str]], timeout: int = 180):
        """初始化飞书审批提供者

        Args:
            feishu_bot: FeishuBotAdapter 实例（用于发送消息和获取 token）
            approvers: 审批人列表，每项包含 open_id 和 name
                      示例：[{"open_id": "ou_xxx", "name": "张三"}]
            timeout: 审批超时时间（秒），默认 180 秒（3 分钟）
        """
        self.feishu_bot = feishu_bot
        self.approvers = approvers
        self.timeout = timeout

        # 审批请求缓存（request_id -> ApprovalRequest）
        self._pending_requests: Dict[str, ApprovalRequest] = {}

        # 审批响应缓存（request_id -> ApprovalResponse）
        self._responses: Dict[str, ApprovalResponse] = {}

        # 请求创建时间（request_id -> datetime）
        self._request_times: Dict[str, datetime] = {}

        # 线程锁
        self._lock = threading.Lock()

    def send_approval_request(self, request: ApprovalRequest) -> str:
        """发送审批请求

        循环向所有审批人发送私聊消息卡片

        Args:
            request: 审批请求数据

        Returns:
            审批请求的唯一标识

        Raises:
            Exception: 发送失败时抛出异常
        """
        request_id = f"approval_{uuid.uuid4().hex[:16]}"

        # 缓存请求
        with self._lock:
            self._pending_requests[request_id] = request
            self._request_times[request_id] = datetime.now()

        logger.info(f"📨 发送审批请求: request_id={request_id}, "
                   f"operator={request.operator.username}, "
                   f"machine={request.operation.machine.name}, "
                   f"command={request.operation.command[:50]}")

        # 构建审批卡片
        card_payload = self._build_approval_card(request, request_id)

        # 向所有审批人发送私聊消息
        success_count = 0
        for approver in self.approvers:
            try:
                success = self._send_private_message(
                    approver["open_id"],
                    card_payload
                )
                if success:
                    success_count += 1
                    logger.info(f"✅ 审批请求已发送给: {approver['name']}")
                else:
                    logger.warning(f"⚠️ 发送审批请求失败: {approver['name']}")
            except Exception as e:
                logger.error(f"❌ 发送审批请求异常: {approver['name']}, error={e}")

        if success_count == 0:
            # 清理缓存
            with self._lock:
                self._pending_requests.pop(request_id, None)
                self._request_times.pop(request_id, None)
            raise Exception("发送审批请求失败：所有审批人都发送失败")

        logger.info(f"📤 审批请求已发送给 {success_count}/{len(self.approvers)} 位审批人")
        return request_id

    def get_approval_status(self, request_id: str) -> Optional[ApprovalResponse]:
        """查询审批状态

        Args:
            request_id: 审批请求标识

        Returns:
            审批响应，如果还未审批则返回 None
        """
        with self._lock:
            # 检查是否已有响应
            if request_id in self._responses:
                return self._responses[request_id]

            # 检查是否超时
            request_time = self._request_times.get(request_id)
            if request_time:
                elapsed = (datetime.now() - request_time).total_seconds()
                if elapsed > self.timeout:
                    # 超时，自动拒绝
                    logger.warning(f"⏱️ 审批请求超时: request_id={request_id}, "
                                 f"elapsed={elapsed:.1f}s")
                    response = ApprovalResponse(
                        approved=False,
                        approver="系统",
                        reason=f"审批超时（{self.timeout}秒未响应）"
                    )
                    self._responses[request_id] = response
                    # 清理缓存
                    self._pending_requests.pop(request_id, None)
                    self._request_times.pop(request_id, None)
                    return response

            return None

    def cancel_approval_request(self, request_id: str) -> bool:
        """取消审批请求

        Args:
            request_id: 审批请求标识

        Returns:
            是否成功取消
        """
        with self._lock:
            if request_id in self._pending_requests:
                self._pending_requests.pop(request_id, None)
                self._request_times.pop(request_id, None)
                logger.info(f"🚫 审批请求已取消: request_id={request_id}")
                return True
            return False

    def record_approval(self, request_id: str, response: ApprovalResponse) -> bool:
        """记录审批响应（供 feishu_bot 卡片回调使用）

        Args:
            request_id: 审批请求标识
            response: 审批响应

        Returns:
            是否成功记录
        """
        with self._lock:
            if request_id not in self._pending_requests:
                logger.warning(f"⚠️ 未找到待审批请求: request_id={request_id}")
                return False

            # 记录响应
            self._responses[request_id] = response

            # 清理待审批列表
            self._pending_requests.pop(request_id, None)
            self._request_times.pop(request_id, None)

            logger.info(f"✅ 审批响应已记录: request_id={request_id}, "
                       f"approved={response.approved}, "
                       f"approver={response.approver}")
            return True

    def _build_approval_card(self, request: ApprovalRequest, request_id: str) -> dict:
        """构建审批卡片

        Args:
            request: 审批请求
            request_id: 请求标识

        Returns:
            飞书消息卡片 payload
        """
        operation = request.operation
        operator = request.operator
        machine = operation.machine

        # 风险等级映射
        risk_config = {
            OperationRisk.LOW: {"color": "blue", "icon": "ℹ️", "text": "低风险"},
            OperationRisk.MEDIUM: {"color": "orange", "icon": "⚠️", "text": "中风险"},
            OperationRisk.HIGH: {"color": "red", "icon": "🔥", "text": "高风险"},
        }
        risk = risk_config.get(operation.risk_level, risk_config[OperationRisk.MEDIUM])

        # 计算过期时间
        expires_at = datetime.now() + timedelta(seconds=self.timeout)
        expires_str = expires_at.strftime("%H:%M:%S")

        # 构建卡片内容
        card = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": "red",
                "title": {
                    "tag": "plain_text",
                    "content": "🔐 运维操作审批"
                }
            },
            "elements": [
                # 操作者信息
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**操作人：** {operator.display_name or operator.username}"
                    }
                },
                {
                    "tag": "hr"
                },
                # 目标机器
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**目标机器：** {machine.display_name}\n**主机：** {machine.ssh_config.host}"
                    }
                },
                {
                    "tag": "hr"
                },
                # 风险等级
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**风险等级：** {risk['icon']} {risk['text']}"
                    }
                },
                {
                    "tag": "hr"
                },
                # 执行命令
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**执行命令：**\n```\n{operation.command}\n```"
                    }
                },
                {
                    "tag": "hr"
                },
                # 操作原因
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**操作原因：**\n{operation.reason or '无'}"
                    }
                },
                {
                    "tag": "hr"
                },
                # 超时提示
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⏱️ 请在 {expires_str} 前完成审批（{self.timeout}秒后自动取消）"
                        }
                    ]
                },
                # 审批按钮
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "✅ 批准执行"
                            },
                            "type": "primary",
                            "value": {
                                "action": "approve_operation",
                                "request_id": request_id
                            }
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "❌ 拒绝执行"
                            },
                            "type": "danger",
                            "value": {
                                "action": "reject_operation",
                                "request_id": request_id
                            }
                        }
                    ]
                }
            ]
        }

        return {
            "msg_type": "interactive",
            "content": json.dumps(card)
        }

    def _send_private_message(self, user_open_id: str, payload: dict) -> bool:
        """发送私聊消息

        Args:
            user_open_id: 用户 open_id
            payload: 消息 payload

        Returns:
            是否成功
        """
        try:
            import requests

            token = self.feishu_bot.get_tenant_access_token()
            url = f"{self.feishu_bot.base_url}/open-apis/im/v1/messages"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            params = {"receive_id_type": "open_id"}

            # 构建请求体
            body = {
                "receive_id": user_open_id,
                "msg_type": payload["msg_type"],
                "content": payload["content"]
            }

            logger.debug(f"发送私聊消息: user={user_open_id[:8]}...")
            response = requests.post(url, headers=headers, json=body, params=params, timeout=10)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 0:
                logger.info(f"私聊消息发送成功: user={user_open_id[:8]}...")
                return True
            else:
                logger.warning(f"私聊消息发送失败: code={result.get('code')}, "
                             f"msg={result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"发送私聊消息异常: {e}")
            return False
