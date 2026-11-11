"""Несекретные параметры установки; подключения с токенами читает только MCP."""

import json
import os
from pathlib import Path

from network_policy import domain

KIT = Path(__file__).resolve().parents[1]
SERVICES = ('bitbucket', 'jira', 'confluence', 'git', 'github')


def load(path=None):
    path = Path(path) if path is not None else KIT / '.engineering-kit/local.json'
    if path.is_symlink():
        raise ValueError('Локальная конфигурация не должна быть симлинком')
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError('Локальная конфигурация недоступна или имеет неверный JSON; значения скрыты') from None
    if not isinstance(value, dict) or set(value) - {'ai_box', 'network_hosts', 'mcp_hosts'}:
        raise ValueError('Неизвестная структура локальной конфигурации; токены хранятся отдельно')
    if 'ai_box' in value and (not isinstance(value['ai_box'], str) or not Path(value['ai_box']).is_absolute()):
        raise ValueError('Путь зависимостей должен быть абсолютным')
    hosts = value.get('mcp_hosts', {})
    if not isinstance(hosts, dict) or set(hosts) - set(SERVICES):
        raise ValueError('Неизвестная структура хостов MCP')
    for items in [value.get('network_hosts', []), *hosts.values()]:
        if not isinstance(items, list) or any(not isinstance(item, str) or domain(item) != item for item in items):
            raise ValueError('Нужны списки точных DNS-имён')
    return value


def ai_box():
    return Path(os.environ.get('KIT_AI_BOX') or load().get('ai_box') or KIT / '.engineering-kit/dependencies/ai-box')


def network_hosts():
    return tuple(sorted({'github.com', 'api.github.com', *load().get('network_hosts', [])}))


def mcp_hosts(service):
    return load().get('mcp_hosts', {}).get(service, ['api.github.com'] if service == 'github' else
                                        ['github.com', 'api.github.com'] if service == 'git' else [])
