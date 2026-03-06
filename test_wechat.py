#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信适配器测试脚本

用于测试微信适配器的基本功能
"""

import sys
from pathlib import Path

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from loguru import logger

# 配置日志
logger.remove()
logger.add(sys.stderr, level="INFO")


def test_import():
    """测试导入"""
    try:
        from ai_assistant.adapters.wechat_adapter import WeChatAdapter, PYWECHAT_AVAILABLE

        if PYWECHAT_AVAILABLE:
            logger.info("✅ pywechat 已安装")
            return True
        else:
            logger.warning("❌ pywechat 未安装")
            logger.info("请运行: pip install git+https://github.com/Hello-Mr-Crab/pywechat.git")
            return False

    except Exception as e:
        logger.error(f"❌ 导入失败: {e}")
        return False


def test_adapter_init():
    """测试适配器初始化"""
    try:
        from ai_assistant.adapters.wechat_adapter import WeChatAdapter

        config = {
            "poll_interval": 1.0,
            "monitored_chats": []
        }

        adapter = WeChatAdapter(config)
        logger.info("✅ 微信适配器初始化成功")
        return True

    except ImportError:
        logger.warning("⚠️ pywechat 未安装，跳过初始化测试")
        return False
    except Exception as e:
        logger.error(f"❌ 初始化失败: {e}")
        return False


def test_detect_window():
    """测试窗口检测"""
    try:
        from ai_assistant.adapters.wechat_adapter import WeChatAdapter

        config = {
            "poll_interval": 1.0,
            "monitored_chats": []
        }

        adapter = WeChatAdapter(config)

        if adapter.detect_active_window():
            logger.info("✅ 检测到微信窗口")
            return True
        else:
            logger.warning("⚠️ 未检测到微信窗口（请确保微信正在运行）")
            return False

    except ImportError:
        logger.warning("⚠️ pywechat 未安装，跳过窗口检测测试")
        return False
    except Exception as e:
        logger.error(f"❌ 窗口检测失败: {e}")
        return False


def main():
    """主函数"""
    logger.info("=" * 50)
    logger.info("微信适配器测试")
    logger.info("=" * 50)

    # 测试导入
    logger.info("\n[1/3] 测试导入...")
    if not test_import():
        logger.error("\n测试失败：pywechat 未安装")
        logger.info("请先安装: pip install git+https://github.com/Hello-Mr-Crab/pywechat.git")
        return

    # 测试初始化
    logger.info("\n[2/3] 测试适配器初始化...")
    if not test_adapter_init():
        logger.error("\n测试失败：适配器初始化失败")
        return

    # 测试窗口检测
    logger.info("\n[3/3] 测试窗口检测...")
    test_detect_window()

    logger.info("\n" + "=" * 50)
    logger.info("测试完成！")
    logger.info("=" * 50)
    logger.info("\n提示：")
    logger.info("1. 如果 pywechat 未安装，请运行:")
    logger.info("   pip install git+https://github.com/Hello-Mr-Crab/pywechat.git")
    logger.info("2. 确保微信客户端正在运行")
    logger.info("3. 在 config.yaml 中启用微信适配器:")
    logger.info("   adapters:")
    logger.info("     - name: \"wechat\"")
    logger.info("       enabled: true")


if __name__ == "__main__":
    main()
