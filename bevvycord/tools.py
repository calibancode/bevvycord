"""Small tool registry and trusted, explicitly enabled Python plugin interface."""
from dataclasses import dataclass
import importlib
from jsonschema import Draft202012Validator


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: object


class Registry:
    def __init__(self):
        self.tools = {}

    def add(self, name, description, schema, handler):
        if (not name or len(name) > 64 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in name)
                or name in self.tools or not callable(handler)):
            raise ValueError('Invalid or duplicate tool registration')
        if schema.get('type') != 'object':
            raise ValueError('Tool arguments must be an object')
        Draft202012Validator.check_schema(schema)
        self.tools[name] = Tool(name, description, schema, handler)

    def definitions(self):
        return [{'type': 'function', 'function': {'name': tool.name, 'description': tool.description,
                                                'parameters': tool.schema}} for tool in self.tools.values()]

    async def call(self, name, arguments, context):
        if name not in self.tools:
            return {'error': 'Unknown tool'}
        tool = self.tools[name]
        if not Draft202012Validator(tool.schema).is_valid(arguments):
            return {'error': 'Arguments do not match the tool schema'}
        try:
            return await tool.handler(context, **arguments)
        except (ValueError, RuntimeError) as exc:
            return {'error': str(exc)}
        except Exception as exc:
            # Plugins/download clients may embed credentials in exceptions.
            return {'error': f'Tool failed ({type(exc).__name__})'}

    def load_plugins(self, modules):
        for module in modules:
            importlib.import_module(module).register(self)


def arguments(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


async def execute(context, command, timeout_seconds=None):
    return await context.execute(command, timeout_seconds)


async def read_file(context, path, offset_bytes=0):
    return context.read_file(path, offset_bytes)


async def write_file(context, path, content):
    return context.write_file(path, content)


async def get_attachment(context, attachment_id):
    return await context.get_attachment(attachment_id)


async def return_file(context, path, filename=None):
    return context.return_file(path, filename)


async def remember(context, note, message_ids=None):
    return context.memory_note('remember', note, message_ids)


async def forget(context, note, message_ids=None):
    return context.memory_note('forget', note, message_ids)


async def open_job(context, job_id):
    return context.open_job(job_id)


async def finish(context, text=None, reply_to=None, reactions=()):
    return context.finish(text, reply_to, reactions)


def builtin_registry(settings, memory_enabled):
    registry = Registry()
    text = {'type': 'string', 'minLength': 1}
    registry.add('finish', 'Finish this turn. text sends your Discord message and staged files (empty text sends files only); '
                 'reply_to optionally selects a conversation message. '
                 'reactions add emoji to conversation messages. Omit text to only react; omit both to stay quiet. '
                 'This must be the last call in a batch.',
                 arguments({'text': {'type': 'string', 'maxLength': settings['max_working_chars']},
                            'reply_to': {'type': 'integer', 'minimum': 1},
                            'reactions': {'type': 'array', 'maxItems': 10, 'items': arguments({
                                'message_id': {'type': 'integer', 'minimum': 1},
                                'emoji': {**text, 'maxLength': 100}}, ['message_id', 'emoji'])}}), finish)
    if settings['sandbox_enabled']:
        registry.add('exec', 'Run shell/Python/system tools in the isolated /workspace. No network: curl/wget/pip fail; use web tools, if offered, to download. Output is bounded.',
                     arguments({'command': {**text, 'maxLength': 20000},
                                'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': settings['exec_seconds']}}, ['command']), execute)
        registry.add('read_file', 'Read a UTF-8 workspace file or an execution log handle (bounded output). Offset is in bytes.',
                     arguments({'path': text, 'offset_bytes': {'type': 'integer', 'minimum': 0}}, ['path']), read_file)
        registry.add('write_file', 'Write a UTF-8 workspace file.', arguments({'path': text, 'content': {'type': 'string', 'maxLength': settings['file_bytes']}}, ['path', 'content']), write_file)
        registry.add('get_attachment', 'Download one attachment listed in this conversation into the workspace.',
                     arguments({'attachment_id': text}, ['attachment_id']), get_attachment)
        registry.add('return_file', 'Freeze a workspace file for delivery with your final Discord reply.',
                     arguments({'path': text, 'filename': {**text, 'maxLength': 100}}, ['path']), return_file)
        registry.add('open_job', 'Copy a surviving previous job workspace into this job, with compact receipts. Same channel and requester only; no action is replayed.',
                     arguments({'job_id': {**text, 'pattern': '^[0-9a-f]{32}$'}}, ['job_id']), open_job)
    if memory_enabled:
        sources = {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 20, 'uniqueItems': True}
        registry.add('remember', 'Save something to your long-term memory, e.g. "Bevvy likes eating shoes". Use when asked to remember '
                     'something or when it matters to you. message_ids are the messages it comes from (default: the message you are '
                     'answering); if those are all deleted, the request is withdrawn. Applied at your next memory update, not instantly.',
                     arguments({'note': {**text, 'maxLength': 1000}, 'message_ids': sources}, ['note']), remember)
        registry.add('forget', 'Ask your next memory update to remove something from your long-term memory. Use whenever someone asks you '
                     'to forget something: saying you forgot without calling this changes nothing. Name the topic, not its details '
                     '(e.g. "Bevvy\'s home address"). Chat history is unchanged; only your memory file is edited.',
                     arguments({'note': {**text, 'maxLength': 1000}, 'message_ids': sources}, ['note']), forget)
    registry.load_plugins(settings['plugins'])
    return registry
