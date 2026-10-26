"""
Bi-directional Time Synchronization Engine between Source Media Time and CutWire Drift Timeline Time.
Handles:
1. Exact conversion `source_to_timeline` and `timeline_to_source` across multiple split/rippled clips.
2. Detection of already-cut words from existing Drift timeline clips.
3. Calculation of `keep_ranges` and `deleted_ranges` with acoustic padding and valley snapping.
"""

from typing import Any, Dict, List, Optional, Tuple
import numpy as np


def source_to_timeline(src_time: float, clips: List[Dict[str, Any]]) -> Tuple[float, bool, Optional[str]]:
    """
    Convert a timestamp in the original source video (`src_time` in seconds)
    to the exact playhead position on the Drift timeline (`timeline_time` in seconds).

    Returns:
        (timeline_time, is_inside_kept_clip, clip_id)
    """
    if not clips:
        return round(max(0.0, src_time), 4), True, None

    sorted_clips = sorted(clips, key=lambda c: c["inPoint"])

    for c in sorted_clips:
        in_pt = float(c["inPoint"])
        out_pt = float(c["outPoint"])
        tl_start = float(c["start"])
        speed = float(c.get("speed", 1.0) or 1.0)

        if in_pt - 0.005 <= src_time <= out_pt + 0.005:
            clamped = max(in_pt, min(out_pt, src_time))
            tl_time = tl_start + (clamped - in_pt) / speed
            return round(tl_time, 4), True, c.get("id")

    # If src_time falls inside a cut gap (a struck-through word), snap to the cut boundary on the timeline
    if src_time < float(sorted_clips[0]["inPoint"]):
        return round(float(sorted_clips[0]["start"]), 4), False, sorted_clips[0].get("id")

    for i in range(len(sorted_clips) - 1):
        cur_c = sorted_clips[i]
        next_c = sorted_clips[i + 1]
        if float(cur_c["outPoint"]) < src_time < float(next_c["inPoint"]):
            # Land right at the cut junction (start of next_c)
            return round(float(next_c["start"]), 4), False, next_c.get("id")

    last = sorted_clips[-1]
    return round(float(last["start"]) + float(last["duration"]), 4), False, last.get("id")


def timeline_to_source(tl_time: float, clips: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[str]]:
    """
    Convert a playhead position on the Drift timeline (`tl_time` in seconds)
    back to the corresponding timestamp in the source media file (`src_time`).

    Returns:
        (src_time_or_None, clip_id_or_None)
    """
    if not clips:
        return round(max(0.0, tl_time), 4), None

    sorted_clips = sorted(clips, key=lambda c: c["start"])
    for c in sorted_clips:
        tl_start = float(c["start"])
        tl_end = tl_start + float(c["duration"])
        if tl_start - 0.005 <= tl_time <= tl_end + 0.005:
            speed = float(c.get("speed", 1.0) or 1.0)
            offset = max(0.0, min(float(c["duration"]), tl_time - tl_start))
            src_t = float(c["inPoint"]) + offset * speed
            return round(src_t, 4), c.get("id")

    return None, None


def detect_cut_words_from_timeline_clips(
    words: List[Dict[str, Any]],
    clips: List[Dict[str, Any]],
) -> List[int]:
    """
    Given a list of words (with `start`, `end` in source time) and the current clips
    of that asset on the Drift timeline, return the indices of words that are currently
    cut out on the Drift timeline.
    """
    if not clips or not words:
        return []

    kept_intervals = [(float(c["inPoint"]), float(c["outPoint"])) for c in clips]
    cut_indices: List[int] = []

    for idx, w in enumerate(words):
        w_start = float(w["start"])
        w_end = float(w["end"])
        w_mid = 0.5 * (w_start + w_end)
        # Check if midpoint or >60% of the word is inside any kept interval
        is_kept = False
        for k_in, k_out in kept_intervals:
            if k_in - 0.015 <= w_mid <= k_out + 0.015:
                is_kept = True
                break
            overlap = min(w_end, k_out) - max(w_start, k_in)
            if overlap > 0 and overlap >= 0.55 * max(0.01, w_end - w_start):
                is_kept = True
                break
        if not is_kept:
            cut_indices.append(idx)

    return cut_indices


def snap_to_rms_valley(
    target_sec: float,
    min_sec: float,
    max_sec: float,
    rms_10ms: Optional[np.ndarray],
) -> float:
    """
    If a 10ms RMS energy envelope is available, find the quietest point (minimum energy valley)
    within [min_sec, max_sec] near target_sec to avoid cutting inside a phoneme.
    """
    clamped = max(min_sec, min(max_sec, target_sec))
    if rms_10ms is None or len(rms_10ms) == 0 or max_sec - min_sec < 0.015:
        return round(clamped, 4)

    f0 = max(0, min(len(rms_10ms) - 1, int(round(min_sec * 100))))
    f1 = max(f0 + 1, min(len(rms_10ms), int(round(max_sec * 100)) + 1))
    window = rms_10ms[f0:f1]
    if len(window) == 0:
        return round(clamped, 4)

    best_rel = int(np.argmin(window))
    snapped = (f0 + best_rel) * 0.01
    return round(max(min_sec, min(max_sec, snapped)), 4)


def trim_cut_to_silent_core(
    cut_start: float,
    cut_end: float,
    rms_10ms: Optional[np.ndarray],
) -> Tuple[float, float]:
    """
    When cutting a pause, ensure we never cut into a trailing vowel release at `cut_start`
    or a leading consonant/breath onset at `cut_end`. Shrinking the cut inward to the truly
    quiet core avoids the "picotado" (clipped phoneme edge) effect.
    """
    if rms_10ms is None or len(rms_10ms) == 0 or cut_end - cut_start < 0.08:
        return cut_start, cut_end

    f0 = max(0, min(len(rms_10ms) - 1, int(round(cut_start * 100))))
    f1 = max(f0 + 1, min(len(rms_10ms), int(round(cut_end * 100))))
    window = rms_10ms[f0:f1]
    if len(window) < 4:
        return cut_start, cut_end

    # Silence ceiling inside this pause: 25th percentile * 2.2 (or at least 0.006)
    local_quiet = float(np.percentile(window, 25))
    silence_ceil = max(local_quiet * 2.2, 0.006)

    quiet_idx = np.where(window <= silence_ceil)[0]
    if len(quiet_idx) >= 4:
        # Advance cut_start past any trailing word decay at the left edge
        new_start = (f0 + int(quiet_idx[0])) * 0.01
        # Pull cut_end before any leading word onset at the right edge
        new_end = (f0 + int(quiet_idx[-1]) + 1) * 0.01
        cut_start = max(cut_start, new_start)
        cut_end = min(cut_end, new_end)

    return round(cut_start, 4), round(cut_end, 4)


def compute_keep_and_deleted_ranges(
    words: List[Dict[str, Any]],
    source_duration: float,
    deleted_pauses: Optional[List[Dict[str, float]]] = None,
    padding_sec: float = 0.055,
    rms_10ms: Optional[np.ndarray] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Compute exact `deleted_ranges` and `keep_ranges` in source seconds from:
    - `words`: list of word/pause dicts with `start`, `end`, `deleted` (bool), `kind` ("word"|"pause")
    - `source_duration`: total duration of the source media file in seconds
    - `deleted_pauses`: optional list of explicit silence ranges `[{start, end}]` cut by the user
    - `padding_sec`: protective air (in seconds, default 0.055s = 55ms) preserved around neighboring kept words

    Guarantees:
    1. Consecutive deleted tokens merge into a single contiguous cut.
    2. Pause-only cuts preserve a generous natural transition cushion (`>= 90ms` on each side = `180ms` total cadence air)
       and shrink inward to the truly silent acoustic core so word tails/onsets are never clipped.
    3. Micro-cuts shorter than `80ms` (`0.08s`) are ignored so the audio/video never sounds "picotado".
    """
    dur = max(0.1, float(source_duration))
    if not words:
        return {
            "deleted_ranges": [],
            "keep_ranges": [{"start": 0.0, "end": round(dur, 4)}],
        }

    n = len(words)
    raw_cuts: List[Dict[str, Any]] = []

    # 1. Group consecutive deleted tokens into contiguous runs [run_start_idx .. run_end_idx]
    i = 0
    while i < n:
        if not words[i].get("deleted", False):
            i += 1
            continue

        run_start = i
        while i + 1 < n and words[i + 1].get("deleted", False):
            i += 1
        run_end = i

        w_first = words[run_start]
        w_last = words[run_end]

        is_pause_only = all(words[k].get("kind") == "pause" for k in range(run_start, run_end + 1))
        is_granular_or_stretch = all(
            words[k].get("kind") == "stretch" or "pause_symbol" in words[k]
            for k in range(run_start, run_end + 1)
        )

        if is_granular_or_stretch:
            # Granular dot/dash silence tokens (`•`, `—`, `(tempo)`) or stretched syllable tails (`〰`):
            # Cut the exact token slice selected by the user, with a 20ms guard only when touching a spoken word directly.
            cut_start = float(w_first["start"])
            if run_start > 0:
                prev_w = words[run_start - 1]
                if prev_w.get("kind") == "word" and w_first.get("kind") != "stretch":
                    cut_start = max(cut_start, float(prev_w["end"]) + 0.020)

            cut_end = float(w_last["end"])
            if run_end < n - 1:
                next_w = words[run_end + 1]
                if next_w.get("kind") == "word":
                    cut_end = min(cut_end, float(next_w["start"]) - 0.020)

            min_cut_len = 0.035
        else:
            # Pause cuts without granular dots get a wider natural speech cadence cushion (90ms each side)
            eff_pad = max(padding_sec, 0.090) if is_pause_only else max(padding_sec, 0.050)

            # Determine safe left boundary of the cut (always protect prev kept token end + eff_pad)
            if run_start > 0:
                prev_w = words[run_start - 1]
                prev_end = float(prev_w["end"])
                first_start = float(w_first["start"])
                gap_left = first_start - prev_end
                safe_left = prev_end + eff_pad
                if gap_left <= 0.45:
                    target_left = safe_left
                else:
                    target_left = max(safe_left, first_start - eff_pad)
                upper_left = max(target_left, min(float(w_last["end"]) - 0.02, target_left + 0.04))
                cut_start = snap_to_rms_valley(target_left, safe_left, upper_left, rms_10ms)
            else:
                first_start = float(w_first["start"])
                cut_start = max(0.0, first_start - eff_pad) if w_first.get("kind") != "pause" else 0.0

            # Determine safe right boundary of the cut (always protect next kept token start - eff_pad)
            if run_end < n - 1:
                next_w = words[run_end + 1]
                next_start = float(next_w["start"])
                last_end = float(w_last["end"])
                gap_right = next_start - last_end
                safe_right = next_start - eff_pad
                if gap_right <= 0.45:
                    target_right = safe_right
                else:
                    target_right = min(safe_right, last_end + eff_pad)
                lower_right = min(target_right, max(cut_start + 0.02, target_right - 0.04))
                cut_end = snap_to_rms_valley(target_right, lower_right, safe_right, rms_10ms)
            else:
                last_end = float(w_last["end"])
                cut_end = min(dur, last_end + eff_pad)

            if is_pause_only:
                cut_start, cut_end = trim_cut_to_silent_core(cut_start, cut_end, rms_10ms)

            min_cut_len = 0.080 if is_pause_only else 0.030

        if cut_end - cut_start >= min_cut_len:
            raw_cuts.append(
                {
                    "start": round(cut_start, 4),
                    "end": round(cut_end, 4),
                    "word_indices": list(range(run_start, run_end + 1)),
                    "kind": "pause" if is_pause_only else "words",
                }
            )
        i += 1

    # 2. Include any explicitly deleted pause intervals
    if deleted_pauses:
        for p in deleted_pauses:
            ps = max(0.0, float(p.get("start", 0.0)) + 0.085)
            pe = min(dur, float(p.get("end", 0.0)) - 0.085)
            ps, pe = trim_cut_to_silent_core(ps, pe, rms_10ms)
            if pe - ps >= 0.080:
                raw_cuts.append(
                    {
                        "start": round(ps, 4),
                        "end": round(pe, 4),
                        "word_indices": [],
                        "kind": "pause",
                    }
                )

    # 3. Sort and merge overlapping or nearly-touching (< 0.04s) cut intervals
    raw_cuts.sort(key=lambda r: (r["start"], r["end"]))
    merged_cuts: List[Dict[str, Any]] = []
    for cut in raw_cuts:
        if not merged_cuts:
            merged_cuts.append(dict(cut))
        else:
            prev = merged_cuts[-1]
            if cut["start"] <= prev["end"] + 0.04:
                prev["end"] = round(max(prev["end"], cut["end"]), 4)
                prev["word_indices"] = sorted(set(prev["word_indices"] + cut["word_indices"]))
            else:
                merged_cuts.append(dict(cut))

    # 4. Complement `merged_cuts` against `[0.0, dur]` to produce base `keep_ranges`
    base_keep_ranges: List[Dict[str, float]] = []
    cursor = 0.0
    for cut in merged_cuts:
        if cut["start"] - cursor >= 0.05:
            base_keep_ranges.append({"start": round(cursor, 4), "end": round(cut["start"], 4), "speed": 1.0})
        cursor = max(cursor, cut["end"])

    if dur - cursor >= 0.05:
        base_keep_ranges.append({"start": round(cursor, 4), "end": round(dur, 4), "speed": 1.0})

    # 5. Collect accelerated non-deleted runs (`speed > 1.01`) and subdivide `base_keep_ranges`
    speed_ranges: List[Dict[str, Any]] = []
    j = 0
    while j < n:
        w = words[j]
        spd = float(w.get("speed", 1.0) or 1.0)
        if w.get("deleted", False) or spd <= 1.01:
            j += 1
            continue
        s_run_start = j
        while (
            j + 1 < n
            and not words[j + 1].get("deleted", False)
            and abs(float(words[j + 1].get("speed", 1.0) or 1.0) - spd) < 0.05
        ):
            j += 1
        s_run_end = j
        sr_start = round(float(words[s_run_start]["start"]), 4)
        sr_end = round(float(words[s_run_end]["end"]), 4)
        if sr_end - sr_start >= 0.04:
            speed_ranges.append(
                {
                    "start": sr_start,
                    "end": sr_end,
                    "speed": round(spd, 2),
                    "word_indices": list(range(s_run_start, s_run_end + 1)),
                }
            )
        j += 1

    if not speed_ranges:
        return {
            "deleted_ranges": merged_cuts,
            "keep_ranges": base_keep_ranges,
            "speed_ranges": [],
        }

    # Subdivide `base_keep_ranges` by `speed_ranges` so each segment in `keep_ranges` has its exact playback speed
    final_keep_ranges: List[Dict[str, float]] = []
    for kr in base_keep_ranges:
        k_start = kr["start"]
        k_end = kr["end"]
        cur = k_start
        for sr in speed_ranges:
            if sr["end"] <= cur + 0.03 or sr["start"] >= k_end - 0.03:
                continue
            ov_s = max(cur, sr["start"])
            ov_e = min(k_end, sr["end"])
            if ov_e - ov_s < 0.04:
                continue
            if ov_s - cur >= 0.04:
                final_keep_ranges.append({"start": round(cur, 4), "end": round(ov_s, 4), "speed": 1.0})
            final_keep_ranges.append({"start": round(ov_s, 4), "end": round(ov_e, 4), "speed": sr["speed"]})
            cur = ov_e
        if k_end - cur >= 0.04:
            final_keep_ranges.append({"start": round(cur, 4), "end": round(k_end, 4), "speed": 1.0})

    return {
        "deleted_ranges": merged_cuts,
        "keep_ranges": final_keep_ranges,
        "speed_ranges": speed_ranges,
    }
