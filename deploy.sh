#!/usr/bin/env bash
# ============================================================
# Agent Proxy 一键部署脚本
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/deploy.sh | bash -s -- <Git仓库URL>
# 或本地执行:
#   bash deploy.sh <Git仓库URL>
# ============================================================
set -euo pipefail

INSTALL_DIR="/opt/agent-proxy"
SERVICE_NAME="agent-proxy"
REPO_URL="${1:-}"

# ---- 修复 curl|bash 管道执行时 read 无法交互的问题 ----
# 管道执行时 stdin 不是终端，read 会读到脚本本身的内容。
# 尝试重定向到 /dev/tty，失败则使用默认值非交互部署。
if [ ! -t 0 ]; then
  if exec </dev/tty 2>/dev/null; then
    echo ""
  else
    NON_INTERACTIVE=1
  fi
fi

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---- 检查 root ----
if [[ $EUID -ne 0 ]]; then
  error "请用 root 运行: sudo bash deploy.sh"
fi

# ---- 检查仓库 URL ----
if [[ -z "$REPO_URL" ]]; then
  # 如果当前目录就是项目根（本地部署），跳过克隆
  if [[ -f "requirements.txt" && -d "app" ]]; then
    info "检测到本地项目文件，将从当前目录部署"
    REPO_URL=""
  else
    error "请提供 Git 仓库 URL: bash deploy.sh https://github.com/xxx/agent-proxy.git"
  fi
fi

# ---- 检查系统 ----
info "检查系统依赖..."
for cmd in python3 pip3 git; do
  if ! command -v $cmd &>/dev/null; then
    warn "$cmd 未安装，尝试安装..."
    if command -v apt-get &>/dev/null; then
      apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv git
    elif command -v yum &>/dev/null; then
      yum install -y python3 python3-pip git
    else
      error "无法自动安装 $cmd，请手动安装后重试"
    fi
  fi
done

# 确保 python3-venv 可用
if ! python3 -c "import venv" &>/dev/null; then
  warn "安装 python3-venv..."
  apt-get install -y -qq python3-venv 2>/dev/null || true
fi

# ---- 克隆/更新代码 ----
if [[ -n "$REPO_URL" ]]; then
  if [[ -d "$INSTALL_DIR" ]]; then
    info "$INSTALL_DIR 已存在，执行 git pull 更新..."
    cd "$INSTALL_DIR"
    git fetch --all
    git reset --hard origin/main 2>/dev/null || git reset --hard origin/master
  else
    info "克隆仓库到 $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
  fi
else
  # 本地部署：复制当前目录到 INSTALL_DIR
  mkdir -p "$INSTALL_DIR"
  info "复制本地文件到 $INSTALL_DIR ..."
  rsync -a --exclude='venv' --exclude='data' --exclude='__pycache__' --exclude='config.yaml' ./ "$INSTALL_DIR/" 2>/dev/null || cp -r . "$INSTALL_DIR/"
  cd "$INSTALL_DIR"
fi

# ---- 虚拟环境 ----
if [[ ! -d "venv" ]]; then
  info "创建 Python 虚拟环境..."
  python3 -m venv venv
fi
info "安装 Python 依赖..."
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

# ---- 生成配置 ----
CONFIG_FILE="$INSTALL_DIR/config.yaml"
if [[ ! -f "$CONFIG_FILE" ]]; then
  info "生成配置文件..."
  cp config.example.yaml "$CONFIG_FILE"

  if [[ "${NON_INTERACTIVE:-0}" == "1" ]]; then
    warn "非交互模式（无终端），使用默认配置：代理8080 / 管理8081 / 用户名admin / 密码admin123 / 不启用HTTPS"
    PROXY_PORT="8080"
    ADMIN_PORT="8081"
    ADMIN_USER="admin"
    ADMIN_PASS="admin123"
    HTTPS_CERT=""
    HTTPS_KEY=""
  else
    # 交互式配置
    echo ""
    read -rp "代理端口 [8080]: " PROXY_PORT
    PROXY_PORT="${PROXY_PORT:-8080}"
    read -rp "管理端口 [8081]: " ADMIN_PORT
    ADMIN_PORT="${ADMIN_PORT:-8081}"
    read -rp "管理用户名 [admin]: " ADMIN_USER
    ADMIN_USER="${ADMIN_USER:-admin}"
    read -rsp "管理密码: " ADMIN_PASS
    echo ""
    while [[ -z "$ADMIN_PASS" ]]; do
      read -rsp "密码不能为空，请输入: " ADMIN_PASS
      echo ""
    done
    # HTTPS 配置
    read -rp "是否启用 HTTPS? (y/N): " ENABLE_HTTPS
    HTTPS_CERT=""
    HTTPS_KEY=""
    if [[ "$ENABLE_HTTPS" =~ ^[Yy]$ ]]; then
      read -rp "证书完整路径 (fullchain.pem): " HTTPS_CERT
      read -rp "私钥完整路径 (privkey.pem): " HTTPS_KEY
      while [[ ! -f "$HTTPS_CERT" || ! -f "$HTTPS_KEY" ]]; do
        warn "证书文件不存在，请重新输入"
        read -rp "证书完整路径: " HTTPS_CERT
        read -rp "私钥完整路径: " HTTPS_KEY
      done
    fi
  fi

  # 写入配置（通过环境变量传参，避免命令行参数错位）
  AP_CONFIG_FILE="$CONFIG_FILE" \
  AP_PROXY_PORT="$PROXY_PORT" \
  AP_ADMIN_PORT="$ADMIN_PORT" \
  AP_ADMIN_USER="$ADMIN_USER" \
  AP_ADMIN_PASS="$ADMIN_PASS" \
  AP_HTTPS_CERT="$HTTPS_CERT" \
  AP_HTTPS_KEY="$HTTPS_KEY" \
  python3 <<'PYEOF'
import os, yaml
path = os.environ["AP_CONFIG_FILE"]
with open(path) as f:
    cfg = yaml.safe_load(f)
cfg['proxy']['port'] = int(os.environ["AP_PROXY_PORT"])
cfg['admin']['port'] = int(os.environ["AP_ADMIN_PORT"])
cfg['admin']['username'] = os.environ["AP_ADMIN_USER"]
cfg['admin']['password'] = os.environ["AP_ADMIN_PASS"]
cert = os.environ.get("AP_HTTPS_CERT", "")
key = os.environ.get("AP_HTTPS_KEY", "")
if cert and key:
    cfg['proxy']['https']['enabled'] = True
    cfg['proxy']['https']['cert'] = cert
    cfg['proxy']['https']['key'] = key
    cfg['admin']['https']['enabled'] = True
    cfg['admin']['https']['cert'] = cert
    cfg['admin']['https']['key'] = key
with open(path, 'w') as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
print("配置已写入")
PYEOF
else
  info "配置文件已存在，跳过生成（如需重新配置请删除 $CONFIG_FILE 后重跑）"
fi

# ---- 权限 ----
mkdir -p "$INSTALL_DIR/data"
# 尝试用 www-data，没有就用当前用户
if id www-data &>/dev/null; then
  RUN_USER="www-data"
else
  RUN_USER="root"
fi
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"
chmod 600 "$CONFIG_FILE"

# ---- systemd 服务 ----
info "配置 systemd 服务..."
cat > /etc/systemd/system/$SERVICE_NAME.service <<EOF
[Unit]
Description=Agent Proxy Gateway
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m app.main -c $CONFIG_FILE
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME

# ---- 等待启动 ----
sleep 2
if systemctl is-active --quiet $SERVICE_NAME; then
  info "服务启动成功!"
else
  error "服务启动失败，查看日志: journalctl -u $SERVICE_NAME -n 50"
fi

# ---- 获取本机 IP ----
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
PROTO="http"
if grep -q "enabled: true" "$CONFIG_FILE" 2>/dev/null; then
  PROTO="https"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Agent Proxy 部署完成!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  代理地址:  $PROTO://$LOCAL_IP:$(grep 'port:' "$CONFIG_FILE" | head -1 | awk '{print $2}')"
echo "  管理面板:  $PROTO://$LOCAL_IP:$(grep 'port:' "$CONFIG_FILE" | tail -1 | awk '{print $2}')"
echo ""
echo "  服务管理:"
echo "    启动: systemctl start $SERVICE_NAME"
echo "    停止: systemctl stop $SERVICE_NAME"
echo "    重启: systemctl restart $SERVICE_NAME"
echo "    状态: systemctl status $SERVICE_NAME"
echo "    日志: journalctl -u $SERVICE_NAME -f"
echo ""
echo "  配置文件: $CONFIG_FILE"
echo ""
