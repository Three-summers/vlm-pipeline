#!/usr/bin/env bash
# 安装 systemd 单元（需要 root）。用法: sudo deploy/install.sh
# 安装前请编辑 vlm-*.service 中的 User/Group 与路径（见 docs/DEPLOY.md）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行" >&2; exit 1; }

if grep -q 'CHANGE_ME' "$HERE/vlm-server.service" "$HERE/vlm-web.service"; then
  echo "请先把 deploy/vlm-*.service 里的 User/Group=CHANGE_ME 改成实际系统用户。" >&2
  exit 1
fi

install -m 644 "$HERE/vlm-server.service" /etc/systemd/system/
install -m 644 "$HERE/vlm-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable vlm-server.service vlm-web.service

echo "已安装并设为开机自启。启动："
echo "  sudo systemctl start vlm-server   # 等 60-90s 模型加载"
echo "  sudo systemctl start vlm-web"
echo "健康检查： curl http://127.0.0.1:8090/healthz"
