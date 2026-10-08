import discord

from .context import Message


def speaker_name(author):
    # REST fetches lack guild member data (no members intent), so server
    # nicknames appear only on live gateway messages. Use the account-level
    # name everywhere so one speaker keeps one name across the transcript.
    return getattr(author, 'global_name', None) or getattr(author, 'name', None) or getattr(author, 'display_name', str(author.id))


def component_text(components):
    result = []
    for component in components:
        content = getattr(component, 'content', None)
        if isinstance(content, str) and content:
            result.append(content)
        children = getattr(component, 'children', None) or getattr(component, 'components', None) or []
        result.extend(component_text(children))
    return result


def normalize(message):
    parts = [message.content] if message.content else []
    parts.extend(component_text(getattr(message, 'components', [])))
    for embed in message.embeds:
        # Rich bot embeds carry answers. Ordinary link previews duplicate content.
        if embed.type != 'rich':
            continue
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f'{field.name}: {field.value}')
    for attachment in message.attachments:
        parts.append(f'[Attachment: {attachment.filename}; content not read]')
    for sticker in getattr(message, 'stickers', []):
        parts.append(f'[Sticker: {sticker.name}]')
    if not parts:
        parts.append('[No readable text content]')
    return Message(
        id=message.id, author_id=message.author.id,
        name=speaker_name(message.author), timestamp=message.created_at.isoformat(),
        text='\n'.join(dict.fromkeys(parts)),
        bot=message.author.bot or bool(message.webhook_id),
        reply_to=getattr(message.reference, 'message_id', None), webhook_id=message.webhook_id,
        username=None if message.webhook_id else getattr(message.author, 'name', None),
    )


async def reaction_snapshot(message):
    """Attribute normal/burst reactions, bounded to 100 reactors per message."""
    members, incomplete = [], False
    reactions = sorted(getattr(message, 'reactions', []), key=lambda r: str(r.emoji))
    if len(reactions) > 20:
        incomplete = True
    for reaction in reactions[:20]:
        for kind, count, reaction_type in (
            ('normal', getattr(reaction, 'normal_count', reaction.count), discord.ReactionType.normal),
            ('burst', getattr(reaction, 'burst_count', 0), discord.ReactionType.burst),
        ):
            if not count:
                continue
            limit = 100 - len(members)
            if limit <= 0:
                incomplete = True
                continue
            found = 0
            async for user in reaction.users(limit=limit, type=reaction_type):
                members.append({'emoji': str(reaction.emoji), 'actor': user.id,
                                'name': speaker_name(user)[:100], 'bot': user.bot, 'kind': kind})
                found += 1
            incomplete |= count > found
    members.sort(key=lambda item: (item['emoji'], item['kind'], item['actor']))
    return {'members': members, 'incomplete': incomplete}


def split_answer(text, limit=2000):
    """Never drop characters, including single words longer than Discord's limit."""
    pieces = []
    while len(text) > limit:
        boundary = text.rfind('\n', 0, limit + 1)
        if boundary < limit // 2:
            boundary = text.rfind(' ', 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        pieces.append(text[:boundary])
        text = text[boundary:]
    if text:
        pieces.append(text)
    return pieces


def delivery_parts(answer, artifacts=()):
    """Pair text with file batches; leave room below Discord's 25 MiB request cap."""
    batches, batch, size = [], [], 0
    maximum = 24 * 1024 * 1024
    for artifact in artifacts:
        if artifact.size > maximum:
            raise ValueError('Returned file exceeds the Discord request size limit')
        if batch and (len(batch) == 10 or size + artifact.size > maximum):
            batches.append(batch)
            batch, size = [], 0
        batch.append(artifact)
        size += artifact.size
    if batch:
        batches.append(batch)
    pieces = split_answer(answer)
    return [(pieces[i] if i < len(pieces) else '', batches[i] if i < len(batches) else [])
            for i in range(max(len(pieces), len(batches)))]


def is_trigger(message, bot_id, allowed_channels, allowed_users, allow_dms=False):
    if message.author.bot or message.webhook_id:
        return False
    if allowed_users and message.author.id not in allowed_users:
        return False
    if message.guild is None:
        return allow_dms
    # Threads are unsupported, even if a thread ID is listed.
    if isinstance(message.channel, discord.Thread) or message.channel.id not in allowed_channels:
        return False
    if any(user.id == bot_id for user in message.mentions):
        return True
    reference = getattr(message.reference, 'resolved', None)
    return getattr(getattr(reference, 'author', None), 'id', None) == bot_id
