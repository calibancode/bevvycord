# Bevvycord

A lean agentic harness for Discord character bots, with shared chat context,
memory, and tools. Built around DeepSeek and inspired by
[llmcord](https://github.com/jakobdylanc/llmcord).

- **Shared conversation:** characters see other people's and bots' messages,
  with speaker labels, timestamps, reply references, and attributed reactions.
- **Continuity:** a rolling context uses 20 speaker chunks by default, growing
  to 40 and bridging longer absences with part of the previous conversation.
- **Optional memory:** characters keep a local `MEMORY.md` per channel, updating
  it after quiet periods or explicit remember/forget requests.
- **Personal library:** deliberate file saves survive job expiry, with a small
  recent-file snapshot and personal or channel scope. Separate from conversational memory.
- **Optional tools:** sandboxed code, attachment processing, returned files,
  and web search/fetch through an included SearXNG plugin.
- **Optional check-ins:** characters can speak, react, or stay quiet without a
  ping. No new human activity means no check-in model request.

One bot account per character; one checkout for all of them. A supervisor can run
multiple characters with shared channel turns. Prompts, credentials, conversation
history, and memory remain separate for each character.

## Setup

Requires **Python 3.11+**. Sandbox execution also requires Linux, Bubblewrap,
and util-linux (`prlimit`).

```bash
git clone https://github.com/calibancode/bevvycord.git
cd bevvycord
./onboard.sh
```

Onboarding prepares Python, asks for your character prompt, keys and allowed
channel IDs, and offers memory, tools and check-ins. It saves credentials locally
with restricted permissions and prints an invite if you provide the application
ID. It does not start the bot or call DeepSeek.

Create the Discord application and enable **Message Content Intent** in the
[developer portal](https://discord.com/developers/applications). The generated
invite requests the permissions used by chat, history, uploads and reactions;
check channel overrides too.

Then run your character:

```bash
./bin/run-rowan --check-config
./bin/run-rowan
```

Mention it or reply to its message. Bots and webhooks are visible in context but
cannot invoke it. Ctrl+C shuts down cleanly. Run onboarding again for another
character and launch it in a separate terminal.

Each turn prints a concise activity summary: tools used, messages/reactions sent,
private work or silence, and token/cache usage. Inspect local history without
credentials or network calls:

```bash
./bin/run-rowan --activity
./bin/run-rowan --activity --job FULL_JOB_ID
./bin/run-rowan --activity --memory-diffs
```

Memory diffs are shown only when requested. Activity stays in SQLite after job
files expire; older jobs have unknown origin and token usage.

## Multiple characters

Run characters together to coordinate their conversation turns:

```yaml
# supervisor.yaml
pause_seconds: 15
characters:
  - config: characters/faust.yaml
    enabled: true
  - config: characters/scooter.yaml
    enabled: true
```

```bash
python -m bevvycord --supervisor supervisor.yaml --check-config
python -m bevvycord --supervisor supervisor.yaml
```

Use the project's virtual-environment Python. Config paths are relative to the
manifest. Set `enabled: false` to leave a character out, then restart the supervisor.
Stop the separate character launchers before switching; existing ownership locks
prevent duplicate instances from sharing character storage.

Characters take turns per channel, with direct invocations ahead of waiting
check-ins. After a message, optional check-ins wait `pause_seconds`, then fetch
fresh context and decide whether to contribute. Each character's next check is
scheduled from its own completion, so their intervals naturally spread apart.
Silent checks need no channel pause. Bot messages still cannot wake another bot.
Different channels remain independent. A failed character stops independently;
restart the supervisor to retry it. Memory updates use a separate scheduler.

## Configuration

Edit `characters/<name>.yaml` and restart. The reply model defaults to
`deepseek-flash`; `deepseek-v4-pro` is also supported. Memory, tools, check-ins,
and DMs are off by default. Check-ins need an explicit channel list, so testing
can stay ping-only.

For web search, enable `bevvycord.plugins.search` under `tools.plugins` and set
`BEVVYCORD_SEARCH_URL` in the character's env file. SearXNG must allow JSON
results. No search service is installed automatically.

Generated character configs, credentials, environments and local state are
ignored by Git. Replies post as completed text and files; private tool traces and
reasoning stay out of Discord. Threads, native model vision and streaming edits
are not implemented.

## More

- [Configuration and behavior guide](docs/guide.md): manual setup, context,
  memory timing, check-ins, web integration, and testing.
- [Example configuration](config.example.yaml): all defaults and limits.
- [Tool runtime](docs/agent-runtime.md): sandbox boundaries, receipts and plugins.
- [Design notes](docs/agent-runtime-design.md): architecture and reference projects.

[MIT licensed](LICENSE).
