# QBR Insight Agent

QBR Insight Agent 是一个证据优先的季度业务复盘应用。它安全导入 `.pptx`，优先从内嵌工作簿或图表缓存提取精确数值，将页面、元素、图表点和检索块写入 SQLite，并让每条回答回链到原幻灯片与元素边界框。

当前版本实现了适合 3 天面试作业交付的完整垂直切片：

- FastAPI REST/SSE API、SQLite 数据库队列与单写 worker；
- OOXML 安全预检、原生文本/表格/备注解析和现有复杂图表 Skill；
- LibreOffice + Poppler 渲染，失败时使用显式降级的结构 SVG；
- FTS5 BM25 + 可选 FAISS 语义召回 + RRF 混合排序、结构化图表查询、可重放增长率计算、证据不足拒答；
- LangChain OpenAI-compatible 模型适配器和 LangGraph 受控生成/引用校验；
- 持久化异步问答队列、SSE 增量事件和 worker lease heartbeat/自动重试；
- React 文档库、幻灯片/图表查看、流式问答与 bbox 证据高亮；
- 可领取/修正/留 revision/局部重建索引的图表复核闭环；
- JWT HS256 + workspace RBAC、审计事件、结构化请求日志和运行分析页；
- 单元、API 集成、真实 PPTX 端到端链路和 Playwright UI 测试。

## 本地运行

要求：Python 3.11+、Node.js 20+；推荐安装 LibreOffice 与 Poppler 以获得真实幻灯片渲染。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd apps/web && npm install && cd ../..
cp .env.example .env
docker compose up --build
```

不使用 Docker 时：

```bash
RUN_INLINE_WORKER=true .venv/bin/uvicorn apps.api.main:app --reload --port 8000
cd apps/web && npm run dev
```

打开 [http://localhost:3000](http://localhost:3000)。本地演示默认使用 `ws_demo/user_demo`，问答使用确定性证据模式，无需模型密钥。

## 公网 Demo 登录

本地默认 `AUTH_MODE=demo`，方便开发。AWS/公网部署推荐使用内置密码登录：

```dotenv
APP_ENV=production
AUTH_MODE=password
JWT_SECRET=replace-with-at-least-32-random-bytes
PASSWORD_USERNAME=interviewer
PASSWORD_HASH=replace-with-generated-scrypt-hash
```

生成密码哈希时密码不会显示在终端，也不会写入 shell history：

```bash
.venv/bin/python scripts/create_password_hash.py
```

登录采用标准库 `scrypt` 哈希、常量时间比较、失败限流以及短期签名会话。会话只保存在 `HttpOnly + SameSite=Strict` Cookie 中；生产环境自动增加 `Secure`，因此必须通过 HTTPS 访问。`APP_ENV=production` 与 `AUTH_MODE=demo` 同时出现时服务会拒绝启动。

`AUTH_MODE=jwt` 仍保留给自动化/API 调用；浏览器公网演示不需要把 token 放进 URL、源码或 localStorage。

## LLM 配置

服务端支持 OpenAI-compatible Chat Completions。复制 `.env.example` 后配置以下变量：

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=your-provider
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=replace-with-a-server-side-secret
LLM_MODEL=your-model
```

API Key 只允许保存在服务端环境或密钥管理服务中，禁止使用 `VITE_*` 前缀。启用外部 provider 后，问题、最近会话片段和召回到的文档证据会发送给该 provider；关键数值仍由本地工具计算，生成结果必须通过引用编号和数字白名单校验，否则回退到确定性回答并显式记录 warning。

## 混合检索配置

默认保持 `RETRIEVAL_STRATEGY=fts`，无需额外依赖。启用语义或混合检索时安装可选 FAISS 依赖，并配置独立的 Embedding 模型：

```bash
.venv/bin/pip install -e '.[vector]'
```

```dotenv
RETRIEVAL_STRATEGY=hybrid
EMBEDDING_PROVIDER=openai-compatible
EMBEDDING_BASE_URL=https://provider.example/v1
EMBEDDING_API_KEY=replace-with-a-server-side-secret
EMBEDDING_MODEL=your-embedding-model
```

SQLite 的 `chunk_embeddings` 是 embedding 权威数据，FAISS 文件只是按 workspace 原子重建的缓存。查询同时执行 FTS 与向量召回，以 RRF 融合；向量依赖、模型或索引失败时自动降级到 FTS。`EMBEDDING_PROVIDER=hashing` 只用于离线测试检索链路，不是生产语义模型。

## 验证

```bash
python3 skills/extract-ppt-chart-data/scripts/self_test.py
.venv/bin/pytest
.venv/bin/ruff check apps packages tests
cd apps/web && npm run lint && npm test && npm run build
```

已有数据时可执行版本化小型检索黄金集：

```bash
.venv/bin/python scripts/evaluate_retrieval.py
.venv/bin/python scripts/evaluate_qbr_benchmark.py --retrieval-strategy fts
.venv/bin/python scripts/evaluate_qbr_benchmark.py --retrieval-strategy hybrid
```

默认使用 QBR-50 数据集；分别运行 `fts`、`vector`、`hybrid` 即可进行检索消融。原 QBR-30 数据集保留用于历史回归对比，报告会记录实际策略和向量降级诊断。

浏览器测试使用 `cd apps/web && npm run test:e2e`。Playwright 会在隔离的临时数据目录中启动真实 FastAPI 和内联 worker；
真实链路覆盖合成 PPTX 上传、动态 ParserSkill 加载、解析完成、SSE 问答与引用展示，其余 UI 状态继续使用路由 mock 做快速验证。
完整设计、质量目标与已知边界见 [docs/README.md](docs/README.md)。

## 重要边界

- 当前 SQLite 设计是单节点、单写者 MVP，不支持透明横向扩容。
- FAISS 只负责语义候选召回；图表系列、左右轴、类别、数值与计算仍通过 SQLite 结构化查询，不以向量相似度代替事实关联。
- 当前“流式”是在完整生成并通过数值/引用校验后再通过 SSE 增量传送，优先保证证据安全；不是未经校验的 provider token 直通。
- 图片化图表目前只进入视觉复核候选；不会把像素估读伪装成精确数值。
- 默认仅接收无宏 `.pptx`，不访问外部工作簿或远程模板。
- 未配置或 provider 不可用时会回退到本地确定性回答，并在 run 中记录模型状态与 warning。
