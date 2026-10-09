# Tool runtime

The bot retains responsibility for Discord authorization, channel context,
character voice and memory. `Runtime` owns one bounded model/tool exchange;
`Registry` validates and dispatches explicit tools; `Job` owns scoped workspace,
attachments, receipts and staged output. `Sandbox` executes ordinary system tools.
No other agent framework is a dependency.

## Turn lifecycle

An authorized human invocation queues behind its channel lock and the character's
concurrency semaphore. It snapshots history ending at that trigger, assembles the
existing character/context prompt, and appends a short job/attachment description
as a separate final API message. Character instructions, memory and each labelled
Discord message occupy separate API messages. Unchanged history entries remain
identical message prefixes across invocations, including when a speaker's chunk
grows. The 20/40 window limits still count speaker chunks, not API messages.
Message edits/deletions and window resets legitimately change these prefixes. Private tool messages are never replayed into later Discord turns. Current memory
follows the transcript so updating it does not invalidate the conversation prefix.

Successful requests record the requested model, the served model/backend identifier
when returned, and content-free hashes/counts for prompt sections and tool definitions
in the existing activity usage JSON. The conversation's first-message hash distinguishes
window resets from growth. These hashes are local diagnostics, not DeepSeek cache keys.
`--activity --job FULL_JOB_ID` shows per-request usage and fingerprints; old records
remain readable. Routine summaries stay compact. No prompt bodies, API credentials,
or raw private tool traces are added to usage records.

Current attributed reactions occupy a separate user message after the transcript
and before the job description. Changing reactions do not rewrite transcript entries.
Schemas remain stable. The provider returns either a completed final answer or
structured calls. Calls execute sequentially, get protocol-linked tool results,
and grow the private working transcript. DeepSeek assistant reasoning fields are
replayed inside that exchange and discarded when it ends.

Before posting, the bot checks that the trigger still exists and matches the
snapshot. It freezes files when `return_file` is called, records delivery starts,
posts text and files together, records the returned Discord IDs, and commits the completed
conversation window. An error records a failed job without advancing the successful
context checkpoint. An already accepted memory request remains pending. A partial
Discord delivery is not atomic and is never silently retried.

The first outgoing message replies to the human trigger and combines its text
with staged files. Further text or file batches are ordinary channel messages
without another reply reference. Text is split without dropping characters;
files are grouped in staging order, up to ten per message and 24 MiB of file
data, leaving room under Discord's 25 MiB request cap for multipart metadata.
File handles are closed even on failed uploads. Delivery receipts track each
combined message, and the archive records attachments alongside that message's
text. A failure after an earlier message was sent preserves those receipts.

Turn failures get one additional text-only character explanation, bounded to 60
seconds, outside the ordinary request count. Runtime preserves the last complete
protocol exchange for this request, excluding orphaned/malformed tool calls and
truncated answers. If it exceeds the working-context limit, recovery uses the
original bounded conversation instead. Safe error categories and scoped receipt
states explain what stopped and which effects may already have happened. No tools
are offered and `tool_choice: "none"` prevents new calls. Returned tool calls or
incomplete recovery text are rejected. Staged files and earlier deliveries are
never retried; recovery receipts start after existing parts, including uncertain
ones. After any delivery attempt, explanation messages continue without another
reply reference. Failed jobs keep their state and do not advance the successful
window. If the model cannot explain, a generic notice is attempted. Explicit
cancellation and silent check-ins do not start recovery requests.

Exact `status`/`cancel` human commands bypass the work queue. A requester can cancel
their own active job; shutdown cancels all work. Memory writing acquires the same
channel lock, runs after jobs settle, and follows the existing retry schedule.
Tool-enabled startup marks abandoned jobs interrupted. No automatic job replay.

Optional `initiative` checks share the scheduler but run as separate tool turns.
Only explicitly configured channels participate. Persistent activity revisions,
an observed message cursor, and a deduplicated inbox distinguish unread eligible
human activity from bot chatter and previously handled invocations. The first
enable baselines the current channel; subsequent checks scan the newest messages
after the cursor, within the configured fetch limit, to recover offline activity.
No new eligible human activity means no model request. A batch is consumed before
generation to prevent automatic retries of uncertain side effects. New human
activity during generation remains pending and suppresses stale Discord output.
The check-in child task can be cancelled without cancelling the scheduler;
shutdown cancels both. Queued human invocations take priority.

`finish(text?, reply_to?, reactions?)` is a terminal control tool, offered on all
tool turns. It must be last in a batch and does not consume the work-dispatch cap.
An omitted text and no reactions explicitly choose silence; this also suppresses
previously staged files. Explicit empty text can deliver staged files alone.
Replies and reactions are scoped to the selected window, and their source content
is refreshed before effects. Scheduled text defaults to channel.send; manual text
defaults to a reply to its trigger. Reactions follow any text/files, with separate
started/sent receipts. Partial delivery is retained without replay. A successful
silent turn commits the observed window and last_seen_id without inventing a bot
message or replacing the last actual response ID.

Reaction snapshots distinguish normal/burst reactors by ID, name and bot status,
up to 20 emoji types and 100 reactors per message. Incomplete attribution is
marked explicitly; no original reaction times are inferred. Gateway changes
refresh encountered messages and memory deltas. Human reaction events can wake
enabled check-in channels; bot reaction events never do.

## Execution and enforced bounds

Defaults in `config.example.yaml`:

| Bound | Default |
|---|---:|
| Model requests per turn | 12 |
| Tool dispatches per turn | 24 |
| Runtime duration | 600 seconds |
| Command wall time and per-process CPU | 60 seconds |
| Per-process address space | 1024 MiB |
| Per-file bytes and default upload cap | 8 MiB |
| Monitored workspace storage | 256 MiB |
| Workspace entries | 2000 |
| Immediate tool output | 12000 characters |
| Serialized working context | 300000 characters |
| Returned files | 5 |
| Parallel turns across channels | 2 |
| Waiting invocations per channel | 3 |
| Terminal workspace retention | 1 day |

Bubblewrap unshares user, PID, mount, network and other namespaces, drops capabilities,
creates a new session, and exposes read-only `/usr` with usual `/bin` and library
links. Writable workspace, `/tmp` and `/dev/shm` all refer to job storage. The host
home, repo, history database, credentials, display sockets and Docker socket are
not mounted. The environment is constructed from fixed values. `prlimit` applies
CPU, address-space, file-size, descriptor and process limits. Timeout/cancellation
kills the process group, reaps Bubblewrap, and drains/closes bounded output.

Address-space/CPU limits apply per process; this backend does not provide an
aggregate cgroup memory/CPU quota. The process limit also depends on Linux's
per-user accounting. Workspace size/count are monitored every 100 ms and checked
after execution; this is an overshoot-detecting limit, not a filesystem quota.
Filesystem walks run in worker threads so they do not block gateway handling.
File-tool work also runs off the loop, while SQLite stays on its owning thread;
cancelled writes settle before the job is released. The address-space bound
includes reserved virtual mappings, so some JVM/V8/BLAS builds can fail despite
low resident memory. It is configurable through `tools.memory_mb` and disclosed
in the exec schema; Python is not the only supported runtime.
Use a dedicated OS account/volume with hard quotas if those are required. `/usr`
comes from the operator's host, so installed utilities/libraries and distro layout
are part of the runtime environment. A pinned container image can be a later backend.

Command output is drained even after the immediate excerpt fills. A private log
stores up to `file_bytes`; `exec` returns a `log:` handle for paged `read_file`
access with `offset_bytes`. Logs are not mounted into the sandbox. Private logs
and frozen artifacts have their own bounded per-call/file storage outside the
monitored work directory. They expire with the job.

The model cannot increase these limits through arguments. A command can use a
shorter timeout. The last model request is reserved for a final reply; reaching
the tool-call cap also moves directly to this final request. The runtime preserves
the complete exchange and adds a short finish instruction. This request offers
only `finish` with `tool_choice: "auto"`, preserving DeepSeek thinking-mode support
(forced tools are unsupported). Native final text is also accepted. Work tools
requested despite this are rejected before any effects. A batch
that crosses the call cap receives error results for its excess calls before the
final request. With `max_steps: 1`, the turn can only produce a final outcome.
Timeouts, excessive context and malformed provider responses still produce a
failed job with surviving files/receipts; this does not implement automatic
continuation or compaction.

## Files, attachments and continuation

File tools accept relative paths or `/workspace/...`; they reject absolute host
paths, traversal, symlinks, devices and other nonregular files. Text reads are
bounded and UTF-8 decoded with replacement. Code can create/edit other files using
the ordinary sandbox environment.

`get_attachment` accepts only IDs from actual attachment objects in this turn's
selected conversation. The host refreshes the source Discord message and signed
URL on demand, downloads without redirects, checks declared and actual byte sizes,
and assigns a safe destination. A URL or path supplied by the model cannot select
a different channel or host file. Downloads run in the host adapter; parsing or
extracting their content belongs in the sandbox.

`return_file` reads a bounded regular file into a private frozen copy, records its
size/digest/display filename, and adds it to final delivery. Later workspace writes
do not change that copy. The cap is the smaller of configuration and guild upload
limit; it does not silently publish oversized files elsewhere.

`open_job` requires the same character and channel and a terminal prior job.
Jobs belong to the character, so a different requester or a self-initiated
check-in can resume earlier work. The original initiator, requester (absent for
self-initiated work), channel, trigger and input message IDs remain recorded and
are returned with the receipts. Legacy jobs retain their original actor and
trigger; their initiation mode is unknown and their source list contains only
the recorded trigger. Attachments and workspaces never cross the channel boundary.
It validates and copies surviving workspace files into `prior/<job-id>` and returns
compact outcome receipts. It rejects unsafe file types or excessive storage. This
is a new model turn, not protocol replay. A receipt with no settled result means
uncertain execution; the model must inspect the workspace before deciding what
further work is needed. Reusing a call ID in one job returns its recorded result;
changed arguments are rejected and uncertain executions are not repeated.

SQLite stores small job/receipt/delivery records independently of successful
conversation snapshots. Generated working files live at
`data/<character>/jobs/<channel>/<job-id>/`. Cleanup only visits recorded, expired,
terminal job directories older than `retention_days` (default 24 hours), at startup
and hourly while running; it preserves active jobs and all conversation/memory data.
Discord uploads remain available independently of their expired local source files.
`--jobs` reads states locally without loading keys or contacting services.

## Durable character library

Sandbox file tools also provide `library_save`, `library_list`, `library_get`, and
`library_delete`, enabled by default when those tools are enabled. A file is durable
only after an explicit save; saving does not send it to Discord. Ordinary workspaces
still expire, and `MEMORY.md` still holds conversational recollections.

`library_save(path, name?, scope?, overwrite?)` copies one workspace file. `name`
defaults to its basename and may contain relative directory components for grouping.
These are logical names, not host paths. Scope defaults to `channel`: only turns in
that originating channel can discover, retrieve, replace or delete it. Explicit
`personal` scope makes a file visible across this character's channels. Use channel
scope for channel material and attachments unless deliberately making them personal.
Other characters never share a library. The same name can exist in both scopes;
retrieval and deletion use the specified scope, without an implicit fallback.

`library_list(prefix?, scope?, limit?, offset?)` returns filenames, scopes, byte sizes,
and UTC modification dates, newest first. It defaults to personal plus current-channel
files, with up to 20 entries per page and a `next_offset` for further pages. The optional
prefix is a literal filename prefix. `library_get(name, path, scope?)` copies a saved
file into a new workspace path; it never overwrites an existing workspace file. Use
ordinary `read_file`/`exec` to work with that copy, and save again to preserve edits.
`library_delete(name, scope?)` removes the saved copy, leaving workspace copies intact.
Replacing a saved name requires `overwrite: true`; there is no version history.

When accessible saved files exist, the turn's final context message includes at most
five recent entries and the visible total. No file contents, descriptions or tags are
injected, and an empty library adds no listing. This snapshot follows the cached
conversation prefix and is never added to memory or the Discord transcript. Tool
notes explain persistence once per turn without requesting inspection or saving.
There is no automatic saving, retrieval or model request dedicated to library upkeep.

The optional `library` config has `enabled` (default true), `max_bytes` (1 GiB),
`max_files` (1000) and `file_bytes` (8 MiB). Save/get also obey `tools.file_bytes`,
and retrieval obeys workspace byte and entry limits. These are character-wide quotas
across all scopes. Full libraries return an error; no saved content is auto-evicted.
Content normally lives in `data/<character>/library/`. An optional `library.storage_dir`
is resolved relative to the config and gets the character ID appended; it cannot live
under temporary jobs. The existing per-character SQLite database stores the index
and save provenance. Back up both the content directory and that database; changing
the content directory requires moving the content with it.

Each save records its job, channel, initiator, trigger, and input message IDs. Save/get
results include compact provenance (up to 20 source IDs plus the full source count);
the complete source list remains in SQLite. Ordinary tool receipts and activity
summaries record library operations. Durable files are independent copies: deleting
source messages or changing conversational memory does not remove them. Use
`library_delete` for that.

The library is never mounted into the execution sandbox. The host validates regular
files, rejects symlinks/traversal, serializes operations per character, and completes
in-progress file and metadata changes before cancellation releases the job. Replacement
writes a new immutable blob before switching its database record; failed saves retain
the previous file. Unreferenced blobs from interrupted saves are removed before the
next save. A missing or unsafe stored file reports an error and can still be deleted;
no action is silently replayed.


## Memory requests

`remember(note, message_ids?)` and `forget(note, message_ids?)` store a short
note with the current human actor, date and source message IDs (default: the
triggering message). Source IDs must occur in the selected window. No transcript
copy is stored; sources are read from the archive, so edits and deletions apply.
A remember note whose sources are all deleted is withdrawn.

Scheduled updates show pending notes to the writer, which returns the complete
revised MEMORY.md in one call; applied notes are marked and not shown again. A full
refresh (`--memory-once`) rewrites from the current MEMORY.md and the archive and
lists every standing note in date order, so forget notes keep suppressing facts
the archive still contains. Writer instructions say never to record that something
was forgotten.

The tools return queued, not written. Requests coalesce, bypass ordinary
quiet/cooldown after the job settles, and retain failure backoff.

## Plugin interface

An enabled Python module exports `register(registry)` and calls:

```python
from bevvycord.tools import arguments

async def search(context, query):
    # Call an operator-configured external integration here.
    return {"results": []}

def register(registry):
    registry.add(
        "my_search", "Search my service and return compact source links.",
        arguments({"query": {"type": "string", "maxLength": 500}}, ["query"]),
        search,
    )
```

Registration validates JSON schemas and rejects duplicate/invalid names. Call
arguments are validated before dispatch; handlers are async and must return JSON.
Unknown tools, argument failures and handler errors become bounded tool results.
The registry loads only the modules explicitly listed in `tools.plugins`, in order.

Python plugins are trusted operator code with host privileges, not a sandboxed
extension format. Plugins read character-specific settings from `registry.environment` during
registration; reading `os.environ` bypasses supervisor credential/settings isolation.

The context is the scoped Job, including its workspace, actor,
channel, attachment and artifact helpers, and store. It does not include the
Discord client. Trusted modules can still import host APIs and read the process
environment; do not promote model-written workspace code to a plugin.

The included `bevvycord.plugins.search` uses `BEVVYCORD_SEARCH_URL` and SearXNG's
JSON API for general/image search and also registers `web_fetch`. The configured
search service can be local. Model-selected fetch destinations must be public:
all DNS answers are checked, and a custom httpcore backend connects to a validated
literal IP while retaining the original HTTP Host and TLS hostname. Loopback,
private, link-local, shared-address, reserved and translation/tunnel ranges are
rejected, including on redirected connections. Environment proxies are ignored;
downloads have no access to provider or Discord authorization headers.

Fetch streams original bytes into a generated workspace filename, enforces the
existing file/workspace limits, allows five redirects and has a 60-second total
deadline bounded by the configured turn duration. Compressed responses despite
`Accept-Encoding: identity` are rejected. Failure/cancellation removes partial
files. HTML gets a UTF-8 text companion with scripts/styles omitted; the original
remains for sandbox processing. Text excerpts are bounded. It does not run a
browser, execute scripts, bypass site login or archive the working exchange.
The downloaded path works with existing `read_file`, `exec` and `return_file`.
More integrations can use this registry without changing the Discord/context
engine.
