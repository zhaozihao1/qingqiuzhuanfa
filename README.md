# Agent Proxy Gateway

一个轻量级的 Agent 请求转发网关，支持多 BaseURL 切换、流式输出透传、全量请求日志留存，自带手机端管理面板。

## 功能特性

- **请求转发**：代理端口接收所有路径，原样拼接到当前生效的 BaseURL 后面，请求头/请求体除必改项外原样转发
- **多目标管理**：可配置多个 HTTP/HTTPS BaseURL，同一时间只有一个生效，管理页一键切换
- **流式输出**：原生支持 SSE (`text/event-stream`) 和 chunked 流式响应，逐块透传，不缓冲
- **全量日志**：保存每一条请求的请求头、请求体、响应状态、响应头、响应体、耗时
- **自动过期清理**：可设置保留天数（默认 10 天），每小时自动清理过期日志
- **失败筛选**：管理面板一键筛选失败请求（状态码 >= 400 或连接异常），按时间倒序排列
- **手机 UI**：管理面板移动端优先设计，手机浏览器直接操作
- **HTTPS 支持**：代理端口和管理端口均可独立配置 HTTPS 证书
- **双端口隔离**：代理端口和管理端口分开，管理端口可限制内网访问

## 快速开始

### 一键部署（推荐）

将本项目上传到 GitHub 后，在你的 Linux 服务器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/deploy.sh | sudo bash -s -- https://github.com/<你的用户名>/<仓库名>.git
```

脚本会自动完成：安装依赖 → 克隆代码 → 创建虚拟环境 → 安装 Python 包 → 交互式生成配置 → 配置 systemd 服务 → 启动。

### 手动部署

```bash
# 1. 克隆代码
git clone https://github.com/<你的用户名>/<仓库名>.git /opt/agent-proxy
cd /opt/agent-proxy

# 2. 创建虚拟环境并安装依赖
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 3. 生成配置
cp config.example.yaml config.yaml
vim config.yaml  # 修改端口、用户名密码、HTTPS 证书路径等

# 4. 启动
./venv/bin/python -m app.main -c config.yaml
```

### 配置 systemd 服务

```bash
sudo cp agent-proxy.service /etc/systemd/system/
sudo sed -i "s|/opt/agent-proxy|$(pwd)|g" /etc/systemd/system/agent-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now agent-proxy
```

## 上传到 GitHub

```bash
cd agent-proxy
git init
git add .
git commit -m "Initial commit: Agent Proxy Gateway"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

## 使用方法

### 1. 配置目标

打开管理面板（默认 `http://服务器IP:8081`），登录后在「目标」页添加 BaseURL，例如：

- 名称：`OpenAI`
- URL：`https://api.openai.com`

添加后点击「切换生效」。

### 2. 转发请求

将原本发给目标服务的请求，改发到代理端口即可。路径会原样拼接：

```
# 原本请求
https://api.openai.com/v1/chat/completions

# 改为（代理端口 8080）
http://服务器IP:8080/v1/chat/completions
```

请求头、请求体、流式响应都会原样转发。

### 3. 查看日志

在管理面板「日志」页查看所有请求记录，可勾选「仅看失败」筛选异常请求，点击单条查看完整请求/响应内容。

### 4. 设置保留天数

在「设置」页修改日志保留天数，保存后生效。超过保留天数的日志会被自动清理。

## 配置说明

`config.yaml` 主要字段：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `proxy.host` | 代理监听地址 | `0.0.0.0` |
| `proxy.port` | 代理端口 | `8080` |
| `proxy.https.enabled` | 代理端口是否启用 HTTPS | `false` |
| `admin.host` | 管理面板监听地址 | `0.0.0.0` |
| `admin.port` | 管理面板端口 | `8081` |
| `admin.username` | 管理面板用户名 | `admin` |
| `admin.password` | 管理面板密码 | `admin123` |
| `db_path` | SQLite 数据库路径 | `data/proxy.db` |
| `max_body_log_size` | 单条 body 最大记录字节数 | `10485760` (10MB) |
| `proxy_timeout` | 代理转发超时（秒） | `300` |

## 安全建议

- **务必修改默认密码**，使用强密码
- 管理面板建议只监听内网地址，或通过防火墙限制访问 IP
- 生产环境建议启用 HTTPS
- 代理端口如果暴露公网，建议加一层鉴权或 IP 白名单

## 技术栈

- **后端**：Python 3.10+ / FastAPI / httpx / aiosqlite
- **前端**：单页 HTML + Tailwind CSS + 原生 JS（移动端优先）
- **存储**：SQLite
- **部署**：systemd + 一键 shell 脚本

## License

MIT
