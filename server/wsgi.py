"""gunicorn 入口：初始化数据库/管理员/看门狗后暴露 app。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import app, bootstrap    # noqa: E402

bootstrap()
