# QBR Insight Agent：AWS / 阿里云部署操作手册

> 适用工程：`QBR Insight Agent 0.1.0`  
> 手册版本：2026-08-08  
> 推荐形态：单台云主机 + 独立加密数据盘 + Nginx + systemd

## 1. 先读结论

当前工程应按**单节点应用**部署：AWS 使用 EC2 + 加密 EBS，阿里云使用 ECS + 加密 ESSD。不要把 API 或 worker 扩成多个副本，也不要把 SQLite 放在 EFS、NAS 等共享网络文件系统上。

原因是当前实现同时使用 SQLite、数据库任务队列和本地对象目录：

- API：FastAPI，监听 `127.0.0.1:8000`；
- worker：单进程消费 SQLite 中的解析任务；
- 前端：Vite 构建后的静态文件，由 Nginx 提供；
- 数据库：`/data/qbr/app.sqlite3`；
- 上传文件、解析产物和幻灯片预览：`/data/qbr/objects`；
- LibreOffice + Poppler：把 PPTX 渲染为预览图；
- 当前问答：FTS5 + 结构化图表查询的确定性回答，不调用外部大模型。

推荐拓扑：

```text
浏览器
  │ HTTPS :443
  ▼
ALB/SLB（推荐）或主机 Nginx
  ├── /              -> apps/web/dist
  ├── /api/*         -> FastAPI 127.0.0.1:8000
  └── /health/*      -> FastAPI 127.0.0.1:8000
                           │
                    SQLite + objects
                           │
                 加密 EBS / 加密 ESSD
```

## 2. 当前代码能力与部署前缺口

部署人员必须先理解下表，尤其不要误以为配置一个模型 Key 就会自动启用大模型。

| 项目 | 当前代码状态 | 本手册处理方式 |
|---|---|---|
| FastAPI / React / worker | 已实现 | 可按本手册部署 |
| SQLite 和本地文件存储 | 已实现 | 放在独立加密数据盘 |
| LibreOffice / Poppler | 已实现自动发现 | 安装系统包 |
| Dockerfile / Compose | 已实现并由 CI 构建 | 可选 Compose；本手册仍提供 systemd 方案 |
| `.env.example` | 已实现 | 生产使用本目录的 fail-closed 模板 |
| S3 / OSS 对象存储适配器 | 仅设计文档中规划，代码未实现 | 当前仍使用本地数据盘；S3/OSS 仅做备份目标 |
| OpenAI-compatible 模型适配器 | 已实现，配置不完整时安全回退 | API 与 worker 必须使用同一模型配置 |
| 生产认证 | 已实现 scrypt 密码登录、安全 Cookie、JWT + workspace RBAC，production 禁止 demo auth | 面试部署使用密码页；企业化再接 OIDC/JWKS |
| Python / Node 锁文件 | Python 依赖固定；前端已有 `package-lock.json` | 发布使用 `npm ci` |

因此有两条上线门槛：

1. **内部演示/合成数据**：可直接按本手册上线。
2. **真实 QBR/生产使用**：必须先完成认证、模型适配器（若启用模型）、备份恢复演练及安全评审。

## 3. 资源规划

### 3.1 建议起步规格

| 资源 | 演示/测试 | 小规模生产起步 |
|---|---:|---:|
| CPU | 2 vCPU | 4 vCPU |
| 内存 | 8 GiB | 16 GiB |
| 系统盘 | 30–40 GiB | 40 GiB |
| 独立数据盘 | 50 GiB | 100 GiB 起，按 PPT 留存量扩容 |
| 实例数 | 1 | 1 |
| API Uvicorn worker | 1 | 1 |
| 解析 worker | 1 | 1 |

LibreOffice 渲染会产生瞬时 CPU、内存和磁盘压力。若 PPT 较大，优先增加单机规格，不要直接增加实例数。

### 3.2 操作系统与软件

- Ubuntu Server 24.04 LTS（示例命令以此为准）；
- Python 3.11+；
- Node.js `20.19+` 或 `22.12+`；当前 Vite 的官方最低要求见 [Vite Getting Started](https://vite.dev/guide/)；
- Nginx、LibreOffice、Poppler、SQLite CLI、中文字体；
- AWS CLI v2 或阿里云 CLI，仅用于云资源检查和备份。

## 4. 云资源创建

### 4.1 AWS：EC2 + EBS

1. 选择同时满足数据驻留要求和目标 Bedrock 模型可用性的 Region。
2. 创建 VPC；生产建议 EC2 位于私有子网，由公网 ALB 接收 HTTPS。
3. 创建 Ubuntu 24.04 EC2。
4. 创建并挂载一块 **gp3、加密** EBS 数据盘；AWS 说明 EBS 加密同时覆盖卷、快照及实例与卷之间的数据，见 [Amazon EBS encryption](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-encryption.html)。
5. 安全组只允许：
   - ALB 方案：EC2 的 `80/443` 仅允许来自 ALB 安全组；
   - 单机直连方案：公网只开放 `80/443`；
   - 永远不要向公网开放 `8000`；
   - 推荐用 Session Manager 管理主机，不开放 `22`。Session Manager 可在没有入站端口和 SSH key 的情况下管理实例，见 [AWS Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)。
6. 创建并绑定 EC2 IAM Role / Instance Profile：
   - `AmazonSSMManagedInstanceCore`；
   - 启用 Bedrock 后，允许选定模型的 `bedrock:InvokeModel` 与 `bedrock:InvokeModelWithResponseStream`；
   - 使用第三方模型密钥时，只允许读取指定 Secrets Manager secret；
   - 若备份到 S3，只允许指定 bucket/prefix 的读写。
7. 配置域名与 TLS：优先 ACM + ALB；单机直连时使用受信任证书配置 Nginx。
8. 配置 EBS 自动快照，并额外执行应用级 SQLite 备份。

EC2 上的 SDK 可以通过 Instance Profile 自动获得轮换的临时凭据，不需要把 `AWS_ACCESS_KEY_ID` 或 `AWS_SECRET_ACCESS_KEY` 写入服务器，见 [IAM roles for EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html)。

### 4.2 阿里云：ECS + ESSD

1. 选择满足数据驻留要求，并与百炼业务空间/API Key 相匹配的地域。
2. 创建 VPC；生产建议 ECS 位于私网，由 ALB/CLB 接收 HTTPS。
3. 创建 Ubuntu 24.04 ECS。
4. 创建并挂载一块**加密 ESSD** 数据盘。ESSD 加密可使用服务密钥或指定 KMS 密钥，限制见 [ECS 数据加密](https://help.aliyun.com/en/ecs/user-guide/encrypt-data-stored-on-ecs-resources)。
5. 安全组只开放 `80/443`；不要向公网开放 `8000`。如公司已有堡垒机/云助手，`22` 也不开放公网。
6. 创建 ECS 实例 RAM 角色并绑定到实例：
   - 读取指定 KMS secret；
   - 若备份到 OSS，只允许指定 bucket/prefix；
   - 不在 ECS 上保存 RAM 用户长期 AccessKey。实例 RAM 角色会通过元数据服务提供 STS 临时凭据，见 [ECS 实例 RAM 角色](https://help.aliyun.com/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance)。
7. 配置域名、证书及 ALB/CLB 健康检查。
8. 给 ESSD 绑定自动快照策略。ECS 云盘不会自动备份，需显式配置策略，见 [阿里云快照概述](https://help.aliyun.com/zh/ecs/user-guide/snapshot-overview)。

## 5. 初始化主机

以下命令在云主机执行。先通过 `lsblk -f` 确认新数据盘设备名；**只有确认是全新空盘后才允许格式化**。

```bash
lsblk -f

# 示例占位符，必须替换为实际的新数据盘设备；不要复制未确认的设备名。
sudo mkfs.ext4 /dev/DEVICE
sudo mkdir -p /data
sudo mount /dev/DEVICE /data
sudo blkid /dev/DEVICE
```

把 `blkid` 输出的 UUID 写入 `/etc/fstab`（将占位符替换为真实 UUID），然后验证：

```fstab
UUID=<DATA_DISK_UUID> /data ext4 defaults,nofail 0 2
```

```bash
sudo mount -a
findmnt /data
df -h /data
```

安装运行依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  libreoffice poppler-utils sqlite3 nginx \
  fonts-noto-cjk fonts-liberation unzip

python3 --version
libreoffice --version
pdftoppm -v
nginx -v
```

用组织批准的软件源安装 Node.js，然后验证版本满足 `20.19+` 或 `22.12+`：

```bash
node --version
npm --version
```

创建专用用户和目录：

```bash
sudo useradd --system --home /opt/qbr --shell /usr/sbin/nologin qbr
sudo mkdir -p /opt/qbr/app /opt/qbr/.config /data/qbr /etc/qbr
sudo chown -R qbr:qbr /opt/qbr /data/qbr
sudo chown root:qbr /etc/qbr
sudo chmod 755 /opt/qbr /opt/qbr/app
sudo chmod 750 /data/qbr /etc/qbr
```

## 6. 发布应用

将经过测试的源码制品上传并解压到 `/opt/qbr/app`。不要把本地 `.env`、`data/`、`.venv/`、真实 PPT 或测试数据库打进发布包。

在工程根目录执行：

```bash
cd /opt/qbr/app

sudo -u qbr python3 -m venv .venv
sudo -u qbr .venv/bin/pip install --upgrade pip
sudo -u qbr .venv/bin/pip install -e '.[dev]'

cd /opt/qbr/app/apps/web
sudo -u qbr npm ci
sudo -u qbr npm run build

cd /opt/qbr/app
sudo -u qbr .venv/bin/pytest
sudo -u qbr .venv/bin/ruff check apps packages tests
sudo -u qbr .venv/bin/python skills/extract-ppt-chart-data/scripts/self_test.py
```

构建、测试和镜像创建也会在 GitHub Actions 中执行；云主机部署前仍建议保留一次 smoke test。

## 7. 安装配置与服务

复制本目录模板：

```bash
sudo install -o root -g qbr -m 0640 \
  cloud-deployment-runbook/templates/runtime.env.example \
  /etc/qbr/runtime.env

sudo install -o root -g root -m 0644 \
  cloud-deployment-runbook/templates/qbr-api.service \
  /etc/systemd/system/qbr-api.service

sudo install -o root -g root -m 0644 \
  cloud-deployment-runbook/templates/qbr-worker.service \
  /etc/systemd/system/qbr-worker.service

sudo install -o root -g root -m 0644 \
  cloud-deployment-runbook/templates/nginx-qbr.conf \
  /etc/nginx/sites-available/qbr

sudo ln -s /etc/nginx/sites-available/qbr /etc/nginx/sites-enabled/qbr
```

编辑 `/etc/qbr/runtime.env`，把 `CORS_ORIGINS` 改成正式域名。若 Nginx 与 API 同域，仍应填写精确的 `https://your-domain.example`，不要使用 `*`。

编辑 `/etc/nginx/sites-available/qbr`，替换 `server_name`。若 TLS 在 ALB/SLB 终止，云主机 Nginx 可只监听内网 HTTP；若 Nginx 直接面向公网，必须增加完整的 `listen 443 ssl` 与证书配置。

启动：

```bash
sudo systemctl daemon-reload
sudo nginx -t
sudo systemctl enable --now qbr-api qbr-worker nginx

sudo systemctl status qbr-api --no-pager
sudo systemctl status qbr-worker --no-pager
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

不要同时设置 `RUN_INLINE_WORKER=true` 并启动 `qbr-worker.service`。模板使用独立 worker，因此固定为 `false`。

## 8. 大模型与密钥配置

### 8.1 当前版本：模型可选

未配置模型时，回答由 SQLite FTS5 BM25、结构化图表查询和确定性计算产生。配置 OpenAI-compatible provider 后，worker 会在证据和数字白名单校验内生成最终表述；provider 失败会回退到确定性回答。API 与 worker 必须同时设置：

```text
LLM_ENABLED=true
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=<从 Secrets Manager 注入>
LLM_MODEL=<固定并评估的模型名>
CORS_ORIGINS
```

不要把 Key 写入源码、前端变量、镜像或 GitHub 仓库。

### 8.2 后续企业化模型工作

建议增加统一的 `ModelSettings` 和 provider adapter，至少包含：

```text
LLM_PROVIDER=bedrock|dashscope
LLM_MODEL=<经过评估并固定的模型 ID>
VISION_PROVIDER=bedrock|dashscope
VISION_MODEL=<支持图像输入的模型 ID>
EMBEDDING_PROVIDER=<provider>
EMBEDDING_MODEL=<model ID>
MODEL_TIMEOUT_SECONDS=60
MODEL_CONCURRENCY=2
```

并补齐以下行为：

1. 启动时验证 provider、region、endpoint 与模型能力，但不打印密钥；
2. 所有模型调用设置超时、重试、限流和每日预算；
3. 只发送必要的 top-k 文本或页面裁剪，不默认上传整份 PPT；
4. 文档内容按“不可信数据”处理，不能让 PPT 文本改变系统指令或工具权限；
5. 记录 model ID、token、延迟和 request ID，不记录完整提示词、答案、图片或 Key；
6. 模型不可用时回退到当前确定性问答；
7. 用脱敏黄金集验证引用、数值准确率和跨租户隔离后才能上线。

### 8.3 AWS Bedrock：生产不配置长期 Key

推荐让 EC2 Instance Profile 直接获得 Bedrock 权限。应用使用 AWS SDK 默认凭据链：

```text
LLM_PROVIDER=bedrock
AWS_REGION=<部署 Region>
LLM_MODEL=<Bedrock model ID 或 inference profile ID>
```

**不要设置：**

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN
```

Bedrock `Converse` 调用需要 `bedrock:InvokeModel` 权限，见 [AWS CLI Converse 参考](https://docs.aws.amazon.com/cli/latest/reference/bedrock-runtime/converse.html)。若模型调用需要流式响应，再增加 `bedrock:InvokeModelWithResponseStream`。IAM Policy 应在确定模型 ID 后将 `Resource` 收紧到对应 foundation model、inference profile 或已部署 endpoint。

AWS 现在也提供 Bedrock 短期和长期 API Key，但官方建议生产使用短期凭据，长期 Key 仅用于探索，见 [Bedrock API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-reference.html)。对本工程的 EC2 部署，Instance Profile 更简单，也没有 Key 轮换负担。

如果未来调用的是 OpenAI、Anthropic 等第三方直连 API，而不是 Bedrock：

1. 在 AWS Secrets Manager 创建一个只含该 provider Key 的 secret；
2. EC2 Role 仅授予该 secret 的 `secretsmanager:GetSecretValue`；
3. 应用启动时从 SDK 读取并只保存在进程内存；
4. 不通过 `user-data`、AMI、源码、前端变量或日志传递；
5. 轮换时先创建新版本，验证后重启服务，再撤销旧 Key。

AWS 对单个 secret 的最小读取权限示例见 [Secrets Manager identity policies](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access_iam-policies.html)。

### 8.4 阿里云百炼：Key 放 KMS Secrets Manager

百炼当前使用 API Key。创建步骤：

1. 在百炼/Model Studio 中创建独立的 production workspace；
2. 只授权本工程需要的模型，并设置限流与费用告警；
3. 在 Key Management 页面创建该 workspace 的 API Key；
4. 立即把 Key 写入 KMS Secrets Manager 的 Generic Secret，例如 `qbr/prod/dashscope-api-key`；
5. ECS 绑定实例 RAM 角色，只允许读取这个 secret；
6. adapter 启动时通过 KMS Secrets Manager Client + `ecs_ram_role` 读取 secret，将值放在进程内存；
7. 不把明文持久化到 `/etc/qbr/runtime.env`。阿里云 KMS Client 支持使用 ECS RAM Role 获取临时凭证，见 [Secrets Manager Client](https://help.aliyun.com/en/kms/key-management-service/developer-reference/secrets-manager-client)。

目标配置示例（需 adapter 实现后才生效）：

```text
LLM_PROVIDER=dashscope
LLM_MODEL=<已评估的模型 ID>
DASHSCOPE_BASE_URL=https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
DASHSCOPE_SECRET_NAME=qbr/prod/dashscope-api-key
```

生产推荐 workspace 专属域名。百炼的 API Key 与 Base URL 必须属于同一地域/计费计划，否则会返回 401；各地域地址见 [Model Studio Base URL overview](https://help.aliyun.com/en/model-studio/base-url)。

官方 SDK 常用环境变量名是 `DASHSCOPE_API_KEY`，但本工程更推荐 adapter 直接从 KMS 读取到内存。仅在可信开发机临时验证时，可以用隐藏输入读取 Key，避免把明文写入命令历史：

```bash
read -rsp 'DashScope API Key: ' DASHSCOPE_API_KEY
export DASHSCOPE_API_KEY
```

不要把这条命令写进 shell history、脚本、systemd unit、Dockerfile 或 Git。Key 创建与环境变量说明见 [Model Studio API Key](https://help.aliyun.com/en/model-studio/get-api-key)。

### 8.5 密钥红线

- 前端只能获得公开 API Base URL，绝不能获得模型 Key；
- 禁止使用 `VITE_*` 保存任何 secret，所有 `VITE_*` 都可能进入浏览器构建产物；
- 禁止把 secret 写入 Git、PPT、数据库、日志、错误响应、监控 tag 和备份脚本参数；
- 禁止直接使用 AWS root、阿里云主账号或 RAM 用户长期 AccessKey；
- staging 与 production 使用不同账号/项目、workspace、secret、bucket 和模型预算；
- 泄漏时先禁用/轮换 Key，再排查 Git 历史、日志、构建制品和终端历史。

## 9. 备份与恢复

仅有云盘快照还不够。SQLite 运行中可能有 WAL，需做应用级一致备份。

### 9.1 每日备份

1. 用 SQLite `.backup` 创建一致副本：

```bash
sudo -u qbr mkdir -p /data/qbr/backups
backup_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="/data/qbr/backups/app-${backup_stamp}.sqlite3"
sudo -u qbr sqlite3 /data/qbr/app.sqlite3 \
  ".backup '${backup_path}'"
```

2. 对备份副本执行校验：

```bash
sqlite3 "${backup_path}" 'PRAGMA integrity_check;'
```

3. 将数据库副本与 `/data/qbr/objects` 清单上传到私有 S3/OSS；bucket 开启服务端加密、版本控制和生命周期。
4. EBS/ESSD 自动快照作为第二层保护。
5. 至少每季度在隔离环境执行一次完整恢复演练。

定时任务还应验证 `/data/qbr/backups` 位于已挂载的数据盘、限制保留数量，并在上传成功后记录对象版本号和校验和。

### 9.2 恢复

```bash
sudo systemctl stop qbr-worker qbr-api

# 保留故障副本，不要直接覆盖后删除。
sudo cp -a /data/qbr/app.sqlite3 /data/qbr/app.sqlite3.failed
sudo cp -a /data/qbr/backups/<chosen-backup>.sqlite3 /data/qbr/app.sqlite3
sudo chown qbr:qbr /data/qbr/app.sqlite3

sqlite3 /data/qbr/app.sqlite3 'PRAGMA integrity_check;'
sudo systemctl start qbr-api qbr-worker
curl --fail http://127.0.0.1:8000/health/ready
```

数据库与 `objects` 必须来自相容的备份点。恢复后抽查文档列表、幻灯片预览、结构化图表和问答引用。

## 10. 发布、回滚与日常运维

### 10.1 发布

1. 在 staging 跑测试及合成 PPT 上传/问答。
2. 备份 SQLite 和 objects manifest。
3. 将新版本解压到新的 release 目录，而不是覆盖正在运行的目录。
4. 安装依赖并构建前端。
5. 停止 worker，再停止 API。
6. 原子切换 `/opt/qbr/app` 指向新 release。
7. 启动 API 和 worker，检查 health，再放流量。
8. 上传一份合成 PPT 做端到端 smoke test。

当前没有数据库 migration 工具；升级前必须检查 `packages/qbr_core/db.py` 的 schema 变化是否向后兼容。

### 10.2 回滚

1. 停止 worker 与 API；
2. 切回上一版 release；
3. 若 schema 已变化，恢复发布前应用一致备份；
4. 启动服务并执行 smoke test；
5. 保留失败版本日志、数据库副本和 request ID 供分析。

### 10.3 常用检查

```bash
systemctl is-active qbr-api qbr-worker nginx
journalctl -u qbr-api -u qbr-worker --since '30 minutes ago'
curl --fail http://127.0.0.1:8000/health/ready
df -h /data
du -sh /data/qbr/objects /data/qbr/backups
sqlite3 /data/qbr/app.sqlite3 'PRAGMA quick_check;'
```

问答和模型调用默认由 `qbr-worker` 执行，因此排查 Analytics 中的降级提示时先看 worker。新版本会输出
`provider_call_failed` 和 `answer_run_completed_with_signals` 两类 JSON 事件，并携带 `run_id`、组件、模型、
HTTP 状态码（若 SDK 提供）和耗时；不会记录问题正文、证据原文或 API Key。

systemd 部署可直接执行：

```bash
sudo journalctl -u qbr-worker --since '30 minutes ago' -o cat
sudo journalctl -u qbr-worker -f -o cat
sudo journalctl -u qbr-worker --since today -o cat | grep -E 'provider_call_failed|QUERY_PLANNER_PROVIDER_ERROR|LLM_PROVIDER_ERROR'
```

拿到 Analytics/API 返回的 `run_id` 后，可精确过滤一次问答：

```bash
sudo journalctl -u qbr-worker --since today -o cat | grep 'run_abc123'
```

若实际使用仓库根目录的 Docker Compose，则服务名为 `api`、`worker`、`web`：

```bash
docker compose ps
docker compose logs --since 30m --tail 500 worker api
docker compose logs -f worker api
docker compose logs --since 24h worker | grep -E 'provider_call_failed|QUERY_PLANNER_PROVIDER_ERROR|LLM_PROVIDER_ERROR'
```

先检查生效配置，不要输出 Key 本身：

```bash
curl --fail http://127.0.0.1:8000/health/ready | jq '.llm'
sudo grep -E '^(LLM_ENABLED|LLM_PROVIDER|LLM_BASE_URL|LLM_MODEL|PLANNER_MODEL|LLM_TIMEOUT_SECONDS)=' /etc/qbr/runtime.env
sudo sh -c 'grep -q "^LLM_API_KEY=." /etc/qbr/runtime.env && echo "LLM_API_KEY is set" || echo "LLM_API_KEY is missing"'
```

Compose 部署可用下面的等价检查：

```bash
docker compose exec worker sh -lc 'env | grep -E "^(LLM_ENABLED|LLM_PROVIDER|LLM_BASE_URL|LLM_MODEL|PLANNER_MODEL|LLM_TIMEOUT_SECONDS)="'
docker compose exec worker sh -lc 'test -n "$LLM_API_KEY" && echo "LLM_API_KEY is set" || echo "LLM_API_KEY is missing"'
```

若 EC2 已安装 CloudWatch Agent，先发现实际日志组，再跟踪日志（不要假设日志组名称）：

```bash
aws logs describe-log-groups --query 'logGroups[].logGroupName' --output table
aws logs tail '/your/qbr/log-group' --since 30m --follow
```

`401/403` 通常检查 Key、模型权限和 endpoint；`404` 检查 `LLM_BASE_URL`/模型名；`429` 检查配额与限流；
超时或连接错误检查 EC2 出网、DNS、安全组/NACL 和 `LLM_TIMEOUT_SECONDS`。修改 `/etc/qbr/runtime.env` 后需执行
`sudo systemctl restart qbr-api qbr-worker`；修改 Compose `.env` 后执行 `docker compose up -d --force-recreate api worker`。

日志里若出现 PPT 原文、完整问题/回答、签名 URL、token 或 Key，应视为安全事件处理。

## 11. 监控和告警

最低告警集合：

- `/health/ready` 连续失败；
- API 5xx、P95 延迟或 SSE 中断率升高；
- `qbr-worker` 停止或任务长期 pending/running；
- `/data` 使用率超过 70% 预警、80% 严重告警；
- 最近成功 SQLite 备份超过 24 小时；
- `PRAGMA quick_check` 非 `ok`；
- LibreOffice/Poppler 渲染失败率升高；
- 模型 401/403/429/5xx、超时、token 与费用超过预算；
- KMS/Secrets Manager 读取失败或 secret 即将轮换。

AWS 使用 CloudWatch + CloudTrail；阿里云使用云监控 + SLS + ActionTrail。应用日志默认只记 ID、状态、耗时和错误码。

## 12. 上线验收清单

### 基础部署

- [ ] 只有一台实例、一个 Uvicorn worker、一个解析 worker；
- [ ] SQLite 和 objects 位于独立加密数据盘；
- [ ] 公网不可访问 `22` 和 `8000`；
- [ ] HTTPS 证书有效，HTTP 自动跳转 HTTPS；
- [ ] `CORS_ORIGINS` 是精确正式域名；
- [ ] `/health/live`、`/health/ready` 正常；
- [ ] 合成 PPT 上传、解析、预览和问答通过；
- [ ] 重启实例后数据盘、API、worker、Nginx 自动恢复；
- [ ] SQLite 备份、云盘快照和恢复演练通过。

### 大模型

- [ ] 已完成 provider adapter，而不是只添加环境变量；
- [ ] AWS 使用 Instance Profile；或阿里云使用 ECS RAM Role + KMS secret；
- [ ] 前端、Git、日志、systemd unit 和镜像中不存在 Key；
- [ ] provider、模型、region、数据保留和训练条款经数据所有者批准；
- [ ] prompt injection、引用正确性、数值正确性和预算限制通过测试；
- [ ] 模型不可用时可降级到确定性问答。

### 真实数据额外门槛

- [ ] 已替换演示身份头，接入 OIDC/SSO 和真实 RBAC；
- [ ] 完成跨 workspace IDOR 测试；
- [ ] 明确数据分类、地域、保留期、删除和审计策略；
- [ ] 原文件、预览、备份和模型输入均符合公司数据政策。

## 13. 故障速查

| 现象 | 优先检查 |
|---|---|
| `502 Bad Gateway` | `systemctl status qbr-api`、端口 8000、Nginx error log |
| 前端刷新后 404 | Nginx 是否有 `try_files ... /index.html` |
| 上传返回 413 | `/api/` 的 Nginx 是否设置 `client_max_body_size 11m;`（10 MiB 文件还包含 multipart 开销）；修改容器配置后是否重建了 `web` 镜像 |
| PPT 只有结构化 SVG | `libreoffice --version`、`pdftoppm -v`、字体和 worker 日志 |
| worker 不消费任务 | `RUN_INLINE_WORKER=false`、`qbr-worker` 状态、数据库目录权限 |
| SQLite busy | 是否误开多实例/多 worker、是否有长事务、数据盘延迟和空间 |
| 重启后数据库丢失 | `/data` 是否自动挂载、`runtime.env` 路径是否仍指向 `/data/qbr` |
| Bedrock 403 | EC2 Role、Region、模型 ID、`bedrock:InvokeModel` Resource |
| 百炼 401 | Key 与 Base URL 是否同地域/同计费计划、secret 是否轮换 |
| 配了 Key 仍无模型回答 | 当前代码尚未实现 provider adapter，见第 2、8 节 |
