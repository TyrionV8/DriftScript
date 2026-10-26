"""
Session State & Persistence Manager for DriftScript.
Maintains the active transcript, word strikethrough (`deleted`) states,
Undo/Redo history, filler/repetition analysis, and disk persistence so both
the Web UI and MCP tools operate on the exact same live state.
"""

import copy
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import CACHE_DIR
from .drift_client import DriftMCPClient
from .sync_engine import (
    compute_keep_and_deleted_ranges,
    detect_cut_words_from_timeline_clips,
    source_to_timeline,
    timeline_to_source,
)
from .transcriber import build_segments_and_pauses, get_or_compute_rms_10ms

logger = logging.getLogger("driftscript.state")

PT_FILLERS = {
    "é",
    "é...",
    "eh",
    "hum",
    "uh",
    "um",
    "ah",
    "ãh",
    "né",
    "né?",
    " né,",
    "tipo",
    "tipo,",
    "tipo.",
}


def _normalize_token(word: str) -> str:
    return re.sub(r"[^\wÀ-ÿ]", "", word.lower()).strip()


class DriftScriptState:
    """Thread-safe / multi-process synchronized state for the active Drift media asset."""

    def __init__(self, drift_client: Optional[DriftMCPClient] = None):
        self.drift = drift_client or DriftMCPClient()
        self.asset_id: Optional[str] = None
        self.file_path: Optional[str] = None
        self.source_duration: float = 0.0
        self.profile: str = self._load_preferred_profile()
        self.language: str = "pt"
        self.padding_sec: float = 0.055
        self.declick_sec: float = 0.03
        self.auto_sync_drift: bool = True
        self.words: List[Dict[str, Any]] = []
        self.deleted_pauses: List[Dict[str, float]] = []
        self.undo_stack: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []
        self.last_Seek_word_index: Optional[int] = None
        self.last_updated: float = time.time()

    def _prefs_file_path(self) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR / "user_prefs.json"

    def _load_preferred_profile(self) -> str:
        pf = self._prefs_file_path()
        if pf.exists():
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                prof = str(data.get("last_whisper_profile", "normal"))
                if prof in ("fast", "normal", "heavy"):
                    return prof
            except Exception:
                pass
        return "normal"

    def save_preferred_profile(self, profile: str) -> None:
        if profile not in ("fast", "normal", "heavy"):
            return
        self.profile = profile
        pf = self._prefs_file_path()
        try:
            data = {}
            if pf.exists():
                with open(pf, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["last_whisper_profile"] = profile
            with open(pf, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed saving preferred profile: {e}")

    def _state_file_path(self, file_path: str) -> Path:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        h = hashlib.md5(str(Path(file_path).resolve()).encode("utf-8")).hexdigest()
        return CACHE_DIR / f"{h}_session_state_v4.json"

    def save_session(self) -> None:
        if not self.file_path or not self.words:
            return
        sf = self._state_file_path(self.file_path)
        relevance_map = {
            str(w["index"]): {
                "score": w.get("relevance_score"),
                "category": w.get("relevance_category"),
                "reason": w.get("relevance_reason"),
            }
            for w in self.words
            if w.get("relevance_score") is not None or w.get("relevance_category") is not None
        }
        payload = {
            "asset_id": self.asset_id,
            "file_path": self.file_path,
            "source_duration": self.source_duration,
            "profile": self.profile,
            "language": self.language,
            "padding_sec": self.padding_sec,
            "declick_sec": self.declick_sec,
            "auto_sync_drift": self.auto_sync_drift,
            "deleted_indices": [w["index"] for w in self.words if w.get("deleted", False)],
            "deleted_pauses": self.deleted_pauses,
            "relevance_map": relevance_map,
            "last_updated": time.time(),
        }
        try:
            with open(sf, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed saving session state: {e}")

    def reload_session_flags_from_disk(self) -> None:
        """Keep web server and stdio MCP processes in sync via the shared session state file."""
        if not self.file_path or not self.words:
            return
        sf = self._state_file_path(self.file_path)
        if not sf.exists():
            return
        try:
            mtime = sf.stat().st_mtime
            if mtime <= self.last_updated:
                return
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
            del_set = set(data.get("deleted_indices", []))
            rel_map = data.get("relevance_map", {})
            for w in self.words:
                w["deleted"] = w["index"] in del_set
                rel = rel_map.get(str(w["index"]))
                if rel:
                    w["relevance_score"] = rel.get("score")
                    w["relevance_category"] = rel.get("category")
                    w["relevance_reason"] = rel.get("reason")
                else:
                    w.pop("relevance_score", None)
                    w.pop("relevance_category", None)
                    w.pop("relevance_reason", None)
            self.deleted_pauses = data.get("deleted_pauses", [])
            self.padding_sec = float(data.get("padding_sec", self.padding_sec))
            self.last_updated = mtime
        except Exception:
            pass

    def _push_undo(self) -> None:
        snapshot = {
            "deleted_indices": [w["index"] for w in self.words if w.get("deleted", False)],
            "deleted_pauses": copy.deepcopy(self.deleted_pauses),
        }
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def load_transcript_data(
        self,
        transcript_data: Dict[str, Any],
        asset_id: Optional[str] = None,
        source_duration: Optional[float] = None,
        drift_clips: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        self.file_path = transcript_data["file_path"]
        self.asset_id = asset_id or self.asset_id
        self.profile = transcript_data.get("profile", self.profile)
        self.save_preferred_profile(self.profile)
        self.language = transcript_data.get("language", "pt")
        self.words = copy.deepcopy(transcript_data.get("words", []))
        self.deleted_pauses = []
        self.undo_stack.clear()
        self.redo_stack.clear()

        if source_duration and source_duration > 0:
            self.source_duration = float(source_duration)
        elif self.words:
            self.source_duration = max(float(self.words[-1]["end"]) + 1.0, self.source_duration)

        # Restore saved session deletions if present, otherwise inspect current Drift timeline cuts
        sf = self._state_file_path(self.file_path)
        restored_from_session = False
        if sf.exists():
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                saved_del = set(saved.get("deleted_indices", []))
                rel_map = saved.get("relevance_map", {})
                if saved_del:
                    for w in self.words:
                        w["deleted"] = w["index"] in saved_del
                    self.deleted_pauses = saved.get("deleted_pauses", [])
                    restored_from_session = True
                if rel_map:
                    for w in self.words:
                        rel = rel_map.get(str(w["index"]))
                        if rel:
                            w["relevance_score"] = rel.get("score")
                            w["relevance_category"] = rel.get("category")
                            w["relevance_reason"] = rel.get("reason")
            except Exception:
                pass

        if not restored_from_session and drift_clips:
            cut_idxs = set(detect_cut_words_from_timeline_clips(self.words, drift_clips))
            for w in self.words:
                w["deleted"] = w["index"] in cut_idxs

        self.save_session()
        self.last_updated = time.time()
        return self.get_full_state(drift_clips=drift_clips)

    def set_words_deleted(
        self,
        indices: List[int],
        deleted: bool = True,
        sync_drift: Optional[bool] = None,
    ) -> Dict[str, Any]:
        self.reload_session_flags_from_disk()
        if not self.words:
            return {"ok": False, "error": "no_transcript_loaded"}

        self._push_undo()
        idx_set = set(int(i) for i in indices if 0 <= int(i) < len(self.words))
        changed = 0
        for idx in idx_set:
            if bool(self.words[idx].get("deleted", False)) != bool(deleted):
                self.words[idx]["deleted"] = bool(deleted)
                changed += 1

        self.save_session()
        self.last_updated = time.time()

        should_sync = self.auto_sync_drift if sync_drift is None else bool(sync_drift)
        drift_sync_result = None
        if should_sync and self.asset_id:
            drift_sync_result = self.sync_to_drift()

        state = self.get_full_state()
        state["changed_words"] = changed
        state["drift_sync"] = drift_sync_result
        return state

    def set_words_relevance(
        self,
        annotations: Optional[List[Dict[str, Any]]] = None,
        clear: bool = False,
    ) -> Dict[str, Any]:
        """
        Assign relevance scores (`0.0` red -> `0.5` yellow -> `1.0` green) or `category='off_topic'` (blue)
        to word ranges `[start_index, end_index]` or `indices`. Pass `clear=True` to reset all words to white.
        """
        self.reload_session_flags_from_disk()
        if not self.words:
            return {"ok": False, "error": "no_transcript_loaded"}

        if clear:
            for w in self.words:
                w.pop("relevance_score", None)
                w.pop("relevance_category", None)
                w.pop("relevance_reason", None)

        updated = 0
        if annotations:
            for item in annotations:
                cat = str(item.get("category") or "relevant").strip().lower()
                if cat in ("blue", "off_topic", "desvio", "fora_do_assunto", "interrupcao"):
                    cat = "off_topic"
                score_raw = item.get("score")
                score = round(max(0.0, min(1.0, float(score_raw))), 3) if score_raw is not None else (0.0 if cat == "off_topic" else 0.5)
                reason = str(item.get("reason") or "").strip() or None

                target_idxs = set()
                if "indices" in item and isinstance(item["indices"], list):
                    for idx in item["indices"]:
                        target_idxs.add(int(idx))
                if "start_index" in item and "end_index" in item:
                    a, b = int(item["start_index"]), int(item["end_index"])
                    if a > b:
                        a, b = b, a
                    for idx in range(a, b + 1):
                        target_idxs.add(idx)

                for idx in target_idxs:
                    if 0 <= idx < len(self.words):
                        self.words[idx]["relevance_score"] = score
                        self.words[idx]["relevance_category"] = cat
                        if reason:
                            self.words[idx]["relevance_reason"] = reason
                        updated += 1

        self.save_session()
        self.last_updated = time.time()
        state = self.get_full_state()
        state["updated_relevance_count"] = updated
        return state

    def toggle_pause_deleted(
        self,
        start_sec: float,
        end_sec: float,
        deleted: Optional[bool] = None,
        sync_drift: Optional[bool] = None,
    ) -> Dict[str, Any]:
        self.reload_session_flags_from_disk()
        self._push_undo()

        s = round(float(start_sec), 3)
        e = round(float(end_sec), 3)
        existing_idx = None
        for idx, p in enumerate(self.deleted_pauses):
            if abs(p["start"] - s) < 0.05 and abs(p["end"] - e) < 0.05:
                existing_idx = idx
                break

        if deleted is True and existing_idx is None:
            self.deleted_pauses.append({"start": s, "end": e})
        elif deleted is False and existing_idx is not None:
            self.deleted_pauses.pop(existing_idx)
        elif deleted is None:
            if existing_idx is not None:
                self.deleted_pauses.pop(existing_idx)
            else:
                self.deleted_pauses.append({"start": s, "end": e})

        self.save_session()
        self.last_updated = time.time()

        should_sync = self.auto_sync_drift if sync_drift is None else bool(sync_drift)
        drift_sync_result = None
        if should_sync and self.asset_id:
            drift_sync_result = self.sync_to_drift()

        state = self.get_full_state()
        state["drift_sync"] = drift_sync_result
        return state

    def undo(self) -> Dict[str, Any]:
        if not self.undo_stack:
            return self.get_full_state()
        current = {
            "deleted_indices": [w["index"] for w in self.words if w.get("deleted", False)],
            "deleted_pauses": copy.deepcopy(self.deleted_pauses),
        }
        self.redo_stack.append(current)
        prev = self.undo_stack.pop()
        del_set = set(prev["deleted_indices"])
        for w in self.words:
            w["deleted"] = w["index"] in del_set
        self.deleted_pauses = prev["deleted_pauses"]
        self.save_session()
        self.last_updated = time.time()
        if self.auto_sync_drift and self.asset_id:
            self.sync_to_drift()
        return self.get_full_state()

    def redo(self) -> Dict[str, Any]:
        if not self.redo_stack:
            return self.get_full_state()
        current = {
            "deleted_indices": [w["index"] for w in self.words if w.get("deleted", False)],
            "deleted_pauses": copy.deepcopy(self.deleted_pauses),
        }
        self.undo_stack.append(current)
        nxt = self.redo_stack.pop()
        del_set = set(nxt["deleted_indices"])
        for w in self.words:
            w["deleted"] = w["index"] in del_set
        self.deleted_pauses = nxt["deleted_pauses"]
        self.save_session()
        self.last_updated = time.time()
        if self.auto_sync_drift and self.asset_id:
            self.sync_to_drift()
        return self.get_full_state()

    def compute_ranges(self) -> Dict[str, List[Dict[str, Any]]]:
        rms = None
        if self.file_path and Path(self.file_path).exists():
            try:
                rms = get_or_compute_rms_10ms(Path(self.file_path))
            except Exception:
                rms = None
        return compute_keep_and_deleted_ranges(
            self.words,
            source_duration=self.source_duration,
            deleted_pauses=self.deleted_pauses,
            padding_sec=self.padding_sec,
            rms_10ms=rms,
        )

    def sync_to_drift(self) -> Dict[str, Any]:
        self.reload_session_flags_from_disk()
        if not self.asset_id or not self.words:
            return {"ok": False, "error": "no_active_asset_or_transcript"}
        ranges_info = self.compute_ranges()
        res = self.drift.sync_asset_keep_ranges(
            asset_id=self.asset_id,
            keep_ranges=ranges_info["keep_ranges"],
            declick=self.declick_sec,
        )
        return res

    def seek_to_word(self, word_index: int) -> Dict[str, Any]:
        self.reload_session_flags_from_disk()
        if not self.words or not (0 <= word_index < len(self.words)):
            return {"ok": False, "error": "word_index_out_of_range"}

        w = self.words[word_index]
        src_t = float(w["start"])
        self.last_Seek_word_index = word_index

        # Map `src_t` to current Drift timeline time
        summary = self.drift.get_media_assets_summary()
        clips = []
        if summary.get("ok") and self.asset_id:
            asset_obj = next((a for a in summary.get("assets", []) if a["asset_id"] == self.asset_id), None)
            if asset_obj:
                clips = asset_obj.get("clips", [])

        tl_t, is_inside, clip_id = source_to_timeline(src_t, clips)
        drift_res = self.drift.seek(tl_t)
        return {
            "ok": True,
            "word_index": word_index,
            "word": w["word"],
            "deleted": bool(w.get("deleted", False)),
            "source_time": round(src_t, 3),
            "timeline_time": round(tl_t, 3),
            "is_inside_kept_clip": is_inside,
            "clip_id": clip_id,
            "drift_seek": drift_res,
        }

    def analyze_candidates(self, min_pause_sec: float = 0.8) -> Dict[str, Any]:
        """Identify candidate filler words, immediate repetitions/stutters, and long pauses."""
        self.reload_session_flags_from_disk()
        fillers = []
        repetitions = []
        hesitations = []
        long_pauses = []

        for i, w in enumerate(self.words):
            raw = w["word"].strip()
            norm = _normalize_token(raw)
            ctx_start = max(0, i - 3)
            ctx_end = min(len(self.words), i + 4)
            context = " ".join(
                (f"[{x['index']}:~{x['word']}~]" if x.get("deleted") else f"[{x['index']}:{x['word']}]")
                for x in self.words[ctx_start:ctx_end]
            )

            if raw.endswith("..."):
                hesitations.append({"index": i, "word": raw, "start": w["start"], "end": w["end"], "deleted": w.get("deleted", False), "context": context})
            elif norm in PT_FILLERS or raw.lower() in PT_FILLERS:
                fillers.append({"index": i, "word": raw, "start": w["start"], "end": w["end"], "deleted": w.get("deleted", False), "context": context})

            if i + 1 < len(self.words):
                next_norm = _normalize_token(self.words[i + 1]["word"])
                gap = float(self.words[i + 1]["start"]) - float(w["end"])
                if norm and norm == next_norm and gap < 0.6:
                    repetitions.append(
                        {
                            "index": i,
                            "repeated_with": i + 1,
                            "word": raw,
                            "start": w["start"],
                            "end": w["end"],
                            "deleted": w.get("deleted", False),
                            "context": context,
                        }
                    )
                if gap >= min_pause_sec:
                    long_pauses.append(
                        {
                            "after_word_index": i,
                            "start": round(float(w["end"]), 3),
                            "end": round(float(self.words[i + 1]["start"]), 3),
                            "duration": round(gap, 3),
                            "context": context,
                        }
                    )

        return {
            "fillers": fillers,
            "repetitions": repetitions,
            "hesitations": hesitations,
            "long_pauses": long_pauses,
        }

    def get_full_state(self, drift_clips: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        self.reload_session_flags_from_disk()
        segments = build_segments_and_pauses(self.words)
        ranges_info = self.compute_ranges()

        if drift_clips is None and self.asset_id:
            summary = self.drift.get_media_assets_summary()
            if summary.get("ok"):
                asset_obj = next((a for a in summary.get("assets", []) if a["asset_id"] == self.asset_id), None)
                if asset_obj:
                    drift_clips = asset_obj.get("clips", [])

        deleted_count = sum(1 for w in self.words if w.get("deleted", False))
        kept_duration = sum(r["end"] - r["start"] for r in ranges_info["keep_ranges"])
        cut_duration = max(0.0, self.source_duration - kept_duration)

        return {
            "ok": True,
            "asset_id": self.asset_id,
            "file_path": self.file_path,
            "source_duration": round(self.source_duration, 3),
            "kept_duration": round(kept_duration, 3),
            "cut_duration": round(cut_duration, 3),
            "profile": self.profile,
            "language": self.language,
            "padding_sec": self.padding_sec,
            "declick_sec": self.declick_sec,
            "auto_sync_drift": self.auto_sync_drift,
            "total_words": len(self.words),
            "deleted_words_count": deleted_count,
            "deleted_pauses": self.deleted_pauses,
            "can_undo": len(self.undo_stack) > 0,
            "can_redo": len(self.redo_stack) > 0,
            "words": self.words,
            "segments": segments,
            "deleted_ranges": ranges_info["deleted_ranges"],
            "keep_ranges": ranges_info["keep_ranges"],
            "drift_clips": drift_clips or [],
        }
