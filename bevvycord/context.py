from dataclasses import asdict, dataclass, field
import json


@dataclass(frozen=True)
class Message:
    id: int
    author_id: int
    name: str
    timestamp: str
    text: str
    bot: bool = False
    reply_to: int | None = None
    # Webhooks can impersonate distinct characters through one webhook ID.
    webhook_id: int | None = None

    @property
    def speaker(self):
        return (self.author_id, self.name if self.webhook_id else None)


@dataclass
class Window:
    messages: list[Message] = field(default_factory=list)
    gap_before: int | None = None
    last_response_id: int | None = None
    last_seen_id: int | None = None

    def dumps(self):
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def loads(cls, text):
        data = json.loads(text)
        data['messages'] = [Message(**m) for m in data['messages']]
        return cls(**data)


def chunks(messages, gap_before=None):
    result = []
    for message in messages:
        if not result or result[-1][-1].speaker != message.speaker or message.id == gap_before:
            result.append([])
        result[-1].append(message)
    return result


def tail(messages, count, gap_before=None):
    return [m for group in chunks(messages, gap_before)[-count:] for m in group]


def select_window(history, previous=None, soft=20, hard=40):
    """History ends at the trigger; previous includes our last completed answer.

    Grow a stable prefix until hard. On an absence, retain one old soft window
    and one recent soft window. Further growth retires the bridge at hard.
    """
    if not 1 <= soft or hard < 2 * soft:
        raise ValueError('hard_chunks must be at least twice soft_chunks')
    history = sorted({m.id: m for m in history}.values(), key=lambda m: m.id)
    if not previous or not previous.messages:
        return Window(tail(history, soft))
    # A trigger queued while generating must not acquire a reply posted after it.
    anchor = previous.last_seen_id or previous.last_response_id
    if history and anchor and anchor > history[-1].id:
        return Window(tail(history, soft))
    latest = {m.id: m for m in history}
    prior = [latest.get(m.id, m) for m in previous.messages]
    last = anchor
    if last in latest:
        addition = [m for m in history if m.id > last]
        # Messages posted while we generated our last answer precede that answer,
        # but weren't in its input snapshot. Recover them from channel history.
        if previous.gap_before:
            recovered = [m for m in history if m.id >= previous.gap_before]
        else:
            recovered = [m for m in history if prior and m.id >= prior[0].id]
        combined = sorted({m.id: m for m in prior + recovered}.values(), key=lambda m: m.id)
        if len(chunks(combined, previous.gap_before)) <= hard:
            return Window(combined, previous.gap_before)
        if len(chunks(addition)) <= soft:
            if previous.gap_before:
                recent = [m for m in combined if m.id >= previous.gap_before]
                room = hard - len(chunks(recent))
                if room > 0:
                    older = [m for m in combined if m.id < previous.gap_before]
                    return Window(tail(older, room) + recent, previous.gap_before)
            return Window(tail(history, soft))
    old = tail(prior, soft, previous.gap_before)
    recent = tail(history, soft)
    # If we can recover uninterrupted context, preserve it rather than invent a gap.
    if old and old[0].id in latest:
        continuous = [m for m in history if m.id >= old[0].id]
        if len(chunks(continuous)) <= hard:
            return Window(continuous)
    old = [m for m in old if recent and m.id < recent[0].id]
    # A subsequent absence keeps just one old window, never nested gap notes.
    return Window(old + recent, recent[0].id if old and recent else None)


def transcript(window):
    lines = []
    for group in chunks(window.messages, window.gap_before):
        first = group[0]
        if first.id == window.gap_before:
            lines.append('[Context note: Conversation continued after your last reply. '
                         'Some intervening messages are omitted; the recent conversation follows.]')
        kind = 'bot' if first.bot else 'user'
        # JSON quoting prevents newlines in display names from becoming headers.
        lines.append(f'{json.dumps(first.name, ensure_ascii=False)} ({kind}:{first.author_id})')
        for message in group:
            reply = f' reply-to:{message.reply_to}' if message.reply_to else ''
            lines.append(f'[{message.timestamp} message:{message.id}{reply}]')
            # Indentation makes message content distinct from generated context notes.
            lines.extend('  ' + line for line in message.text.splitlines())
        lines.append('')
    return '\n'.join(lines).rstrip()
