## API Reference

This section provides a comprehensive reference for all UiPath SDK services and methods. Each service is documented with complete method signatures, including parameter types and return types.

### SDK Initialization

Initialize the UiPath SDK client

```python
from uipath.platform import UiPath

# Initialize with environment variables
sdk = UiPath()

# Or with explicit credentials
sdk = UiPath(base_url="https://cloud.uipath.com/...", secret="your_token")
```

### Agenthub

Agenthub service

```python
# Fetch available models from LLM Gateway discovery endpoint.
sdk.agenthub.get_available_llm_models(headers: dict[str, Any] | None=None) -> list[uipath.platform.agenthub.agenthub.LlmModel]

# Asynchronously fetch available models from LLM Gateway discovery endpoint.
sdk.agenthub.get_available_llm_models_async(headers: dict[str, Any] | None=None) -> list[uipath.platform.agenthub.agenthub.LlmModel]

# Start a system agent job.
sdk.agenthub.invoke_system_agent(agent_name: str, entrypoint: str, input_arguments: dict[str, Any] | None=None, folder_key: str | None=None, folder_path: str | None=None, headers: dict[str, Any] | None=None) -> str

# Asynchronously start a system agent and return the job.
sdk.agenthub.invoke_system_agent_async(agent_name: str, entrypoint: str, input_arguments: dict[str, Any] | None=None, folder_key: str | None=None, folder_path: str | None=None, headers: dict[str, Any] | None=None) -> str

```

### Api Client

Api Client service

```python
# Access api_client service methods
service = sdk.api_client

```

### Assets

Assets service

```python
# List assets using OData API with offset-based pagination.
sdk.assets.list(folder_path: Optional[str]=None, folder_key: Optional[str]=None, filter: Optional[str]=None, orderby: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.assets.Asset]

# Asynchronously list assets using OData API with offset-based pagination.
sdk.assets.list_async(folder_path: Optional[str]=None, folder_key: Optional[str]=None, filter: Optional[str]=None, orderby: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.assets.Asset]

# Retrieve an asset by its name.
sdk.assets.retrieve(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.orchestrator.assets.UserAsset | uipath.platform.orchestrator.assets.Asset

# Asynchronously retrieve an asset by its name.
sdk.assets.retrieve_async(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.orchestrator.assets.UserAsset | uipath.platform.orchestrator.assets.Asset

# Gets a specified Orchestrator credential.
sdk.assets.retrieve_credential(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.Optional[str]

# Asynchronously gets a specified Orchestrator credential.
sdk.assets.retrieve_credential_async(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.Optional[str]

# Update an asset's value.
sdk.assets.update(robot_asset: uipath.platform.orchestrator.assets.UserAsset, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> httpx.Response

# Asynchronously update an asset's value.
sdk.assets.update_async(robot_asset: uipath.platform.orchestrator.assets.UserAsset, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> httpx.Response

```

### Attachments

Attachments service

```python
# Delete an attachment.
sdk.attachments.delete(key: uuid.UUID, folder_key: str | None=None, folder_path: str | None=None) -> None

# Delete an attachment asynchronously.
sdk.attachments.delete_async(key: uuid.UUID, folder_key: str | None=None, folder_path: str | None=None) -> None

# Download an attachment.
sdk.attachments.download(key: uuid.UUID, destination_path: str, folder_key: str | None=None, folder_path: str | None=None) -> str

# Download an attachment asynchronously.
sdk.attachments.download_async(key: uuid.UUID, destination_path: str, folder_key: str | None=None, folder_path: str | None=None) -> str

# Get the BlobFileAccess information for an attachment.
sdk.attachments.get_blob_file_access_uri(key: uuid.UUID, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.attachments.attachments.BlobFileAccessInfo

# Get the BlobFileAccess information for an attachment asynchronously.
sdk.attachments.get_blob_file_access_uri_async(key: uuid.UUID, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.attachments.attachments.BlobFileAccessInfo

# Open an attachment.
sdk.attachments.open(attachment: uipath.platform.attachments.attachments.Attachment, mode: <enum 'AttachmentMode="AttachmentMode.READ", content: Union[str, bytes, Iterable[bytes], AsyncIterable[bytes], NoneType]=None, folder_key: str | None=None, folder_path: str | None=None) -> typing.Iterator[typing.Tuple[uipath.platform.attachments.attachments.Attachment, httpx.Response]]

# Open an attachment asynchronously.
sdk.attachments.open_async(attachment: uipath.platform.attachments.attachments.Attachment, mode: <enum 'AttachmentMode="AttachmentMode.READ", content: Union[str, bytes, Iterable[bytes], AsyncIterable[bytes], NoneType]=None, folder_key: str | None=None, folder_path: str | None=None) -> typing.AsyncIterator[typing.Tuple[uipath.platform.attachments.attachments.Attachment, httpx.Response]]

# Upload a file or content to UiPath as an attachment.
sdk.attachments.upload(name: str, content: str | bytes | None=None, source_path: str | None=None, folder_key: str | None=None, folder_path: str | None=None) -> uuid.UUID

# Upload a file or content to UiPath as an attachment asynchronously.
sdk.attachments.upload_async(name: str, content: str | bytes | None=None, source_path: str | None=None, folder_key: str | None=None, folder_path: str | None=None) -> uuid.UUID

```

### Buckets

Buckets service

```python
# Create a new bucket.
sdk.buckets.create(name: str, description: Optional[str]=None, identifier: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> uipath.platform.orchestrator.buckets.Bucket

# Async version of create().
sdk.buckets.create_async(name: str, description: Optional[str]=None, identifier: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> uipath.platform.orchestrator.buckets.Bucket

# Delete a bucket.
sdk.buckets.delete(name: Optional[str]=None, key: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> None

# Async version of delete().
sdk.buckets.delete_async(name: Optional[str]=None, key: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> None

# Delete a file from a bucket.
sdk.buckets.delete_file(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Delete a file from a bucket asynchronously.
sdk.buckets.delete_file_async(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Download a file from a bucket.
sdk.buckets.download(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, destination_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Download a file from a bucket asynchronously.
sdk.buckets.download_async(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, destination_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Check if bucket exists.
sdk.buckets.exists(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Async version of exists().
sdk.buckets.exists_async(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Check if a file exists in a bucket.
sdk.buckets.exists_file(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Async version of exists_file().
sdk.buckets.exists_file_async(name: Optional[str]=None, key: Optional[str]=None, blob_file_path: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Get files using OData GetFiles API with offset-based pagination.
sdk.buckets.get_files(name: Optional[str]=None, key: Optional[str]=None, prefix: str="", recursive: bool=False, file_name_glob: Optional[str]=None, skip: int=0, top: int=500, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.BucketFile]

# Async version of get_files() with offset-based pagination.
sdk.buckets.get_files_async(name: Optional[str]=None, key: Optional[str]=None, prefix: str="", recursive: bool=False, file_name_glob: Optional[str]=None, skip: int=0, top: int=500, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.BucketFile]

# List buckets using OData API with offset-based pagination.
sdk.buckets.list(folder_path: Optional[str]=None, folder_key: Optional[str]=None, name: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.Bucket]

# Async version of list() with offset-based pagination.
sdk.buckets.list_async(folder_path: Optional[str]=None, folder_key: Optional[str]=None, name: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.Bucket]

# List files in a bucket using cursor-based pagination.
sdk.buckets.list_files(name: Optional[str]=None, key: Optional[str]=None, prefix: str="", take_hint: int=500, continuation_token: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.BucketFile]

# Async version of list_files() with cursor-based pagination.
sdk.buckets.list_files_async(name: Optional[str]=None, key: Optional[str]=None, prefix: str="", take_hint: int=500, continuation_token: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.buckets.BucketFile]

# Retrieve bucket information by its name.
sdk.buckets.retrieve(name: Optional[str]=None, key: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.orchestrator.buckets.Bucket

# Asynchronously retrieve bucket information by its name.
sdk.buckets.retrieve_async(name: Optional[str]=None, key: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.orchestrator.buckets.Bucket

# Upload a file to a bucket.
sdk.buckets.upload(key: Optional[str]=None, name: Optional[str]=None, blob_file_path: str, content_type: Optional[str]=None, source_path: Optional[str]=None, content: Union[str, bytes, NoneType]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Upload a file to a bucket asynchronously.
sdk.buckets.upload_async(key: Optional[str]=None, name: Optional[str]=None, blob_file_path: str, content_type: Optional[str]=None, source_path: Optional[str]=None, content: Union[str, bytes, NoneType]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

```

### Connections

Connections service

```python
# Invoke an activity synchronously.
sdk.connections.invoke_activity(activity_metadata: uipath.platform.connections.connections.ActivityMetadata, connection_id: str, activity_input: Dict[str, Any]) -> typing.Any

# Invoke an activity asynchronously.
sdk.connections.invoke_activity_async(activity_metadata: uipath.platform.connections.connections.ActivityMetadata, connection_id: str, activity_input: Dict[str, Any]) -> typing.Any

# Lists all connections with optional filtering.
sdk.connections.list(name: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, connector_key: Optional[str]=None, skip: Optional[int]=None, top: Optional[int]=None) -> typing.List[uipath.platform.connections.connections.Connection]

# Asynchronously lists all connections with optional filtering.
sdk.connections.list_async(name: Optional[str]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, connector_key: Optional[str]=None, skip: Optional[int]=None, top: Optional[int]=None) -> typing.List[uipath.platform.connections.connections.Connection]

# Synchronously retrieve connection API metadata.
sdk.connections.metadata(element_instance_id: int, connector_key: str, tool_path: str, parameters: Optional[Dict[str, str]]=None, schema_mode: bool=True, max_jit_depth: int=5) -> uipath.platform.connections.connections.ConnectionMetadata

# Asynchronously retrieve connection API metadata.
sdk.connections.metadata_async(element_instance_id: int, connector_key: str, tool_path: str, parameters: Optional[Dict[str, str]]=None, schema_mode: bool=True, max_jit_depth: int=5) -> uipath.platform.connections.connections.ConnectionMetadata

# Retrieve connection details by its key.
sdk.connections.retrieve(key: str) -> uipath.platform.connections.connections.Connection

# Asynchronously retrieve connection details by its key.
sdk.connections.retrieve_async(key: str) -> uipath.platform.connections.connections.Connection

# Retrieve event payload from UiPath Integration Service.
sdk.connections.retrieve_event_payload(event_args: uipath.platform.connections.connections.EventArguments) -> typing.Dict[str, typing.Any]

# Retrieve event payload from UiPath Integration Service.
sdk.connections.retrieve_event_payload_async(event_args: uipath.platform.connections.connections.EventArguments) -> typing.Dict[str, typing.Any]

# Retrieve an authentication token for a connection.
sdk.connections.retrieve_token(key: str, token_type: <enum 'ConnectionTokenType="ConnectionTokenType.DIRECT") -> uipath.platform.connections.connections.ConnectionToken

# Asynchronously retrieve an authentication token for a connection.
sdk.connections.retrieve_token_async(key: str, token_type: <enum 'ConnectionTokenType="ConnectionTokenType.DIRECT") -> uipath.platform.connections.connections.ConnectionToken

```

### Context Grounding

Context Grounding service

```python
# Add content to the index.
sdk.context_grounding.add_to_index(name: str, blob_file_path: str, content_type: Optional[str]=None, content: Union[str, bytes, NoneType]=None, source_path: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, ingest_data: bool=True) -> None

# Asynchronously add content to the index.
sdk.context_grounding.add_to_index_async(name: str, blob_file_path: str, content_type: Optional[str]=None, content: Union[str, bytes, NoneType]=None, source_path: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, ingest_data: bool=True) -> None

# Create a new ephemeral context grounding index.
sdk.context_grounding.create_ephemeral_index(usage: <enum 'EphemeralIndexUsage, attachments: list[str]) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Create a new ephemeral context grounding index.
sdk.context_grounding.create_ephemeral_index_async(usage: <enum 'EphemeralIndexUsage, attachments: list[str]) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Create a new context grounding index.
sdk.context_grounding.create_index(name: str, source: Union[uipath.platform.context_grounding.context_grounding_payloads.BucketSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.GoogleDriveSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.DropboxSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.OneDriveSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.ConfluenceSourceConfig], description: Optional[str]=None, extraction_strategy: Optional[str]=None, embeddings_enabled: Optional[bool]=None, is_encrypted: Optional[bool]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Create a new context grounding index.
sdk.context_grounding.create_index_async(name: str, source: Union[uipath.platform.context_grounding.context_grounding_payloads.BucketSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.GoogleDriveSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.DropboxSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.OneDriveSourceConfig, uipath.platform.context_grounding.context_grounding_payloads.ConfluenceSourceConfig], description: Optional[str]=None, extraction_strategy: Optional[str]=None, embeddings_enabled: Optional[bool]=None, is_encrypted: Optional[bool]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Delete a context grounding index.
sdk.context_grounding.delete_index(index: uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Asynchronously delete a context grounding index.
sdk.context_grounding.delete_index_async(index: uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Downloads the Batch Transform result file to the specified path.
sdk.context_grounding.download_batch_transform_result(id: str, destination_path: str, validate_status: bool=True, index_name: str | None=None) -> None

# Asynchronously downloads the Batch Transform result file to the specified path.
sdk.context_grounding.download_batch_transform_result_async(id: str, destination_path: str, validate_status: bool=True, index_name: str | None=None) -> None

# Ingest data into the context grounding index.
sdk.context_grounding.ingest_data(index: uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Asynchronously ingest data into the context grounding index.
sdk.context_grounding.ingest_data_async(index: uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> None

# Retrieve context grounding index information by its name.
sdk.context_grounding.retrieve(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Asynchronously retrieve context grounding index information by its name.
sdk.context_grounding.retrieve_async(name: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uipath.platform.context_grounding.context_grounding_index.ContextGroundingIndex

# Retrieves a Batch Transform task status.
sdk.context_grounding.retrieve_batch_transform(id: str, index_name: str | None=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformResponse

# Asynchronously retrieves a Batch Transform task status.
sdk.context_grounding.retrieve_batch_transform_async(id: str, index_name: str | None=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformResponse

# Retrieve context grounding index information by its ID.
sdk.context_grounding.retrieve_by_id(id: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.Any

# Retrieve asynchronously context grounding index information by its ID.
sdk.context_grounding.retrieve_by_id_async(id: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.Any

# Retrieves a Deep RAG task.
sdk.context_grounding.retrieve_deep_rag(id: str, index_name: str | None=None) -> uipath.platform.context_grounding.context_grounding.DeepRagResponse

# Asynchronously retrieves a Deep RAG task.
sdk.context_grounding.retrieve_deep_rag_async(id: str, index_name: str | None=None) -> uipath.platform.context_grounding.context_grounding.DeepRagResponse

# Search for contextual information within a specific index.
sdk.context_grounding.search(name: str, query: str, number_of_results: int=10, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.List[uipath.platform.context_grounding.context_grounding.ContextGroundingQueryResponse]

# Search asynchronously for contextual information within a specific index.
sdk.context_grounding.search_async(name: str, query: str, number_of_results: int=10, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.List[uipath.platform.context_grounding.context_grounding.ContextGroundingQueryResponse]

# Starts a Batch Transform, task on the targeted index.
sdk.context_grounding.start_batch_transform(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], output_columns: list[uipath.platform.context_grounding.context_grounding.BatchTransformOutputColumn], storage_bucket_folder_path_prefix: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, target_file_name: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, enable_web_search_grounding: bool=False, index_name: str | None=None, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformCreationResponse

# Asynchronously starts a Batch Transform, task on the targeted index.
sdk.context_grounding.start_batch_transform_async(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], output_columns: list[uipath.platform.context_grounding.context_grounding.BatchTransformOutputColumn], storage_bucket_folder_path_prefix: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, target_file_name: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, enable_web_search_grounding: bool=False, index_name: str | None=None, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformCreationResponse

# Asynchronously starts a Batch Transform, task on the targeted index.
sdk.context_grounding.start_batch_transform_ephemeral(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], output_columns: list[uipath.platform.context_grounding.context_grounding.BatchTransformOutputColumn], storage_bucket_folder_path_prefix: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, enable_web_search_grounding: bool=False, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformCreationResponse

# Asynchronously starts a Batch Transform, task on the targeted index.
sdk.context_grounding.start_batch_transform_ephemeral_async(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], output_columns: list[uipath.platform.context_grounding.context_grounding.BatchTransformOutputColumn], storage_bucket_folder_path_prefix: Annotated[str | None, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]=None, enable_web_search_grounding: bool=False, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None) -> uipath.platform.context_grounding.context_grounding.BatchTransformCreationResponse

# Starts a Deep RAG task on the targeted index.
sdk.context_grounding.start_deep_rag(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], glob_pattern: Annotated[str, FieldInfo(annotation=NoneType, required=False, default='*', metadata=[MaxLen(max_length=512)])]="**", citation_mode: <enum 'CitationMode="CitationMode.SKIP", index_name: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.context_grounding.context_grounding.DeepRagCreationResponse

# Asynchronously starts a Deep RAG task on the targeted index.
sdk.context_grounding.start_deep_rag_async(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], glob_pattern: Annotated[str, FieldInfo(annotation=NoneType, required=False, default='*', metadata=[MaxLen(max_length=512)])]="**", citation_mode: <enum 'CitationMode="CitationMode.SKIP", index_name: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None, folder_key: str | None=None, folder_path: str | None=None) -> uipath.platform.context_grounding.context_grounding.DeepRagCreationResponse

# Asynchronously starts a Deep RAG task on the targeted index.
sdk.context_grounding.start_deep_rag_ephemeral(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], glob_pattern: Annotated[str, FieldInfo(annotation=NoneType, required=False, default='*', metadata=[MaxLen(max_length=512)])]="**", citation_mode: <enum 'CitationMode="CitationMode.SKIP", index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None) -> uipath.platform.context_grounding.context_grounding.DeepRagCreationResponse

# Asynchronously starts a Deep RAG task on the targeted index.
sdk.context_grounding.start_deep_rag_ephemeral_async(name: str, prompt: Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=250000)])], glob_pattern: Annotated[str, FieldInfo(annotation=NoneType, required=False, default='*', metadata=[MaxLen(max_length=512)])]="**", citation_mode: <enum 'CitationMode="CitationMode.SKIP", index_id: Optional[Annotated[str, FieldInfo(annotation=NoneType, required=True, metadata=[MaxLen(max_length=512)])]]=None) -> uipath.platform.context_grounding.context_grounding.DeepRagCreationResponse

```

### Conversational

Conversational service

```python
# Access conversational service methods
service = sdk.conversational

```

### Documents

Documents service

```python
# Classify a document using a DU Modern project.
sdk.documents.classify(project_type: <enum 'ProjectType, tag: Optional[str]=None, version: Optional[int]=None, project_name: Optional[str]=None, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None) -> typing.List[uipath.platform.documents.documents.ClassificationResult]

# Asynchronously version of the [`classify`][uipath.platform.documents._documents_service.DocumentsService.classify] method.
sdk.documents.classify_async(project_type: <enum 'ProjectType, tag: Optional[str]=None, version: Optional[int]=None, project_name: Optional[str]=None, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None) -> typing.List[uipath.platform.documents.documents.ClassificationResult]

# Create a validate classification action for a document based on the classification results. More details about validation actions can be found in the [official documentation](https://docs.uipath.com/ixp/automation-cloud/latest/user-guide/validating-classifications).
sdk.documents.create_validate_classification_action(classification_results: List[uipath.platform.documents.documents.ClassificationResult], action_title: str, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_catalog: Optional[str]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.ValidateClassificationAction

# Asynchronous version of the [`create_validation_action`][uipath.platform.documents._documents_service.DocumentsService.create_validate_classification_action] method.
sdk.documents.create_validate_classification_action_async(classification_results: List[uipath.platform.documents.documents.ClassificationResult], action_title: str, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_catalog: Optional[str]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.ValidateClassificationAction

# Create a validate extraction action for a document based on the extraction response. More details about validation actions can be found in the [official documentation](https://docs.uipath.com/ixp/automation-cloud/latest/user-guide/validating-extractions).
sdk.documents.create_validate_extraction_action(extraction_response: uipath.platform.documents.documents.ExtractionResponse, action_title: str, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_catalog: Optional[str]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.ValidateExtractionAction

# Asynchronous version of the [`create_validation_action`][uipath.platform.documents._documents_service.DocumentsService.create_validate_extraction_action] method.
sdk.documents.create_validate_extraction_action_async(extraction_response: uipath.platform.documents.documents.ExtractionResponse, action_title: str, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_catalog: Optional[str]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.ValidateExtractionAction

# Extract predicted data from a document using an DU Modern/IXP project.
sdk.documents.extract(tag: Optional[str]=None, version: Optional[int]=None, project_name: Optional[str]=None, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None, classification_result: Optional[uipath.platform.documents.documents.ClassificationResult]=None, project_type: Optional[uipath.platform.documents.documents.ProjectType]=None, document_type_name: Optional[str]=None) -> typing.Union[uipath.platform.documents.documents.ExtractionResponse, uipath.platform.documents.documents.ExtractionResponseIXP]

# Asynchronously version of the [`extract`][uipath.platform.documents._documents_service.DocumentsService.extract] method.
sdk.documents.extract_async(tag: Optional[str]=None, version: Optional[int]=None, project_name: Optional[str]=None, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None, classification_result: Optional[uipath.platform.documents.documents.ClassificationResult]=None, project_type: Optional[uipath.platform.documents.documents.ProjectType]=None, document_type_name: Optional[str]=None) -> typing.Union[uipath.platform.documents.documents.ExtractionResponse, uipath.platform.documents.documents.ExtractionResponseIXP]

# Get the result of a validate classification action.
sdk.documents.get_validate_classification_result(validation_action: uipath.platform.documents.documents.ValidateClassificationAction) -> typing.List[uipath.platform.documents.documents.ClassificationResult]

# Asynchronous version of the [`get_validation_result`][uipath.platform.documents._documents_service.DocumentsService.get_validate_classification_result] method.
sdk.documents.get_validate_classification_result_async(validation_action: uipath.platform.documents.documents.ValidateClassificationAction) -> typing.List[uipath.platform.documents.documents.ClassificationResult]

# Get the result of a validate extraction action.
sdk.documents.get_validate_extraction_result(validation_action: uipath.platform.documents.documents.ValidateExtractionAction) -> typing.Union[uipath.platform.documents.documents.ExtractionResponse, uipath.platform.documents.documents.ExtractionResponseIXP]

# Asynchronous version of the [`get_validation_result`][uipath.platform.documents._documents_service.DocumentsService.get_validate_extraction_result] method.
sdk.documents.get_validate_extraction_result_async(validation_action: uipath.platform.documents.documents.ValidateExtractionAction) -> typing.Union[uipath.platform.documents.documents.ExtractionResponse, uipath.platform.documents.documents.ExtractionResponseIXP]

# Retrieve the result of an IXP extraction operation (single-shot, non-blocking).
sdk.documents.retrieve_ixp_extraction_result(project_id: str, tag: str, operation_id: str) -> uipath.platform.documents.documents.ExtractionResponseIXP

# Asynchronous version of the [`retrieve_ixp_extraction_result`][uipath.platform.documents._documents_service.DocumentsService.retrieve_ixp_extraction_result] method.
sdk.documents.retrieve_ixp_extraction_result_async(project_id: str, tag: str, operation_id: str) -> uipath.platform.documents.documents.ExtractionResponseIXP

# Retrieve the result of an IXP create validate extraction action operation (single-shot, non-blocking).
sdk.documents.retrieve_ixp_extraction_validation_result(project_id: str, tag: str, operation_id: str) -> uipath.platform.documents.documents.ValidateExtractionAction

# Asynchronous version of the [`retrieve_ixp_extraction_validation_result`][uipath.platform.documents._documents_service.DocumentsService.retrieve_ixp_extraction_validation_result] method.
sdk.documents.retrieve_ixp_extraction_validation_result_async(project_id: str, tag: str, operation_id: str) -> uipath.platform.documents.documents.ValidateExtractionAction

# Start an IXP extraction process without waiting for results (non-blocking).
sdk.documents.start_ixp_extraction(project_name: str, tag: str, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None) -> uipath.platform.documents.documents.StartExtractionResponse

# Asynchronous version of the [`start_ixp_extraction`][uipath.platform.documents._documents_service.DocumentsService.start_ixp_extraction] method.
sdk.documents.start_ixp_extraction_async(project_name: str, tag: str, file: Union[IO[bytes], bytes, str, NoneType]=None, file_path: Optional[str]=None) -> uipath.platform.documents.documents.StartExtractionResponse

# Start an IXP extraction validation action without waiting for results (non-blocking).
sdk.documents.start_ixp_extraction_validation(extraction_response: uipath.platform.documents.documents.ExtractionResponseIXP, action_title: str, action_catalog: Optional[str]=None, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.StartExtractionValidationResponse

# Asynchronous version of the [`start_ixp_extraction_validation`][uipath.platform.documents._documents_service.DocumentsService.start_ixp_extraction_validation] method.
sdk.documents.start_ixp_extraction_validation_async(extraction_response: uipath.platform.documents.documents.ExtractionResponseIXP, action_title: str, action_catalog: Optional[str]=None, action_priority: Optional[uipath.platform.documents.documents.ActionPriority]=None, action_folder: Optional[str]=None, storage_bucket_name: Optional[str]=None, storage_bucket_directory_path: Optional[str]=None) -> uipath.platform.documents.documents.StartExtractionValidationResponse

```

### Entities

Entities service

```python
# Delete multiple records from an entity in a single batch operation.
sdk.entities.delete_records(entity_key: str, record_ids: List[str]) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

# Asynchronously delete multiple records from an entity in a single batch operation.
sdk.entities.delete_records_async(entity_key: str, record_ids: List[str]) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

# Insert multiple records into an entity in a single batch operation.
sdk.entities.insert_records(entity_key: str, records: List[Any], schema: Optional[Type[Any]]=None) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

# Asynchronously insert multiple records into an entity in a single batch operation.
sdk.entities.insert_records_async(entity_key: str, records: List[Any], schema: Optional[Type[Any]]=None) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

# List all entities in Data Service.
sdk.entities.list_entities() -> typing.List[uipath.platform.entities.entities.Entity]

# Asynchronously list all entities in the Data Service.
sdk.entities.list_entities_async() -> typing.List[uipath.platform.entities.entities.Entity]

# List records from an entity with optional pagination and schema validation.
sdk.entities.list_records(entity_key: str, schema: Optional[Type[Any]]=None, start: Optional[int]=None, limit: Optional[int]=None) -> typing.List[uipath.platform.entities.entities.EntityRecord]

# Asynchronously list records from an entity with optional pagination and schema validation.
sdk.entities.list_records_async(entity_key: str, schema: Optional[Type[Any]]=None, start: Optional[int]=None, limit: Optional[int]=None) -> typing.List[uipath.platform.entities.entities.EntityRecord]

# Retrieve an entity by its key.
sdk.entities.retrieve(entity_key: str) -> uipath.platform.entities.entities.Entity

# Asynchronously retrieve an entity by its key.
sdk.entities.retrieve_async(entity_key: str) -> uipath.platform.entities.entities.Entity

# Update multiple records in an entity in a single batch operation.
sdk.entities.update_records(entity_key: str, records: List[Any], schema: Optional[Type[Any]]=None) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

# Asynchronously update multiple records in an entity in a single batch operation.
sdk.entities.update_records_async(entity_key: str, records: List[Any], schema: Optional[Type[Any]]=None) -> uipath.platform.entities.entities.EntityRecordsBatchResponse

```

### Folders

Folders service

```python
# Retrieve the personal workspace folder for the current user.
sdk.folders.get_personal_workspace() -> uipath.platform.orchestrator.folder.PersonalWorkspace

# Asynchronously retrieve the personal workspace folder for the current user.
sdk.folders.get_personal_workspace_async() -> uipath.platform.orchestrator.folder.PersonalWorkspace

# Resolve a folder path to its corresponding folder key.
sdk.folders.retrieve_folder_key(folder_path: str | None) -> str | None

# Asynchronously resolve a folder path to its corresponding folder key.
sdk.folders.retrieve_folder_key_async(folder_path: str | None) -> str | None

# Retrieve the folder key by folder path with pagination support.
sdk.folders.retrieve_key(folder_path: str) -> typing.Optional[str]

# Retrieve the folder key by folder path with pagination support.
sdk.folders.retrieve_key_async(folder_path: str) -> typing.Optional[str]

```

### Guardrails

Guardrails service

```python
# Validate input text using the provided guardrail.
sdk.guardrails.evaluate_guardrail(input_data: str | dict[str, Any], guardrail: uipath.platform.guardrails.guardrails.BuiltInValidatorGuardrail) -> uipath.core.guardrails.guardrails.GuardrailValidationResult

```

### Jobs

Jobs service

```python
# Create and upload an attachment, optionally linking it to a job.
sdk.jobs.create_attachment(name: str, content: Union[str, bytes, NoneType]=None, source_path: Union[str, pathlib.Path, NoneType]=None, job_key: Union[str, uuid.UUID, NoneType]=None, category: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uuid.UUID

# Create and upload an attachment asynchronously, optionally linking it to a job.
sdk.jobs.create_attachment_async(name: str, content: Union[str, bytes, NoneType]=None, source_path: Union[str, pathlib.Path, NoneType]=None, job_key: Union[str, uuid.UUID, NoneType]=None, category: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> uuid.UUID

# Check if job exists.
sdk.jobs.exists(job_key: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Async version of exists().
sdk.jobs.exists_async(job_key: str, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> bool

# Get the actual output data, downloading from attachment if necessary.
sdk.jobs.extract_output(job: uipath.platform.orchestrator.job.Job) -> typing.Optional[str]

# Asynchronously fetch the actual output data, downloading from attachment if necessary.
sdk.jobs.extract_output_async(job: uipath.platform.orchestrator.job.Job) -> typing.Optional[str]

# Link an attachment to a job.
sdk.jobs.link_attachment(attachment_key: uuid.UUID, job_key: uuid.UUID, category: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None)

# Link an attachment to a job asynchronously.
sdk.jobs.link_attachment_async(attachment_key: uuid.UUID, job_key: uuid.UUID, category: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None)

# List jobs using OData API with offset-based pagination.
sdk.jobs.list(folder_path: Optional[str]=None, folder_key: Optional[str]=None, filter: Optional[str]=None, orderby: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.job.Job]

# Async version of list() with offset-based pagination.
sdk.jobs.list_async(folder_path: Optional[str]=None, folder_key: Optional[str]=None, filter: Optional[str]=None, orderby: Optional[str]=None, skip: int=0, top: int=100) -> uipath.platform.common.paging.PagedResult[uipath.platform.orchestrator.job.Job]

# List attachments associated with a specific job.
sdk.jobs.list_attachments(job_key: uuid.UUID, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.List[str]

# List attachments associated with a specific job asynchronously.
sdk.jobs.list_attachments_async(job_key: uuid.UUID, folder_key: Optional[str]=None, folder_path: Optional[str]=None) -> typing.List[str]

# Restart a completed or failed job.
sdk.jobs.restart(job_key: str, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> uipath.platform.orchestrator.job.Job

# Async version of restart().
sdk.jobs.restart_async(job_key: str, folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> uipath.platform.orchestrator.job.Job

# Sends a payload to resume a paused job waiting for input, identified by its inbox ID.
sdk.jobs.resume(inbox_id: Optional[str]=None, job_id: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, payload: Any) -> None

# Asynchronously sends a payload to resume a paused job waiting for input, identified by its inbox ID.
sdk.jobs.resume_async(inbox_id: Optional[str]=None, job_id: Optional[str]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, payload: Any) -> None

# Retrieve a job identified by its key.
sdk.jobs.retrieve(job_key: str, folder_key: str | None=None, folder_path: str | None=None, process_name: str | None=None) -> uipath.platform.orchestrator.job.Job

# Fetch payload data for API triggers.
sdk.jobs.retrieve_api_payload(inbox_id: str) -> typing.Any

# Asynchronously fetch payload data for API triggers.
sdk.jobs.retrieve_api_payload_async(inbox_id: str) -> typing.Any

# Asynchronously retrieve a job identified by its key.
sdk.jobs.retrieve_async(job_key: str, folder_key: str | None=None, folder_path: str | None=None, process_name: str | None=None) -> uipath.platform.orchestrator.job.Job

# Stop one or more jobs with specified strategy.
sdk.jobs.stop(job_keys: List[str], strategy: str="SoftStop", folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> None

# Async version of stop().
sdk.jobs.stop_async(job_keys: List[str], strategy: str="SoftStop", folder_path: Optional[str]=None, folder_key: Optional[str]=None) -> None

```

### Llm

Llm service

```python
# Generate chat completions using UiPath's normalized LLM Gateway API.
sdk.llm.chat_completions(messages: list[dict[str, str]] | list[tuple[str, str]], model: str="gpt-4.1-mini-2025-04-14", max_tokens: int=4096, temperature: float=0, n: int=1, frequency_penalty: float=0, presence_penalty: float=0, top_p: float | None=1, top_k: int | None=None, tools: list[uipath.platform.chat.llm_gateway.ToolDefinition] | None=None, tool_choice: Union[uipath.platform.chat.llm_gateway.AutoToolChoice, uipath.platform.chat.llm_gateway.RequiredToolChoice, uipath.platform.chat.llm_gateway.SpecificToolChoice, Literal['auto', 'none'], NoneType]=None, response_format: dict[str, Any] | type[pydantic.main.BaseModel] | None=None, api_version: str="2024-08-01-preview")

```

### Llm Openai

Llm Openai service

```python
# Generate chat completions using UiPath's LLM Gateway service.
sdk.llm_openai.chat_completions(messages: list[dict[str, str]], model: str="gpt-4.1-mini-2025-04-14", max_tokens: int=4096, temperature: float=0, response_format: dict[str, Any] | type[pydantic.main.BaseModel] | None=None, api_version: str="2024-10-21")

# Generate text embeddings using UiPath's LLM Gateway service.
sdk.llm_openai.embeddings(input: str, embedding_model: str="text-embedding-ada-002", openai_api_version: str="2024-10-21")

```

### Mcp

Mcp service

```python
# List all MCP servers.
sdk.mcp.list(folder_path: str | None=None) -> typing.List[uipath.platform.orchestrator.mcp.McpServer]

# Asynchronously list all MCP servers.
sdk.mcp.list_async(folder_path: str | None=None) -> typing.List[uipath.platform.orchestrator.mcp.McpServer]

# Retrieve a specific MCP server by its slug.
sdk.mcp.retrieve(slug: str, folder_path: str | None=None) -> uipath.platform.orchestrator.mcp.McpServer

# Asynchronously retrieve a specific MCP server by its slug.
sdk.mcp.retrieve_async(slug: str, folder_path: str | None=None) -> uipath.platform.orchestrator.mcp.McpServer

```

### Processes

Processes service

```python
# Start execution of a process by its name.
sdk.processes.invoke(name: str, input_arguments: Optional[Dict[str, Any]]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, attachments: Optional[list[uipath.platform.attachments.attachments.Attachment]]=None, **kwargs) -> uipath.platform.orchestrator.job.Job

# Asynchronously start execution of a process by its name.
sdk.processes.invoke_async(name: str, input_arguments: Optional[Dict[str, Any]]=None, folder_key: Optional[str]=None, folder_path: Optional[str]=None, attachments: Optional[list[uipath.platform.attachments.attachments.Attachment]]=None, **kwargs) -> uipath.platform.orchestrator.job.Job

```

### Queues

Queues service

```python
# Completes a transaction item with the specified result.
sdk.queues.complete_transaction_item(transaction_key: str, result: Union[Dict[str, Any], uipath.platform.orchestrator.queues.TransactionItemResult]) -> httpx.Response

# Asynchronously completes a transaction item with the specified result.
sdk.queues.complete_transaction_item_async(transaction_key: str, result: Union[Dict[str, Any], uipath.platform.orchestrator.queues.TransactionItemResult]) -> httpx.Response

# Creates a new queue item in the Orchestrator.
sdk.queues.create_item(item: Union[Dict[str, Any], uipath.platform.orchestrator.queues.QueueItem]) -> httpx.Response

# Asynchronously creates a new queue item in the Orchestrator.
sdk.queues.create_item_async(item: Union[Dict[str, Any], uipath.platform.orchestrator.queues.QueueItem]) -> httpx.Response

# Creates multiple queue items in bulk.
sdk.queues.create_items(items: List[Union[Dict[str, Any], uipath.platform.orchestrator.queues.QueueItem]], queue_name: str, commit_type: <enum 'CommitType) -> httpx.Response

# Asynchronously creates multiple queue items in bulk.
sdk.queues.create_items_async(items: List[Union[Dict[str, Any], uipath.platform.orchestrator.queues.QueueItem]], queue_name: str, commit_type: <enum 'CommitType) -> httpx.Response

# Creates a new transaction item in a queue.
sdk.queues.create_transaction_item(item: Union[Dict[str, Any], uipath.platform.orchestrator.queues.TransactionItem], no_robot: bool=False) -> httpx.Response

# Asynchronously creates a new transaction item in a queue.
sdk.queues.create_transaction_item_async(item: Union[Dict[str, Any], uipath.platform.orchestrator.queues.TransactionItem], no_robot: bool=False) -> httpx.Response

# Retrieves a list of queue items from the Orchestrator.
sdk.queues.list_items() -> httpx.Response

# Asynchronously retrieves a list of queue items from the Orchestrator.
sdk.queues.list_items_async() -> httpx.Response

# Updates the progress of a transaction item.
sdk.queues.update_progress_of_transaction_item(transaction_key: str, progress: str) -> httpx.Response

# Asynchronously updates the progress of a transaction item.
sdk.queues.update_progress_of_transaction_item_async(transaction_key: str, progress: str) -> httpx.Response

```

### Resource Catalog

Resource Catalog service

```python
# Get tenant scoped resources and folder scoped resources (accessible to the user).
sdk.resource_catalog.list(resource_types: Optional[List[uipath.platform.resource_catalog.resource_catalog.ResourceType]]=None, resource_sub_types: Optional[List[str]]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, page_size: int=20) -> typing.Iterator[uipath.platform.resource_catalog.resource_catalog.Resource]

# Asynchronously get tenant scoped resources and folder scoped resources (accessible to the user).
sdk.resource_catalog.list_async(resource_types: Optional[List[uipath.platform.resource_catalog.resource_catalog.ResourceType]]=None, resource_sub_types: Optional[List[str]]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, page_size: int=20) -> typing.AsyncIterator[uipath.platform.resource_catalog.resource_catalog.Resource]

# Get resources of a specific type (tenant scoped or folder scoped).
sdk.resource_catalog.list_by_type(resource_type: <enum 'ResourceType, name: Optional[str]=None, resource_sub_types: Optional[List[str]]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, page_size: int=20) -> typing.Iterator[uipath.platform.resource_catalog.resource_catalog.Resource]

# Asynchronously get resources of a specific type (tenant scoped or folder scoped).
sdk.resource_catalog.list_by_type_async(resource_type: <enum 'ResourceType, name: Optional[str]=None, resource_sub_types: Optional[List[str]]=None, folder_path: Optional[str]=None, folder_key: Optional[str]=None, page_size: int=20) -> typing.AsyncIterator[uipath.platform.resource_catalog.resource_catalog.Resource]

# Search for tenant scoped resources and folder scoped resources (accessible to the user).
sdk.resource_catalog.search(name: Optional[str]=None, resource_types: Optional[List[uipath.platform.resource_catalog.resource_catalog.ResourceType]]=None, resource_sub_types: Optional[List[str]]=None, page_size: int=20) -> typing.Iterator[uipath.platform.resource_catalog.resource_catalog.Resource]

# Asynchronously search for tenant scoped resources and folder scoped resources (accessible to the user).
sdk.resource_catalog.search_async(name: Optional[str]=None, resource_types: Optional[List[uipath.platform.resource_catalog.resource_catalog.ResourceType]]=None, resource_sub_types: Optional[List[str]]=None, page_size: int=20) -> typing.AsyncIterator[uipath.platform.resource_catalog.resource_catalog.Resource]

```

### Tasks

Tasks service

```python
# Creates a new task synchronously.
sdk.tasks.create(title: str, data: Optional[Dict[str, Any]]=None, app_name: Optional[str]=None, app_key: Optional[str]=None, app_folder_path: Optional[str]=None, app_folder_key: Optional[str]=None, assignee: Optional[str]=None, recipient: Optional[uipath.platform.action_center.tasks.TaskRecipient]=None, priority: Optional[str]=None, labels: Optional[List[str]]=None, is_actionable_message_enabled: Optional[bool]=None, actionable_message_metadata: Optional[Dict[str, Any]]=None, source_name: str="Agent") -> uipath.platform.action_center.tasks.Task

# Creates a new action asynchronously.
sdk.tasks.create_async(title: str, data: Optional[Dict[str, Any]]=None, app_name: Optional[str]=None, app_key: Optional[str]=None, app_folder_path: Optional[str]=None, app_folder_key: Optional[str]=None, assignee: Optional[str]=None, recipient: Optional[uipath.platform.action_center.tasks.TaskRecipient]=None, priority: Optional[str]=None, labels: Optional[List[str]]=None, is_actionable_message_enabled: Optional[bool]=None, actionable_message_metadata: Optional[Dict[str, Any]]=None, source_name: str="Agent") -> uipath.platform.action_center.tasks.Task

# Retrieves a task by its key synchronously.
sdk.tasks.retrieve(action_key: str, app_folder_path: str="", app_folder_key: str="", app_name: str | None=None) -> uipath.platform.action_center.tasks.Task

# Retrieves a task by its key asynchronously.
sdk.tasks.retrieve_async(action_key: str, app_folder_path: str="", app_folder_key: str="", app_name: str | None=None) -> uipath.platform.action_center.tasks.Task

```

