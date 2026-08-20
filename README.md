# Agent Relay

面向 Agent 的轻量 HTTP/HTTPS 流式请求转发器，默认监听转发端口 `8081`、管理端口 `8082`。

## 关键行为

- 支持配置多个 `http://` / `https://` Base URL，但只允许一个生效。
- 转发地址为：`生效 Base URL + 客户端原始 path/query`。例如 Base URL 为 `https://api.example.com/v1`，客户端请求 `/chat/completions`，上游地址为 `https://api.example.com/v1/chat/completions`。
- 使用流式读写，不等待完整响应，适合 SSE、chunked response 和 Agent 流式输出。
- 默认**只启用 HTTP 转发**：8081 收到 HTTPS 不会被支持，也不会记录；不会做无效的 HTTPS 直通。
- 在管理端上传服务器证书和私钥并启用后，8081 才切换为 HTTPS，终止 TLS、按路径转发并记录 HTTPS 内容。8082 管理页面始终为 HTTP。
- 记录请求/响应头、正文、状态码、耗时和失败原因；正文按流写入磁盘。
- 默认保留 10 天，每小时清理过期日志，可在页面设置 1–3650 天。
- 管理页面有“一键筛选失败”，默认按时间倒序。
- 默认管理账号和密码均为 `admin`，公网使用前请修改 `.env`。

## GitHub 一键部署

把整个目录上传到 GitHub 仓库 `zhaozihao1/qingqiuzhuanfa` 后，在 Linux 服务器执行下面一条命令：

```text
curl -fsSL https://raw.githubusercontent.com/zhaozihao1/qingqiuzhuanfa/main/install.sh | bash -s -- zhaozihao1/qingqiuzhuanfa
```

脚本会：

1. 检查 Docker Engine 和 Docker Compose Plugin；
2. 将仓库下载到 `~/agent-relay`；
3. 创建 `.env`（默认账号密码 `admin/admin`）；
4. 构建并启动容器，数据保存在 Docker volume `agent-relay-data`。

自定义安装目录或分支：

```text
AGENT_RELAY_DIR=/opt/agent-relay AGENT_RELAY_REF=main curl -fsSL https://raw.githubusercontent.com/zhaozihao1/qingqiuzhuanfa/main/install.sh | bash -s -- zhaozihao1/qingqiuzhuanfa
```

部署后编辑服务器上的 `.env`：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请改成随机长密码
RELAY_PORT=8081
ADMIN_PORT=8082
```

然后执行：

```text
docker compose up -d --build
```

## 管理页面

打开 `http://服务器IP:8082`，默认登录：

- 用户名：`admin`
- 密码：`admin`

先在“上游配置”添加并启用 Base URL，再将 Agent 的 Base URL 指向：

```text
http://服务器IP:8081
```

如果 Agent SDK 自己拼接 `/v1/...`，请将上游 Base URL 配成对应的 `/v1` 前缀。

## HTTPS 记录配置

默认不处理 8081 的 HTTPS。需要记录 HTTPS 内容时：

1. 通过 HTTP 管理页面进入“系统设置”；
2. 粘贴服务器证书 PEM 和私钥 PEM；
3. 勾选“解密并记录 HTTPS 内容”；
4. 应用设置，8081 会切换为 HTTPS，8082 不变；
5. 客户端改用 `https://服务器IP:8081`。

证书私钥仅写入数据卷中的 `private-key.pem`，权限为 `0600`。生产环境建议在 Agent Relay 前使用 Nginx/云负载均衡终止公网 TLS，并按实际安全要求配置访问控制。

## 常用运维命令

```text
docker compose ps
docker compose logs -f agent-relay
docker compose restart agent-relay
docker compose down
```

## 注意事项

日志包含完整请求和响应正文，可能含有 Token、个人信息或业务敏感数据。请限制 `8082` 管理端口、防火墙只允许可信来源，并妥善备份/保护 Docker volume。
