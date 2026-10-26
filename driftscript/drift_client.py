"""
Client for CutWire Drift's HTTP MCP server (http://127.0.0.1:4731/mcp).
Communicates via JSON-RPC 2.0 with Bearer token read locally from CutWire Drift.conf.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import DEFAULT_DRIFT_MCP_URL, read_drift_mcp_token

logger = logging.getLogger("driftscript.drift_client")


class DriftMCPClient:
    """Lightweight, zero-dependency HTTP JSON-RPC client for CutWire Drift MCP."""

    def __init__(self, url: str = DEFAULT_DRIFT_MCP_URL):
        self.url = url
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Dict[str, Any]:
        """Call a high-level MCP tool on Drift (inspect, apply, seek, catalog, toolbox, etc.)."""
        token = read_drift_mcp_token()
        if not token:
            return {"ok": False, "error": "drift_token_missing", "message": "Token MCP do Drift não encontrado em CutWire Drift.conf."}

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"http_{e.code}", "message": f"Drift HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "error": "drift_unreachable", "message": f"Drift inacessível em {self.url}: {e}"}

        # Handle either plain JSON or SSE data lines
        data_obj = None
        if raw.startswith("{"):
            try:
                data_obj = json.loads(raw)
            except Exception:
                pass
        if data_obj is None:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    candidate = line[5:].strip()
                    if candidate.startswith("{"):
                        try:
                            data_obj = json.loads(candidate)
                        except Exception:
                            pass

        if not data_obj:
            return {"ok": False, "error": "invalid_json", "raw": raw[:300]}

        if "error" in data_obj:
            return {"ok": False, "error": "rpc_error", "detail": data_obj["error"]}

        result = data_obj.get("result", {})
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"ok": True, "data": parsed}
                except Exception:
                    return {"ok": not result.get("isError", False), "text": text}

        return {"ok": True, "result": result}

    def inspect(self, clips: bool = True, detail: bool = True, since: Optional[int] = None) -> Dict[str, Any]:
        args: Dict[str, Any] = {"clips": clips, "detail": detail}
        if since is not None:
            args["since"] = since
        return self.call_tool("inspect", args, timeout=5.0)

    def apply(self, ops: List[Dict[str, Any]], timeout: float = 20.0) -> Dict[str, Any]:
        return self.call_tool("apply", {"ops": ops}, timeout=timeout)

    def seek(self, at_seconds: float) -> Dict[str, Any]:
        return self.apply([{"tool": "seek", "args": {"at": max(0.0, round(float(at_seconds), 4))}}], timeout=5.0)

    def play(self) -> Dict[str, Any]:
        return self.apply([{"tool": "play", "args": {}}], timeout=5.0)

    def pause(self) -> Dict[str, Any]:
        return self.apply([{"tool": "pause", "args": {}}], timeout=5.0)

    def get_media_assets_summary(self) -> Dict[str, Any]:
        """
        Inspect the active Drift project and group timeline clips by assetId.
        Returns assets that have a valid media file path on the timeline.
        """
        state = self.inspect(clips=True, detail=True)
        if not state.get("ok"):
            return state

        assets_by_id: Dict[str, Dict[str, Any]] = {}
        for a in state.get("assets", []):
            aid = a.get("id")
            if aid:
                assets_by_id[aid] = {
                    "asset_id": aid,
                    "name": a.get("name", ""),
                    "kind": a.get("kind", "video"),
                    "duration": float(a.get("dur", 0.0)),
                    "index": a.get("index", 0),
                    "path": None,
                    "track_index": None,
                    "clips": [],
                }

        for track in state.get("tracks", []):
            t_idx = track.get("i", 0)
            t_type = track.get("type", "video")
            if t_type not in ("video", "audio"):
                continue
            for item in track.get("items", []):
                aid = item.get("assetId")
                if not aid:
                    continue
                if aid not in assets_by_id:
                    assets_by_id[aid] = {
                        "asset_id": aid,
                        "name": item.get("name", ""),
                        "kind": item.get("kind", t_type),
                        "duration": float(item.get("sourceDuration") or item.get("duration") or 0.0),
                        "index": item.get("assetIndex", 0),
                        "path": item.get("path"),
                        "track_index": t_idx,
                        "clips": [],
                    }
                entry = assets_by_id[aid]
                if not entry["path"] and item.get("path"):
                    entry["path"] = item.get("path")
                if item.get("sourceDuration") and float(item["sourceDuration"]) > entry["duration"]:
                    entry["duration"] = float(item["sourceDuration"])
                if entry["track_index"] is None:
                    entry["track_index"] = t_idx
                # Collect clips on the primary track for this asset
                if entry["track_index"] == t_idx:
                    entry["clips"].append(
                        {
                            "id": item.get("id"),
                            "track": t_idx,
                            "start": float(item.get("start", 0.0)),
                            "duration": float(item.get("duration", 0.0)),
                            "inPoint": float(item.get("inPoint", 0.0)),
                            "outPoint": float(item.get("outPoint", item.get("duration", 0.0))),
                            "speed": float(item.get("speed", 1.0) or 1.0),
                            "sourceDuration": float(item.get("sourceDuration") or entry["duration"] or 0.0),
                        }
                    )

        # Sort clips by timeline start
        media_assets = []
        for aid, info in assets_by_id.items():
            info["clips"].sort(key=lambda c: c["start"])
            if info["clips"] and info["path"]:
                media_assets.append(info)

        # Prefer longer main footage first (e.g. main recording over short overlays)
        media_assets.sort(key=lambda a: a["duration"], reverse=True)

        return {
            "ok": True,
            "project_name": state.get("name", "Untitled"),
            "project_path": state.get("path", ""),
            "playhead": float(state.get("playhead", 0.0)),
            "playing": bool(state.get("playing", False)),
            "fps": float(state.get("fps", 30.0)),
            "timeline_duration": float(state.get("dur", 0.0)),
            "revision": state.get("revision", 0),
            "assets": media_assets,
        }

    def sync_asset_keep_ranges(
        self,
        asset_id: str,
        keep_ranges: List[Dict[str, float]],
        declick: float = 0.03,
    ) -> Dict[str, Any]:
        """
        Synchronize an asset's clips on the Drift timeline to match `keep_ranges`
        (in source seconds) inside a single `apply` call (one Undo step in Drift).
        """
        summary = self.get_media_assets_summary()
        if not summary.get("ok"):
            return summary

        target_asset = next((a for a in summary.get("assets", []) if a["asset_id"] == asset_id), None)
        if not target_asset or not target_asset["clips"]:
            return {"ok": False, "error": "asset_not_on_timeline", "message": f"Asset {asset_id} não encontrado na timeline do Drift."}

        clips = target_asset["clips"]
        primary_clip = clips[0]
        extra_clips = clips[1:]

        # Sanitize and merge micro-gaps (< 0.025s) in keep_ranges
        valid_ranges: List[Dict[str, float]] = []
        for r in keep_ranges:
            s = round(max(0.0, float(r["start"])), 4)
            e = round(float(r["end"]), 4)
            if e - s < 0.04:
                continue
            if valid_ranges and (s - valid_ranges[-1]["end"]) < 0.025:
                valid_ranges[-1]["end"] = max(valid_ranges[-1]["end"], e)
            else:
                valid_ranges.append({"start": s, "end": e})

        if not valid_ranges:
            return {"ok": False, "error": "empty_keep_ranges", "message": "Nenhum trecho restante para manter."}

        # Check if the current timeline clips already match valid_ranges within 5ms tolerance
        if len(clips) == len(valid_ranges):
            already_synced = True
            for c, r in zip(clips, valid_ranges):
                if abs(c["inPoint"] - r["start"]) > 0.006 or abs(c["outPoint"] - r["end"]) > 0.006:
                    already_synced = False
                    break
            if already_synced:
                return {
                    "ok": True,
                    "unchanged": True,
                    "clips_count": len(clips),
                    "message": "Timeline do Drift já está 100% sincronizada com os cortes atuais.",
                }

        ops: List[Dict[str, Any]] = [
            {"tool": "pause", "args": {}},
            {"tool": "set_ripple", "args": {"enabled": True}},
        ]

        # Delete secondary clips from back to front so primary_clip stays intact at the start
        for extra in reversed(extra_clips):
            ops.append({"tool": "delete_clip", "args": {"clip": extra["id"]}})

        # Rebuild primary_clip using keep_ranges (preserves transform, volume, effects, and applies declick + ripple)
        ops.append(
            {
                "tool": "keep_ranges",
                "args": {
                    "clip": primary_clip["id"],
                    "ranges": valid_ranges,
                    "declick": round(float(declick), 3),
                    "padding": 0.0,
                    "ripple": True,
                },
            }
        )

        res = self.apply(ops, timeout=30.0)
        return res
