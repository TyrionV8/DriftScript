"""
FastAPI Backend Server for DriftScript:
- Hosts the Descript-style Web UI (`/`)
- Hosts the HTTP MCP JSON-RPC 2.0 endpoint (`/mcp`)
- Provides REST endpoints for transcription (`fast` / `normal` / `heavy`),
  GPU VRAM monitoring, word clicking/seeking, strikethrough cutting, and live Drift synchronization
- Provides HTTP 206 Partial Content video streaming (`/api/media`) for instant video seeking
"""

import hashlib
import logging
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import CACHE_DIR, DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, WHISPER_PROFILES
from .mcp_server import handle_mcp_jsonrpc
from .state import DriftScriptState
from .sync_engine import source_to_timeline, timeline_to_source
from .transcriber import get_runtime_memory_report, transcribe_media, unload_whisper_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("driftscript.server")

app = FastAPI(title="DriftScript — Descript-style Addon & MCP for CutWire Drift", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
state = DriftScriptState()


class TranscribeRequest(BaseModel):
    asset_id: Optional[str] = None
    file_path: Optional[str] = None
    profile: str = "normal"
    language: Optional[str] = "pt"
    force: bool = False


class CutWordsRequest(BaseModel):
    indices: List[int]
    deleted: bool = True
    sync_drift: Optional[bool] = None


class TogglePauseRequest(BaseModel):
    start: float
    end: float
    deleted: Optional[bool] = None
    sync_drift: Optional[bool] = None


class SeekWordRequest(BaseModel):
    word_index: int


class SeekTimeRequest(BaseModel):
    source_time: float
    sync_drift_playhead: bool = True


class SettingsRequest(BaseModel):
    padding_sec: Optional[float] = None
    declick_sec: Optional[float] = None
    auto_sync_drift: Optional[bool] = None
    preferred_profile: Optional[str] = None


class ProxyRequest(BaseModel):
    path: Optional[str] = None
    scale: int = 1


class RelevanceRequest(BaseModel):
    annotations: Optional[List[Dict[str, Any]]] = None
    clear: bool = False


def get_or_create_video_proxy(file_path: Path, scale: int) -> Path:
    """
    Return the original video when `scale == 1`, or generate/reuse a downscaled (`1/2`, `1/4`, `1/16`)
    fast-decode proxy with frequent keyframes (`-g 4`) so `<video>` seeking across cuts has zero lag.
    """
    if scale not in (2, 4, 16):
        return file_path

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stat = file_path.stat()
    key = f"{file_path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}:scale_{scale}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    proxy_path = CACHE_DIR / f"{digest}_proxy_1_{scale}.mp4"

    if proxy_path.exists() and proxy_path.stat().st_size > 1024:
        return proxy_path

    tmp_path = CACHE_DIR / f"{digest}_proxy_1_{scale}.tmp.mp4"
    vf = f"scale=max(16\\,trunc(iw/{scale}/2)*2):max(16\\,trunc(ih/{scale}/2)*2):flags=fast_bilinear"

    # Try GPU-accelerated NVDEC decoding first, fallback to CPU decoding if needed
    cmds = [
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-hwaccel", "cuda",
            "-i", str(file_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode",
            "-g", "4", "-crf", "30",
            "-c:a", "copy", "-movflags", "+faststart",
            str(tmp_path),
        ],
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(file_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode",
            "-g", "4", "-crf", "30",
            "-c:a", "copy", "-movflags", "+faststart",
            str(tmp_path),
        ],
    ]

    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 1024:
                tmp_path.replace(proxy_path)
                logger.info(f"Created 1/{scale} video proxy: {proxy_path}")
                return proxy_path
        except Exception as e:
            logger.warning(f"Proxy generation attempt failed (1/{scale}): {e}")

    return file_path


@app.get("/")
async def index_page():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.post("/mcp")
async def mcp_http_endpoint(request: Request):
    """MCP JSON-RPC 2.0 HTTP endpoint (same protocol as CutWire Drift)."""
    body = await request.json()
    if isinstance(body, list):
        responses = []
        for item in body:
            r = handle_mcp_jsonrpc(state, item)
            if r is not None:
                responses.append(r)
        return JSONResponse(responses)
    resp = handle_mcp_jsonrpc(state, body)
    if resp is None:
        return Response(status_code=202)
    return JSONResponse(resp)


@app.get("/api/status")
async def api_status():
    """Live status polling endpoint: GPU VRAM, Drift connection, and playhead sync."""
    mem = get_runtime_memory_report()
    drift_summary = state.drift.get_media_assets_summary()
    drift_ok = bool(drift_summary.get("ok"))

    playhead_tl = float(drift_summary.get("playhead", 0.0)) if drift_ok else 0.0
    playing_drift = bool(drift_summary.get("playing", False)) if drift_ok else False

    clips = []
    if drift_ok and state.asset_id:
        asset_obj = next((a for a in drift_summary.get("assets", []) if a["asset_id"] == state.asset_id), None)
        if asset_obj:
            clips = asset_obj.get("clips", [])

    playhead_src, active_clip_id = timeline_to_source(playhead_tl, clips) if clips else (playhead_tl, None)

    return {
        "ok": True,
        "memory": mem,
        "drift": {
            "connected": drift_ok,
            "project_name": drift_summary.get("project_name"),
            "playhead_timeline": round(playhead_tl, 3),
            "playhead_source": round(playhead_src, 3) if playhead_src is not None else None,
            "playing": playing_drift,
            "timeline_duration": drift_summary.get("timeline_duration", 0.0),
            "assets": drift_summary.get("assets", []) if drift_ok else [],
            "error": drift_summary.get("message") if not drift_ok else None,
        },
        "session": {
            "asset_id": state.asset_id,
            "file_path": state.file_path,
            "profile": state.profile,
            "total_words": len(state.words),
            "deleted_words_count": sum(1 for w in state.words if w.get("deleted")),
            "last_seek_word_index": state.last_Seek_word_index,
            "last_updated": state.last_updated,
        },
    }


@app.post("/api/memory/unload")
async def api_memory_unload():
    return unload_whisper_model()


@app.get("/api/transcript")
async def api_get_transcript():
    """Return current loaded transcript; auto-loads main Drift asset if cached."""
    if not state.words:
        summary = state.drift.get_media_assets_summary()
        if summary.get("ok") and summary.get("assets"):
            main_asset = summary["assets"][0]
            try:
                tdata = transcribe_media(
                    file_path=main_asset["path"],
                    profile_id=state.profile,
                    language=state.language,
                    force=False,
                )
                return state.load_transcript_data(
                    tdata,
                    asset_id=main_asset["asset_id"],
                    source_duration=float(main_asset.get("duration", 0.0)),
                    drift_clips=main_asset.get("clips", []),
                )
            except Exception as e:
                logger.warning(f"Auto-load transcript skipped: {e}")
    return state.get_full_state()


@app.post("/api/transcribe")
async def api_transcribe(req: TranscribeRequest):
    summary = state.drift.get_media_assets_summary()
    asset_id = req.asset_id
    file_path = req.file_path
    source_dur = 0.0
    drift_clips = []

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
        raise HTTPException(status_code=400, detail="Nenhum arquivo de vídeo encontrado no Drift.")

    tdata = transcribe_media(
        file_path=file_path,
        profile_id=req.profile,
        language=req.language,
        force=req.force,
    )
    return state.load_transcript_data(
        tdata,
        asset_id=asset_id,
        source_duration=source_dur,
        drift_clips=drift_clips,
    )


@app.post("/api/words/cut")
async def api_cut_words(req: CutWordsRequest):
    return state.set_words_deleted(
        indices=req.indices,
        deleted=req.deleted,
        sync_drift=req.sync_drift,
    )


@app.post("/api/words/relevance")
async def api_words_relevance(req: RelevanceRequest):
    return state.set_words_relevance(
        annotations=req.annotations,
        clear=req.clear,
    )


@app.post("/api/pauses/toggle")
async def api_toggle_pause(req: TogglePauseRequest):
    return state.toggle_pause_deleted(
        start_sec=req.start,
        end_sec=req.end,
        deleted=req.deleted,
        sync_drift=req.sync_drift,
    )


@app.post("/api/seek/word")
async def api_seek_word(req: SeekWordRequest):
    return state.seek_to_word(req.word_index)


@app.post("/api/seek/time")
async def api_seek_time(req: SeekTimeRequest):
    src_t = max(0.0, float(req.source_time))
    tl_t = src_t
    is_inside = True
    drift_res = None

    if req.sync_drift_playhead:
        summary = state.drift.get_media_assets_summary()
        clips = []
        if summary.get("ok") and state.asset_id:
            asset_obj = next((a for a in summary.get("assets", []) if a["asset_id"] == state.asset_id), None)
            if asset_obj:
                clips = asset_obj.get("clips", [])
        tl_t, is_inside, _ = source_to_timeline(src_t, clips)
        drift_res = state.drift.seek(tl_t)

    return {
        "ok": True,
        "source_time": round(src_t, 3),
        "timeline_time": round(tl_t, 3),
        "is_inside_kept_clip": is_inside,
        "drift_seek": drift_res,
    }


@app.post("/api/undo")
async def api_undo():
    return state.undo()


@app.post("/api/redo")
async def api_redo():
    return state.redo()


@app.post("/api/sync")
async def api_sync():
    res = state.sync_to_drift()
    full = state.get_full_state()
    full["drift_sync"] = res
    return full


@app.get("/api/candidates")
async def api_candidates(min_pause_sec: float = 0.8):
    return state.analyze_candidates(min_pause_sec=min_pause_sec)


@app.post("/api/settings")
async def api_settings(req: SettingsRequest):
    if req.padding_sec is not None:
        state.padding_sec = max(0.0, min(0.25, float(req.padding_sec)))
    if req.declick_sec is not None:
        state.declick_sec = max(0.0, min(0.2, float(req.declick_sec)))
    if req.auto_sync_drift is not None:
        state.auto_sync_drift = bool(req.auto_sync_drift)
    if req.preferred_profile in ("fast", "normal", "heavy"):
        state.save_preferred_profile(req.preferred_profile)
    state.save_session()
    if state.auto_sync_drift and state.asset_id:
        state.sync_to_drift()
    return state.get_full_state()


@app.post("/api/proxy")
async def api_prepare_proxy(req: ProxyRequest):
    target_str = req.path or state.file_path
    if not target_str:
        raise HTTPException(status_code=400, detail="Nenhum arquivo ativo.")
    file_path = Path(target_str).resolve()
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    scale = int(req.scale or 1)
    proxy_file = get_or_create_video_proxy(file_path, scale)
    return {
        "ok": True,
        "scale": scale,
        "label": "1x" if scale == 1 else f"1/{scale}",
        "proxy_path": str(proxy_file),
    }


@app.get("/api/media")
async def stream_media(path: str = Query(...), scale: int = Query(1), request: Request = None):
    """Stream local video/audio file (or 1/2, 1/4, 1/16 fast-seek proxy) with HTTP Range (206 Partial Content)."""
    raw_path = Path(path).resolve()
    if not raw_path.exists() or not raw_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de mídia não encontrado.")

    file_path = get_or_create_video_proxy(raw_path, int(scale or 1))

    file_size = file_path.stat().st_size
    content_type, _ = mimetypes.guess_type(str(file_path))
    content_type = content_type or "video/mp4"

    range_header = request.headers.get("range") if request else None
    if not range_header:
        return FileResponse(file_path, media_type=content_type, headers={"Accept-Ranges": "bytes"})

    try:
        range_val = range_header.strip().lower().replace("bytes=", "")
        start_str, end_str = range_val.split("-", 1)
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else min(start + 4 * 1024 * 1024 - 1, file_size - 1)
        end = min(end, file_size - 1)
    except Exception:
        start = 0
        end = file_size - 1

    chunk_size = (end - start) + 1

    def iter_file():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = chunk_size
            while remaining > 0:
                data = f.read(min(65536, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_size),
    }
    return StreamingResponse(iter_file(), status_code=206, media_type=content_type, headers=headers)


def main():
    import uvicorn

    uvicorn.run(app, host=DEFAULT_SERVER_HOST, port=DEFAULT_SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
