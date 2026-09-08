import copy
import json
import re
import secrets
import urllib.error
import urllib.request

API = 'https://api.telegram.org'
LOCAL_API = 'http://127.0.0.1:8090'


def call(token, method, **data):
    request = urllib.request.Request(f'{API}/bot{token}/{method}', data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(f'Telegram {method} failed (HTTP {error.code}); check the bot token and group permissions.') from None
    except (OSError, ValueError):
        raise ValueError(f'Telegram {method} request failed; check network access and retry.') from None
    if not payload.get('ok'):
        raise ValueError(f'Telegram {method} failed; check the bot token and group permissions.')
    return payload['result']


def preflight(token, chat_id=None, user_id=None, bot_id=None):
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('a Telegram bot token is required; supply ATTOSYS_TELEGRAM_BOT_TOKEN')
    me = call(token, 'getMe')
    if bot_id not in (None, me['id']):
        raise ValueError('this token belongs to a different bot than the saved company')
    if not me.get('can_join_groups'):
        raise ValueError('Telegram pre-start check: enable groups for this bot in BotFather (/setjoingroups).')
    if not me.get('can_read_all_group_messages'):
        raise ValueError('Telegram pre-start check: disable bot privacy in BotFather (/setprivacy).')
    if call(token, 'getWebhookInfo').get('url'):
        raise ValueError('Telegram pre-start check: this bot has an active webhook; disconnect its existing integration before using polling.')
    if chat_id is None:
        chats = {}
        for update in call(token, 'getUpdates', timeout=0):
            event = update.get('my_chat_member') or update.get('message') or {}
            chat = event.get('chat') or {}
            if chat.get('type') in ('group', 'supergroup'):
                chats[str(chat['id'])] = chat
        if not chats:
            raise ValueError('Telegram pre-start check: no group found. Create a group with Topics, add the bot as admin with Manage Topics, and send a group message; or supply --telegram-chat-id.')
        if len(chats) != 1:
            raise ValueError('Telegram pre-start check: multiple groups found; select one with --telegram-chat-id.')
        chat_id = next(iter(chats))
    chat_id = str(chat_id)
    if not re.fullmatch(r'-[1-9][0-9]*', chat_id):
        raise ValueError('Telegram pre-start check: the group ID must be a negative number.')
    chat = call(token, 'getChat', chat_id=chat_id)
    if str(chat['id']) != chat_id or chat.get('type') != 'supergroup' or not chat.get('is_forum'):
        raise ValueError('Telegram pre-start check: use a supergroup with Topics enabled.')
    member = call(token, 'getChatMember', chat_id=chat_id, user_id=me['id'])
    if member.get('status') != 'creator' and not (member.get('status') == 'administrator' and member.get('can_manage_topics')):
        raise ValueError('Telegram pre-start check: add the bot as a group admin with Manage Topics permission.')
    if user_id is None:
        admins = call(token, 'getChatAdministrators', chat_id=chat_id)
        owners = [member['user']['id'] for member in admins if member.get('status') == 'creator']
        if len(owners) != 1:
            raise ValueError('Telegram pre-start check: cannot identify the group owner; supply --telegram-user-id for the CEO.')
        user_id = owners[0]
    if not re.fullmatch(r'[1-9][0-9]*', str(user_id)):
        raise ValueError('Telegram pre-start check: the CEO user ID must be a positive number.')
    return {'telegram_chat_id': chat_id, 'telegram_user_id': int(user_id),
            'telegram_bot_id': me['id'], 'telegram_bot_username': me['username']}


def configure(root, company, options, write, existing=False):
    import yaml
    remote = company.get('telegram_api_base', API) != LOCAL_API if existing else bool(options.get('telegram'))
    if existing and options.get('telegram') and not remote:
        raise ValueError('this is a saved local-chat company; use a new --name for Telegram')
    credentials_path = root / 'secrets.yaml'
    credentials = yaml.safe_load(credentials_path.read_text()) if credentials_path.exists() else {}
    if not remote:
        if not existing:
            company.update(telegram_chat_id='-1001', telegram_api_base=LOCAL_API)
            write(root / 'company.yaml', yaml.safe_dump(company, sort_keys=False))
        if not credentials:
            write(credentials_path, yaml.safe_dump({'telegram_bot_token': secrets.token_hex(24), 'api_key': ''}), 0o600)
        return
    candidate = copy.deepcopy(company)
    for option, saved in (('telegram_chat_id', company.get('telegram_chat_id')),
                          ('telegram_user_id', company['ceo'].get('telegram_user_id'))):
        supplied = options.get(option)
        if existing and supplied is not None and str(supplied) != str(saved):
            raise ValueError(f'{option} differs from the saved company; use a new --name')
    chat_id = company.get('telegram_chat_id') if existing else options.get('telegram_chat_id')
    user_id = company['ceo'].get('telegram_user_id') if existing else options.get('telegram_user_id')
    token = options.get('telegram_bot_token') or credentials.get('telegram_bot_token') or ''
    checked = preflight(token, chat_id, user_id, company.get('telegram_bot_id'))
    chat_id = checked['telegram_chat_id']
    candidate.update(telegram_api_base=API, telegram_chat_id=chat_id, telegram_bot_id=checked['telegram_bot_id'])
    candidate['ceo']['telegram_user_id'] = checked['telegram_user_id']
    credentials['telegram_bot_token'] = token
    credentials.setdefault('api_key', '')
    write(credentials_path, yaml.safe_dump(credentials), 0o600)
    company.clear()
    company.update(candidate)
    write(root / 'company.yaml', yaml.safe_dump(company, sort_keys=False))
    for role, spec in company['agents'].items():
        if spec.get('topic_id') is None:
            topic = call(token, 'createForumTopic', chat_id=chat_id, name=f"{company['org']}-{role}")
            spec['topic_id'] = int(topic['message_thread_id'])
            write(root / 'company.yaml', yaml.safe_dump(company, sort_keys=False))
    print(f"Telegram ready: @{checked['telegram_bot_username']}, group {chat_id}, {len(company['agents'])} employee topics.", flush=True)
