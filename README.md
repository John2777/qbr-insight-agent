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

### 阿里云百炼（新加坡）推荐组合

同一个 workspace host 下，Chat/Embedding/Vision 使用 `compatible-mode/v1`，Rerank 使用独立的 `compatible-api/v1` 地址：

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=qwen-bailian-singapore
LLM_BASE_URL=https://YOUR-WORKSPACE.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=server-side-secret
LLM_MODEL=qwen3.7-plus
PLANNER_MODEL=qwen3.6-flash
DEEP_LLM_MODEL=qwen3.7-max
LLM_MAX_TOKENS=2000
CONVERSATION_CONTEXT_MAX_TURNS=4
CONVERSATION_CONTEXT_TOKEN_BUDGET=2400
CONVERSATION_SUMMARY_ENABLED=true
CONVERSATION_SUMMARY_TOKEN_BUDGET=800

RETRIEVAL_STRATEGY=hybrid
EMBEDDING_BASE_URL=https://YOUR-WORKSPACE.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
EMBEDDING_BATCH_SIZE=10

RERANK_ENABLED=true
RERANK_BASE_URL=https://YOUR-WORKSPACE.ap-southeast-1.maas.aliyuncs.com/compatible-api/v1
RERANK_MODEL=qwen3-rerank

VISION_ENABLED=true
VISION_BASE_URL=https://YOUR-WORKSPACE.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
VISION_MODEL=qwen3.7-plus
VISION_MAX_SLIDES=50
VISION_ENRICH_ALL_SLIDES=false
```

未单独设置 `EMBEDDING_API_KEY`、`RERANK_API_KEY` 或 `VISION_API_KEY` 时会复用服务端 `LLM_API_KEY`。默认只分析含图片元素的页面，并受 `VISION_MAX_SLIDES` 成本上限保护；显式设置 `VISION_ENRICH_ALL_SLIDES=true` 才分析整份演示文稿。视觉模型生成 `slide_visual_summary`、`visual_ocr`、`visual_observation` 三类可审计 chunk；它们标记为补充视觉证据，不能覆盖原生表格/图表数值。Embedding 身份包含 endpoint、模型与配置维度，任一配置变化都会安全重建旧向量。

语义规划器要求输出紧凑 JSON，因此应用会显式关闭该角色的 thinking；答案角色同样关闭 thinking，把 token 预算留给可见、可校验的正文。四份测试 PPT 的复杂题验证表明，千问答案上限使用 `LLM_MAX_TOKENS=2000` 可避免 1200-token 配置下的长答案截断。

每个回答 run 在入队事务中绑定本轮用户消息，并冻结当时的结构化摘要与已完成的最近对话轮次。默认保留最近 4 个完整问答轮次，摘要最多约 800 token，摘要与近期原文合计最多约 2400 token。更早的完整轮次会增量压缩为目标、实体、时间范围、术语、已解析指代、待解决问题和用户偏好；摘要只用于指代消解和对话连贯，不能作为业务证据，回答仍必须重新检索文档。摘要版本、截止序号、预算、截断状态和 context hash 会写入冻结快照及回答 metadata，便于重试复现和审计；摘要模型失败时保留确定性摘要，不影响已完成回答。

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
.venv/bin/python scripts/evaluate_qbr_benchmark.py --retrieval-strategy hybrid --rerank
.venv/bin/python scripts/evaluate_qbr_benchmark.py \
  --cases benchmarks/qbr_terms/cases.json \
  --output-json benchmark_results/qbr_terms_latest.json \
  --output-md benchmark_results/qbr_terms_latest.md \
  --min-score 95
```

默认使用 QBR-50 数据集；分别运行 `fts`、`vector`、`hybrid` 即可进行检索消融。原 QBR-30 数据集保留用于历史回归对比。`qbr-terms` 提供 50 个管理、财务、寿险、客户与销售术语的独立解释回归，报告会同时检查回答模式、无关内容和引用节制。

浏览器测试使用 `cd apps/web && npm run test:e2e`。Playwright 会在隔离的临时数据目录中启动真实 FastAPI 和内联 worker；
真实链路覆盖合成 PPTX 上传、动态 ParserSkill 加载、解析完成、SSE 问答与引用展示，其余 UI 状态继续使用路由 mock 做快速验证。
完整设计、质量目标与已知边界见 [docs/README.md](docs/README.md)。

## 重要边界

- 当前 SQLite 设计是单节点、单写者 MVP，不支持透明横向扩容。
- FAISS 只负责语义候选召回；图表系列、左右轴、类别、数值与计算仍通过 SQLite 结构化查询，不以向量相似度代替事实关联。
- 当前“流式”是在完整生成并通过数值/引用校验后再通过 SSE 增量传送，优先保证证据安全；不是未经校验的 provider token 直通。
- 未启用视觉模型时，图片化内容只进入视觉复核候选；启用后会生成带模型、提示版本和置信度的补充检索块，但不会把像素估读伪装成精确数值。
- 默认仅接收无宏 `.pptx`，不访问外部工作簿或远程模板。
- 未配置或 provider 不可用时会回退到本地确定性回答，并在 run 中记录模型状态与 warning。
