"""
MCP (Model Context Protocol) Server for DriftScript.
Allows AI models (Antigravity, Claude Code, Codex, etc.) to inspect GPU memory profiles,
transcribe Drift clips (fast / normal / heavy), read word-indexed transcripts,
and edit videos by cutting/restoring words (fillers, stutters, names, bad takes)
directly on the CutWire Drift timeline while keeping deleted words visible as strikethrough.

Supports both:
- HTTP JSON-RPC 2.0 at `http://127.0.0.1:4732/mcp`
- Standard I/O (stdio) via `python -m driftscript.mcp_server`
"""

import json
import sys
import urllib.request
from typing import Any, Dict, List, Optional

from .config import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, WHISPER_PROFILES
from .state import DriftScriptState
from .transcriber import (
    build_segments_and_pauses,
    get_runtime_memory_report,
    transcribe_media,
    unload_whisper_model,
)

MCP_TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "driftscript_memory_status",
        "description": (
            "Check GPU VRAM / System RAM usage, CPU AVX2 compatibility mode, currently loaded Whisper model, "
            "and the 3 Whisper memory profiles: fast (~1.2 GB VRAM), normal (~2.8 GB VRAM), heavy (~4.8 GB VRAM). "
            "Pass unload=true to free GPU VRAM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "unload": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, unloads the Whisper model from GPU VRAM and clears CUDA cache.",
                }
            },
        },
    },
    {
        "name": "driftscript_list_clips",
        "description": "List all video/audio assets and timeline clips currently open in CutWire Drift.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "driftscript_transcribe",
        "description": (
            "Transcribe (or load cached acoustic-aligned transcript for) a clip/asset in CutWire Drift using Whisper on GPU (no AVX2). "
            "Available modes by memory footprint: 'fast' (small, ~1.2 GB VRAM), 'normal' (medium, ~2.8 GB VRAM), 'heavy' (large-v3, ~4.8 GB VRAM)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {
                    "type": "string",
                    "description": "Drift asset UUID (optional: defaults to the main/longest video asset on the Drift timeline).",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to media file (optional if asset_id is provided or detected from Drift).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "normal", "heavy"],
                    "default": "normal",
                    "description": "Whisper memory tier: fast (~1.2 GB VRAM), normal (~2.8 GB VRAM), heavy (~4.8 GB VRAM).",
                },
                "language": {
                    "type": "string",
                    "default": "pt",
                    "description": "Language code (e.g. 'pt', 'en').",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force re-transcription even if a cached transcript exists.",
                },
            },
        },
    },
    {
        "name": "driftscript_get_transcript",
        "description": (
            "Read the word-indexed transcript for text-based video editing. "
            "Each word is formatted as `[index] word` (or `[index] ~word~` if already struck-through/cut), "
            "with pauses shown as `(PAUSA Xs)` and timestamps per paragraph. "
            "Use the word indices with `driftscript_cut_words` or `driftscript_restore_words`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Starting word index (0-based).",
                },
                "limit": {
                    "type": "integer",
                    "default": 500,
                    "description": "Maximum number of words to return (up to 3000).",
                },
                "start_sec": {
                    "type": "number",
                    "description": "Optional filter: only return words starting at or after this source timestamp (seconds).",
                },
                "end_sec": {
                    "type": "number",
                    "description": "Optional filter: only return words ending at or before this source timestamp (seconds).",
                },
            },
        },
    },
    {
        "name": "driftscript_search_words",
        "description": (
            "Search the transcript for specific words/phrases (e.g. names, bad takes) OR automatically detect "
            "speech fillers ('é', 'hum', 'tipo', 'né'), stutters/immediate repetitions ('o o', 'que que'), "
            "hesitations ('...'), and long pauses."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional text or word to search for (case-insensitive).",
                },
                "include_candidates": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include automatic detection of fillers, repetitions, and hesitations.",
                },
                "min_pause_sec": {
                    "type": "number",
                    "default": 0.8,
                    "description": "Minimum gap in seconds to report as a long pause.",
                },
            },
        },
    },
    {
        "name": "driftscript_cut_words",
        "description": (
            "Cut words from the video by marking them as struck-through (`deleted=true`) by word index or `[start_idx, end_idx]` ranges. "
            "Removed words do NOT disappear from the transcript — they stay visible with strikethrough and can be restored anytime. "
            "Automatically updates the CutWire Drift timeline in a single Undo step."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of individual word indices to cut (strike through).",
                },
                "ranges": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "description": "List of inclusive word index ranges `[start_index, end_index]` to cut (e.g. `[[0, 3], [15, 18]]`).",
                },
                "sync_drift": {
                    "type": "boolean",
                    "default": True,
                    "description": "Immediately apply the cuts to the CutWire Drift timeline.",
                },
            },
        },
    },
    {
        "name": "driftscript_restore_words",
        "description": (
            "Restore previously struck-through (`deleted`) words by word index, `[start_idx, end_idx]` ranges, "
            "or `restore_all=true`, and re-sync the CutWire Drift timeline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of word indices to restore.",
                },
                "ranges": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "description": "List of inclusive word index ranges `[start_index, end_index]` to restore.",
                },
                "restore_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, restores all struck-through words in the transcript.",
                },
                "sync_drift": {
                    "type": "boolean",
                    "default": True,
                    "description": "Immediately sync the restored words back onto the CutWire Drift timeline.",
                },
            },
        },
    },
    {
        "name": "driftscript_seek_word",
        "description": (
            "Jump the CutWire Drift playhead (and web preview player) directly to the exact start of word `word_index`, "
            "automatically converting from source media time to the current Drift timeline time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "word_index": {
                    "type": "integer",
                    "description": "Index of the word to seek to.",
                }
            },
            "required": ["word_index"],
        },
    },
    {
        "name": "driftscript_sync_drift",
        "description": "Force immediate synchronization of the current word cuts (`keep_ranges`) to the CutWire Drift timeline.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "driftscript_set_relevance",
        "description": (
            "Color the transcript Relevance Meter by assigning relevance scores (`score`: 1.0 = green/most relevant, "
            "0.5 = yellow/moderate, 0.0 = red/least relevant) or `category='off_topic'` (blue = off-topic, tangents, interruptions) "
            "to word index ranges `[start_index, end_index]`. Pass `clear=true` to reset all words back to white."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "annotations": {
                    "type": "array",
                    "description": "List of relevance segments: `{start_index, end_index, score (0.0..1.0), category ('relevant'|'off_topic'), reason}`.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_index": {"type": "integer"},
                            "end_index": {"type": "integer"},
                            "indices": {"type": "array", "items": {"type": "integer"}},
                            "score": {"type": "number", "description": "0.0 (red, low relevance) to 0.5 (yellow) to 1.0 (green, high relevance)."},
                            "category": {"type": "string", "enum": ["relevant", "off_topic"], "default": "relevant"},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clears existing relevance colors (resets text to white) before applying any annotations.",
                },
            },
        },
    },
    {
        "name": "driftscript_accelerate_words",
        "description": (
            "Accelerate (speed up: 2x or 4x) words, silences (`•`/`—`), or stretched syllables (`〰`) "
            "by index or `[start_index, end_index]` ranges instead of cutting them, preserving video fluidity while shortening duration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "indices": {"type": "array", "items": {"type": "integer"}},
                "ranges": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
                "speed": {"type": "number", "default": 2.0, "description": "Playback multiplier (2.0, 4.0, or 1.0 to reset; max 4.0)."},
                "sync_drift": {"type": "boolean", "default": True},
            },
        },
    },
]


def _expand_indices_and_ranges(
    indices: Optional[List[int]] = None,
    ranges: Optional[List[List[int]]] = None,
) -> List[int]:
    out = set()
    if indices:
        for i in indices:
            out.add(int(i))
    if ranges:
        for r in ranges:
            if isinstance(r, (list, tuple)) and len(r) >= 2:
                a, b = int(r[0]), int(r[1])
                if a > b:
                    a, b = b, a
                for k in range(a, b + 1):
                    out.add(k)
    return sorted(out)


def execute_mcp_tool(state: DriftScriptState, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a DriftScript MCP tool against the shared `DriftScriptState`."""
    # Ensure transcript is auto-loaded if a project is open in Drift
    if name not in ("driftscript_memory_status", "driftscript_list_clips", "driftscript_transcribe") and not state.words:
        _auto_ensure_loaded(state)

    if name == "driftscript_memory_status":
        if args.get("unload"):
            return unload_whisper_model()
        return get_runtime_memory_report()

    if name == "driftscript_list_clips":
        return state.drift.get_media_assets_summary()

    if name == "driftscript_transcribe":
        mode = str(args.get("mode") or "normal").lower()
        if mode not in WHISPER_PROFILES:
            mode = "normal"
        lang = args.get("language", "pt")
        force = bool(args.get("force", False))
        asset_id = args.get("asset_id")
        file_path = args.get("file_path")
        source_dur = 0.0
        drift_clips = []

        summary = state.drift.get_media_assets_summary()
        if summary.get("ok") and summary.get("assets"):
            assets = summary["assets"]
            chosen = None
            if asset_id:
                chosen = next((a for a in assets if a["asset_id"] == asset_id), None)
            if not chosen and file_path:
                chosen = next((a for a in assets if a["path"] == file_path), None)
            if not chosen:
                chosen = assets[0]
            asset_id = chosen["asset_id"]
            file_path = file_path or chosen["path"]
            source_dur = float(chosen.get("duration", 0.0))
            drift_clips = chosen.get("clips", [])

        if not file_path:
            return {"ok": False, "error": "missing_file_path", "message": "Nenhum clipe com arquivo de mídia encontrado no Drift."}

        tdata = transcribe_media(file_path=file_path, profile_id=mode, language=lang, force=force)
        full = state.load_transcript_data(
            tdata,
            asset_id=asset_id,
            source_duration=source_dur,
            drift_clips=drift_clips,
        )
        return {
            "ok": True,
            "asset_id": full["asset_id"],
            "file_path": full["file_path"],
            "profile": mode,
            "profile_memory": WHISPER_PROFILES[mode]["vram_display"],
            "total_words": full["total_words"],
            "deleted_words_count": full["deleted_words_count"],
            "source_duration": full["source_duration"],
            "kept_duration": full["kept_duration"],
            "cut_duration": full["cut_duration"],
            "memory": get_runtime_memory_report()["gpu"],
        }

    if name == "driftscript_get_transcript":
        state.reload_session_flags_from_disk()
        if not state.words:
            return {"ok": False, "error": "no_transcript_loaded", "message": "Chame driftscript_transcribe primeiro."}

        offset = max(0, int(args.get("offset", 0)))
        limit = max(1, min(3000, int(args.get("limit", 500))))
        start_sec = args.get("start_sec")
        end_sec = args.get("end_sec")

        filtered = state.words
        if start_sec is not None:
            filtered = [w for w in filtered if float(w["end"]) >= float(start_sec)]
        if end_sec is not None:
            filtered = [w for w in filtered if float(w["start"]) <= float(end_sec)]

        sliced = filtered[offset : offset + limit]
        segments = build_segments_and_pauses(sliced, preserve_indices=True)

        lines = []
        for seg in segments:
            tokens_str = []
            for wi in seg["word_indices"]:
                w = next((x for x in sliced if x["index"] == wi), None)
                if not w:
                    continue
                if w.get("deleted"):
                    tokens_str.append(f"[{w['index']}] ~{w['word']}~")
                else:
                    tokens_str.append(f"[{w['index']}] {w['word']}")
                if w.get("gap_after", 0.0) >= 0.55:
                    tokens_str.append(f"(PAUSA {w['gap_after']:.2f}s)")
            lines.append(f"({seg['start']:.2f}s - {seg['end']:.2f}s) " + " ".join(tokens_str))

        next_offset = offset + limit if (offset + limit) < len(filtered) else None
        return {
            "ok": True,
            "asset_id": state.asset_id,
            "total_words": len(state.words),
            "deleted_words_count": sum(1 for w in state.words if w.get("deleted")),
            "returned_words": len(sliced),
            "offset": offset,
            "next_offset": next_offset,
            "formatted_transcript": "\n".join(lines),
            "words": sliced,
        }

    if name == "driftscript_search_words":
        state.reload_session_flags_from_disk()
        if not state.words:
            return {"ok": False, "error": "no_transcript_loaded"}

        query = (args.get("query") or "").strip().lower()
        matches = []
        if query:
            q_parts = query.split()
            n_q = len(q_parts)
            for i in range(len(state.words) - n_q + 1):
                window = state.words[i : i + n_q]
                joined = " ".join(w["word"].lower().strip(".,?!;:()") for w in window)
                if query in joined or any(query in w["word"].lower() for w in window):
                    ctx_s = max(0, i - 3)
                    ctx_e = min(len(state.words), i + n_q + 3)
                    ctx = " ".join(
                        (f"[{x['index']}:~{x['word']}~]" if x.get("deleted") else f"[{x['index']}:{x['word']}]")
                        for x in state.words[ctx_s:ctx_e]
                    )
                    matches.append(
                        {
                            "indices": [w["index"] for w in window],
                            "text": " ".join(w["word"] for w in window),
                            "start": window[0]["start"],
                            "end": window[-1]["end"],
                            "deleted": any(w.get("deleted") for w in window),
                            "context": ctx,
                        }
                    )

        candidates = (
            state.analyze_candidates(min_pause_sec=float(args.get("min_pause_sec", 0.8)))
            if args.get("include_candidates", True)
            else {}
        )
        return {
            "ok": True,
            "query": query,
            "matches": matches,
            "candidates": candidates,
        }

    if name == "driftscript_cut_words":
        target_indices = _expand_indices_and_ranges(args.get("indices"), args.get("ranges"))
        if not target_indices:
            return {"ok": False, "error": "no_indices_provided", "message": "Forneça `indices` ou `ranges` para riscar."}
        res = state.set_words_deleted(
            indices=target_indices,
            deleted=True,
            sync_drift=args.get("sync_drift", True),
        )
        cut_preview = [
            f"[{i}] ~{state.words[i]['word']}~"
            for i in target_indices
            if 0 <= i < len(state.words)
        ]
        return {
            "ok": True,
            "action": "cut_words (strikethrough)",
            "cut_count": len(target_indices),
            "changed_words": res.get("changed_words", 0),
            "words_struck_through": cut_preview[:60],
            "total_deleted_words": res["deleted_words_count"],
            "kept_duration": res["kept_duration"],
            "cut_duration": res["cut_duration"],
            "drift_sync": res.get("drift_sync"),
        }

    if name == "driftscript_restore_words":
        if args.get("restore_all"):
            target_indices = [w["index"] for w in state.words if w.get("deleted")]
        else:
            target_indices = _expand_indices_and_ranges(args.get("indices"), args.get("ranges"))
        res = state.set_words_deleted(
            indices=target_indices,
            deleted=False,
            sync_drift=args.get("sync_drift", True),
        )
        return {
            "ok": True,
            "action": "restore_words",
            "restored_count": len(target_indices),
            "changed_words": res.get("changed_words", 0),
            "total_deleted_words": res["deleted_words_count"],
            "kept_duration": res["kept_duration"],
            "cut_duration": res["cut_duration"],
            "drift_sync": res.get("drift_sync"),
        }

    if name == "driftscript_seek_word":
        w_idx = int(args["word_index"])
        return state.seek_to_word(w_idx)

    if name == "driftscript_sync_drift":
        sync_res = state.sync_to_drift()
        full = state.get_full_state()
        return {
            "ok": sync_res.get("ok", False),
            "drift_sync": sync_res,
            "kept_duration": full["kept_duration"],
            "cut_duration": full["cut_duration"],
            "keep_ranges_count": len(full["keep_ranges"]),
        }

    if name == "driftscript_set_relevance":
        res = state.set_words_relevance(
            annotations=args.get("annotations") or [],
            clear=bool(args.get("clear", False)),
        )
        return {
            "ok": True,
            "action": "set_relevance",
            "updated_relevance_count": res.get("updated_relevance_count", 0),
            "total_words": res.get("total_words", 0),
        }

    if name == "driftscript_accelerate_words":
        target_indices = _expand_indices_and_ranges(args.get("indices"), args.get("ranges"))
        if not target_indices:
            return {"ok": False, "error": "no_indices_provided", "message": "Forneça `indices` ou `ranges` para acelerar."}
        spd = float(args.get("speed", 1.5))
        res = state.set_words_speed(
            indices=target_indices,
            speed=spd,
            sync_drift=args.get("sync_drift", True),
        )
        return {
            "ok": True,
            "action": f"accelerate_words ({spd}x)",
            "accelerated_count": len(target_indices),
            "kept_duration": res["kept_duration"],
            "cut_duration": res["cut_duration"],
            "drift_sync": res.get("drift_sync"),
        }

    return {"ok": False, "error": f"unknown_tool: {name}"}


def _auto_ensure_loaded(state: DriftScriptState) -> None:
    """Automatically load the primary Drift asset transcript from cache if available."""
    try:
        summary = state.drift.get_media_assets_summary()
        if summary.get("ok") and summary.get("assets"):
            main_asset = summary["assets"][0]
            tdata = transcribe_media(
                file_path=main_asset["path"],
                profile_id=state.profile,
                language=state.language,
                force=False,
            )
            state.load_transcript_data(
                tdata,
                asset_id=main_asset["asset_id"],
                source_duration=float(main_asset.get("duration", 0.0)),
                drift_clips=main_asset.get("clips", []),
            )
    except Exception:
        pass


def handle_mcp_jsonrpc(state: DriftScriptState, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Process a single MCP JSON-RPC 2.0 request and return the JSON-RPC response."""
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "notifications/initialized" or req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "driftscript", "version": "0.1.0"},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS_SCHEMA},
        }

    if method == "tools/call":
        t_name = params.get("name", "")
        t_args = params.get("arguments") or {}
        try:
            res = execute_mcp_tool(state, t_name, t_args)
            is_err = not bool(res.get("ok", True))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                    "isError": is_err,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _forward_to_running_http_daemon(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """If the DriftScript HTTP daemon is running on port 4732, proxy MCP calls to it so UI updates live."""
    url = f"http://{DEFAULT_SERVER_HOST}:{DEFAULT_SERVER_PORT}/mcp"
    body = json.dumps(req).encode("utf-8")
    http_req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_req, timeout=60.0) as resp:
            raw = resp.read().decode("utf-8").strip()
            if raw:
                return json.loads(raw)
    except Exception:
        return None
    return None


def run_stdio_server() -> None:
    """Run MCP server over stdin/stdout (proxies to running HTTP daemon if active, else runs in-process)."""
    local_state = DriftScriptState()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        # Try forwarding to the live HTTP server first so Web UI and MCP share in-RAM state immediately
        resp = _forward_to_running_http_daemon(req)
        if resp is None:
            resp = handle_mcp_jsonrpc(local_state, req)

        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
