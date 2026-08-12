# 02. System Architecture

## 1. Architectural conclusion

The system is a modular, single-node, asynchronous architecture. React owns interaction; FastAPI owns resources and authorization; workers execute ingestion and answer runs; SQLite/FTS5 stores business truth and lexical indexes; FAISS provides a rebuildable semantic index; model providers are adapters.

Single-node deployment is a deliberate fit for the present scale: consistency, operating cost, and recovery remain simple. Application boundaries, domain identifiers, object storage, and retrieval contracts are already separated so infrastructure can evolve without rewriting the product core.

## 2. System context

```mermaid
flowchart TB
    User["Analyst / executive / reviewer"]

    subgraph Access["Access"]
        Web["React Web<br/>documents · chat · review · analytics"]
        API["FastAPI<br/>REST · SSE · RBAC"]
    end

    subgraph Application["Application"]
        Ingest["Ingestion Service"]
        Answer["Answer Run Executor"]
        Resource["Resource / Review Services"]
    end

    subgraph Intelligence["Knowledge and intelligence"]
        Parser["PPT Parser Skill"]
        Retrieval["Hybrid Retrieval"]
        Reasoning["Chart · Table · Reconciliation"]
        Guard["Coverage · Verification"]
    end

    subgraph Data["Data"]
        DB[("SQLite + FTS5<br/>domain · queues · audit")]
        Vector[("FAISS<br/>semantic index")]
        Object[("Originals · Renders · Canonical JSON")]
    end

    subgraph Provider["Replaceable providers"]
        Models["Chat · Embedding · Rerank · Vision"]
    end

    User --> Web --> API
    API --> Ingest
    API --> Answer
    API --> Resource
    Ingest --> Parser --> DB
    Parser --> Object
    Answer --> Retrieval --> DB
    Retrieval --> Vector
    Answer --> Reasoning --> Guard
    Parser -.on demand.-> Models
    Retrieval -.on demand.-> Models
    Answer -.on demand.-> Models

    classDef core fill:#16324F,color:#fff,stroke:#16324F;
    classDef data fill:#EAF4EE,color:#173B24,stroke:#5A8F6B;
    classDef edge fill:#F5F7FA,color:#243447,stroke:#8292A2;
    class Ingest,Answer,Resource,Parser,Retrieval,Reasoning,Guard core;
    class DB,Vector,Object data;
    class Web,API,Models edge;
```

## 3. Two critical paths

### 3.1 Ingestion

```mermaid
sequenceDiagram
    actor U as User
    participant W as React
    participant A as FastAPI
    participant D as SQLite
    participant K as Worker
    participant S as Parser Skill

    U->>W: Upload PPTX
    W->>A: POST /api/v1/documents
    A->>A: Streaming limit + OOXML inspection
    A->>D: DocumentVersion + IngestionJob
    A-->>W: 202 / job_id
    K->>D: Claim by lease
    K->>S: Native parse, render, selective vision
    S-->>K: Canonical SlideDocument
    K->>D: Atomically persist elements, charts, chunks, index state
    K-->>W: SSE progress / ready
```

The request path remains short. Durable jobs, leases, heartbeats, and attempts carry long-running work, so worker restart and browser refresh do not erase state.

### 3.2 Evidence-grounded QA

```mermaid
sequenceDiagram
    actor U as User
    participant W as React
    participant A as FastAPI
    participant D as SQLite
    participant K as Answer Worker
    participant M as Model Provider

    U->>W: Submit question
    W->>A: POST /conversations/{id}/messages
    A->>D: Message + ContextSnapshot + Run
    A-->>W: 202 / run_id
    W->>A: GET /runs/{id}/events
    K->>D: Claim run and read frozen context
    K->>M: Produce retrieval expansion
    K->>D: FTS / chart / table retrieval
    K->>K: Evidence coverage and deterministic calculation
    K->>M: Constrained generation and polish
    K->>K: Number, citation, and scope verification
    K->>D: Answer + Citations + Events
    A-->>W: SSE completed
```

## 4. Dependency direction

```mermaid
flowchart LR
    Routes["API Routes"] --> App["Application Services"]
    Worker["Worker Entry"] --> App
    App --> Planning
    App --> Retrieval
    App --> Analysis
    App --> Documents
    App --> Conversations
    App --> Foundation
    Planning --> Foundation
    Retrieval --> Foundation
    Analysis --> Foundation
    Documents --> Foundation
    Conversations --> Foundation
    App --> Providers
```

Routes do not contain SQL, providers do not own business policy, and analysis is independent of the web layer. `QBRService` is a composition root and compatibility facade; focused services execute the use cases.

## 5. Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Web | React 19, TypeScript, Vite | Typed SPA suited to streaming and evidence preview |
| API | FastAPI | OpenAPI, dependency injection, SSE, streaming uploads |
| Background work | Durable SQLite queue + worker | Recoverable state with minimal operating surface |
| Domain storage | SQLite WAL | Short transactions, simple backup, portable demonstration |
| Lexical search | FTS5 | Reliable for abbreviations, periods, values, and terms |
| Semantic search | FAISS | Rebuildable index separated from domain truth |
| Agent | LangGraph plus explicit orchestration | Controlled model nodes and visible deterministic boundaries |
| Parsing | python-pptx, OOXML, workbook | Maximum use of exact source-file information |

## 6. Runtime topology

The delivery topology is Nginx, static web assets, FastAPI, a worker, and encrypted persistent storage. API and worker use the same image with different commands. Cloud mappings are EC2/EBS/S3 or ECS/ESSD/OSS without changing application contracts.

## 7. Implementation anchors

- Composition root: `packages/qbr_core/application/service.py`
- Ingestion orchestration: `packages/qbr_core/application/ingestion.py`
- Answer orchestration: `packages/qbr_core/application/answer_runs.py`
- Durable runs: `packages/qbr_core/application/run_store.py`
- Entrypoints: `apps/api/main.py`, `apps/worker/main.py`
