# Configuration and behavior guide

[Back to README](../README.md)

A small llmcord-inspired Discord character bot with channel conversation context,
continuity across absences, optional locally archived character memory, and lean
opt-in tool turns for code, files, and external integrations.
One bot account/process represents one character. Each channel has separate state,
archive, and memory, including your testing channel.

This implementation follows the provider and Discord interaction approach of
[llmcord](https://github.com/jakobdylanc/llmcord), with a separate, testable context
engine. It is not a full copy of upstream: `/model`, native vision input, and
editable streaming embeds are not implemented. Rich embeds and Discord text-display
components from other characters are readable. Attachments appear by filename;
tool-enabled characters can download them on demand into their isolated workspace.

## Setup

Requires Python 3.11 or newer. No service starts during setup.

For guided local onboarding on Linux, run:

```bash
./onboard.sh
```

This prepares the shared `.venv`, installs requirements, and collects the character
ID, prompt (inline or from a file), channel IDs, and keys. Secret prompts are hidden.
It guides the Discord developer-portal steps and generates a bot invite link if
you supply the public application ID, saving that ID in the config. Every bot also
prints its join link automatically after connecting, including existing characters
without an application ID in their config. Creating the application and enabling
Message Content Intent still happen in the portal. Setup never starts the bot or
calls DeepSeek.

Each character receives:

```text
characters/rowan.yaml       # Prompt, model, allowed channels, local env paths
.secrets/rowan.env          # Discord token and character API key, mode 0600
bin/run-rowan               # Loads the configured local files through Python
```

The secrets directory is mode 0700. Generated configs, launchers and secrets are
ignored by Git. Per-character keys are the default; optionally use one shared
key in `.secrets/global.env`, referenced by each character's config rather than
copied. Explicit process environment variables take precedence over local files.
Environment files are parsed as data, never sourced or executed as shell code.

Run onboarding again for each character, then launch them from the same checkout:

```bash
./bin/run-rowan --check-config
./bin/run-rowan
# In another terminal:
./bin/run-mira
```

The launch commands connect to Discord and can make paid provider requests.
Launching the same character twice against the same storage directory is rejected
by a process lock; different characters run independently. The lock also applies
to manual memory rebuilds. Relative storage and environment-file paths are anchored
to each configuration's directory, so launch cwd doesn't change their locations.

For manual setup instead:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml`: set your character's stable ID and description, and explicitly
list both your normal channel ID and testing channel ID in `allowed_channel_ids`.
An empty list allows no server channels. This is an exact-channel allowlist, not
a category/server allowlist. Threads are not supported. DMs default off.

Set `DISCORD_BOT_TOKEN` and `DEEPSEEK_API_KEY` in your local process environment.
For local loading, set `env_file` in your config; it is relative to that config.
An optional `shared_env_file` is loaded afterward. Without those entries, the bot
uses only its process environment.
Do not put secrets in the configuration or character prompt.

Enable Message Content Intent in the Discord developer portal. All generated
join links request View Channel, Read Message History, Send Messages, Attach Files,
Embed Links, and Add Reactions. These cover the supported history, chat, upload, reaction and link-preview
features. Channel/category overrides can still deny these permissions; allow them
in each configured channel. The bot ignores messages in threads. See
[Discord permission rules](https://docs.discord.com/developers/topics/permissions).

```bash
.venv/bin/python -m bevvycord --check-config
.venv/bin/python -m bevvycord
```

The second command starts the bot and prints its join link after connecting.
You can also print a link without keys or network calls:

```bash
# Existing character; use its public Application ID from the developer portal:
./bin/run-faust --invite APPLICATION_ID
# If onboarding saved application_id in the character config:
./bin/run-rowan --invite
```

For a legacy configuration with no application ID, simply start the character to
have Discord supply its identity for the printed join link. Keep its character ID,
credentials and storage directory as they are. No repeat onboarding is required.

Ctrl+C starts orderly shutdown: cancel active/queued jobs and memory work, terminate
sandbox descendants, close the Discord/provider clients and database, and release
the character lock. It logs a short stopped message rather than a traceback.
A second Ctrl+C can interrupt cleanup if a forced exit is necessary.

The running bot can make paid provider requests. Mention it
or reply to its message to invoke it. Only authorized humans can invoke it; bots
and webhooks remain visible in history but cannot trigger replies. Outgoing
mentions, including reply notifications, are disabled. The typing indicator stays
active during generation; the completed answer is posted as plain text, split
into Discord-sized messages without repeated edits. Reasoning output is neither
posted nor archived. Truncated provider answers are treated as failures.

Both reply and memory requests identify the character's Discord speaker ID, so
its own past replies remain distinguishable from other bots regardless of nicknames.

Use a distinct configuration and character ID for each character/process.
Configuration changes require a restart.

## Optional character check-ins

Tool-enabled characters can catch up without a ping, then choose to speak, react,
do both, or stay quiet. This is off by default and uses the existing character
prompt and 20/40-chunk continuity. Enable only the channels where you want it:

```yaml
initiative:
  enabled: true
  interval_minutes: 30
  channel_ids:
    - 123456789012345678  # Main channel; leave testing out
```

These IDs must also be in `allowed_channel_ids`. Onboarding offers a separate
check-in channel list. Other allowed channels continue to require a ping or reply.
The timer shares the memory scheduler, but check-ins and memory writing are
separate jobs; check-ins work with memory disabled.

At most once per interval, the bot checks for new eligible human messages, edits
to encountered messages, or reaction changes. Without new human activity it makes
no model request. Bots and webhooks cannot wake it. A normal completed invocation
consumes the activity it read. Silence also saves the context it read, so it keeps
continuity without repeatedly reconsidering the same conversation. New human
activity during a check-in defers its Discord output to a later check. Busy channels
wait for queued invocations to finish.

First enable starts from the current channel position. Subsequent restarts can
catch up on messages posted while offline, bounded by `context.max_fetch_messages`.
Reaction/edit events missed while offline cannot independently wake it. A failed
or cancelled check-in consumes that activity batch, preventing automatic replay
of potentially delivered actions; fresh human activity can start another.

Current reactions are shown with the reactor's name and user/bot ID, including
normal and burst reactions. Attribution is bounded to 20 emoji types and 100
reactors per message; incomplete lists are labelled. Reaction removals and clears
update the observations and pending memory. They follow the transcript as a separate
context message, preserving its unchanged cached prefix. Discord provides current
reactors, so the transcript does not invent when they reacted.

`finish(text?, reply_to?, reactions?)` selects the final outcome. No arguments means
silence; reactions alone post no text. A scheduled text response is an ordinary
channel message unless the character selects a message to reply to. Returned files
accompany text; an explicit empty `text` sends staged files alone, while omitting
`text` leaves staged files private. Targets must be messages in the selected context.
Ensure Add Reactions is allowed in the channel. An authorized human can cancel a
scheduled check-in with `@bot cancel`; Ctrl+C cancels scheduler and active work.

## Conversation continuity

A chunk is an uninterrupted run of messages by the same author. Individual posting
timestamps and reply references remain visible in the transcript. Webhook names
also distinguish speakers when a webhook represents several characters.

The first invocation uses up to the latest 20 chunks, including the invoking
message once. Frequent participation extends that window, preserving its beginning
until it reaches the 40-chunk request cap; it then resets to the latest 20.

After a longer absence, when uninterrupted continuity no longer fits, retain up to
20 chunks of the previous exchange (including the character's own answer) alongside
the latest 20. A context note identifies omitted intervening conversation. As new
conversation accumulates, the older retained part shrinks under the 40-chunk cap;
the bridge retires once the recent portion fills the window. Repeated absences
retain only one bridge, rather than accumulating older windows.

The cap applies to input conversation chunks. A completed response is saved intact
even if adding it temporarily makes the saved state 41 chunks; the next request
applies the cap again. A trigger received during generation is queued, but its
history ends at its own posting position, excluding a later answer it hadn't seen.

`context.soft_chunks` and `hard_chunks` are configurable; hard must be at least
twice soft. Independent fetch/character bounds prevent unbounded requests. A
bounded lookahead distinguishes a complete channel from a cut-off speaker run.
When the fetch limit prevents obtaining whole requested chunks, use bounded recent
context with an explicit omission note instead of refusing to reply. That window
starts fresh rather than accumulating an indefinitely long monologue from old
snapshots. Skipped system messages count toward the fetch bound. An oversized
prompt still fails rather than silently clipping it.

The character prompt and context instructions stay fixed. Memory and each labelled
Discord message have separate API-message boundaries; changing job details come
last in their own message. Follow-ups retain the unchanged history as an identical
message prefix, even when a speaker's chunk grows. Resets, edits, bridge retirement,
and memory changes can break that prefix. Usage logs report DeepSeek cache
hit/miss fields when the provider supplies them; savings are not guaranteed.

## Optional memory

Memory is disabled by default. Set `memory.enabled: true` to preload the channel's
memory on replies and enable automatic writing after quiet periods. Successful
participation or an archived edit/deletion makes that channel's memory pending.

- `quiet_minutes: 30`: wait for a quiet period after character participation or
  archived corrections. Another invocation postpones quiet time; unrelated channel
  traffic does not.
- `cooldown_hours: 4`: don't automatically rewrite more frequently than this.
- `max_wait_hours: 24`: force a pending update after this wait even if conversation
  never settles or the configured cooldown would otherwise delay it.
- `retry_minutes: 30`: delay retries after a failed writer call; keep the existing
  memory intact. Retry delay also applies after the maximum-wait deadline.

The scheduler checks once a minute, only while the bot is running, and only for
allowed channels. Pending age, activity, last successful writing time and failure
retry time persist across restarts. Overdue pending work is checked after connecting;
idle channels with no new material are never rewritten. No separate service starts.
Existing archives acquire timing state automatically; the former `daily_hour_utc`
setting is no longer used (the config loader rejects it). The manual refresh
command bypasses quiet/cooldown waits and records a new successful writing time.
Choose a writer model and its supported parameters separately under `memory`.

Scheduled updates **edit** the existing MEMORY.md. The writer receives the current
file plus what changed since the last update: new conversation, current versions
of edited earlier messages, deleted messages (to retract what depended on them),
and pending `remember`/`forget` requests. It edits with exact-match `replace` and
`append` tools, so lines it doesn't touch can't be lost. A channel without a
MEMORY.md gets a full first write instead.

A **full refresh** (`--memory-once`) rewrites MEMORY.md from scratch using the
current file, the whole deduplicated participation archive (messages the character
encountered in successful interactions, plus its answers), and every standing
memory request in date order. Use it to clear accumulated drift. Neither mode
receives unseen server conversation. The character's own prompt shapes the
memory's voice; shared writer instructions cover compact entries, attribution,
dates, and requests. There is no token-count instruction.

```bash
.venv/bin/python -m bevvycord --memory-once YOUR_CHANNEL_ID
```

This makes a writer API call when memory is enabled and that channel is allowed.
Run it while the bot process is stopped. It doesn't connect to Discord or launch
the bot. If the writer input exceeds `memory.max_archive_chars`, the run (scheduled
or manual) is skipped with an error, keeping the existing memory. Set this operational bound to fit your
chosen model; archives are never silently reduced to a recent-only slice. Output
token limits are provider generation settings, not instructions to the writer.

Local files:

```text
data/<character-id>/history.sqlite3
data/<character-id>/<channel-id>/MEMORY.md
data/<character-id>/<channel-id>/MEMORY.previous.md
data/<character-id>/<channel-id>/memory-provenance.json
```

SQLite stores successful interaction snapshots and current encountered messages.
Memory rewrites are atomic and retain one previous revision. Edit/delete events
update current archived message versions, and retained context is refreshed from
Discord before use. A deleted message's text is kept only until the next memory
update has seen it retracted, then cleared (immediately when memory is disabled).
A `remember` request whose source messages are all deleted is withdrawn. Historical
interaction snapshots still retain their original text in SQLite. Previously
written memory can retain obsolete information until the next update. Offline
deletions of messages no longer in the retained window cannot be detected without
a future archive reconciliation feature.

## Optional tool turns

Existing configurations remain text-only. Enable this per character:

```yaml
tools:
  enabled: true
  sandbox_enabled: true
  plugins: []
```

Onboarding offers this option for new characters. Linux file execution requires
Bubblewrap, util-linux (`prlimit`), and working unprivileged namespaces. System
Python, FFmpeg and ImageMagick are available if installed under `/usr`. The bot's
virtual environment and credentials are not exposed to model-written code.
There is no host-shell fallback or automatic package installation. A missing or
blocked backend becomes a tool error; ordinary replies can still finish.

The initial tools are `finish`, `exec`, `read_file`, `write_file`, `get_attachment`,
`return_file`, `open_job`, and, with memory enabled, `remember` and `forget`. They support ordinary
file/media/code work directly; no operation-specific media plugin is required.
For example, mention the character with an attached photo and ask it to resize it.
It can download the scoped attachment, run installed tools, inspect the output,
and stage a file with its final response. Give the Discord bot Attach Files
permission. Native model vision and automatic public file hosting are not included.
Final text and files are combined in one reply when they fit. Longer replies or
larger file batches continue as ordinary channel messages without repeating the
reply header.

A turn can use multiple model requests, with limits enforced by the host. Only
the final text and staged files are posted; intermediate responses, command output,
and private reasoning stay out of the channel transcript and memory archive.
DeepSeek tool exchanges preserve `reasoning_content` on replay as required by its
[thinking-mode protocol](https://api-docs.deepseek.com/guides/thinking_mode/).
Schemas have a stable order and the working transcript grows within the turn;
cache usage is logged on each request. Tools do not add permanent model chat history.

The example configuration gives reply and memory requests a `max_tokens` allowance
of 65,536, including reasoning. This is an upper bound, not a requested reply
length; the character prompt controls how much it says. Existing configurations
with explicit lower limits retain them until edited.

Tool errors are returned to the ongoing model turn so the character can adapt.
If a turn fails outside that path, an invoked character gets one additional,
text-only request to explain the problem in its own voice. Recovery uses the
last complete working exchange when it fits, otherwise the original conversation
or a bounded current-request context. Raw library errors are reduced to safe
categories; authentication failures skip retrying the same credentials. Recovery
does not dispatch tools, resend files or replay uncertain deliveries. Its output
has a separate delivery receipt; the failed job and original receipts remain for
inspection. A failed optional reaction can be explained while retaining the
already completed reply. Silent scheduled check-ins stay quiet on failure.
Only failed recovery falls back to the generic error notice. Recovery has a
separate timeout of at most 60 seconds and does not commit a failed turn's context
checkpoint. Its Discord text is visible to subsequent history reads.

`remember` ("Bevvy likes eating shoes") and `forget` ("Bevvy's home address") store
a short dated note with its requester and source message IDs, then queue a memory
update after the active job settles. They bypass automatic quiet/cooldown, but
respect failure backoff. Several requests coalesce into one update; they survive a
failed final reply. Each is applied once by the next update. Forget requests stay
on record so a full refresh doesn't re-learn the fact from the archive; a later
remember request can bring it back. Chat history is never altered. A tool receipt
says **queued**, not that the file has changed. Memory remains independently optional.

Mention the bot with exactly `status` or `cancel` to control a running job, or reply
with those words. These commands bypass the work queue; cancellation of requested
work is restricted to its requesting human. Authorized humans can also cancel
scheduled check-ins. Defaults allow two concurrent tool turns across
channels and three waiting invocations per channel. Work within a channel remains
serial. Shutdown cancels active and waiting work and terminates sandbox descendants.

Jobs record started/settled tool receipts and started/sent delivery receipts.
A crash marks abandoned active jobs interrupted on restart; commands and uploads
are never automatically replayed. A started delivery without a returned message ID
is uncertain: check Discord before sending it again. Local inspection needs no keys:

```bash
.venv/bin/python -m bevvycord --config characters/rowan.yaml --jobs
```

To continue, ask the character to open the job ID in a new turn. `open_job` copies
surviving workspace files and returns compact receipts, restricted to the same
requester, channel, and character. It does not replay private reasoning or old
actions. Completed, failed, cancelled and interrupted workspaces expire after
`retention_days` (default 1), checked at startup and hourly while the bot runs. Small database
receipts and memory requests remain. Conversation and MEMORY.md never enter
job cleanup.

See [runtime details](agent-runtime.md) for limits, file boundaries, and the
plugin interface. All tool settings and defaults are in [config.example.yaml](../config.example.yaml).

## Optional web plugin

A small included plugin uses an operator-configured SearXNG instance:

```yaml
tools:
  enabled: true
  sandbox_enabled: true
  plugins: [bevvycord.plugins.search]
```

Set `BEVVYCORD_SEARCH_URL=https://your-search-instance` in that character's local
environment file or process environment. Enable JSON results in SearXNG's
`search.formats` ([API documentation](https://docs.searxng.org/dev/search_api.html)).
This adds `web_search(query, category="general" | "images")` and `web_fetch(url)`.
Image searches include a direct `image_url` and the source page `url`. Fetch saves
the original page or file under the job's `web/` directory and returns its path,
content type and size. HTML pages also get a readable `.txt` file and a bounded
excerpt; larger text can be read with `read_file`. Documents and other binary
files can be processed with ordinary sandbox tools. To post an image, search,
fetch its `image_url`, then call `return_file` with the saved path.

Search results and fetched excerpts stay in private working context. Source links
in replies use `<https://example.com/page>` to suppress Discord embeds. No search
service starts automatically, and no particular external instance is assumed.
The plugin is separate from the network-disabled shell environment. Fetch uses
public HTTP(S) destinations only, pins validated DNS addresses at connection time,
and checks redirects. It ignores environment proxies and sends no bot/API
credentials. Downloads use existing file/workspace limits, a 60-second total
timeout (or the turn limit if shorter), and at most five redirects. Partial files
are removed on failure or cancellation. Sites requiring browser JavaScript, login,
or compression despite an identity request may be unavailable.

## Validation

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
# Opt-in real Linux namespace tests:
BEVVYCORD_SANDBOX_TESTS=1 .venv/bin/python -m pytest -q tests/test_sandbox.py
```

Tests cover continuity and memory scheduling, provider/Discord adapters, structured
tool replay, malformed arguments, duplicate/uncertain receipts, limits,
owner cancellation, bounded queues, scoped continuation, attachment downloads,
frozen artifacts, failed delivery, and plugin registration/search adapters.
Sandbox tests perform real file transformations and check credential/filesystem/
network isolation, output bounds, per-file limits, scratch storage monitoring,
and descendant cleanup. They include an attachment -> Python transformation ->
Discord adapter delivery -> memory -> next ordinary reply flow with a scripted model.

No real Discord account, paid model request, or external search backend is used by
these tests. Live Discord behavior, model tool selection, and actual DeepSeek cache
performance still need testing in an allowed testing channel.
