#!/usr/bin/env python3
"""vlm-pipeline Web 初始化：建目录、建库、创建管理员、登记默认端点。

由 systemd 单元 vlm-web.service 的 ExecStartPre 调用；也可手工执行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.app import init_app        # noqa: E402

init_app()
print("初始化完成")
