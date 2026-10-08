import asyncio
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import logging
import json
import re

import httpx

import discord

from .config import tool_settings
from .context import chunks, select_window, Window
from .jobs import Attachment, Job, clean_jobs
from .invite import invite_url
from .runtime import Runtime
from .tools import builtin_registry
from .discord_io import delivery_parts, is_trigger, normalize, reaction_snapshot, speaker_name
from .memory import update_memory
from .prompts import reply_messages
from .recovery import recover, TurnError, explanation

log = logging.getLogger(__name__)


class CharacterBot(discord.Client):
    def __init__(self, config, store, provider):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config, self.store, self.provider = config, store, provider
        self.locks = {channel: asyncio.Lock() for channel in config['allowed_channel_ids']}
        self.memory_task = None
        self.cleanup_task = None
        self.tool_settings = tool_settings(config)
        self.runtime = None
        self.active, self.waiting, self.tasks = {}, {}, set()
        self.stopping = False
        initiative = config.get('initiative', {})
        self.initiative_channels = set(initiative.get('channel_ids', ())) if initiative.get('enabled', False) else set()
        self.history_objects = {}
        self.history_limits = {}
        self.capacity = asyncio.Semaphore(self.tool_settings['max_parallel'])
        if self.tool_settings['enabled']:
            registry = builtin_registry(self.tool_settings, config.get('memory', {}).get('enabled', False))
            self.runtime = Runtime(provider, registry, self.tool_settings)
            store.recover_jobs()

    async def setup_hook(self):
        if self.runtime:
            self.cleanup_task = asyncio.create_task(self.cleanup_loop())
        if self.config.get('memory', {}).get('enabled', False) or self.initiative_channels:
            self.memory_task = asyncio.create_task(self.memory_loop())

    async def close(self):
        self.stopping = True
        pending = [task for task in (self.memory_task, self.cleanup_task, *self.tasks)
                   if task and task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await super().close()

    async def cleanup_loop(self):
        while not self.is_closed():
            clean_jobs(self.store, self.tool_settings)
            await asyncio.sleep(3600)

    async def on_ready(self):
        log.info('Connected as %s; allowed channels: %s', self.user, sorted(self.config['allowed_channel_ids']))
        log.info('Invite %s to your server: %s', self.user,
                 invite_url(self.application_id or self.user.id))

    async def history(self, trigger):
        settings = self.config['context']
        newest = [normalize(trigger)]
        objects = self.history_objects[trigger.channel.id] = {trigger.id: trigger}
        self.history_limits[trigger.channel.id] = False
        group_count = 1
        fetched = 1
        # One lookahead distinguishes an exhausted channel from a cut-off run.
        async for message in trigger.channel.history(before=trigger, limit=settings['max_fetch_messages']):
            fetched += 1
            if fetched > settings['max_fetch_messages']:
                self.history_limits[trigger.channel.id] = True
                break
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                continue
            item = normalize(message)
            objects[message.id] = message
            if item.speaker != newest[-1].speaker:
                group_count += 1
            newest.append(item)
            if group_count > settings['hard_chunks']:
                break
        # If the limit cut the oldest group, exclude that incomplete group.
        ordered = list(reversed(newest))
        if self.history_limits[trigger.channel.id] and settings['soft_chunks'] < group_count <= settings['hard_chunks']:
            ordered = [m for group in chunks(ordered)[1:] for m in group]
        return ordered

    async def refresh_previous(self, channel, previous, history):
        # Fresh channel history may include previously archived messages outside
        # the retained window. Update their current versions too.
        self.store.update_many(channel.id, history)
        if not previous:
            return None
        fresh = {m.id: m for m in history}
        for old in previous.messages:
            if old.id in fresh:
                self.store.update(channel.id, fresh[old.id])
                continue
            try:
                message = await channel.fetch_message(old.id)
            except discord.NotFound:
                self.store.delete(channel.id, old.id)
            else:
                self.store.update(channel.id, normalize(message))
                self.history_objects.setdefault(channel.id, {})[message.id] = message
        return self.store.previous(channel.id)

    def human_activity(self, channel_id, author, webhook_id=None, channel=None):
        return (channel_id in self.initiative_channels and not getattr(author, 'bot', True) and not webhook_id
                and not isinstance(channel, discord.Thread)
                and (not self.config['allowed_user_ids'] or author.id in self.config['allowed_user_ids']))

    def message_activity(self, message):
        text = re.sub(rf'<@!?{self.user.id}>', '', message.content).strip().lower()
        return (text not in ('status', 'cancel')
                and self.human_activity(message.channel.id, message.author, message.webhook_id, message.channel))

    async def refresh_reactions(self, channel, window):
        sources = self.history_objects.get(channel.id, {})
        for item in window.messages:
            source = sources.get(item.id)
            if source is None:
                try:
                    source = await channel.fetch_message(item.id)
                except discord.NotFound:
                    continue
            try:
                self.store.set_reactions(channel.id, item.id, await reaction_snapshot(source))
            except discord.HTTPException:
                log.warning('Could not refresh reaction attribution for message %s', item.id)

    async def on_message(self, message):
        cfg = self.config
        revision = None
        if message.channel.id in self.initiative_channels and not isinstance(message.channel, discord.Thread):
            if self.store.attention(message.channel.id) is None:
                self.store.baseline_attention(message.channel.id,
                                             message.id - 1 if self.message_activity(message) else message.id)
            revision = self.store.note_activity(message.channel.id, message.id,
                                                self.message_activity(message))
        accepted = is_trigger(message, self.user.id, cfg['allowed_channel_ids'], cfg['allowed_user_ids'], cfg.get('allow_dms', False))
        if not accepted:
            # Reply references may be uncached, especially after a restart. Resolve
            # only authorized humans' same-channel replies, never bot pings.
            eligible = (not message.author.bot and not message.webhook_id
                        and (not cfg['allowed_user_ids'] or message.author.id in cfg['allowed_user_ids'])
                        and ((message.guild is None and cfg.get('allow_dms', False))
                             or (message.guild is not None and not isinstance(message.channel, discord.Thread)
                                 and message.channel.id in cfg['allowed_channel_ids'])))
            reference_id = getattr(message.reference, 'message_id', None)
            if not eligible or not reference_id or getattr(message.reference, 'resolved', None) is not None:
                return
            try:
                referenced = await message.channel.fetch_message(reference_id)
            except discord.HTTPException:
                return
            if referenced.author.id != self.user.id:
                return
        if self.runtime and await self.control(message):
            return
        channel_id = message.channel.id
        if self.runtime and self.waiting.get(channel_id, 0) >= self.tool_settings['max_queue']:
            await self.notice(message, 'My work queue here is full. Please try again when it settles.')
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        self.waiting[channel_id] = self.waiting.get(channel_id, 0) + 1
        acquired = False
        try:
            async with self.locks.setdefault(channel_id, asyncio.Lock()):
                async with self.capacity:
                    self.waiting[channel_id] -= 1
                    acquired = True
                    await self.respond(message, activity_revision=revision)
        finally:
            if not acquired:
                self.waiting[channel_id] -= 1
            self.tasks.discard(task)

    async def notice(self, message, text):
        return await message.reply(content=text, mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())

    async def control(self, message):
        command = re.sub(rf'<@!?{self.user.id}>', '', message.content).strip().lower()
        if command not in ('status', 'cancel'):
            return False
        active = self.active.get(message.channel.id)
        if not active:
            waiting = self.waiting.get(message.channel.id, 0)
            await self.notice(message, f'No job is running here; {waiting} invocation(s) waiting.' if waiting else 'No job is running here.')
        elif command == 'status':
            job = active.get('job')
            label = job.id if job else 'starting'
            await self.notice(message, f'Job {label} is running; {self.waiting.get(message.channel.id, 0)} invocation(s) waiting.')
        elif not active.get('initiative') and active['actor'] != message.author.id:
            await self.notice(message, 'Only the person who requested this job can cancel it.')
        else:
            active['task'].cancel()
            await self.notice(message, 'Cancellation requested.')
        return True

    async def attachments(self, message, window):
        found = []
        for item in window.messages:
            if '[Attachment:' not in item.text:
                continue
            try:
                original = message if item.id == message.id else await message.channel.fetch_message(item.id)
            except discord.NotFound:
                continue
            for attachment in original.attachments:
                async def download(path, maximum, message_id=item.id, attachment_id=attachment.id):
                    # Fetch again to refresh signed CDN URLs and confirm availability.
                    source = await message.channel.fetch_message(message_id)
                    current = next((a for a in source.attachments if a.id == attachment_id), None)
                    if current is None:
                        raise ValueError('Attachment no longer exists')
                    if current.size > maximum:
                        raise ValueError('Attachment exceeds size limit')
                    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                        async with client.stream('GET', current.url) as response:
                            if response.status_code != 200:
                                raise RuntimeError('Discord attachment download failed')
                            total = 0
                            with path.open('xb') as handle:
                                async for data in response.aiter_bytes():
                                    total += len(data)
                                    if total > maximum:
                                        raise ValueError('Attachment exceeded actual download size limit')
                                    handle.write(data)
                found.append(Attachment(f'{item.id}:{attachment.id}', attachment.filename, attachment.size, download))
        return found

    async def respond(self, message, initiative=False, activity_revision=None):
        cfg, job, messages, reaction_error = self.config, None, None, None
        channel_id = message.channel.id
        self.store.note_invocation(channel_id)
        if self.runtime:
            self.active[channel_id] = {'actor': self.user.id if initiative else message.author.id,
                                       'task': asyncio.current_task(), 'job': None, 'initiative': initiative}
        try:
            async with asyncio.timeout(self.tool_settings['turn_seconds'] if self.runtime else None):
                async with (nullcontext() if initiative else message.channel.typing()):
                    history = await self.history(message)
                    previous = await self.refresh_previous(message.channel, self.store.previous(channel_id), history)
                    settings = cfg['context']
                    limited = self.history_limits.get(channel_id, False)
                    # Do not grow an indefinitely long single-speaker run by
                    # joining older snapshots back onto a bounded fetch.
                    window = select_window(history, None if limited else previous,
                                           settings['soft_chunks'], settings['hard_chunks'])
                    window.history_limited = limited
                    await self.refresh_reactions(message.channel, window)
                    memory = self.store.memory(channel_id) if cfg.get('memory', {}).get('enabled', False) else None
                    messages = reply_messages(cfg['character']['prompt'], self.user.id, window, memory)
                    reactions = self.store.reactions(channel_id, {m.id for m in window.messages})
                    if reactions:
                        messages.append({'role': 'user', 'content': '<reactions>\n' + reactions + '\n</reactions>'})
                    if sum(len(m['content']) for m in messages) > settings['max_prompt_chars']:
                        raise ValueError('Context exceeds context.max_prompt_chars; no conversation was silently truncated')
                    if self.runtime:
                        job = Job(self.store, channel_id, self.user.id if initiative else message.author.id,
                                  message.id, window, self.tool_settings,
                                  attachments=await self.attachments(message, window),
                                  upload_limit=getattr(message.guild, 'filesize_limit', 8 * 1024 * 1024), initiative=initiative)
                        self.active[channel_id]['job'] = job
                        result = await self.runtime.run(messages, job)
                        answer = result.answer
                    else:
                        answer = await self.provider.generate(messages)
                    current_trigger = await message.channel.fetch_message(message.id)
                    if initiative and activity_revision is not None and self.store.attention(channel_id)[0] > activity_revision:
                        raise ValueError('New human activity arrived during check-in; deferred to the next check')
                    # Compare content only: author names can legitimately differ
                    # between gateway and REST copies of the same message.
                    if replace(normalize(current_trigger), name='') != replace(normalize(message), name=''):
                        raise ValueError('The triggering message changed during generation; please invoke again')
                    decision = job.decision if job and job.decision else {'reply_to': None, 'reactions': []}
                    target = None if initiative else message
                    if decision['reply_to'] is not None:
                        target = await message.channel.fetch_message(decision['reply_to'])
                        original = next(m for m in window.messages if m.id == target.id)
                        if replace(normalize(target), name='') != replace(original, name=''):
                            raise ValueError('The reply target changed during generation')
                    reaction_targets = []
                    originals = {m.id: m for m in window.messages}
                    for item in decision['reactions']:
                        source = await message.channel.fetch_message(item['message_id'])
                        if replace(normalize(source), name='') != replace(originals[source.id], name=''):
                            raise ValueError('A reaction target changed during generation')
                        reaction_targets.append((source, item['emoji']))
                    if job:
                        self.store.job_state(job.id, 'delivering')
                    sent = []
                    artifacts_to_send = job.artifacts if job and (not job.decision or job.decision['send_files']) else ()
                    for piece, artifacts in delivery_parts(answer, artifacts_to_send):
                        if job:
                            self.store.delivery(job.id, len(sent))
                        with ExitStack() as opened:
                            files = []
                            for artifact in artifacts:
                                file = discord.File(str(artifact.path), filename=artifact.filename)
                                opened.callback(file.close)
                                files.append(file)
                            options = {'content': piece, 'allowed_mentions': discord.AllowedMentions.none()}
                            if files:
                                options['files'] = files
                            if not sent and target:
                                reply = await target.reply(**options, mention_author=False)
                            else:
                                reply = await message.channel.send(**options)
                        if job:
                            self.store.delivery(job.id, len(sent), reply.id)
                        sent.append(normalize(reply))
                    for index, (source, emoji) in enumerate(reaction_targets, len(sent)):
                        self.store.delivery(job.id, index, source.id, kind='reaction', emoji=emoji, state='started')
                        try:
                            await source.add_reaction(emoji)
                        except discord.HTTPException as exc:
                            # Usually an unknown emoji; the reply is already sent.
                            log.warning('Could not add reaction to message %s', source.id)
                            self.store.delivery(job.id, index, source.id, kind='reaction', emoji=emoji, state='failed')
                            reaction_error = TurnError(f'Discord could not add a reaction (HTTP {exc.status}, code {exc.code})')
                            continue
                        self.store.delivery(job.id, index, source.id, kind='reaction', emoji=emoji)
                        # Record our acknowledged action even before gateway delivery.
                        row = self.store.db.execute('SELECT data FROM reaction_state WHERE channel=? AND message_id=?',
                                                    (str(channel_id), source.id)).fetchone()
                        snapshot = json.loads(row[0]) if row else {'members': [], 'incomplete': False}
                        member = {'emoji': emoji, 'actor': self.user.id, 'name': speaker_name(self.user), 'bot': True, 'kind': 'normal'}
                        if not any(m['actor'] == self.user.id and m['emoji'] == emoji and m['kind'] == 'normal' for m in snapshot['members']):
                            snapshot['members'].append(member)
                            snapshot['members'].sort(key=lambda m: (m['emoji'], m['kind'], m['actor']))
                            self.store.set_reactions(channel_id, source.id, snapshot)
                    window.messages.extend(sent)
                    window.last_response_id = sent[-1].id if sent else (previous.last_response_id if previous else None)
                    window.last_seen_id = window.messages[-1].id
                    self.store.complete(channel_id, window, datetime.now(timezone.utc).isoformat())
                    if activity_revision is not None:
                        self.store.checked_attention(channel_id, activity_revision)
                        if not initiative:
                            self.store.scanned_attention(channel_id, message.id)
                    if job:
                        self.store.job_state(job.id, 'complete')
            if reaction_error and not initiative:
                recovered = await self.recover_failure(message, messages, job, reaction_error)
                if recovered:
                    window.messages.extend(recovered)
                    window.last_response_id = window.last_seen_id = recovered[-1].id
                    self.store.complete(channel_id, window, datetime.now(timezone.utc).isoformat())
        except asyncio.CancelledError:
            if job:
                self.store.job_state(job.id, 'cancelled')
            # Shutdown avoids additional network calls while closing.
            if not self.stopping and not initiative:
                try:
                    await self.notice(message, 'Stopped this job. Completed actions remain recorded.')
                except discord.HTTPException:
                    pass
            raise
        except Exception as exc:
            if job:
                # Library exceptions can embed secrets even in ValueError or
                # RuntimeError; use the same safe explanation as model recovery.
                detail = f'{type(exc).__name__}: {explanation(exc)[:300]}'
                self.store.job_state(job.id, 'failed', detail)
            log.error('Reply failed in channel %s (%s)', channel_id, type(exc).__name__)
            if not initiative:
                if not await self.recover_failure(message, messages, job, exc):
                    try:
                        detail = f' Job {job.id} remains available for inspection.' if job else ''
                        text = 'I couldn’t complete that reply. Please try again; check the bot log if it keeps happening.' + detail
                        if job and self.store.db.execute('SELECT 1 FROM deliveries WHERE job=? LIMIT 1', (job.id,)).fetchone():
                            await message.channel.send(content=text, allowed_mentions=discord.AllowedMentions.none())
                        else:
                            await self.notice(message, text)
                    except (discord.HTTPException, RuntimeError):
                        pass
            if isinstance(exc, (ValueError, RuntimeError)):
                log.error('%s', explanation(exc))
        finally:
            self.active.pop(channel_id, None)
            self.history_objects.pop(channel_id, None)
            self.history_limits.pop(channel_id, None)

    async def recover_failure(self, message, messages, job, error):
        """Explain a failed turn once, without reusing staged actions or files."""
        try:
            async with asyncio.timeout(min(60, self.tool_settings['turn_seconds'])):
                current = await message.channel.fetch_message(message.id)
                snapshot = replace(normalize(current), name='')
                changed = snapshot != replace(normalize(message), name='')
                if (not messages or changed or sum(len(m['content']) for m in messages)
                        > self.config['context']['max_prompt_chars']):
                    messages = reply_messages(self.config['character']['prompt'], self.user.id,
                                              Window([normalize(current)]))
                    if job:
                        job.recovery_messages = None
                if sum(len(m['content']) for m in messages) > self.config['context']['max_prompt_chars']:
                    return False
                async with message.channel.typing():
                    answer = await recover(self.provider, messages, job, error,
                                           self.tool_settings['max_working_chars'])
                # Edits/deletions during the recovery also invalidate its output.
                fresh = await message.channel.fetch_message(message.id)
                if replace(normalize(fresh), name='') != snapshot:
                    return False
                existing = self.store.db.execute('SELECT part,state FROM deliveries WHERE job=? ORDER BY part',
                                                (job.id,)).fetchall() if job else []
                part = max((p for p, _ in existing), default=-1) + 1
                # A started delivery may have reached Discord even if its ID
                # was lost. Continue plainly rather than issuing another reply.
                continuation = bool(existing)
                delivered = []
                for piece, _ in delivery_parts(answer):
                    if job:
                        self.store.delivery(job.id, part)
                    options = {'content': piece, 'allowed_mentions': discord.AllowedMentions.none()}
                    if continuation:
                        sent = await message.channel.send(**options)
                    else:
                        sent = await fresh.reply(**options, mention_author=False)
                    if job:
                        self.store.delivery(job.id, part, sent.id)
                    self.store.update(message.channel.id, normalize(sent))
                    delivered.append(normalize(sent))
                    continuation = True
                    part += 1
                return delivered
        except Exception as exc:
            log.warning('Character recovery failed in channel %s (%s)', message.channel.id, type(exc).__name__)
            return False

    async def on_raw_message_delete(self, payload):
        if payload.channel_id in self.locks:
            async with self.locks[payload.channel_id]:
                known = self.store.db.execute('SELECT 1 FROM messages WHERE channel=? AND id=?', (str(payload.channel_id), payload.message_id)).fetchone()
                if known:
                    self.store.delete(payload.channel_id, payload.message_id)

    async def on_raw_bulk_message_delete(self, payload):
        if payload.channel_id in self.locks:
            async with self.locks[payload.channel_id]:
                for message_id in payload.message_ids:
                    known = self.store.db.execute('SELECT 1 FROM messages WHERE channel=? AND id=?', (str(payload.channel_id), message_id)).fetchone()
                    if known:
                        self.store.delete(payload.channel_id, message_id)

    async def on_raw_message_edit(self, payload):
        # Update only messages already encountered; this is not a server-wide recorder.
        if payload.channel_id not in self.locks:
            return
        async with self.locks[payload.channel_id]:
            known = self.store.db.execute('SELECT 1 FROM messages WHERE channel=? AND id=?', (str(payload.channel_id), payload.message_id)).fetchone()
            if not known:
                return
            try:
                channel = self.get_channel(payload.channel_id) or await self.fetch_channel(payload.channel_id)
                source = await channel.fetch_message(payload.message_id)
                self.store.update(payload.channel_id, normalize(source))
                if self.human_activity(channel.id, source.author, source.webhook_id, channel):
                    self.store.note_activity(channel.id, source.id, event=True)
            except discord.NotFound:
                self.store.delete(payload.channel_id, payload.message_id)
            except discord.HTTPException:
                log.warning('Could not refresh edited message %s', payload.message_id)

    async def reaction_event(self, payload):
        channel_id = payload.channel_id
        if channel_id not in self.config['allowed_channel_ids']:
            return
        known = self.store.db.execute('SELECT 1 FROM messages WHERE channel=? AND id=?',
                                      (str(channel_id), payload.message_id)).fetchone()
        if not known and channel_id not in self.initiative_channels:
            return
        actor_id = getattr(payload, 'user_id', None)
        if actor_id and channel_id in self.initiative_channels:
            actor = getattr(payload, 'member', None) or self.get_user(actor_id)
            if actor is None:
                try:
                    actor = await self.fetch_user(actor_id)
                except discord.HTTPException:
                    actor = None
            if actor and self.human_activity(channel_id, actor):
                self.store.note_activity(channel_id, payload.message_id, event=True)
        async with self.locks.setdefault(channel_id, asyncio.Lock()):
            try:
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                if isinstance(channel, discord.Thread):
                    return
                source = await channel.fetch_message(payload.message_id)
                self.store.update(channel_id, normalize(source))
                self.store.set_reactions(channel_id, source.id, await reaction_snapshot(source))
            except discord.NotFound:
                self.store.delete(channel_id, payload.message_id)
            except discord.HTTPException:
                log.warning('Could not refresh reactions for message %s', payload.message_id)

    async def on_raw_reaction_add(self, payload):
        await self.reaction_event(payload)

    async def on_raw_reaction_remove(self, payload):
        await self.reaction_event(payload)

    async def on_raw_reaction_clear(self, payload):
        await self.reaction_event(payload)

    async def on_raw_reaction_clear_emoji(self, payload):
        await self.reaction_event(payload)

    async def initiative_tick(self):
        interval = self.config.get('initiative', {}).get('interval_minutes', 30) * 60
        for channel_id in sorted(self.initiative_channels):
            row = self.store.attention(channel_id)
            if row and self.store.clock() < row[2] + interval:
                continue
            lock = self.locks.setdefault(channel_id, asyncio.Lock())
            if lock.locked() or self.waiting.get(channel_id, 0) or self.capacity.locked():
                continue
            async with lock, self.capacity:
                try:
                    channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                    if isinstance(channel, discord.Thread):
                        continue
                    latest = None
                    async for source in channel.history(limit=self.config['context']['max_fetch_messages']):
                        if source.type in (discord.MessageType.default, discord.MessageType.reply):
                            latest = source
                            break
                    if not latest:
                        self.store.baseline_attention(channel_id)
                        self.store.checked_attention(channel_id, row[0] if row else 0)
                        continue
                    if row is None:
                        # First enable starts here; it does not revive old chat.
                        self.store.baseline_attention(channel_id, latest.id)
                        continue
                    if latest.id > row[3]:
                        observed = []
                        async for source in channel.history(after=discord.Object(id=row[3]),
                                                            oldest_first=False,
                                                            limit=self.config['context']['max_fetch_messages']):
                            observed.append(source)
                        for source in reversed(observed):
                            self.store.note_activity(channel_id, source.id,
                                source.type in (discord.MessageType.default, discord.MessageType.reply)
                                and self.message_activity(source))
                        readable = [source for source in observed if source.type in (discord.MessageType.default, discord.MessageType.reply)]
                        if readable:
                            latest = max([latest, *readable], key=lambda source: source.id)
                        self.store.scanned_attention(channel_id, max([latest.id, *(source.id for source in observed)]))
                    revision, seen, _, _ = self.store.attention(channel_id)
                    # Consume this batch even if work fails, avoiding replay of
                    # potentially delivered actions. New activity stays pending.
                    self.store.checked_attention(channel_id, revision)
                    if revision <= seen:
                        continue
                    task = asyncio.create_task(self.respond(latest, initiative=True, activity_revision=revision))
                    self.tasks.add(task)
                    try:
                        await task
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    finally:
                        self.tasks.discard(task)
                except discord.HTTPException:
                    log.warning('Could not check initiative channel %s', channel_id)

    async def memory_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            await self.initiative_tick()
            settings = self.config['memory']
            for channel in self.store.due_memory_channels(settings) if settings.get('enabled', False) else ():
                if int(channel) not in self.config['allowed_channel_ids']:
                    continue
                async with self.locks.setdefault(int(channel), asyncio.Lock()):
                    # Participation while waiting for the lock may postpone it.
                    if channel not in self.store.due_memory_channels(settings):
                        continue
                    try:
                        await update_memory(self.store, self.provider, self.config, channel)
                        log.info('Memory updated for channel %s', channel)
                    except Exception as exc:
                        self.store.memory_failed(channel, settings.get('retry_minutes', 30))
                        log.error('Memory update failed for channel %s (%s)', channel, type(exc).__name__)
                        if isinstance(exc, (ValueError, RuntimeError)):
                            log.error('%s', exc)
            await asyncio.sleep(60)
