#!/usr/bin/env bash
set -Eeuo pipefail

# One-command GitHub deployment:
# curl -fsSL https://raw.githubusercontent.com/zhaozihao1/qingqiuzhuanfa/main/install.sh | bash -s -- zhaozihao1/qingqiuzhuanfa
REPO="${1:-${AGENT_RELAY_REPO:-zhaozihao1/qingqiuzhuanfa}}"
REF="${AGENT_RELAY_REF:-main}"
INSTALL_DIR="${AGENT_RELAY_DIR:-$HOME/agent-relay}"

if [[ -z "$REPO" ]]; then
  echo "用法: curl -fsSL https://raw.githubusercontent.com/zhaozihao1/qingqiuzhuanfa/main/install.sh | bash -s -- zhaozihao1/qingqiuzhuanfa" >&2
  exit 2
fi

command -v curl >/dev/null || { echo "缺少 curl" >&2; exit 1; }
command -v docker >/dev/null || { echo "缺少 Docker，请先安装 Docker Engine 与 Compose Plugin" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "缺少 Docker Compose Plugin" >&2; exit 1; }

mkdir -p "$INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --depth=1 origin "$REF"
  git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
else
  command -v git >/dev/null || { echo "首次部署需要 git" >&2; exit 1; }
  git clone --depth=1 --branch "$REF" "https://github.com/$REPO.git" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "已创建 $INSTALL_DIR/.env，默认管理账号密码均为 admin。"
fi

docker compose up -d --build

echo
echo "部署完成："
echo "  管理页面: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${ADMIN_PORT:-8082}"
echo "  转发地址: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${RELAY_PORT:-8081}"
echo "  默认账号: admin"
echo "  默认密码: admin"
echo "  配置文件: $INSTALL_DIR/.env"
echo
echo "安全提示：公网部署前请编辑 .env 修改 ADMIN_PASSWORD，并限制 8082 的访问来源。"
