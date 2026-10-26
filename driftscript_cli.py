#!/usr/bin/env python3
"""
Utilitário CLI e cliente MCP para o DriftScript.
Permite:
  - `status`: verificar memória (GPU VRAM / RAM / modelos Fast, Normal, Heavy) e conexão com o Drift
  - `chamar <ferramenta> [json_args]`: chamar qualquer ferramenta MCP do DriftScript diretamente
  - `configurar-mcp`: registrar o servidor MCP `driftscript` no Antigravity, Claude Code e Codex com backup seguro
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

MCP_URL = "http://127.0.0.1:4732/mcp"
CABECALHO_TOML = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


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


def gravar_atomico(destino: Path, conteudo: str) -> Path | None:
    """Grava o conteúdo no destino real (resolvendo links simbólicos) e gera backup com timestamp."""
    real = destino.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    modo = 0o600
    if real.exists():
        modo = real.stat().st_mode & 0o777
        backup = real.with_name(f"{real.name}.bak-driftscript-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(real, backup)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
        os.chmod(tmp, modo)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def configure_antigravity() -> None:
    cfg = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if not cfg.exists():
        print(f"[aviso] Antigravity: {cfg} não encontrado.")
        return
    data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
    servers = data.setdefault("mcpServers", {})
    if servers.get("driftscript", {}).get("url") == MCP_URL:
        print(f"[ok] Antigravity: entrada 'driftscript' já correta em {cfg}")
        return
    servers["driftscript"] = {"url": MCP_URL}
    backup = gravar_atomico(cfg, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[alterado] Antigravity: entrada 'driftscript' configurada em {cfg}")
    if backup:
        print(f"           backup preservado em {backup}")


def configure_claude() -> None:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = Path(base) / ".claude.json" if base else Path.home() / ".claude.json"
    if not cfg.exists():
        print(f"[aviso] Claude Code: {cfg} não encontrado.")
        return
    data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
    servers = data.setdefault("mcpServers", {})
    entry = servers.get("driftscript", {})
    if entry.get("url") == MCP_URL and entry.get("type") == "http":
        print(f"[ok] Claude Code: entrada 'driftscript' já correta em {cfg}")
        return
    servers["driftscript"] = {"type": "http", "url": MCP_URL}
    backup = gravar_atomico(cfg, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[alterado] Claude Code: entrada 'driftscript' configurada em {cfg}")
    if backup:
        print(f"           backup preservado em {backup}")


def configure_codex() -> None:
    base = os.environ.get("CODEX_HOME")
    cfg = (Path(base) if base else Path.home() / ".codex") / "config.toml"
    if not cfg.exists():
        print(f"[aviso] Codex: {cfg} não encontrado.")
        return
    texto = cfg.read_text(encoding="utf-8")
    try:
        dados = tomllib.loads(texto)
        entrada = dados.get("mcp_servers", {}).get("driftscript")
        if isinstance(entrada, dict) and entrada.get("url") == MCP_URL:
            print(f"[ok] Codex: entrada 'driftscript' já correta em {cfg}")
            return
    except Exception:
        pass

    alvo = "mcp_servers.driftscript"
    saida, pulando = [], False
    for ln in texto.splitlines(keepends=True):
        m = CABECALHO_TOML.match(ln)
        if m:
            nome = m.group(1).replace(" ", "").replace('"', "")
            pulando = nome == alvo or nome.startswith(alvo + ".")
        if not pulando:
            saida.append(ln)
    novo = "".join(saida).rstrip("\n")
    bloco = f'\n\n[mcp_servers.driftscript]\nurl = "{MCP_URL}"\n'
    backup = gravar_atomico(cfg, (novo + bloco).lstrip("\n"))
    print(f"[alterado] Codex: entrada 'driftscript' configurada em {cfg}")
    if backup:
        print(f"           backup preservado em {backup}")


def configure_all_mcp() -> None:
    configure_antigravity()
    configure_claude()
    configure_codex()


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
        configure_all_mcp()
        return 0

    print(f"Comando desconhecido: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
