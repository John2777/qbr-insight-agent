# 06. Modules, APIs, and Data Design

## 1. Application modules

`QBRService` is the shared composition root for the API, worker, and evaluation tools. It assembles the database, skills, retrievers, model providers, and focused application services without turning the facade into a catch-all service.

```mermaid
flowchart TB
    API["FastAPI Routes"] --> Root["QBRService<br/>Composition Root / Facade"]
    Worker["Worker"] --> Root
    Scripts["Benchmarks / Tools"] --> Root

    Root --> Ingest["IngestionService"]
    Root --> QA["QAApplicationService"]
    Root --> Resource["ResourceService"]
    Root --> Purge["DocumentPurgeService"]

    QA --> Executor["AnswerRunExecutor"]
    QA --> Repository["RunRepository"]
    QA --> Events["RunEventStore"]
    Executor --> Contract["Evidence · RunResult · RunMetadata"]

    Ingest --> Domain["Documents / Skills"]
    Executor --> Logic["Planning / Retrieval / Analysis"]
    Root --> Foundation["DB · Config · Lease · Errors"]

    classDef core fill:#16324F,color:#fff,stroke:#16324F;
    classDef support fill:#E9F3FA,color:#16324F,stroke:#4A90B8;
    class Root,Executor,Repository,Events core;
    class Ingest,QA,Resource,Purge,Contract,Domain,Logic,Foundation support;
```

The split has three practical benefits: orchestration can be tested without HTTP; run-state transitions stay inside the repository; and immutable dataclasses validate data crossing boundaries instead of passing loosely shaped dictionaries through multiple layers.

## 2. Frontend–backend contract

REST handles resources and commands; SSE carries long-running task events. Uploads and questions return a `job_id` or `run_id` immediately. A client can reconnect and resume from `Last-Event-ID`.

```mermaid
sequenceDiagram
    actor U as User
    participant W as React
    participant A as FastAPI
    participant D as SQLite
    participant K as Worker

    U->>W: Submit a question
    W->>A: POST /conversations/{id}/messages
    A->>D: Create message, snapshot, and run
    A-->>W: 202 Accepted + run_id
    W->>A: GET /runs/{id}/events
    K->>D: Claim + heartbeat
    K->>D: Append status / query_plan / answer_delta
    A-->>W: SSE events
    K->>D: Atomically commit answer + citations + completed
    A-->>W: completed
    U->>W: Open a citation
    W->>A: GET /slides/{id}/preview
    W->>W: Highlight the bounding box
```

### Core APIs

| Capability | Method and path | Response model |
|---|---|---|
| Upload a document | `POST /api/v1/documents` | 202 + document/job |
| Stream ingestion events | `GET /api/v1/jobs/{id}/events` | SSE |
| Create a conversation | `POST /api/v1/conversations` | 201 |
| Submit a question | `POST /api/v1/conversations/{id}/messages` | 202 + run |
| Stream answer events | `GET /api/v1/runs/{id}/events` | resumable SSE |
| Preview a slide | `GET /api/v1/slides/{id}/preview` | private cached asset |
| Resolve a review task | `POST /api/v1/review-tasks/{id}/resolve` | revision |
| Purge a document | `DELETE /api/v1/documents/{id}/purge` | deletion summary |

Question submission is idempotent through `client_message_id`. Routes derive workspace and user scope from the authenticated principal; the client cannot widen that scope through request parameters.

## 3. Domain model

```mermaid
classDiagram
    Workspace "1" --> "*" Member
    Workspace "1" --> "*" Document
    Document "1" --> "*" DocumentVersion
    DocumentVersion "1" --> "*" ParserRun
    ParserRun "1" --> "*" Slide
    Slide "1" --> "*" Element
    Element "1" --> "0..1" Chart
    Chart "1" --> "*" ChartSeries
    ChartSeries "1" --> "*" ChartPoint
    Slide "1" --> "*" Chunk

    Workspace "1" --> "*" Conversation
    Conversation "1" --> "*" Message
    Message "1" --> "0..1" Run
    Run "1" --> "1" ContextSnapshot
    Run "1" --> "*" RunEvent
    Run "1" --> "*" Citation
    Element "1" --> "*" ReviewTask
    ReviewTask "1" --> "*" ReviewRevision

    class DocumentVersion {
      sha256
      activeParserRunId
    }
    class Element {
      type
      bbox
      provenance
      confidence
    }
    class Run {
      status
      attempts
      lease
    }
```

Separating `DocumentVersion` from `ParserRun` is a key modeling decision. The same immutable file can be reprocessed with a new parser, skill, or model; a parser run becomes active only after its data and indexes have been committed successfully.

## 4. Entity–relationship model

```mermaid
erDiagram
    WORKSPACES ||--o{ WORKSPACE_MEMBERS : contains
    WORKSPACES ||--o{ DOCUMENTS : owns
    DOCUMENTS ||--o{ DOCUMENT_VERSIONS : versions
    DOCUMENT_VERSIONS ||--o{ PARSER_RUNS : parsed_by
    DOCUMENT_VERSIONS ||--o{ INGESTION_JOBS : queues
    INGESTION_JOBS ||--o{ JOB_EVENTS : emits
    PARSER_RUNS ||--o{ SLIDES : produces
    SLIDES ||--o{ ELEMENTS : contains
    ELEMENTS ||--o| CHARTS : represents
    CHARTS ||--o{ CHART_SERIES : has
    CHART_SERIES ||--o{ CHART_POINTS : has
    SLIDES ||--o{ CHUNKS : indexes
    CHUNKS ||--o| CHUNK_EMBEDDINGS : embeds
    WORKSPACES ||--o{ CONVERSATIONS : owns
    CONVERSATIONS ||--o{ MESSAGES : contains
    MESSAGES ||--o| RUNS : triggers
    RUNS ||--|| RUN_CONTEXT_SNAPSHOTS : freezes
    RUNS ||--o{ RUN_EVENTS : emits
    RUNS ||--o{ CITATIONS : supports
    ELEMENTS ||--o{ REVIEW_TASKS : flags
    REVIEW_TASKS ||--o{ REVIEW_REVISIONS : records
    WORKSPACES ||--o{ AUDIT_EVENTS : audits
```

Charts use relational `chart → series → point` records rather than a single JSON blob. This supports SQL queries and deterministic calculations by period, unit, and series. JSON is reserved for extensible metadata; FTS and FAISS remain rebuildable indexes.

## 5. Consistency strategy

- SQLite runs in WAL mode with `busy_timeout` and short write transactions; model and embedding calls happen outside transactions.
- Run completion writes the answer, citations, event, and terminal state in one transaction.
- Workers use leases and heartbeats, while the repository validates every state transition.
- Purge operates on a workspace/document boundary and removes relational data, FTS entries, embeddings, objects, and caches together.
- Structured settings are projected into immutable domain views for storage, models, retrieval, conversations, and workers.

## 6. Implementation anchors

- Database: `packages/qbr_core/foundation/database.py`
- Application contracts: `packages/qbr_core/application/contracts.py`
- Run repository: `packages/qbr_core/application/run_store.py`
- API routes: `apps/api/routes/`
- Web API client: `apps/web/src/api.ts`
