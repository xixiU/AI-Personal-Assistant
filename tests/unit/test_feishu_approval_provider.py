"""
飞书审批提供者单元测试
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from ai_assistant.adapters.feishu_approval_provider import FeishuApprovalProvider
from ai_assistant.core.approval_provider import ApprovalRequest, ApprovalResponse
from ai_assistant.core.operations_models import (
    PendingOperation,
    OperatorIdentity,
    Machine,
    SSHConfig,
    OperationRisk,
)


class TestFeishuApprovalProvider:
    """测试飞书审批提供者"""

    @pytest.fixture
    def mock_feishu_bot(self):
        """模拟飞书机器人"""
        bot = Mock()
        bot.get_tenant_access_token.return_value = "mock_token"
        bot.base_url = "https://open.feishu.cn"
        return bot

    @pytest.fixture
    def approvers(self):
        """审批人列表"""
        return [
            {"open_id": "ou_admin001", "name": "张三"},
            {"open_id": "ou_admin002", "name": "李四"},
        ]

    @pytest.fixture
    def approval_provider(self, mock_feishu_bot, approvers):
        """审批提供者实例"""
        return FeishuApprovalProvider(
            feishu_bot=mock_feishu_bot,
            approvers=approvers,
            timeout=180
        )

    @pytest.fixture
    def sample_request(self):
        """示例审批请求"""
        operator = OperatorIdentity(
            user_id="user_001",
            username="王五",
            display_name="运维工程师王五"
        )

        ssh_config = SSHConfig(
            host="192.168.1.10",
            port=22,
            username="deploy"
        )

        machine = Machine(
            name="prod1",
            display_name="生产服务器1",
            ssh_config=ssh_config,
            description="订单服务器"
        )

        operation = PendingOperation(
            operation_id="op_001",
            operator=operator,
            machine=machine,
            command="./restart.sh",
            risk_level=OperationRisk.MEDIUM,
            reason="重启订单服务"
        )

        return ApprovalRequest(
            operation=operation,
            operator=operator,
            request_message="请求重启订单服务"
        )

    def test_init(self, mock_feishu_bot, approvers):
        """测试初始化"""
        provider = FeishuApprovalProvider(
            feishu_bot=mock_feishu_bot,
            approvers=approvers,
            timeout=300
        )

        assert provider.feishu_bot == mock_feishu_bot
        assert provider.approvers == approvers
        assert provider.timeout == 300
        assert len(provider._pending_requests) == 0
        assert len(provider._responses) == 0

    @patch('requests.post')
    def test_send_approval_request_success(
        self, mock_post, approval_provider, sample_request
    ):
        """测试成功发送审批请求"""
        # 模拟飞书 API 响应
        mock_response = Mock()
        mock_response.json.return_value = {"code": 0, "msg": "success"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        # 发送审批请求
        request_id = approval_provider.send_approval_request(sample_request)

        # 验证返回值
        assert request_id is not None
        assert request_id.startswith("approval_")

        # 验证缓存
        assert request_id in approval_provider._pending_requests
        assert request_id in approval_provider._request_times

        # 验证调用了飞书 API（每个审批人一次）
        assert mock_post.call_count == 2

    @patch('requests.post')
    def test_send_approval_request_all_failed(
        self, mock_post, approval_provider, sample_request
    ):
        """测试所有审批人发送失败"""
        # 模拟飞书 API 失败
        mock_response = Mock()
        mock_response.json.return_value = {"code": 1, "msg": "failed"}
        mock_response.raise_for_status = Mock()
        mock_post.return_value = mock_response

        # 发送审批请求应该抛出异常
        with pytest.raises(Exception, match="发送审批请求失败"):
            approval_provider.send_approval_request(sample_request)

        # 验证缓存被清理
        assert len(approval_provider._pending_requests) == 0

    def test_get_approval_status_pending(self, approval_provider, sample_request):
        """测试查询待审批状态"""
        # 手动添加待审批请求
        request_id = "test_req_001"
        approval_provider._pending_requests[request_id] = sample_request
        approval_provider._request_times[request_id] = datetime.now()

        # 查询状态（未审批）
        status = approval_provider.get_approval_status(request_id)
        assert status is None

    def test_get_approval_status_approved(self, approval_provider):
        """测试查询已批准状态"""
        request_id = "test_req_002"
        response = ApprovalResponse(
            approved=True,
            approver="张三",
            reason="已批准"
        )

        # 记录响应
        approval_provider._responses[request_id] = response

        # 查询状态
        status = approval_provider.get_approval_status(request_id)
        assert status is not None
        assert status.approved is True
        assert status.approver == "张三"

    def test_record_approval(self, approval_provider, sample_request):
        """测试记录审批响应"""
        # 添加待审批请求
        request_id = "test_req_003"
        approval_provider._pending_requests[request_id] = sample_request
        approval_provider._request_times[request_id] = datetime.now()

        # 记录审批响应
        response = ApprovalResponse(
            approved=True,
            approver="李四",
            reason="批准执行"
        )

        success = approval_provider.record_approval(request_id, response)

        # 验证结果
        assert success is True
        assert request_id in approval_provider._responses
        assert request_id not in approval_provider._pending_requests
        assert approval_provider._responses[request_id].approved is True

    def test_cancel_approval_request(self, approval_provider, sample_request):
        """测试取消审批请求"""
        # 添加待审批请求
        request_id = "test_req_004"
        approval_provider._pending_requests[request_id] = sample_request
        approval_provider._request_times[request_id] = datetime.now()

        # 取消审批
        success = approval_provider.cancel_approval_request(request_id)

        # 验证结果
        assert success is True
        assert request_id not in approval_provider._pending_requests
        assert request_id not in approval_provider._request_times

    def test_build_approval_card(self, approval_provider, sample_request):
        """测试构建审批卡片"""
        request_id = "test_req_005"
        card = approval_provider._build_approval_card(sample_request, request_id)

        # 验证卡片结构
        assert card["msg_type"] == "interactive"
        assert "content" in card

        import json
        content = json.loads(card["content"])
        assert "header" in content
        assert content["header"]["template"] == "red"
        assert "🔐 运维操作审批" in content["header"]["title"]["content"]

        # 验证包含关键信息
        elements = content["elements"]
        assert len(elements) > 0

        # 验证按钮
        action_element = None
        for elem in elements:
            if elem.get("tag") == "action":
                action_element = elem
                break

        assert action_element is not None
        actions = action_element["actions"]
        assert len(actions) == 2  # 批准和拒绝按钮

        # 验证按钮包含 request_id
        approve_button = actions[0]
        assert approve_button["value"]["action"] == "approve_operation"
        assert approve_button["value"]["request_id"] == request_id
