# Agent Relay

面向 Agent 的轻量 HTTP/HTTPS 流式请求转发器，默认监听转发端口 `8081`、管理端口 `8082`。

## 关键行为

- 支持配置多个 `http://` / `https://` Base URL，但只允许一个生效。
- 每个上游可单独保存 API 密钥并选择“自动替换 Authorization”；切换上游即可自动使用对应密钥。关闭时会透传客户端原始 Authorization。
- 入口密钥保护默认关闭，此时放行所有请求；开启后，客户端必须通过 `Authorization: Bearer <入口密钥>` 或 `X-Relay-Key: <入口密钥>` 携带已配置密钥，验证通过后才会替换真实上游密钥并转发。
- 入口密钥只保存 SHA-256 摘要，页面只显示首尾各 4 位，保存后只能删除。
- 转发地址为：`生效 Base URL + 客户端原始 path/query`。例如 Base URL 为 `https://api.example.com/v1`，客户端请求 `/chat/completions`，上游地址为 `https://api.example.com/v1/chat/completions`。
- 使用流式读写，不等待完整响应；上游一有可读字节就立即转发，适合 SSE、chunked response 和 Agent 流式输出。
- 转发层不解析或重组 JSON/SSE，`reasoning_content`、`thinking`、增量 `tool_calls` 等字段均按原始字节和原顺序透传。
- SSE 响应自动附加 `X-Accel-Buffering: no`，避免常见 Nginx 配置缓存整个响应；如果前面还有 CDN 或其他反向代理，仍需在对应服务中关闭响应缓冲。
- 默认**只启用 HTTP 转发**：8081 收到 HTTPS 不会被支持，也不会记录；不会做无效的 HTTPS 直通。
- 在管理端上传服务器证书和私钥并启用后，8081 才切换为 HTTPS，终止 TLS、按路径转发并记录 HTTPS 内容。8082 管理页面始终为 HTTP。
- 记录请求/响应头、正文、状态码、耗时和失败原因；正文按流写入磁盘。
- 默认保留 10 天，每小时清理过期的请求日志和拒绝统计，可在页面设置 1–3650 天。
- 管理页面有“一键筛选失败”，默认按时间倒序。
- 默认管理账号和密码均为 `admin`；可在系统设置输入老密码和新密码修改，改密后已有管理会话立即失效。
- 登录页“忘记密码”会清空平台全部数据并将密码恢复为 `admin`。

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

更新现有部署：拉取新代码后重新构建容器：

```text
cd ~/agent-relay
git pull --ff-only origin main
docker compose up -d --build
```

管理页面的“系统更新”功能默认启用，使用宿主机上的受控脚本执行更新。默认命令路径为 `/usr/local/sbin/agent-relay-update`，也可以通过容器环境变量 `RELAY_UPDATE_COMMAND` 覆盖：

```dotenv
RELAY_UPDATE_COMMAND=docker rm -f agent-relay-updater >/dev/null 2>&1 || true; docker run -d --rm --name agent-relay-updater -v /opt/agent-relay:/host-project -v /var/run/docker.sock:/var/run/docker.sock qingqiuzhuanfa:local sh -c "sleep 2; git config --global --add safe.directory /host-project; git -C /host-project pull --ff-only https://github.com/zhaozihao1/qingqiuzhuanfa.git main; docker rm -f agent-relay >/dev/null 2>&1 || true; docker compose -f /host-project/docker-compose.yml up -d --build"
```

安装脚本会将该默认配置写入 `.env`。请在宿主机创建此受控脚本，负责 `git pull --ff-only` 和 `docker compose up -d --build`，并限制为 root 可执行；不要把任意 shell 输入暴露给管理页面。

## 管理页面

打开 `http://服务器IP:8082`，默认登录：

- 用户名：`admin`
- 密码：`admin`

先在“上游配置”添加并启用 Base URL，再将 Agent 的 Base URL 指向：

```text
http://服务器IP:8081
```

如果 Agent SDK 自己拼接 `/v1/...`，请将上游 Base URL 配成对应的 `/v1` 前缀。

还需在“上游配置”中添加入口密钥并让 Agent 携带。保护开启后，无有效密钥的连接会被静默断开；其方法、路径、时间、客户端 IP、脱敏请求头和最多 1 MiB 请求体可在“拒绝统计”查看。

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

## 部署故障排查

### `unable to get image 'agent-relay-agent-relay'`

该名称是旧版 Compose 根据目录和服务名生成的本地镜像名，不是要从 Docker Hub 下载的镜像。新版配置已固定为 `qingqiuzhuanfa:local`，并在启动前执行本机构建。

先把本项目的最新修改推送到 GitHub，然后重新执行一键部署命令。脚本会更新 `~/agent-relay` 并重新构建：

```text
curl -fsSL https://raw.githubusercontent.com/zhaozihao1/qingqiuzhuanfa/main/install.sh | bash -s -- zhaozihao1/qingqiuzhuanfa
```

如果仍然失败，请根据完整错误处理：

- 出现 `permission denied while trying to connect to the Docker daemon socket`：

	```text
	sudo usermod -aG docker $USER
	```

	然后退出 SSH 并重新登录，再执行部署命令。

- 出现 `Cannot connect to the Docker daemon`：

	```text
	sudo systemctl enable --now docker
	```

- 出现 `docker: 'compose' is not a docker command`：安装发行版提供的 Docker Compose Plugin（软件包通常名为 `docker-compose-plugin`）。

安装脚本会检测以上情况；当前用户没有 Docker 权限但支持无密码 `sudo` 时，会自动使用 `sudo docker`。

## 注意事项

日志包含完整请求和响应正文，可能含有 Token、个人信息或业务敏感数据。请限制 `8082` 管理端口、防火墙只允许可信来源，并妥善备份/保护 Docker volume。

流式客户端必须按 SSE 的空行事件边界解析数据，不能假设一次网络读取恰好等于一个 JSON、一个 token 或一个 tool call 增量。部分模型或上游 API 本身不会返回可见思考内容，此时转发器无法生成上游未提供的字段。
