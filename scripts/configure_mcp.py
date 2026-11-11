#!/usr/bin/env python3
"""Регистрация MCP kit без чтения connections.env и копирования секретов."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
import local_settings


SERVICES = ("bitbucket", "github", "jira", "confluence", "git")
KIT = Path(__file__).resolve().parents[1]
CONTEXT7_URL = "https://mcp.context7.com/mcp"


def server_config(service, ai_box, node):
    return {"command": node, "args": [str(KIT / "scripts/mcp-server.mjs"), service, "--ai-box", str(ai_box)],
            "enabled": service != "git", "startup_timeout_sec": 30, "tool_timeout_sec": 60,
            "default_tools_approval_mode": "writes"}


def context7_config():
    return {"url": CONTEXT7_URL, "enabled_tools": ["resolve-library-id", "query-docs"],
            "startup_timeout_sec": 30, "tool_timeout_sec": 60, "default_tools_approval_mode": "writes"}


def configure(config_path, ai_box, node, with_context7=False):
    original = config_path.read_text() if config_path.exists() else ""
    parsed = tomllib.loads(original)
    servers = parsed.get("mcp_servers", {})
    added = []
    blocks = []
    entries = {f"kit-{service}": server_config(service, ai_box, node) for service in SERVICES}
    if with_context7:
        entries["context7"] = context7_config()
    for name, expected in entries.items():
        if name in servers:
            if servers[name] != expected:
                raise ValueError(f"Существующая запись {name} отличается; автоматическая перезапись запрещена")
            continue
        blocks.append(f"\n[mcp_servers.{name}]\n" + "\n".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in expected.items()
        ) + "\n")
        added.append(name)
    if blocks:
        updated = original + "\n" + "".join(blocks)
        tomllib.loads(updated)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Не создаём резервную копию: чужая конфигурация могла содержать секреты.
        with config_path.open("a") as stream:
            stream.write(updated[len(original):])
    return {"registered": list(entries), "added": added,
            "secrets_in_config": False, "restart_session": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser.add_argument("--config", type=Path, default=codex_home / "config.toml")
    parser.add_argument("--ai-box", type=Path, default=local_settings.ai_box())
    parser.add_argument("--with-context7", action="store_true", help="Добавить удалённый MCP публичной документации без локальных пакетов и токенов")
    args = parser.parse_args()
    node = shutil.which("node")
    required = ("packages/bitbucket/dist/client.js", "packages/jira/dist/client.js",
                "node_modules/@modelcontextprotocol/sdk/package.json", "node_modules/zod/package.json")
    if not node or any(not (args.ai_box / item).is_file() for item in required):
        print("Нет Node.js или готовых пакетов ai-box; ничего не установлено", file=sys.stderr)
        return 1
    try:
        result = configure(args.config, args.ai_box.resolve(), node, with_context7=args.with_context7)
    except (OSError, ValueError):
        print("Не удалось обновить конфигурацию MCP; существующие записи сохранены. Проверьте права и совпадение записей kit.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
