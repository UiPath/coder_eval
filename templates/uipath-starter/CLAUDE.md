# AGENTS.md – Unified Guide for UiPath Workflows & Agentic Solutions

This document provides a comprehensive guide to building, testing, and deploying Python automations and intelligent agents on the UiPath platform using the `uipath-python` and `uipath-langchain-python` SDK, just like the agent present in this folder.

---

## 0) Local Environment Setup (with `uv`)

This project assumes you’re using [`uv`](https://github.com/astral-sh/uv) for fast Python installs, virtualenvs, and command execution.

### 0.1 Install Python & create a virtualenv

```bash
# Install a modern Python (adjust the version if you need)
uv python install 3.12

# Create a local virtual environment (uses the latest installed Python by default)
uv venv
```

> **Tip:** You don’t need to “activate” the venv if you use `uv run ...`, but if you prefer activation:
>
> -   macOS/Linux: `source .venv/bin/activate`
> -   Windows PowerShell: `.venv\Scripts\Activate.ps1`

### 0.2 Install dependencies

```bash
uv pip install -e .
```

### 0.3 Run the UiPath CLI via `uv`

Using `uv run` means you don’t have to activate the venv:

```bash
# Log in and write credentials to .env
uv run uipath auth

# Initialize (scans entrypoints and updates uipath.json)
uv run uipath init

# Interactive dev loop (recommended)
uv run uipath dev

# Non-interactive run of classic entrypoint
uv run uipath run main.py '{"message": "Hello from uv"}'

# If you exposed a compiled graph entrypoint called "agent"
# (name exposed in langgraph.json)
uv run uipath run agent '{"topic": "Quarterly sales"}'
```

## 1) Core Developer Workflow (CLI)

The **unified CLI** supports both classic automations and LangGraph agents.

### 1.1 Authenticate

```bash
uipath auth
```

-   Opens a browser login and writes credentials to `.env`.
-   Required before local runs or publishing.

### 1.2 Initialize

```bash
uipath init

```

-   Scans the classic entrypoint (`main.py`) and creates/updates `uipath.json` with **input/output schema** and **resource bindings**.
-   Re‑run when you change function signatures, add Assets/Queues/Buckets, or new graph entrypoints.

### 1.3 Local Run & Debug

```bash
# Interactive development mode (recommended)
uipath dev

# Non‑interactive quick runs
uipath run main.py '{"message": "Hello from the CLI"}'
# For a compiled graph
uipath run agent '{"topic": "Quarterly sales"}'
```

-   `dev` shows live logs, traces, and chat history.

### 1.4 Package, Publish, Deploy

```bash
uipath pack
uipath publish
uipath deploy
```

-   `deploy` is a wrapper that packs and publishes to your Orchestrator feed.
-   Use per‑environment pipelines (Dev → Test → Prod folders/tenants).

### 1.5 Other Useful Commands

```bash
uipath invoke           # Execute a process remotely (when configured)
uipath eval             # Run evaluation scenarios for agents
uipath --help           # Discover flags and subcommands
```

---

## 2) Environment, Credentials & Configuration

Both SDKs read their configuration from **environment variables** (directly, or via `.env` loaded by `python-dotenv`).

### 2.1 Minimal local `.env`

```bash
UIPATH_URL="https://cloud.uipath.com/ORG/TENANT"
UIPATH_ACCESS_TOKEN="your-token"

# Common defaults
UIPATH_FOLDER_PATH="Shared"
```

> **Best practice:** Commit `.env.example` (documenting required vars) but never commit `.env`.

### 2.3 Loading configuration in code

```python
from dotenv import load_dotenv
from uipath import UiPath
from uipath.models.errors import BaseUrlMissingError, SecretMissingError

load_dotenv()
try:
    sdk = UiPath()
except (BaseUrlMissingError, SecretMissingError) as e:
    raise SystemExit(f"Config error: {e}. Run 'uipath auth' or set env vars.")
```

---

## 3) Classic Automation Track (Python SDK, `uipath`)

### 3.1 Entrypoint shape (Pydantic IO strongly recommended)

```python
# src/main.py
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from uipath import UiPath

load_dotenv()

class AutomationInput(BaseModel):
    customer_id: str
    message: str

class AutomationOutput(BaseModel):
    status: str
    confirmation_code: Optional[str] = None

def main(input: AutomationInput) -> AutomationOutput:
    sdk = UiPath()
    cfg = sdk.assets.retrieve(name="GlobalConfig")
    print(f"Using API URL from asset: {cfg.value}")
    return AutomationOutput(status="Success", confirmation_code="ABC-123")
```

### 3.2 Core recipes

#### Execute a child process

```python
import time
from dotenv import load_dotenv
from uipath import UiPath

load_dotenv()
sdk = UiPath()
job = sdk.processes.invoke(
    name="Process_To_Run_In_Finance",
    input_arguments={"customer_id": 12345},
    folder_path="Finance",
)
while job.state in ("Pending", "Running"):
    time.sleep(5)
    job = sdk.jobs.retrieve(key=job.key)
print("State:", job.state, "Output:", job.output_arguments)
```

#### Assets – configuration & credentials

```python
from uipath import UiPath

sdk = UiPath()
plain = sdk.assets.retrieve(name="My_App_Config")
print("Endpoint:", plain.value)

try:
    cred = sdk.assets.retrieve_credential(name="My_App_Credential")
    print("Got credential username:", cred.username)
except ValueError as e:
    print("Credential unavailable in non‑robot context:", e)
```

#### Queues – transactional work

```python
from uipath import UiPath

sdk = UiPath()
QUEUE = "InvoiceProcessing"
sdk.queues.create_item(
    name=QUEUE,
    specific_content={"invoice_id": "INV-9876", "amount": 450.75, "vendor": "Supplier Inc."},
    priority="High",
)
trx = sdk.queues.create_transaction_item(name=QUEUE)
if trx:
    try:
        # ... process trx.specific_content ...
        sdk.queues.complete_transaction_item(trx.id, {"status": "Successful", "message": "OK"})
    except Exception as e:
        sdk.queues.complete_transaction_item(trx.id, {"status": "Failed", "is_successful": False, "processing_exception": str(e)})
```

#### Buckets – file management

```python
from uipath import UiPath

sdk = UiPath()
with open("report.pdf", "w") as f:
    f.write("sample report")

sdk.buckets.upload(name="MonthlyReports", source_path="report.pdf", blob_file_path="2024/July/report.pdf")
sdk.buckets.download(name="InputFiles", blob_file_path="data/customers.xlsx", destination_path="local_customers.xlsx")
```

#### Context Grounding – RAG

```python
from uipath import UiPath

async def main(input: dict):
    sdk = UiPath()
    q = input.get("query")
    hits = sdk.context_grounding.search(name="Internal_Wiki", query=q, number_of_results=3)
    context = "\n".join([h.content for h in hits])
    enriched = f"Context:\n{context}\n\nAnswer: {q}"
    resp = await sdk.llm.chat_completions(model="gpt-4o-mini-2024-07-18", messages=[{"role": "user", "content": enriched}])
    return {"answer": resp.choices[0].message.content}
```

#### Event triggers – Integration Service

```python
from pydantic import BaseModel
from uipath import UiPath
from uipath.models import EventArguments

class Output(BaseModel):
    status: str
    summary: str

def main(input: EventArguments) -> Output:
    sdk = UiPath()
    payload = sdk.connections.retrieve_event_payload(input)
    if "event" in payload and "text" in payload["event"]:
        txt = payload["event"]["text"]
        user = payload["event"].get("user", "Unknown")
        summ = sdk.llm.chat(prompt=f"Summarize from {user}: {txt}", model="gpt-4")
        return Output(status="Processed", summary=getattr(summ, "content", str(summ)))
    return Output(status="Skipped", summary="Not a Slack message event")
```

#### Passing files between jobs – attachments

```python
from uipath import UiPath
from uipath.models import InvokeProcess

def main(input_args: dict):
    sdk = UiPath()
    csv = "id,name\n1,Alice\n2,Bob"
    att_key = sdk.jobs.create_attachment(name="processed.csv", content=csv)
    return InvokeProcess(name="LoadDataToSystem", input_arguments={"dataFileKey": str(att_key)})
```

---

## 4) Agentic Track (LangGraph/LangChain SDK, `uipath-langchain`)

### 4.1 Quick start – chat model

```python
from uipath_langchain.chat import UiPathChat
from langchain_core.messages import HumanMessage

chat = UiPathChat(model="gpt-4o-2024-08-06", max_retries=3)
print(chat.invoke([HumanMessage(content="Hello")]).content)
```

### 4.2 Simple graph example

`graph = builder.compile()` is enough for the agent to run with `uipath run agent '{"topic": "Quarterly sales"}`

```python
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import START, StateGraph, END
from uipath_langchain.chat import UiPathChat
from pydantic import BaseModel
import os

llm = UiPathChat(model="gpt-4o-mini-2024-07-18")

class GraphState(BaseModel):
    topic: str

class GraphOutput(BaseModel):
    report: str

async def generate_report(state: GraphState) -> GraphOutput:
    system_prompt = "You are a report generator. Please provide a brief report based on the given topic."
    output = await llm.ainvoke([SystemMessage(system_prompt), HumanMessage(state.topic)])
    return GraphOutput(report=output.content)

builder = StateGraph(GraphState, output=GraphOutput)

builder.add_node("generate_report", generate_report)

builder.add_edge(START, "generate_report")
builder.add_edge("generate_report", END)

graph = builder.compile()
```

### 4.3 ReAct‑style agent with tools

```python
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import create_react_agent
from langchain_tavily import TavilySearch
from uipath_langchain.chat import UiPathChat
from pydantic import BaseModel

class GraphState(BaseModel):
    company_name: str

tavily = TavilySearch(max_results=5)
llm = UiPathChat(model="gpt-4o-2024-08-06")
agent = create_react_agent(llm, tools=[tavily], prompt="You are a research assistant.")

builder = StateGraph(GraphState)
builder.add_node("research", agent)
builder.add_edge(START, "research")
builder.add_edge("research", END)

graph = builder.compile()
```

### 4.4 RAG – Context Grounding vector store & retriever

```python
from uipath_langchain.vectorstores import ContextGroundingVectorStore
from uipath_langchain.chat import UiPathAzureChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

vs = ContextGroundingVectorStore(index_name="my_knowledge_base")
retriever = vs.as_retriever(search_kwargs={"k": 3})

prompt = ChatPromptTemplate.from_template("""Answer from context:
{context}
Question: {question}
""")
llm = UiPathAzureChatOpenAI(model="gpt-4o-2024-08-06")

docs = retriever.invoke("Vacation policy?")
print(llm.invoke(prompt.format(context=docs, question="Vacation policy? ")).content)
```

### 4.5 Embeddings & structured outputs

```python
from uipath_langchain.embeddings import UiPathAzureOpenAIEmbeddings
from uipath_langchain.chat import UiPathChat
from pydantic import BaseModel, Field

emb = UiPathAzureOpenAIEmbeddings(model="text-embedding-3-large")
vec = emb.embed_query("remote work policy")

class EmailRule(BaseModel):
    rule_name: str = Field(description="Name of the rule")
    conditions: dict = Field(description="Rule conditions")
    target_folder: str = Field(description="Target folder")

schema_chat = UiPathChat(model="gpt-4o-2024-08-06").with_structured_output(EmailRule)
rule = schema_chat.invoke("Create a rule to move emails from noreply@company.com to Archive")
```

### 4.6 Observability – Async tracer

```python
from uipath_langchain.tracers import AsyncUiPathTracer
from uipath_langchain.chat import UiPathChat

tracer = AsyncUiPathTracer(action_name="my_action", action_id="unique_id")
chat = UiPathChat(model="gpt-4o-2024-08-06", callbacks=[tracer])
```

---

## 5) LLM Gateway – Two ways to call models

### 5.1 OpenAI‑compatible path

```python
from uipath import UiPath
sdk = UiPath()
resp = sdk.llm_openai.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Summarize this invoice"}],
)
print(resp.choices[0].message.content)
```

### 5.2 Normalized UiPath LLM path

```python
from uipath import UiPath
sdk = UiPath()
answer = sdk.llm.chat(prompt="Analyze customer feedback", model="gpt-4")
print(answer.content if hasattr(answer, "content") else answer)
```

---

## 6) Testing, Evaluation & Quality Gates

-   **Unit tests**: Pure functions in `src/` and graph nodes in `graphs/`.
-   **E2E tests**: Use `uipath run` against local mocks or a Dev folder tenant.
-   **Evaluations**: For agent behaviors, leverage `uipath eval` scenarios to benchmark prompt/graph changes.
-   **Static checks**: `ruff`, `pyright`/`mypy` with type‑strict public APIs.

Minimal sanity check:

```python
import importlib.metadata, sys
print("uipath:", importlib.metadata.version("uipath"))
print("python:", sys.version)
```

## 8) Security, Secrets & Governance

-   **Never** commit secrets. Use Secret Manager / GitHub Actions secrets.
-   Scope tokens to least privilege; rotate regularly.
-   For **credential assets**, prefer Action Center/Robot context retrieval over plain text.
-   Enable **telemetry** and **tracing** for auditability.

---

## 9) Operational Patterns & Pitfalls

-   **Folder context**: Prefer `folder_path`/`folder_key` explicitly in critical calls.
-   **Idempotency**: For queues and bucket uploads, include natural keys and conflict handling.
-   **Backpressure**: Poll jobs with exponential backoff; avoid tight loops.
-   **Timeouts**: Raise `UIPATH_TIMEOUT_SECONDS` for large payloads.
-   **Version pins**: For LangGraph, stay within `>=0.5,<0.7` range.

---

## 10) Merged API Surface

### 10.1 `uipath` (Python SDK)

#### Processes

-   **`sdk.processes.invoke(name, input_arguments=None, folder_key=None, folder_path=None) -> Job`**
    Start a process by **Release name**. Returns a `Job` with attributes like `.key`, `.state`, `.start_time`, `.end_time`, `.output_arguments` (string or JSON), and `.faulted_reason` when applicable.
    _Common errors_: `httpx.HTTPStatusError 404 /Releases` (bad name or wrong folder), `403 Forbidden` (insufficient RBAC).
    _Example:_

    ```python
    job = sdk.processes.invoke("ACME_Invoice_Load", {"batch_id": "B-42"}, folder_path="Finance")
    while job.state in ("Pending", "Running"):
        await asyncio.sleep(3)
        job = sdk.jobs.retrieve(job.key, folder_path="Finance")
    if job.state == "Successful":
        print("Output:", job.output_arguments)
    else:
        print("Failed:", job.faulted_reason)
    ```

-   **`sdk.processes.invoke_async(...) -> Job`** – Fire‑and‑forget; same return as `invoke` but do not block.

#### Jobs

-   **`sdk.jobs.retrieve(job_key, folder_key=None, folder_path=None) -> Job`** – Refresh job state and metadata.
-   **`sdk.jobs.resume(inbox_id, job_id, folder_key=None, folder_path=None, payload=None) -> None`** – Resume a suspended job (HITL continuation).
-   **`sdk.jobs.extract_output(job) -> Optional[str]`** – Convenience helper to get the output string.
-   **Attachments API**
    -   `sdk.jobs.list_attachments(job_key, folder_key=None, folder_path=None) -> list[str]`
    -   `sdk.jobs.create_attachment(name, content=None, source_path=None, job_key=None, category=None, folder_key=None, folder_path=None) -> uuid.UUID`
    -   `sdk.jobs.link_attachment(attachment_key, job_key, category=None, folder_key=None, folder_path=None) -> None`
        _Example:_
    ```python
    key = sdk.jobs.create_attachment("summary.csv", content="id,val\n1,9")
    sdk.jobs.link_attachment(key, job.key, category="Output")
    for att in sdk.jobs.list_attachments(job.key):
        print("Attachment:", att)
    ```

#### Assets

-   **`sdk.assets.retrieve(name, folder_key=None, folder_path=None) -> Asset | UserAsset`** – Read scalar/JSON assets; access `.value`.
-   **`sdk.assets.retrieve_credential(name, folder_key=None, folder_path=None)`** – Robot‑only; provides `.username` and `.password`.
-   **`sdk.assets.update(robot_asset, folder_key=None, folder_path=None) -> Response`** – Update value (admin required).
    _Tips_: Keep secrets in **Credential** assets. For per‑environment config, store by folder and pass `folder_path` explicitly.

#### Queues

-   **`sdk.queues.create_item(name, specific_content: dict, priority='Normal', reference=None, due_date=None, ... ) -> Response`** – Enqueue work. Consider setting a **`reference`** to ensure idempotency.
-   **`sdk.queues.create_transaction_item(name, no_robot: bool = False) -> TransactionItem | None`** – Claim a transaction for processing. Returned item has `.id`, `.specific_content`, `.reference`.
-   **`sdk.queues.update_progress_of_transaction_item(transaction_key, progress: str) -> Response`** – Heartbeat/progress note.
-   **`sdk.queues.complete_transaction_item(transaction_key, result: dict) -> Response`** – Mark result and, on failure, include `processing_exception`.
-   **`sdk.queues.list_items(status=None, reference=None, top=100, ... ) -> Response`** – Filter queue contents.
    _Example:_
    ```python
    trx = sdk.queues.create_transaction_item("InvoiceProcessing")
    if trx:
        try:
            # process trx.specific_content...
            sdk.queues.complete_transaction_item(trx.id, {"status": "Successful", "message": "OK"})
        except Exception as e:
            sdk.queues.complete_transaction_item(trx.id, {
                "status": "Failed", "is_successful": False, "processing_exception": str(e)
            })
    ```

#### Buckets (Storage)

-   **`sdk.buckets.upload(name, blob_file_path, source_path=None, content=None, content_type=None, folder_key=None, folder_path=None) -> None`** – Upload from disk or in‑memory `content`.
-   **`sdk.buckets.download(name, blob_file_path, destination_path, folder_key=None, folder_path=None) -> None`** – Save a blob to local path.
-   **`sdk.buckets.retrieve(name, key=None, folder_key=None, folder_path=None) -> Bucket`** – Inspect bucket metadata.
    _Tip_: Use MIME `content_type` for correct downstream handling (e.g., `application/pdf`, `text/csv`).

#### Actions (Action Center)

-   **`sdk.actions.create(title=None, data=None, app_name=None, app_key=None, app_folder_path=None, app_folder_key=None, app_version=None, assignee=None) -> Action`**
-   **`sdk.actions.retrieve(action_key, app_folder_path=None, app_folder_key=None) -> Action`**
    _Async variants available (`create_async`)._
    _Pattern_: Create → return `WaitAction` from your `main()` → human completes → automation resumes via `jobs.resume` with payload.

#### Context Grounding (RAG)

-   **`sdk.context_grounding.search(name, query, number_of_results=5, folder_key=None, folder_path=None) -> list[ContextGroundingQueryResponse]`** – Retrieve top‑k chunks (`.content`, `.source`).
-   **`sdk.context_grounding.add_to_index(name, blob_file_path=None, content_type=None, content=None, source_path=None, ingest_data=True, folder_key=None, folder_path=None) -> None`** – Add docs.
-   **`sdk.context_grounding.retrieve(name, folder_key=None, folder_path=None) -> ContextGroundingIndex`** – Inspect index.
-   **`sdk.context_grounding.ingest_data(index, folder_key=None, folder_path=None) -> None`**, **`delete_index(index, ...)`** – Bulk ops.
    _Example:_
    ```python
    hits = sdk.context_grounding.search("Internal_Wiki", "vacation policy", 3)
    ctx = "\n".join(h.content for h in hits)
    answer = await sdk.llm.chat_completions(model="gpt-4o-mini-2024-07-18",
        messages=[{"role":"user","content": f"Use this context:\n{ctx}\n\nQ: What is our policy?"}])
    ```

#### Connections (Integration Service)

-   **`sdk.connections.retrieve(key) -> Connection`** – Connection metadata.
-   **`sdk.connections.retrieve_token(key) -> ConnectionToken`** – OAuth token passthrough.
-   **`sdk.connections.retrieve_event_payload(event_args) -> dict`** – Get full trigger payload for event‑driven agents.

#### Attachments (generic)

-   **`sdk.attachments.upload(name, content=None, source_path=None, folder_key=None, folder_path=None) -> uuid.UUID`**
-   **`sdk.attachments.download(key, destination_path, folder_key=None, folder_path=None) -> str`**
-   **`sdk.attachments.delete(key, folder_key=None, folder_path=None) -> None`**

#### Folders

-   **`sdk.folders.retrieve_key(folder_path) -> str | None`** – Resolve a path to folder key for scoping.

#### LLM Gateway

-   **Normalized path**:
    -   `sdk.llm.chat_completions(model, messages, max_tokens=None, temperature=None, tools=None, tool_choice=None, ...) -> ChatCompletion`
-   **OpenAI‑compatible path**:
    -   `sdk.llm_openai.chat.completions.create(model, messages, max_tokens=None, temperature=None, ...) -> ChatCompletion`
    -   `sdk.llm_openai.embeddings.create(input, embedding_model, openai_api_version=None) -> Embeddings`
        _Tip_: Prefer **normalized** for UiPath‑first features; use **OpenAI‑compatible** to reuse LC/third‑party clients unchanged.

#### Low‑level HTTP

-   **`sdk.api_client.request(method, url, scoped=True, infer_content_type=True, **kwargs) -> Response`\*\*
-   **`sdk.api_client.request_async(...) -> Response`**
    _Use cases_: custom endpoints, preview APIs, or troubleshooting raw requests.

---

### 10.2 `uipath-langchain` (LangGraph SDK)

#### Chat Models

-   **`uipath_langchain.chat.UiPathChat`** (normalized) & **`UiPathAzureChatOpenAI`** (Azure passthrough)
    **Init (common):** `model='gpt-4o-2024-08-06'`, `temperature`, `max_tokens`, `top_p`, `n`, `streaming=False`, `max_retries=2`, `request_timeout=None`, `callbacks=None`, `verbose=False`
    **Messages:** `langchain_core.messages` (`SystemMessage`, `HumanMessage`, `AIMessage`) or plain string.
    **Methods:**

    -   `invoke(messages | str) -> AIMessage` (sync)
    -   `ainvoke(messages | str) -> AIMessage` (async)
    -   `astream(messages)` → async generator of chunks (for streaming UIs)
    -   `with_structured_output(pydantic_model)` → parsed/validated output object
        _Examples:_

    ```python
    chat = UiPathChat(model="gpt-4o-2024-08-06")
    print(chat.invoke("Say hi").content)

    class Answer(BaseModel):
        text: str
        score: float

    tool_chat = chat.with_structured_output(Answer)
    parsed = tool_chat.invoke("Return a JSON with text and score")
    ```

#### Embeddings

-   **`uipath_langchain.embeddings.UiPathAzureOpenAIEmbeddings`**
    -   `embed_documents(list[str]) -> list[list[float]]`
    -   `embed_query(str) -> list[float]`
        _Params:_ `model='text-embedding-3-large'`, `dimensions=None`, `chunk_size=1000`, `max_retries=2`, `request_timeout=None`.

#### Vector Store (Context Grounding)

-   **`uipath_langchain.vectorstores.ContextGroundingVectorStore(index_name, folder_path=None, uipath_sdk=None)`**
    -   `similarity_search(query, k=4) -> list[Document]`
    -   `similarity_search_with_score(query, k=4) -> list[tuple[Document, float]]`
    -   `similarity_search_with_relevance_scores(query, k=4, score_threshold=None) -> list[tuple[Document, float]]`
    -   `.as_retriever(search_kwargs={'k': 3}) -> BaseRetriever`
        _Document fields_: `.page_content`, `.metadata` (source, uri, created_at).

#### Retriever

-   **`uipath_langchain.retrievers.ContextGroundingRetriever(index_name, folder_path=None, folder_key=None, uipath_sdk=None, number_of_results=10)`**
    -   `invoke(query) -> list[Document]` (sync/async)
        _Tip_: Use retriever in LC chains/graphs for clean separation of concerns.

#### Tracing / Observability

-   **`uipath_langchain.tracers.AsyncUiPathTracer(action_name=None, action_id=None, context=None)`**
    Add to `callbacks=[tracer]` on any LC runnable to capture spans/metadata into UiPath.

#### Agent Building with LangGraph

-   **`langgraph.prebuilt.create_react_agent(llm, tools, prompt=None, **kwargs) -> Runnable`\*\* – Get a practical ReAct agent quickly.
-   **`langgraph.graph.StateGraph`**
    -   `add_node(name, fn)` – Node is callable (sync/async) receiving/returning your `State` model.
    -   `add_edge(src, dst)` – Connect nodes (`START` and `END` available).
    -   `compile() -> Graph` – Freeze DAG for execution.
        _Pattern:_ use nodes for **tool‑use**, **HITL interrupt**, **routing**, and **post‑processing**.

---

## 11) Troubleshooting

**Classic**

-   `SecretMissingError` / `BaseUrlMissingError` → run `uipath auth`, verify env.
-   404 for Releases/Assets → check object names and folder context.
-   403 Forbidden → token scopes; re‑authenticate or create proper OAuth app.
-   Timeouts → network/proxy; verify `UIPATH_URL`.

**LangGraph**

-   `ModuleNotFoundError: uipath_langchain` → `pip install uipath-langchain`.
-   401 Unauthorized → check `UIPATH_ACCESS_TOKEN` or OAuth pair.
-   Version mismatch → ensure `langgraph>=0.5,<0.7`.

---

## 12) Glossary

-   **Action Center (HITL)**: Human‑in‑the‑loop task approvals.
-   **Assets**: Centralized config/secret storage.
-   **Buckets**: Cloud file storage.
-   **Context Grounding**: UiPath semantic indexing for RAG.
-   **LLM Gateway**: UiPath’s model broker (normalized and OpenAI‑compatible).
-   **Queues**: Transactional work management.

---

## 13) Checklists

**Before first run**

-   [ ] `uipath auth`
-   [ ] Populate `.env` and copy to teammates as `.env.example` template
-   [ ] `uipath init` after defining `main()` and/or graphs

**Pre‑publish**

-   [ ] Unit + E2E passing
-   [ ] `uipath pack` clean
-   [ ] Secrets in Assets / Connections, not in code

**Production readiness**

-   [ ] Folder scoping & RBAC verified
-   [ ] Tracing/Telemetry enabled
-   [ ] Runbooks for failures, retries, backoff

---

## 14) Links

-   Project README: `./README.md`
-   UiPath Python SDK docs & samples: https://uipath.github.io/uipath-python/
-   UiPath LangGraph SDK docs & samples: https://uipath.github.io/uipath-python/langchain/quick_start/
