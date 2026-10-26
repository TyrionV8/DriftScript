"""
Whisper Transcription Engine with:
- 3 explicit memory tiers: `fast` (small ~1.2GB), `normal` (medium ~2.8GB), `heavy` (large-v3 ~4.8GB)
- Strict GPU (CUDA fp16) preference and zero AVX2 requirement
- Acoustic RMS Island Boundary Refinement (`tighten_word_bounds`) for frame-accurate word timing
"""

import gc
import hashlib
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import (
    CACHE_DIR,
    LEGACY_CUTSCRIPT_CACHE_DIR,
    WHISPER_PROFILES,
    check_cpu_capabilities,
    get_gpu_vram_info,
    get_system_ram_info,
)

logger = logging.getLogger("driftscript.transcriber")

# Global runtime state for memory visibility
_active_model: Any = None
_active_profile_id: Optional[str] = None
_active_model_name: Optional[str] = None
_active_device: str = "none"
_transcription_status: Dict[str, Any] = {
    "running": False,
    "profile": None,
    "stage": "idle",
    "progress": 0,
    "message": "Pronto",
}
_rms_cache: Dict[str, np.ndarray] = {}


def get_runtime_memory_report() -> Dict[str, Any]:
    """Return full memory & model status for UI and MCP inspection."""
    gpu = get_gpu_vram_info()
    ram = get_system_ram_info()
    cpu = check_cpu_capabilities()
    return {
        "gpu": gpu,
        "ram": ram,
        "cpu": cpu,
        "loaded_profile": _active_profile_id,
        "loaded_model": _active_model_name,
        "active_device": _active_device,
        "profiles": WHISPER_PROFILES,
        "job": dict(_transcription_status),
    }


def unload_whisper_model() -> Dict[str, Any]:
    """Explicitly unload Whisper model from GPU VRAM and clear CUDA cache."""
    global _active_model, _active_profile_id, _active_model_name, _active_device
    _active_model = None
    _active_profile_id = None
    _active_model_name = None
    _active_device = "none"
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return get_runtime_memory_report()


def _file_signature(file_path: Path) -> str:
    st = file_path.stat()
    raw = f"{file_path.resolve()}|{st.st_size}|{st.st_mtime}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_audio_16k(file_path: Path) -> np.ndarray:
    """Decode any audio/video file to 16kHz mono float32 numpy array via FFmpeg (no AVX2 needed)."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-threads",
        "4",
        "-i",
        str(file_path),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, check=True)
    pcm = np.frombuffer(res.stdout, np.int16).astype(np.float32) / 32768.0
    return pcm


def get_or_compute_rms_10ms(file_path: Path, audio_16k: Optional[np.ndarray] = None) -> np.ndarray:
    """Compute 10ms (100 Hz) RMS energy envelope for acoustic boundary tightening & cut valley snapping."""
    key = str(file_path.resolve())
    if key in _rms_cache:
        return _rms_cache[key]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sig = _file_signature(file_path)
    rms_file = CACHE_DIR / f"{sig}_rms10ms.npy"
    if rms_file.exists():
        try:
            arr = np.load(rms_file)
            _rms_cache[key] = arr
            return arr
        except Exception:
            pass

    if audio_16k is None:
        audio_16k = load_audio_16k(file_path)

    hop = 160  # 10ms at 16000 Hz
    n_frames = len(audio_16k) // hop
    if n_frames <= 0:
        arr = np.zeros(1, dtype=np.float32)
    else:
        trimmed = audio_16k[: n_frames * hop].reshape(n_frames, hop)
        arr = np.sqrt(np.mean(trimmed * trimmed, axis=1)).astype(np.float32)

    try:
        np.save(rms_file, arr)
    except Exception:
        pass
    _rms_cache[key] = arr
    return arr


def tighten_word_bounds(words: List[Dict[str, Any]], rms_10ms: np.ndarray) -> Tuple[List[Dict[str, Any]], int]:
    """
    Refine word timestamps `[start, end]` using 10ms acoustic RMS energy with higher sensitivity
    to small between-word silences.
    IMPORTANT: Never splits multi-syllable words into islands (preserving voiceless plosives like /k/, /p/, /t/).
    Trims outer pure-silence heads/tails (>= 70ms) on words (> 0.22s), keeping a 30ms pre-roll and 40ms post-roll
    around the first and last active speech frames so small speech pauses (>= 0.10s) are cleanly exposed.
    """
    n_frames = len(rms_10ms)
    if n_frames == 0 or not words:
        return words, 0

    noise_floor = float(np.percentile(rms_10ms, 15))
    silence_ceil = max(noise_floor * 2.8, 0.004)
    tightened_count = 0

    for i, w in enumerate(words):
        s = float(w.get("start", 0.0))
        e = float(w.get("end", s + 0.1))
        if e <= s:
            e = s + 0.08
            w["end"] = round(e, 3)

        if (e - s) <= 0.22:
            continue

        f0 = max(0, min(n_frames - 1, int(round(s * 100))))
        f1 = max(f0 + 1, min(n_frames, int(round(e * 100))))
        win = rms_10ms[f0:f1]
        if len(win) == 0:
            continue

        local_peak = float(np.max(win))
        w_thresh = max(silence_ceil, local_peak * 0.045)
        active = np.where(win >= w_thresh)[0]
        if len(active) == 0:
            continue

        lead_silent_frames = int(active[0])
        trail_silent_frames = len(win) - 1 - int(active[-1])

        changed = False
        # Trim if >= 70ms (7 frames) of pure silence was absorbed at the start or end of the word
        if lead_silent_frames >= 7:
            new_s = (f0 + max(0, lead_silent_frames - 3)) * 0.01  # 30ms pre-roll
            if new_s < e - 0.07:
                w["start"] = round(max(s, new_s), 3)
                changed = True

        if trail_silent_frames >= 7:
            new_e = (f0 + min(len(win), int(active[-1]) + 4)) * 0.01  # 40ms post-roll
            if new_e > float(w["start"]) + 0.07:
                w["end"] = round(min(e, new_e), 3)
                changed = True

        if changed:
            tightened_count += 1

    # Ensure strictly monotonic non-overlapping boundaries
    for i in range(len(words) - 1):
        if words[i]["end"] > words[i + 1]["start"]:
            mid = round(0.5 * (words[i]["end"] + words[i + 1]["start"]), 3)
            words[i]["end"] = max(words[i]["start"] + 0.02, mid)
            words[i + 1]["start"] = max(words[i]["end"], words[i + 1]["start"])

    return words, tightened_count


def detect_and_split_stretched_words(
    words: List[Dict[str, Any]],
    rms_10ms: Optional[np.ndarray] = None,
) -> List[Dict[str, Any]]:
    """
    Identify words with stretched syllables (e.g. 'éeeeehhh', 'vamooooosss', 'entããão')
    and split the dragged-out tail into an attached `kind: 'stretch'` token (`〰0.35s`).
    Deleting or accelerating the `〰` token shortens the word back to its natural speaking duration.
    """
    import re

    only_words = [w for w in words if w.get("kind", "word") == "word"]
    if not only_words:
        return []

    n_frames = len(rms_10ms) if rms_10ms is not None else 0
    noise_floor = float(np.percentile(rms_10ms, 15)) if n_frames > 0 else 0.002
    vocal_floor = min(0.025, max(noise_floor * 2.2, 0.004))

    out: List[Dict[str, Any]] = []
    for w in only_words:
        w_copy = dict(w)
        w_copy["kind"] = "word"
        s = float(w_copy.get("start", 0.0))
        e = float(w_copy.get("end", s + 0.1))
        dur = e - s
        raw_text = str(w_copy.get("word", "")).strip()
        clean = re.sub(r"[^\wÀ-ÿ]", "", raw_text.lower())
        n_chars = max(1, len(clean))

        # Expected normal speaking duration based on word length
        if n_chars <= 2:
            normal_dur = 0.19
            min_total_for_stretch = 0.36 if not raw_text.endswith("...") else 0.30
        else:
            normal_dur = max(0.24, min(0.62, n_chars * 0.072))
            min_total_for_stretch = normal_dur + (0.16 if raw_text.endswith("...") else 0.22)

        is_stretched = False
        if dur >= min_total_for_stretch and (dur - normal_dur) >= 0.14:
            if n_frames > 0:
                f0 = max(0, min(n_frames - 1, int(round((s + normal_dur * 0.7) * 100))))
                f1 = max(f0 + 1, min(n_frames, int(round(e * 100))))
                tail_win = rms_10ms[f0:f1]
                # Confirm sustained vocal energy in the stretched tail
                if len(tail_win) > 0 and float(np.mean(tail_win >= vocal_floor)) >= 0.45:
                    is_stretched = True
            else:
                is_stretched = True

        if is_stretched:
            split_t = round(s + normal_dur, 3)
            stretch_dur = round(e - split_t, 3)
            w_copy["end"] = split_t
            w_copy["stretched"] = True
            out.append(w_copy)
            out.append(
                {
                    "index": 0,
                    "kind": "stretch",
                    "word": f"〰{stretch_dur:.2f}s",
                    "parent_word": raw_text,
                    "start": split_t,
                    "end": round(e, 3),
                    "duration": stretch_dur,
                    "confidence": w_copy.get("confidence", 0.85),
                    "deleted": False,
                }
            )
        else:
            w_copy["stretched"] = False
            out.append(w_copy)

    return out


def _build_silence_group(
    gap_start: float,
    gap_end: float,
    group_id: int,
    dot_unit_sec: float = 0.05,
    min_dots: int = 2,
) -> List[Dict[str, Any]]:
    """
    Decompose a silence `[gap_start, gap_end]` into individual selectable tokens following the rules:
    - 1 pontinho (`•`) = `dot_unit_sec` (0.05s = 50ms). Minimum silence = `min_dots` (2 pontinhos = 0.10s).
    - 1 traço (`—`) = 10 pontinhos (0.50s).
    - In any silence set, the FIRST and LAST tokens are ALWAYS pontinhos (`•`).
    - Therefore, a traço (`—`) only appears starting at 12 pontinhos (`•` + `—` + `•` = 1 + 10 + 1 = 12).
    - Silences larger than 4 traços (> 42 pontinhos, > 2.10s) appear as: `[•] (tempo do silêncio) [•]`.
    """
    gap = round(gap_end - gap_start, 4)
    if gap < dot_unit_sec * min_dots - 0.005:
        return []

    n_dots = max(min_dots, int(round(gap / dot_unit_sec)))
    unit = gap / n_dots

    parts: List[Tuple[str, int]] = []
    if n_dots < 12:
        parts = [("•", 1) for _ in range(n_dots)]
    elif n_dots <= 42:
        rem = n_dots - 2
        n_dashes = rem // 10  # 1 to 4 traços
        rem_dots = rem % 10
        parts = (
            [("•", 1)]
            + [("—", 10) for _ in range(n_dashes)]
            + [("•", 1) for _ in range(rem_dots)]
            + [("•", 1)]
        )
    else:
        # Larger than 4 traços (> 42 pontinhos): [ponto no começo] (tempo do silêncio) [ponto ao final]
        parts = [("•", 1), (f"({gap:.1f}s)", n_dots - 2), ("•", 1)]

    cursor = gap_start
    out: List[Dict[str, Any]] = []
    for pos_idx, (sym, weight) in enumerate(parts):
        t0 = round(cursor, 4)
        cursor += weight * unit
        t1 = round(gap_end if pos_idx == len(parts) - 1 else cursor, 4)
        pos_kind = "edge_start" if pos_idx == 0 else ("edge_end" if pos_idx == len(parts) - 1 else "inner")
        out.append(
            {
                "index": 0,
                "kind": "pause",
                "pause_symbol": sym,
                "pause_group_id": group_id,
                "pause_group_dur": round(gap, 3),
                "pause_group_dots": n_dots,
                "pause_pos": pos_kind,
                "word": sym,
                "start": t0,
                "end": t1,
                "duration": round(t1 - t0, 4),
                "confidence": 1.0,
                "deleted": False,
            }
        )
    return out


def inject_pause_tokens(
    words: List[Dict[str, Any]],
    min_pause_sec: float = 0.10,
    dot_unit_sec: float = 0.05,
) -> List[Dict[str, Any]]:
    """
    Insert granular pause tokens (`•`, `—`, `(tempo)`) wherever a silence gap >= `min_pause_sec` (default 0.10s)
    exists between spoken tokens. Preserves `word` and `stretch` tokens and strips old `pause` tokens.
    """
    non_pause = [w for w in words if w.get("kind", "word") != "pause"]
    if not non_pause:
        return []

    min_dots = max(2, int(round(min_pause_sec / dot_unit_sec)))
    tokens: List[Dict[str, Any]] = []
    group_id = 0

    first_start = float(non_pause[0]["start"])
    if first_start >= min_pause_sec:
        tokens.extend(_build_silence_group(0.0, first_start, group_id, dot_unit_sec, min_dots))
        group_id += 1

    for i, w in enumerate(non_pause):
        w_copy = dict(w)
        if "kind" not in w_copy:
            w_copy["kind"] = "word"
        tokens.append(w_copy)

        if i < len(non_pause) - 1:
            # Do not inject a silence between a word and its own attached stretch token
            if non_pause[i + 1].get("kind") == "stretch":
                continue
            cur_end = float(w["end"])
            nxt_start = float(non_pause[i + 1]["start"])
            gap = round(nxt_start - cur_end, 4)
            if gap >= min_pause_sec - 0.005:
                tokens.extend(_build_silence_group(cur_end, nxt_start, group_id, dot_unit_sec, min_dots))
                group_id += 1

    for idx, tok in enumerate(tokens):
        tok["index"] = idx
    return tokens


def build_segments_and_pauses(
    words: List[Dict[str, Any]],
    pause_threshold: float = 0.65,
    preserve_indices: bool = False,
) -> List[Dict[str, Any]]:
    """
    Group tokens (words, stretch tails, and granular pause tokens) into natural Descript-style paragraphs.
    Never splits a pause group across two paragraphs (only breaks on `pause_pos == 'edge_end'`).
    """
    if not words:
        return []

    for i, w in enumerate(words):
        if not preserve_indices or "index" not in w:
            w["index"] = i
        if "deleted" not in w:
            w["deleted"] = False
        if "kind" not in w:
            w["kind"] = "word"

    segments: List[Dict[str, Any]] = []
    cur_words: List[Dict[str, Any]] = []

    for i, w in enumerate(words):
        cur_words.append(w)
        is_pause_end = w.get("kind") == "pause" and w.get("pause_pos") == "edge_end"
        group_dur = float(w.get("pause_group_dur", 0.0)) if is_pause_end else 0.0

        # Find the last spoken word before this pause group to check sentence punctuation
        last_spoken_text = ""
        for prev_tok in reversed(cur_words):
            if prev_tok.get("kind") == "word":
                last_spoken_text = prev_tok.get("word", "").strip()
                break
        ends_sentence = last_spoken_text.endswith((".", "?", "!"))
        spoken_in_seg = sum(1 for cw in cur_words if cw.get("kind") == "word")

        # Break paragraph right after the closing dot of a longer pause or sentence end
        should_break = (
            (is_pause_end and group_dur >= pause_threshold and spoken_in_seg >= 6)
            or (is_pause_end and ends_sentence and spoken_in_seg >= 6)
            or (is_pause_end and spoken_in_seg >= 28)
        )
        if should_break or i == len(words) - 1:
            segments.append(
                {
                    "id": len(segments),
                    "start": cur_words[0]["start"],
                    "end": cur_words[-1]["end"],
                    "start_index": cur_words[0]["index"],
                    "end_index": cur_words[-1]["index"],
                    "text": " ".join(cw["word"] for cw in cur_words),
                    "word_indices": [cw["index"] for cw in cur_words],
                }
            )
            cur_words = []

    return segments


def _try_load_cached_transcript(file_path: Path, profile_id: str) -> Optional[Dict[str, Any]]:
    """
    Check DriftScript's own cache (`~/.driftscript_cache`) first, then check if a compatible
    transcription already exists in `~/.obs_transcriber_cache` and upgrade it with acoustic tightening,
    stretched syllable splitting, and granular dot/dash pause tokens.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sig = _file_signature(file_path)
    own_cache = CACHE_DIR / f"{sig}_{profile_id}_v4.json"

    if own_cache.exists():
        try:
            with open(own_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("words"):
                logger.info(f"Loaded DriftScript cached transcript: {own_cache}")
                return data
        except Exception as e:
            logger.warning(f"Failed reading cache {own_cache}: {e}")

    # Check legacy CutScript cache ONLY for models matching this specific profile
    profile = WHISPER_PROFILES.get(profile_id, WHISPER_PROFILES["normal"])
    candidate_models = profile.get("legacy_cache_models", [])
    for m_name in candidate_models:
        legacy_file = LEGACY_CUTSCRIPT_CACHE_DIR / f"{sig}_{m_name}_transcribe_wx.json"
        if legacy_file.exists():
            try:
                with open(legacy_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                payload = raw.get("data", {})
                raw_words = payload.get("words", [])
                if raw_words:
                    logger.info(f"Upgrading existing cached transcript ({m_name}) with v4 silences & stretched syllables...")
                    rms_10ms = get_or_compute_rms_10ms(file_path)
                    words_copy = [
                        {
                            "index": idx,
                            "kind": "word",
                            "word": str(w.get("word", "")).strip(),
                            "start": round(float(w.get("start", 0.0)), 3),
                            "end": round(float(w.get("end", 0.0)), 3),
                            "confidence": round(float(w.get("confidence", 0.8)), 3),
                            "deleted": False,
                        }
                        for idx, w in enumerate(raw_words)
                        if str(w.get("word", "")).strip()
                    ]
                    words_tight, n_fixed = tighten_word_bounds(words_copy, rms_10ms)
                    words_with_stretch = detect_and_split_stretched_words(words_tight, rms_10ms)
                    tokens_with_pauses = inject_pause_tokens(words_with_stretch, min_pause_sec=0.10, dot_unit_sec=0.05)
                    segments = build_segments_and_pauses(tokens_with_pauses)
                    upgraded = {
                        "file_path": str(file_path.resolve()),
                        "profile": profile_id,
                        "whisper_model": m_name,
                        "language": payload.get("language", "pt"),
                        "tightened_words_count": n_fixed,
                        "timestamp": time.time(),
                        "words": tokens_with_pauses,
                        "segments": segments,
                    }
                    with open(own_cache, "w", encoding="utf-8") as f:
                        json.dump(upgraded, f, ensure_ascii=False)
                    return upgraded
            except Exception as e:
                logger.warning(f"Failed upgrading legacy cache {legacy_file}: {e}")

    return None


def transcribe_media(
    file_path: str,
    profile_id: str = "normal",
    language: Optional[str] = "pt",
    force: bool = False,
) -> Dict[str, Any]:
    """
    Transcribe a media file using the selected memory profile (`fast`, `normal`, `heavy`).
    Always prioritizes GPU (`cuda:0` fp16) and never executes AVX2 CPU instructions.
    """
    global _active_model, _active_profile_id, _active_model_name, _active_device, _transcription_status

    path = Path(file_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de mídia não encontrado: {path}")

    if profile_id not in WHISPER_PROFILES:
        profile_id = "normal"
    profile = WHISPER_PROFILES[profile_id]
    model_name = profile["whisper_model"]

    if not force:
        cached = _try_load_cached_transcript(path, profile_id)
        if cached:
            _transcription_status.update(
                {
                    "running": False,
                    "profile": profile_id,
                    "stage": "done",
                    "progress": 100,
                    "message": f"Transcrição carregada do cache ({len(cached['words'])} palavras, {cached.get('tightened_words_count', 0)} ajustadas acusticamente).",
                }
            )
            return cached

    _transcription_status.update(
        {
            "running": True,
            "profile": profile_id,
            "stage": "extracting_audio",
            "progress": 10,
            "message": f"Extraindo áudio 16kHz de {path.name}...",
        }
    )

    try:
        audio_16k = load_audio_16k(path)
        rms_10ms = get_or_compute_rms_10ms(path, audio_16k)

        import torch

        cpu_caps = check_cpu_capabilities()
        use_cuda = torch.cuda.is_available()

        if not use_cuda and not cpu_caps["has_avx2"]:
            logger.warning("CUDA indisponível e CPU sem AVX2: utilizando PyTorch Whisper seguro sem CTranslate2.")

        _transcription_status.update(
            {
                "stage": "loading_model",
                "progress": 25,
                "message": f"Carregando modelo {profile['label']} ({model_name} • {profile['vram_display']}) na GPU...",
            }
        )

        words: List[Dict[str, Any]] = []
        detected_lang = language or "pt"

        if use_cuda:
            import whisperx

            if _active_model is None or _active_model_name != model_name:
                unload_whisper_model()
                _active_model = whisperx.load_model(
                    model_name,
                    device="cuda",
                    device_index=0,
                    compute_type=profile["compute_type_cuda"],
                )
                _active_profile_id = profile_id
                _active_model_name = model_name
                _active_device = "cuda:0 (float16)"

            _transcription_status.update(
                {
                    "stage": "transcribing",
                    "progress": 50,
                    "message": f"Transcrevendo na GPU ({model_name}, batch={profile['batch_size']})...",
                }
            )
            transcribe_opts = {"language": language} if language else {}
            result = _active_model.transcribe(audio_16k, batch_size=profile["batch_size"], **transcribe_opts)
            detected_lang = result.get("language", detected_lang)

            _transcription_status.update(
                {
                    "stage": "aligning",
                    "progress": 75,
                    "message": "Alinhando fonemas palavra por palavra na GPU (Wav2Vec2)...",
                }
            )
            align_model, align_metadata = whisperx.load_align_model(
                language_code=detected_lang,
                device="cuda",
            )
            aligned = whisperx.align(
                result["segments"],
                align_model,
                align_metadata,
                audio_16k,
                "cuda",
                return_char_alignments=False,
            )
            del align_model
            del align_metadata
            torch.cuda.empty_cache()

            raw_idx = 0
            for seg in aligned.get("segments", []):
                seg_start = float(seg.get("start", 0.0))
                seg_end = float(seg.get("end", seg_start + 0.2))
                seg_words = seg.get("words", [])
                n_sw = max(1, len(seg_words))
                for j, w in enumerate(seg_words):
                    w_text = str(w.get("word", "")).strip()
                    if not w_text:
                        continue
                    # Guard against missing start/end on numeric tokens
                    fallback_s = seg_start + (j / n_sw) * (seg_end - seg_start)
                    fallback_e = seg_start + ((j + 1) / n_sw) * (seg_end - seg_start)
                    ws = float(w["start"]) if w.get("start") is not None else fallback_s
                    we = float(w["end"]) if w.get("end") is not None else fallback_e
                    words.append(
                        {
                            "index": raw_idx,
                            "word": w_text,
                            "start": round(ws, 3),
                            "end": round(we, 3),
                            "confidence": round(float(w.get("score", 0.85)), 3),
                            "deleted": False,
                        }
                    )
                    raw_idx += 1
        else:
            # Safe PyTorch fallback that never invokes AVX2 CTranslate2 CPU instructions
            import whisper

            _active_model = whisper.load_model(model_name, device="cpu")
            _active_profile_id = profile_id
            _active_model_name = model_name
            _active_device = "cpu (PyTorch AVX-safe)"
            res = _active_model.transcribe(audio_16k, language=language, word_timestamps=True)
            detected_lang = res.get("language", detected_lang)
            raw_idx = 0
            for seg in res.get("segments", []):
                for w in seg.get("words", []):
                    w_text = str(w.get("word", "")).strip()
                    if not w_text:
                        continue
                    words.append(
                        {
                            "index": raw_idx,
                            "word": w_text,
                            "start": round(float(w.get("start", 0.0)), 3),
                            "end": round(float(w.get("end", 0.0)), 3),
                            "confidence": round(float(w.get("probability", 0.8)), 3),
                            "deleted": False,
                        }
                    )
                    raw_idx += 1

        _transcription_status.update(
            {
                "stage": "tightening",
                "progress": 90,
                "message": "Refinando fronteiras acústicas (RMS 10ms)...",
            }
        )
        words, n_fixed = tighten_word_bounds(words, rms_10ms)
        words = detect_and_split_stretched_words(words, rms_10ms)
        words = inject_pause_tokens(words, min_pause_sec=0.10, dot_unit_sec=0.05)
        segments = build_segments_and_pauses(words)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sig = _file_signature(path)
        own_cache = CACHE_DIR / f"{sig}_{profile_id}_v4.json"
        out_data = {
            "file_path": str(path),
            "profile": profile_id,
            "whisper_model": model_name,
            "language": detected_lang,
            "tightened_words_count": n_fixed,
            "timestamp": time.time(),
            "words": words,
            "segments": segments,
        }
        with open(own_cache, "w", encoding="utf-8") as f:
            json.dump(out_data, f, ensure_ascii=False)

        _transcription_status.update(
            {
                "running": False,
                "profile": profile_id,
                "stage": "done",
                "progress": 100,
                "message": f"Concluído: {len(words)} palavras ({n_fixed} sincronizadas por energia acústica).",
            }
        )
        return out_data

    except Exception as e:
        _transcription_status.update(
            {
                "running": False,
                "profile": profile_id,
                "stage": "error",
                "progress": 0,
                "message": f"Erro na transcrição: {e}",
            }
        )
        raise
