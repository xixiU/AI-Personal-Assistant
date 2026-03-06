#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 自动回复助手 - 入口脚本
"""

import sys
from pathlib import Path

# 添加 src 目录到 Python 路径
project_root = Path(__file__).parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))

from ai_assistant.main import main

if __name__ == "__main__":
    main()
