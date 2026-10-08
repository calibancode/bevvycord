"""Discord bot join links; application IDs are public and need no API calls."""
import re
from urllib.parse import urlencode

# Names match discord.Permissions and the documented Discord permission flags.
REQUIRED_PERMISSIONS = {
    'add_reactions': 1 << 6,
    'view_channel': 1 << 10,
    'send_messages': 1 << 11,
    'embed_links': 1 << 14,
    'attach_files': 1 << 15,
    'read_message_history': 1 << 16,
}
PERMISSIONS = sum(REQUIRED_PERMISSIONS.values())


def invite_url(application_id):
    if isinstance(application_id, bool) or not re.fullmatch(r'[0-9]+', str(application_id)) or int(application_id) < 1:
        raise ValueError('Discord application ID must be a positive numeric ID')
    return 'https://discord.com/oauth2/authorize?' + urlencode({
        'client_id': str(int(application_id)), 'permissions': PERMISSIONS, 'scope': 'bot',
    })
