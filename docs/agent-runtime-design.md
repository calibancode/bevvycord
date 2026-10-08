# Agent runtime implementation design

This document records the original design. The core runtime is now implemented;
[agent-runtime.md](agent-runtime.md) describes its actual interface and limits.
Some details below remain extension ideas, including a pinned environment image,
MCP adapters, automated forgetting, and public artifact hosting.
Existing character configurations, credentials, history, memory, and normal replies
remain the basis of the application. No other agent project's runtime is required.

## Ownership

Bevvycord owns Discord invocation, attribution, character/channel context,
continuity, and memory. A small local runtime owns the steps within an invoked
task. Execution and optional integrations live behind explicit tool interfaces.
Character prompts remain user-authored; environment descriptions explain available
capabilities without imposing a coding persona or a workflow for every task.

Use three distinct stores:

1. Conversation: existing channel windows and participation archive.
2. Memory: existing writer output plus durable explicit memory requests.
3. Jobs: bounded working directories, execution/delivery receipts, and artifacts.

Completed jobs do not become an extra hidden channel conversation. Future replies
see the final Discord answer and attachment annotations. A job can be continued
explicitly by referencing its output or ID; that exposes its surviving workspace
and compact outcome record without replaying all prior searches and command output.

## Turn flow

1. Authorize the human invocation; assign an opaque job ID and retain trigger ID,
   character ID, channel ID, actor ID, and reply target as host-controlled scope.
2. Queue with a bound. Cancel/status commands bypass the work queue. Snapshot
   history at the accepted trigger position as today; never silently add subsequent
   channel traffic to the model's current task.
3. Create a job directory and record its state before execution.
4. Assemble the character/context prompt, attachment inventory, short environment
   description, and stable schemas for the enabled tools.
5. Make a model request. A normal answer follows today's path. Tool calls enter
   the registry: validate arguments, resolve scope, execute, settle results, and
   append the protocol messages before the next model request.
6. Continue until a completed final answer, cancellation, or an enforced step/time
   limit. Handle tool errors as tool results so the model can inspect and recover.
7. Validate/stage output artifacts. Recheck the initiating message before delivery.
   Commit delivery receipts as Discord returns message IDs.
8. Record the completed conversation and release job resources. Durable side
   effects already performed remain recorded even if final delivery failed.
9. Schedule memory through the existing policy; explicit remember requests can
   additionally queue a rebuild without waiting for automatic quiet/cooldown.

The runtime carries original assistant responses, tool-call IDs, and required
provider fields between steps. DeepSeek reasoning fields remain internal and are
never posted to Discord or included in the participation archive. The provider
must not discard them before the tool loop has finished.

Keep the current final-text `generate` interface for the memory writer, which
does not need tools. Add a structured request interface rather than forcing
memory generation through a general-purpose execution loop.

## Basic environment and tools

Provide a general workspace: shell, Python, ordinary file operations, common
scientific/image libraries, and utilities such as FFmpeg and ImageMagick.
Inspect files, write scripts, transform media, and verify outputs using those
primitives. Ordinary media work does not require a skill/plugin per operation.

Small initial model-facing surface:

- `exec`: shell command, working-directory-relative path, execution timeout.
- `read_file`: bounded text reads and offsets; identify binary content explicitly.
- `write_file`: create/replace workspace files without putting every file in shell
  heredocs. Other file work can use the ordinary environment.
- `get_attachment`: retrieve a host-assigned attachment handle into the workspace.
- `return_file`: stage a workspace artifact for final Discord delivery.
- `remember`: persist a recollection request with its conversational source.

Execution returns exit status and bounded stdout/stderr excerpts. Full command
output is available through a job-local handle and paged reads. Do not dump large
logs, binary data, or video frames wholesale into the prompt.

Network access is an explicit execution setting. Web/publishing integration
credentials stay with the host adapter; they are not inherited by arbitrary code.
The tool schema cannot ask for host filesystem scope, alternate credentials, or
another character/channel store.

## Execution boundary

The host has Docker and Bubblewrap installed, plus FFmpeg, ImageMagick, Python,
and uv. Installation alone does not establish sandbox compatibility or isolation.
Select one initial execution backend and exercise it directly before enabling
code tools. Avoid a framework of many untested backends.

Define the sandbox interface as create/run/cancel/close. A job gets writable
workspace/scratch storage and deliberately selected read-only runtime files.
Do not mount the repo, `.secrets`, bot history database, user home, desktop sockets,
or the Docker socket. Launch with a small constructed environment rather than
copying the bot's environment and deleting a few known key names.

Bound execution time, output, storage, processes, CPU and memory using the selected
backend/OS controls. Cancellation terminates the entire execution group, waits
for termination, and checks that descendants are gone. Do not fall back to host
shell execution if isolation cannot be established.

Useful libraries belong in a documented, versioned environment. Job-local package
installation can be supported where configured; installing into the bot's own
environment is not a model capability.

## Attachments and artifacts

Give the model metadata and opaque attachment handles: filename, content type,
declared size, source message, and available status. Fetch on demand from actual
Discord attachment objects in the permitted context, including a referenced bot
output when the human asks for a follow-up transformation.

Use host-generated paths, preserve original filenames as metadata, stream downloads
with actual byte limits, and refresh expired Discord attachment references when
possible. Treat extracted archives and media parsers as sandbox work.

An artifact record includes ID, path, display filename, size, digest, and detected
type. Freeze a staged copy before delivery; reject paths outside the workspace,
symlinks/devices, and files still changing. A workspace filename alone is not
authority to upload an arbitrary host path.

The final answer and files are one delivery operation with recorded component
message IDs. If output exceeds Discord's available upload limit, return a useful
failure or use an explicitly configured publishing adapter. Do not silently
publish an attachment to a public host to get around a size limit.

Workspaces/artifacts have an explicit retention period and an announced availability
status. Retain incomplete delivery artifacts for recovery. Cleanup is limited
to known expired job directories; it never sweeps character history or memory.

## Explicit memory

`remember` saves a dated request and source references in the current scoped
character/channel. Scope and actor identity come from the host, not model arguments.
Record whether it is a human request or the character's own recollection.

The receipt distinguishes request stored/update queued from MEMORY.md updated.
Store these requests independently of successful final-answer posting: a posted
answer failing does not erase a memory request that was already accepted.

Include requests as attributed source material in future fresh memory rebuilds,
alongside the participation archive. Do not make the writer read old MEMORY.md.
Requests can be corrected or superseded; never convert an old explicit request
into an unchangeable fact. A later forget operation should withdraw the request
and rebuild rather than merely edit a file that the next writer will regenerate.

Coalesce multiple requests from one turn into one queued rebuild after that turn
settles. Retain a single writer per character/channel; never recursively invoke
the writer under the active turn's channel lock. Failure preserves the request
and follows the existing retry delay. Memory remains optional per character.

## Extensions

Use an explicit registry with namespaced names, JSON schemas, async handlers,
availability checks and side-effect classifications. Validate schemas at startup,
reject duplicate names, validate call arguments at execution, and report unknown
tools as errors instead of running arbitrary functions.

The host passes a scoped context: actor, channel, character, job workspace,
cancellation signal, artifact registration and narrowly exposed integration APIs.
Plugins do not receive the Discord client or credential environment by default.
Python plugins run as trusted host code; registry scoping is not a Python sandbox.

Load operator-enabled local packages in deterministic order. Dependencies and
configuration are separate from instructions. Ordinary scripts the character
writes stay in its workspace; they do not become trusted plugins automatically.

Examples of extensions: a web service, another agent, an authenticated external
application, or an artifact publishing destination. Provide a short capability
catalog and load optional longer instructions on demand. Keep the active tool
schema order stable within a job. MCP can later adapt external toolsets to the same
registry, without making MCP or another agent runtime mandatory now.

## Context, caching and long tasks

Keep fixed character instructions and schemas first, current memory/context next,
and grow the job's working transcript during execution. Runtime time/step/input
limits are enforced in code, not by asking the model to count tokens.

Large tool results use excerpts and resource handles. If the working transcript
reaches its input limit, settle the job with a clear partial result instead of
silently deleting tool messages, changing protocol adjacency, or dropping required
DeepSeek fields. Compaction can be added later as a deliberate checkpoint mechanism.

Plain chat remains one request. Work uses as many bounded steps as needed. Tool
catalog changes, memory changes and channel-window resets can change cache prefixes;
measure hit/miss usage across every step rather than assume savings.

## Recovery and scheduling

Use a short job state progression: queued, running, delivering, complete, failed,
cancelled, interrupted. A tool execution receipt progresses from started to
settled. `(job_id, tool_call_id)` identifies executions within that job.

Retry transient model requests with the same input when appropriate. Never blindly
retry a shell command, upload or external action after an ambiguous failure.
Started-but-unsettled side effects remain uncertain and require inspection.
Do not promise exactly-once external execution or Discord delivery.

On startup, abandoned running jobs become interrupted. Do not automatically rerun
commands. Explicit continuation starts a fresh model turn with surviving files,
known receipts and an outcome summary. This avoids needing to persist/replay private
reasoning to resume a half-finished provider exchange.

Maintain bounded queues and execution concurrency. Serial workspace mutations stay
serial; clearly independent read-only tool calls can run concurrently. Different
character processes remain isolated as today. Avoid holding SQLite transactions
open across downloads/model requests/process execution. Persist short settlements.

Status/cancel should work while a job runs; progress can be a concise character
message when helpful, without streaming command logs into the conversation.
The requesting human can cancel their own job. Shutdown cancels/settles active
work and closes execution resources before releasing the character process lock.

## Implementation order and acceptance checks

1. Structured provider responses and the bounded turn loop. Offline fixtures:
   no-tool reply, repeated tools, nullable content, malformed arguments, tool
   errors, required provider-field replay, limits, cancellation.
2. Durable jobs/receipts and remember. Check scoped writes, duplicates, multiple
   additions, writer failure, final-delivery failure, restart, later fresh rebuild.
3. One tested sandbox and attachment/artifact path. Run a real local file
   transformation and inspect its output, isolation, resource limits and descendant
   cleanup. Check malicious filenames, oversized files, escaped paths and symlinks.
4. Discord delivery, status and cancel. Offline adapters followed by explicit live
   testing in the testing channel. Preserve all existing continuity/memory checks.
5. Extension registry exercised by one external integration. Test missing dependency,
   plugin failure, stable schema order and enablement differences between characters.

The first end-to-end demonstration should be a character receiving an attachment,
using ordinary code to transform it, checking the output, returning the file, and
remembering a requested preference. Its next ordinary reply should retain useful
conversation and memory, with no accumulated execution transcript.

## Reference boundaries

These projects supply examples, not runtime dependencies:

- DeepSeek protocol: https://api-docs.deepseek.com/guides/thinking_mode/
- DeepSeek extension seams: https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md
- Hermes registry: https://hermes-agent.nousresearch.com/docs/developer-guide/tools-runtime/
- OpenCode extensions: https://opencode.ai/docs/plugins/
- Bubblewrap policy ownership: https://github.com/containers/bubblewrap/blob/main/README.md
