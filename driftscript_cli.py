#!/usr/bin/env python3
"""
Utilitário CLI e cliente MCP para o DriftScript.
Permite:
  - `status`: verificar memória (GPU VRAM / RAM / modelos Fast, Normal, Heavy) e conexão com o Drift
  - `chamar <ferramenta> [json_args]`: chamar qualquer ferramenta MCP do DriftScript diretamente
  - `configurar-mcp`: registrar o servidor MCP `driftscript` no Antigravity (~/.gemini/config/mcp_config.json) com backup seguro
"""

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

MCP_URL = "http://127.0.0.1:4732/mcp"


def call_mcp(tool_name: str, args: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": args,
        },
    }
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data.get("result", {}).get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return data


def configure_antigravity_mcp() -> None:
    cfg_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if not cfg_path.exists():
        print(f"[aviso] Arquivo {cfg_path} não encontrado.")
        return

    backup = cfg_path.with_name(f"{cfg_path.name}.bak-driftscript-{int(time.time())}")
    shutil.copy2(cfg_path, backup)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    servers = data.setdefault("mcpServers", {})
    servers["driftscript"] = {
        "url": MCP_URL,
    }
    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[ok] Servidor MCP 'driftscript' ({MCP_URL}) registrado em {cfg_path}")
    print(f"[ok] Backup preservado em {backup}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    cmd = sys.argv[1]
    if cmd == "status":
        res = call_mcp("driftscript_memory_status", {})
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if cmd == "chamar":
        if len(sys.argv) < 3:
            print("Uso: driftscript_cli.py chamar <ferramenta> ['{\"arg\": \"valor\"}']")
            return 1
        tool = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) >= 4 else {}
        res = call_mcp(tool, args)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok", True) else 1

    if cmd == "configurar-mcp":
        configure_antigravity_mcp()
        return 0

    print(f"Comando desconhecido: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
