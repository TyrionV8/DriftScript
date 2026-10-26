/**
 * DriftScript — Descript-style Text Video Editor for CutWire Drift
 * - Snug text layout with wide square left-side scrollbar
 * - Granular silence tokens: • (1 pontinho = 0.05s), — (1 traço = 10 pontinhos), • (tempo) • (> 4 traços)
 * - Stretched syllable tokens: 〰0.35s attached to dragged-out words
 * - Left-click only selects / deselects tokens
 * - Spacebar toggles the active mode (Riscar or Acelerar 2x/4x/8x/16x/32x) across the entire selection
 * - Normal Play plays only the selection in a continuous loop
 */

const savedProfile = localStorage.getItem("driftscript_whisper_profile");

const state = {
  selectedProfile: savedProfile && ["fast", "normal", "heavy"].includes(savedProfile) ? savedProfile : "normal",
  profiles: {
    fast: { vram_estimate_gb: 1.2 },
    normal: { vram_estimate_gb: 2.8 },
    heavy: { vram_estimate_gb: 4.8 },
  },
  assetId: null,
  filePath: null,
  sourceDuration: 0,
  keptDuration: 0,
  cutDuration: 0,
  words: [],
  segments: [],
  deletedRanges: [],
  keepRanges: [],
  speedRanges: [],
  driftClips: [],
  selectedIndices: new Set(),
  anchorIndex: null,
  isDraggingSelection: false,
  dragAction: "select",
  spaceEffect: "cut", // "cut" (Riscar) | "accel" (Acelerar)
  loopSelectionMode: false,
  isTranscribing: false,
  activeWordIndex: -1,
  searchMatches: new Set(),
  auditionUntilSec: 0,
  lastSessionUpdated: 0,
  isSpeed2x: false,
  previewScale: 1,
  lastSkipWallMs: 0,
  lastTickWallMs: 0,
};

const el = {
  driftDot: document.getElementById("drift-dot"),
  assetSelect: document.getElementById("asset-select"),
  newDurationVal: document.getElementById("new-duration-val"),
  origDurationVal: document.getElementById("orig-duration-val"),
  profilePills: document.querySelectorAll(".w-pill"),
  resPills: document.querySelectorAll(".r-pill"),
  btnTranscribe: document.getElementById("btn-transcribe"),
  btnCopyPrompt: document.getElementById("btn-copy-prompt"),
  vramNumbers: document.getElementById("vram-numbers"),
  vramBarUsed: document.getElementById("vram-bar-used"),
  vramBarProjected: document.getElementById("vram-bar-projected"),
  btnUnloadVram: document.getElementById("btn-unload-vram"),
  chkAutoSync: document.getElementById("chk-auto-sync"),
  btnSyncNow: document.getElementById("btn-sync-now"),
  btnUndo: document.getElementById("btn-undo"),
  btnRedo: document.getElementById("btn-redo"),
  btnCutSelected: document.getElementById("btn-cut-selected"),
  tagSpaceCut: document.getElementById("tag-space-cut"),
  selectAccelSpeed: document.getElementById("select-accel-speed"),
  btnAccelSelected: document.getElementById("btn-accel-selected"),
  tagSpaceAccel: document.getElementById("tag-space-accel"),
  btnRestoreSelected: document.getElementById("btn-restore-selected"),
  btnClearSelection: document.getElementById("btn-clear-selection"),
  selectPauseRange: document.getElementById("select-pause-range"),
  chkKeepEdgeDots: document.getElementById("chk-keep-edge-dots"),
  btnSelectPauses: document.getElementById("btn-select-pauses"),
  selCount: document.getElementById("sel-count"),
  searchInput: document.getElementById("search-input"),
  searchCount: document.getElementById("search-count"),
  btnSelectSearchMatches: document.getElementById("btn-select-search-matches"),
  relEmptyTxt: document.getElementById("rel-empty-txt"),
  relSegGreen: document.getElementById("rel-seg-green"),
  relSegYellow: document.getElementById("rel-seg-yellow"),
  relSegRed: document.getElementById("rel-seg-red"),
  relSegBlue: document.getElementById("rel-seg-blue"),
  btnClearRelevance: document.getElementById("btn-clear-relevance"),
  statusBanner: document.getElementById("status-banner"),
  transcriptContainer: document.getElementById("transcript-container"),
  statSummary: document.getElementById("stat-summary"),
  videoPlayer: document.getElementById("video-player"),
  progressBar: document.getElementById("progress-bar"),
  progressFill: document.getElementById("progress-fill"),
  btnRestart: document.getElementById("btn-restart"),
  btnPlayPause: document.getElementById("btn-play-pause"),
  btnSpeed2x: document.getElementById("btn-speed-2x"),
  chkSkipDeleted: document.getElementById("chk-skip-deleted"),
  timeDisplay: document.getElementById("time-display"),
};

function formatTimeShort(seconds) {
  const s = Math.max(0, Number(seconds || 0));
  const mins = Math.floor(s / 60);
  const secs = Math.floor(s % 60);
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function syncProfilePillsUI(profile) {
  if (!["fast", "normal", "heavy"].includes(profile)) return;
  state.selectedProfile = profile;
  localStorage.setItem("driftscript_whisper_profile", profile);
  el.profilePills.forEach((p) => {
    p.classList.toggle("active", p.dataset.profile === profile);
  });
}

function setSpaceEffect(effect) {
  state.spaceEffect = effect === "accel" ? "accel" : "cut";
  const isCut = state.spaceEffect === "cut";
  el.btnCutSelected.classList.toggle("space-mode-active", isCut);
  el.btnAccelSelected.classList.toggle("space-mode-active", !isCut);
  if (el.tagSpaceCut) el.tagSpaceCut.classList.toggle("hidden", !isCut);
  if (el.tagSpaceAccel) el.tagSpaceAccel.classList.toggle("hidden", isCut);
}

function showBanner(msg, autoHideMs = 3200) {
  el.statusBanner.textContent = msg;
  el.statusBanner.classList.remove("hidden");
  if (autoHideMs > 0) {
    setTimeout(() => {
      if (el.statusBanner.textContent === msg) {
        el.statusBanner.classList.add("hidden");
      }
    }, autoHideMs);
  }
}

function findWordIndexAtTime(srcTime) {
  const words = state.words;
  if (!words.length) return -1;
  let lo = 0;
  let hi = words.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    const w = words[mid];
    if (w.end < srcTime) lo = mid + 1;
    else if (w.start > srcTime) hi = mid - 1;
    else return mid;
  }
  return -1;
}

function getSelectionBounds() {
  if (state.selectedIndices.size === 0 || !state.words.length) return null;
  let minStart = Infinity;
  let maxEnd = -Infinity;
  let playStart = Infinity;
  let playEnd = -Infinity;
  let hasNonDeleted = false;

  for (const idx of state.selectedIndices) {
    const w = state.words[idx];
    if (!w) continue;
    const s = Number(w.start || 0);
    const e = Number(w.end || s + 0.05);
    if (s < minStart) minStart = s;
    if (e > maxEnd) maxEnd = e;
    if (!w.deleted) {
      hasNonDeleted = true;
      if (s < playStart) playStart = s;
      if (e > playEnd) playEnd = e;
    }
  }

  if (!Number.isFinite(minStart) || !Number.isFinite(maxEnd)) return null;
  if (!hasNonDeleted || !el.chkSkipDeleted.checked) {
    playStart = minStart;
    playEnd = maxEnd;
  }
  return {
    start: minStart,
    end: Math.max(minStart + 0.04, maxEnd),
    playStart,
    playEnd: Math.max(playStart + 0.04, playEnd),
    allDeleted: !hasNonDeleted,
  };
}

function renderMemoryMonitor(mem) {
  if (!mem) return;
  if (mem.profiles) state.profiles = mem.profiles;
  const gpu = mem.gpu || {};
  if (gpu.available) {
    el.vramNumbers.textContent = `${gpu.used_gb} / ${gpu.total_gb} GB`;
    const usedPct = Math.min(100, gpu.percent || 0);
    el.vramBarUsed.style.width = `${usedPct}%`;
    const selProf = state.profiles[state.selectedProfile] || { vram_estimate_gb: 2.8 };
    const isAlreadyLoaded = mem.loaded_profile === state.selectedProfile;
    const projGb = isAlreadyLoaded ? 0 : selProf.vram_estimate_gb;
    const projPct = gpu.total_gb > 0 ? Math.min(100 - usedPct, (projGb / gpu.total_gb) * 100) : 0;
    el.vramBarProjected.style.width = `${projPct}%`;
  }
}

function renderDriftAssets(driftInfo) {
  if (!driftInfo) return;
  el.driftDot.className = `status-dot ${driftInfo.connected ? "dot-ok" : "dot-err"}`;
  el.driftDot.title = driftInfo.connected
    ? `Conectado ao Drift (${driftInfo.project_name || "Projeto"})`
    : `Drift desconectado`;

  const assets = driftInfo.assets || [];
  if (!assets.length) return;

  const currentVal = el.assetSelect.value || state.assetId;
  const curIds = Array.from(el.assetSelect.options).map((o) => o.value).join(",");
  const newIds = assets.map((a) => a.asset_id).join(",");

  if (curIds !== newIds) {
    el.assetSelect.innerHTML = "";
    for (const a of assets) {
      const opt = document.createElement("option");
      opt.value = a.asset_id;
      opt.textContent = `${a.name} (${formatTimeShort(a.duration)})`;
      el.assetSelect.appendChild(opt);
    }
    if (currentVal && assets.some((a) => a.asset_id === currentVal)) {
      el.assetSelect.value = currentVal;
    }
  }
}

function applyTranscriptState(data) {
  if (!data || !data.ok) return;

  const videoChanged = state.filePath !== data.file_path;
  state.assetId = data.asset_id;
  state.filePath = data.file_path;
  state.sourceDuration = Number(data.source_duration || 0);
  state.keptDuration = Number(data.kept_duration || 0);
  state.cutDuration = Number(data.cut_duration || 0);
  state.words = data.words || [];
  state.segments = data.segments || [];
  state.deletedRanges = data.deleted_ranges || [];
  state.keepRanges = data.keep_ranges || [];
  state.speedRanges = data.speed_ranges || [];
  state.driftClips = data.drift_clips || [];

  if (data.profile && !localStorage.getItem("driftscript_whisper_profile")) {
    syncProfilePillsUI(data.profile);
  }

  el.btnUndo.disabled = !data.can_undo;
  el.btnRedo.disabled = !data.can_redo;
  el.chkAutoSync.checked = Boolean(data.auto_sync_drift);

  if (videoChanged && state.filePath) {
    el.videoPlayer.src = `/api/media?path=${encodeURIComponent(state.filePath)}&scale=${state.previewScale}`;
    el.videoPlayer.playbackRate = state.isSpeed2x ? 2.0 : 1.0;
  }

  // Atualiza indicativo do Novo Comprimento do vídeo (abaixo do vídeo)
  el.newDurationVal.textContent = formatTimeShort(state.keptDuration);
  const diffStr = state.cutDuration >= 0.1 ? `-${formatTimeShort(state.cutDuration)}` : "0s";
  el.origDurationVal.textContent = `(de ${formatTimeShort(state.sourceDuration)} • ${diffStr})`;

  const spokenWords = state.words.filter((w) => w.kind === "word").length;
  const delCount = data.deleted_words_count || 0;
  el.statSummary.textContent = `${spokenWords} palavras • ${delCount} itens riscados • Novo: ${formatTimeShort(state.keptDuration)} (de ${formatTimeShort(state.sourceDuration)})`;
  renderRelevanceMeter();
  renderTranscriptDOM();
}

function getRelevanceColorCSS(score) {
  const s = Math.max(0, Math.min(1, Number(score)));
  const hue = Math.round(s * 138);
  return `hsl(${hue}, 88%, 64%)`;
}

function renderRelevanceMeter() {
  const spoken = state.words.filter((w) => w.kind === "word");
  const annotated = spoken.filter(
    (w) => (w.relevance_score !== undefined && w.relevance_score !== null) || w.relevance_category
  );

  if (!annotated.length) {
    el.relEmptyTxt.classList.remove("hidden");
    el.relSegGreen.style.width = "0%";
    el.relSegYellow.style.width = "0%";
    el.relSegRed.style.width = "0%";
    el.relSegBlue.style.width = "0%";
    el.btnClearRelevance.classList.add("hidden");
    return;
  }

  el.relEmptyTxt.classList.add("hidden");
  el.btnClearRelevance.classList.remove("hidden");

  let nGreen = 0, nYellow = 0, nRed = 0, nBlue = 0;
  for (const w of annotated) {
    if (w.relevance_category === "off_topic") nBlue++;
    else {
      const sc = Number(w.relevance_score ?? 0.5);
      if (sc >= 0.68) nGreen++;
      else if (sc >= 0.38) nYellow++;
      else nRed++;
    }
  }
  const tot = Math.max(1, annotated.length);
  el.relSegGreen.style.width = `${(nGreen / tot) * 100}%`;
  el.relSegYellow.style.width = `${(nYellow / tot) * 100}%`;
  el.relSegRed.style.width = `${(nRed / tot) * 100}%`;
  el.relSegBlue.style.width = `${(nBlue / tot) * 100}%`;
}

/**
 * Render Descript-style paragraphs with granular silence tokens (•, —, (tempo)) and stretched syllable tails (〰)
 */
function renderTranscriptDOM() {
  const container = el.transcriptContainer;
  if (!state.words.length) {
    container.innerHTML = `<p class="empty-msg">Nenhuma transcrição carregada. Clique em "Transcrever" no topo.</p>`;
    return;
  }

  const scrollPos = container.scrollTop;
  const frag = document.createDocumentFragment();

  for (const seg of state.segments) {
    const p = document.createElement("p");
    p.className = "doc-paragraph";

    const indices = seg.word_indices;
    for (let k = 0; k < indices.length; k++) {
      const wIdx = indices[k];
      const w = state.words[wIdx];
      if (!w) continue;

      const nextIdx = k + 1 < indices.length ? indices[k + 1] : -1;
      const nextW = nextIdx >= 0 ? state.words[nextIdx] : null;

      const span = document.createElement("span");
      span.id = `word-${wIdx}`;
      span.dataset.index = wIdx;
      span.className = "word-tok";

      if (w.kind === "pause") {
        span.classList.add("is-pause");
        if (w.pause_symbol === "—") span.classList.add("is-pause-dash");
        else if (w.pause_symbol && w.pause_symbol.startsWith("(")) span.classList.add("is-pause-time");
        span.title = `Silêncio: ${(w.duration || 0).toFixed(2)}s (Total do grupo: ${(w.pause_group_dur || 0).toFixed(2)}s)`;
        span.textContent = w.word;
        if (!nextW || nextW.kind !== "pause") {
          span.textContent += " ";
        }
      } else if (w.kind === "stretch") {
        span.classList.add("is-stretch");
        span.title = `Sílaba esticada de "${w.parent_word || ""}": +${(w.duration || 0).toFixed(2)}s`;
        span.textContent = w.word + " ";
      } else {
        const nextIsStretch = nextW && nextW.kind === "stretch";
        span.textContent = nextIsStretch ? w.word : w.word + " ";

        if (!w.deleted) {
          if (w.relevance_category === "off_topic") {
            span.classList.add("is-off-topic");
            span.title = `🔵 Fora do assunto / Desvio${w.relevance_reason ? ": " + w.relevance_reason : ""}`;
          } else if (w.relevance_score !== undefined && w.relevance_score !== null) {
            span.style.color = getRelevanceColorCSS(w.relevance_score);
            const pct = Math.round(Number(w.relevance_score) * 100);
            span.title = `Relevância: ${pct}%${w.relevance_reason ? " — " + w.relevance_reason : ""}`;
          }
        }
      }

      if (w.deleted) {
        span.classList.add("is-deleted");
      } else if (Number(w.speed || 1.0) > 1.01) {
        span.classList.add("is-accelerated");
        const prevIdx = k > 0 ? indices[k - 1] : -1;
        const prevW = prevIdx >= 0 ? state.words[prevIdx] : null;
        if (!prevW || prevW.deleted || Math.abs(Number(prevW.speed || 1.0) - Number(w.speed)) > 0.05) {
          const badge = document.createElement("span");
          badge.className = "accel-badge";
          badge.textContent = `⚡${Number(w.speed)}x`;
          span.prepend(badge);
        }
      }
      if (state.selectedIndices.has(wIdx)) span.classList.add("is-selected");
      if (state.searchMatches.has(wIdx)) span.classList.add("is-match");
      if (wIdx === state.activeWordIndex) span.classList.add("is-active");

      p.appendChild(span);
    }

    frag.appendChild(p);
  }

  container.innerHTML = "";
  container.appendChild(frag);
  container.scrollTop = scrollPos;
  updateSelectionUI();
}

function updateSelectionUI() {
  el.selCount.textContent = state.selectedIndices.size;
  if (state.selectedIndices.size === 0) {
    state.loopSelectionMode = false;
  }
  document.querySelectorAll(".word-tok.is-selected").forEach((node) => {
    const idx = Number(node.dataset.index);
    if (!state.selectedIndices.has(idx)) node.classList.remove("is-selected");
  });
  state.selectedIndices.forEach((idx) => {
    const node = document.getElementById(`word-${idx}`);
    if (node) node.classList.add("is-selected");
  });
}

async function seekToWord(wordIndex) {
  const w = state.words[wordIndex];
  if (!w) return;

  state.auditionUntilSec = w.deleted ? w.end + 0.08 : 0;

  if (el.videoPlayer.src) {
    el.videoPlayer.currentTime = w.start;
  }
  updatePlayheadUI(w.start);

  try {
    await fetch("/api/seek/word", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word_index: wordIndex }),
    });
  } catch (e) {
    // ignore
  }
}

async function setWordsDeleted(indices, deleted) {
  if (!indices || !indices.length) return;
  const resp = await fetch("/api/words/cut", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      indices: Array.from(indices),
      deleted: Boolean(deleted),
    }),
  });
  const data = await resp.json();
  applyTranscriptState(data);
}

async function setWordsSpeed(indices, speed) {
  if (!indices || !indices.length) return;
  const resp = await fetch("/api/words/speed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      indices: Array.from(indices),
      speed: Number(speed || 2.0),
    }),
  });
  const data = await resp.json();
  applyTranscriptState(data);
}

/**
 * Alterna (toggle) o efeito selecionado para a Barra de Espaço ("cut" = Riscar ou "accel" = Acelerar)
 * em toda a seleção atual.
 */
async function toggleSpaceEffectOnSelection() {
  if (state.selectedIndices.size === 0) {
    el.btnPlayPause.click();
    return;
  }

  const indices = Array.from(state.selectedIndices);
  if (state.spaceEffect === "cut") {
    const allDeleted = indices.every((idx) => Boolean(state.words[idx]?.deleted));
    const targetDeleted = !allDeleted;
    await setWordsDeleted(indices, targetDeleted);
    showBanner(
      targetDeleted
        ? `✂ ${indices.length} itens riscados na seleção.`
        : `↺ ${indices.length} itens desriscados na seleção.`
    );
  } else {
    const targetSpd = Number(el.selectAccelSpeed.value || 2);
    const allAtSpeed = indices.every((idx) => {
      const w = state.words[idx];
      return w && !w.deleted && Math.abs(Number(w.speed || 1.0) - targetSpd) < 0.05;
    });
    const newSpd = allAtSpeed ? 1.0 : targetSpd;
    await setWordsSpeed(indices, newSpd);
    showBanner(
      newSpd > 1.01
        ? `⚡ ${indices.length} itens acelerados em ${newSpd}x.`
        : `↺ ${indices.length} itens restaurados para 1x.`
    );
  }
}

function updatePlayheadUI(srcTime) {
  const dur = el.videoPlayer.duration || state.sourceDuration || 0;
  el.timeDisplay.textContent = `${formatTimeShort(srcTime)} / ${formatTimeShort(dur)}`;
  if (dur > 0) {
    el.progressFill.style.width = `${Math.min(100, (srcTime / dur) * 100)}%`;
  }

  const idx = findWordIndexAtTime(srcTime);
  if (idx !== state.activeWordIndex) {
    const prevNode = document.getElementById(`word-${state.activeWordIndex}`);
    if (prevNode) prevNode.classList.remove("is-active");
    state.activeWordIndex = idx;
    const newNode = document.getElementById(`word-${idx}`);
    if (newNode) newNode.classList.add("is-active");
  }
}

/**
 * Mouse & Keyboard events on the text editor:
 * - Left click on unselected token: selects it (adds to selection) and seeks player
 * - Left click on already-selected token: deselects it (removes from selection)
 * - Right click (contextmenu): DESRISCAR (restore) immediately
 * - Spacebar (Barra de Espaço): toggles active Spacebar mode (Riscar or Acelerar) across entire selection
 */
el.transcriptContainer.addEventListener("mousedown", (e) => {
  if (e.button === 2) return; // Botão direito tratado pelo contextmenu

  const wordNode = e.target.closest(".word-tok");
  if (!wordNode) {
    if (!e.shiftKey && !e.ctrlKey && !e.metaKey) {
      state.selectedIndices.clear();
      updateSelectionUI();
    }
    return;
  }

  const idx = Number(wordNode.dataset.index);

  if (e.shiftKey && state.anchorIndex !== null) {
    const a = Math.min(state.anchorIndex, idx);
    const b = Math.max(state.anchorIndex, idx);
    for (let k = a; k <= b; k++) state.selectedIndices.add(k);
    state.dragAction = "select";
    seekToWord(idx);
  } else {
    if (state.selectedIndices.has(idx)) {
      // Clicar num item já selecionado deve desselecionar
      state.selectedIndices.delete(idx);
      state.dragAction = "deselect";
    } else {
      // Clicar num item não selecionado deve selecionar
      state.selectedIndices.add(idx);
      state.anchorIndex = idx;
      state.dragAction = "select";
      seekToWord(idx);
    }
    state.isDraggingSelection = true;
  }
  updateSelectionUI();
});

// Botão direito desrisca o item clicado ou toda a seleção atual
el.transcriptContainer.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const wordNode = e.target.closest(".word-tok");
  if (wordNode) {
    const idx = Number(wordNode.dataset.index);
    if (state.selectedIndices.has(idx) && state.selectedIndices.size > 0) {
      setWordsDeleted(Array.from(state.selectedIndices), false);
      showBanner(`${state.selectedIndices.size} itens desriscados.`);
    } else {
      setWordsDeleted([idx], false);
    }
  } else if (state.selectedIndices.size > 0) {
    setWordsDeleted(Array.from(state.selectedIndices), false);
    showBanner(`${state.selectedIndices.size} itens desriscados.`);
  }
});

el.transcriptContainer.addEventListener("mouseover", (e) => {
  if (!state.isDraggingSelection) return;
  const wordNode = e.target.closest(".word-tok");
  if (!wordNode) return;
  const idx = Number(wordNode.dataset.index);
  if (state.dragAction === "deselect") {
    state.selectedIndices.delete(idx);
  } else {
    state.selectedIndices.add(idx);
  }
  updateSelectionUI();
});

window.addEventListener("mouseup", () => {
  state.isDraggingSelection = false;
});

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;

  if (e.key === "Escape") {
    state.selectedIndices.clear();
    updateSelectionUI();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    if (e.shiftKey) el.btnRedo.click();
    else el.btnUndo.click();
    return;
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
    e.preventDefault();
    el.btnRedo.click();
    return;
  }
  // Delete ou Backspace = RISCAR selecionados
  if (e.key === "Delete" || e.key === "Backspace") {
    if (state.selectedIndices.size > 0) {
      e.preventDefault();
      setWordsDeleted(Array.from(state.selectedIndices), true);
    }
    return;
  }
  // Barra de Espaço = Alterna o efeito ativo (Riscar ou Acelerar) em toda a seleção
  if (e.key === " " || e.code === "Space") {
    e.preventDefault();
    toggleSpaceEffectOnSelection();
    return;
  }
});

/**
 * Video Player Sync, Selection Loop Playback, Restart from Zero, 2x Speed, Dynamic Segment Acceleration (2x..32x), and Resolution Switcher
 */
function setupVideoPlayer() {
  const video = el.videoPlayer;

  const tick = () => {
    const nowMs = performance.now();
    const dtSec = state.lastTickWallMs > 0 ? Math.min(0.1, (nowMs - state.lastTickWallMs) / 1000) : 0.016;
    state.lastTickWallMs = nowMs;

    if (!video.paused && !video.seeking) {
      let t = video.currentTime;

      // Se o Play normal foi acionado com uma seleção ativa, toca apenas a seleção em loop
      if (state.loopSelectionMode && state.selectedIndices.size > 0) {
        const sel = getSelectionBounds();
        if (sel) {
          if (sel.allDeleted) {
            state.auditionUntilSec = sel.end + 0.25;
          }
          if (t >= sel.playEnd - 0.015 || t < sel.playStart - 0.15) {
            video.currentTime = sel.playStart;
            t = sel.playStart;
          }
        }
      }

      if (el.chkSkipDeleted.checked && t >= state.auditionUntilSec && nowMs - state.lastSkipWallMs > 65) {
        for (const r of state.deletedRanges) {
          if (t >= r.start && t < r.end - 0.025) {
            state.lastSkipWallMs = nowMs;
            // Se estiver em modo loop da seleção e o pulo sair da seleção, volta ao início da seleção
            if (state.loopSelectionMode && state.selectedIndices.size > 0) {
              const sel = getSelectionBounds();
              if (sel && r.end >= sel.playEnd - 0.015) {
                video.currentTime = sel.playStart;
                t = sel.playStart;
                break;
              }
            }
            video.currentTime = r.end;
            t = r.end;
            break;
          }
        }
      }

      // Aplica aceleração dinâmica quando o playhead entra num trecho acelerado (2x, 4x, 8x, 16x, 32x)
      const baseRate = state.isSpeed2x ? 2.0 : 1.0;
      let segMult = 1.0;
      for (const sr of state.speedRanges) {
        if (t >= sr.start && t < sr.end) {
          segMult = Number(sr.speed || 1.0);
          break;
        }
      }
      const desiredRate = Math.min(32.0, baseRate * segMult);
      const nativeRate = Math.min(16.0, Math.max(0.25, desiredRate));
      if (Math.abs(video.playbackRate - nativeRate) > 0.02) {
        try {
          video.playbackRate = nativeRate;
        } catch (err) {
          // ignore browser playbackRate range limit
        }
      }
      // Para 32x (acima do limite nativo de 16x do navegador), avança o tempo complementarmente
      if (desiredRate > 16.0 && dtSec > 0) {
        const extraStep = (desiredRate - nativeRate) * dtSec;
        video.currentTime = Math.min(video.duration || state.sourceDuration || t + extraStep, t + extraStep);
      }

      updatePlayheadUI(video.currentTime);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);

  video.addEventListener("play", () => {
    el.btnPlayPause.textContent = state.loopSelectionMode && state.selectedIndices.size > 0 ? "⏸ Loop" : "⏸ Pause";
  });
  video.addEventListener("pause", () => {
    el.btnPlayPause.textContent = "▶ Play";
  });
  video.addEventListener("ended", () => {
    if (state.loopSelectionMode && state.selectedIndices.size > 0) {
      const sel = getSelectionBounds();
      if (sel) {
        video.currentTime = sel.playStart;
        video.play();
      }
    }
  });
  video.addEventListener("seeked", () => {
    updatePlayheadUI(video.currentTime);
  });
  video.addEventListener("loadedmetadata", () => {
    video.playbackRate = state.isSpeed2x ? 2.0 : 1.0;
    updatePlayheadUI(video.currentTime);
  });

  // Botão Recomeçar do Zero (toca o vídeo inteiro desde 00:00, desativando o loop de seleção)
  el.btnRestart.addEventListener("click", () => {
    state.loopSelectionMode = false;
    state.auditionUntilSec = 0;
    video.currentTime = 0;
    video.play();
    fetch("/api/seek/time", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_time: 0, sync_drift_playhead: true }),
    });
  });

  // Botão Play / Pause (quando há seleção, toca apenas a seleção em loop)
  el.btnPlayPause.addEventListener("click", () => {
    if (video.paused) {
      const sel = getSelectionBounds();
      if (sel) {
        state.loopSelectionMode = true;
        if (sel.allDeleted) {
          state.auditionUntilSec = sel.end + 0.25;
        }
        if (video.currentTime < sel.playStart - 0.05 || video.currentTime >= sel.playEnd - 0.02) {
          video.currentTime = sel.playStart;
        }
      } else {
        state.loopSelectionMode = false;
      }
      video.play();
    } else {
      video.pause();
    }
  });

  // Botão 2x (Velocidade Rápida)
  el.btnSpeed2x.addEventListener("click", () => {
    state.isSpeed2x = !state.isSpeed2x;
    video.playbackRate = state.isSpeed2x ? 2.0 : 1.0;
    el.btnSpeed2x.classList.toggle("active-2x", state.isSpeed2x);
    el.btnSpeed2x.textContent = state.isSpeed2x ? "2x Ativo" : "2x";
  });

  // Seletor de Resolução (1x, 1/2, 1/4, 1/16)
  el.resPills.forEach((pill) => {
    pill.addEventListener("click", async () => {
      const scale = Number(pill.dataset.scale || 1);
      if (scale === state.previewScale || !state.filePath) return;

      el.resPills.forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      state.previewScale = scale;

      const savedTime = video.currentTime || 0;
      const wasPlaying = !video.paused;
      const label = scale === 1 ? "1x (Original)" : `1/${scale} da resolução`;

      if (scale > 1) {
        showBanner(`Preparando visualização leve em ${label} (busca rápida)...`, 0);
        try {
          await fetch("/api/proxy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: state.filePath, scale }),
          });
        } catch (e) {
          // fallback handled by /api/media
        }
      }

      video.src = `/api/media?path=${encodeURIComponent(state.filePath)}&scale=${scale}`;
      video.addEventListener(
        "loadedmetadata",
        () => {
          video.currentTime = savedTime;
          video.playbackRate = state.isSpeed2x ? 2.0 : 1.0;
          if (wasPlaying) video.play();
          showBanner(`Visualização alterada para ${label}.`, 2500);
        },
        { once: true }
      );
    });
  });

  // Clique na barra de progresso do vídeo
  el.progressBar.addEventListener("click", (e) => {
    const rect = el.progressBar.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const targetTime = ratio * (video.duration || 0);
    state.loopSelectionMode = false;
    video.currentTime = targetTime;
    fetch("/api/seek/time", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_time: targetTime, sync_drift_playhead: true }),
    });
  });
}

/**
 * Topbar & Toolbar Controls
 */
syncProfilePillsUI(state.selectedProfile);
setSpaceEffect("cut");

el.profilePills.forEach((pill) => {
  pill.addEventListener("click", async () => {
    const prof = pill.dataset.profile;
    syncProfilePillsUI(prof);
    try {
      await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preferred_profile: prof }),
      });
    } catch (e) {
      // ignore
    }
    pollStatus();
  });
});

// Botão que copia o Prompt completo para qualquer Agente de IA controlar o DriftScript via chat
el.btnCopyPrompt.addEventListener("click", async () => {
  const promptText = [
    "Você pode editar o vídeo aberto no CutWire Drift pelo texto através do addon DriftScript (rodando em http://127.0.0.1:4732 e CLI em /home/davi/repos/DriftScript/driftscript_cli.py).",
    "",
    "Regras e estrutura dos tokens:",
    "- Palavras removidas não somem: elas ficam riscadas (`deleted: true`) e podem ser desriscadas (`deleted: false`).",
    "- Silêncios são decompostos em pontinhos `•` (1 pontinho = 0.05s), traços `—` (1 traço = 10 pontinhos = 0.50s, sempre com `•` no início e no fim a partir de 12 pontinhos) e `• (tempo) •` (> 4 traços). Você pode riscar ou acelerar apenas alguns pontinhos/traços (`pause_pos == 'inner'`) para encurtar parte do silêncio.",
    "- Sílabas esticadas aparecem como tokens `〰` (`kind: 'stretch'`, ex: `〰0.35s`) logo após a palavra; riscar o token `〰` encurta a sílaba esticada.",
    "",
    "Comandos rápidos via terminal (ou chame via MCP HTTP POST http://127.0.0.1:4732/mcp):",
    "1. Ver status e GPU VRAM:",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py status",
    "2. Ler transcrição com índices `[idx]`:",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_get_transcript '{\"max_words\":800}'",
    "3. Analisar vícios de linguagem, repetições e pausas:",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_analyze_fillers '{\"min_pause_sec\":0.4}'",
    "4. Riscar palavras/silêncios/esticadas por índice ou intervalo (sincroniza direto na timeline do Drift):",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_cut_words '{\"indices\":[10,11],\"ranges\":[[20,25]],\"deleted\":true}'",
    "5. Acelerar trechos (2x, 4x, 8x, 16x ou 32x) em vez de cortar:",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_accelerate_words '{\"ranges\":[[30,45]],\"speed\":2.0}'",
    "6. Colorir o Medidor de Relevância (ou use a skill `analisar-relevancia-da-transcricao`):",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_set_relevance '{\"annotations\":[{\"start_index\":0,\"end_index\":45,\"score\":0.95,\"category\":\"relevant\",\"reason\":\"Objetivo principal\"},{\"start_index\":46,\"end_index\":80,\"score\":0.0,\"category\":\"off_topic\",\"reason\":\"Desvio\"}]}'",
    "7. Ir para uma palavra específica no player e no Drift:",
    "   python3 /home/davi/repos/DriftScript/driftscript_cli.py chamar driftscript_seek_word '{\"word_index\":50}'",
  ].join("\n");

  try {
    await navigator.clipboard.writeText(promptText);
    showBanner("📋 Prompt para Agente de IA copiado para a área de transferência!");
  } catch (err) {
    const ta = document.createElement("textarea");
    ta.value = promptText;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    showBanner("📋 Prompt para Agente de IA copiado!");
  }
});

el.btnUnloadVram.addEventListener("click", async () => {
  const resp = await fetch("/api/memory/unload", { method: "POST" });
  renderMemoryMonitor(await resp.json());
  showBanner("VRAM liberada.");
});

// Clicar em Transcrever SEMPRE refaz a transcrição do vídeo com o perfil escolhido (force: true)
el.btnTranscribe.addEventListener("click", async () => {
  const assetId = el.assetSelect.value || null;
  el.btnTranscribe.disabled = true;
  el.btnTranscribe.textContent = "Transcrevendo...";
  state.isTranscribing = true;
  showBanner(`Refazendo transcrição na GPU com modelo ${state.selectedProfile.toUpperCase()}...`, 0);
  try {
    const resp = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: assetId,
        profile: state.selectedProfile,
        language: "pt",
        force: true,
      }),
    });
    const data = await resp.json();
    state.selectedIndices.clear();
    applyTranscriptState(data);
    showBanner(`Transcrição (${state.selectedProfile.toUpperCase()}) refeita com sucesso!`);
  } catch (e) {
    showBanner(`Erro na transcrição: ${e.message}`);
  } finally {
    state.isTranscribing = false;
    el.btnTranscribe.disabled = false;
    el.btnTranscribe.textContent = "Transcrever";
  }
});

el.btnSyncNow.addEventListener("click", async () => {
  const resp = await fetch("/api/sync", { method: "POST" });
  applyTranscriptState(await resp.json());
  showBanner("Sincronizado com o Drift.");
});

el.chkAutoSync.addEventListener("change", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_sync_drift: el.chkAutoSync.checked }),
  });
});

// Selecionar o botão de Riscar ou o de Acelerar para ser o efeito da Barra de Espaço
el.btnCutSelected.addEventListener("click", () => {
  setSpaceEffect("cut");
  showBanner("Barra de Espaço configurada para: ✂ Riscar / Desriscar toda a seleção.");
});

el.btnAccelSelected.addEventListener("click", () => {
  setSpaceEffect("accel");
  const spd = Number(el.selectAccelSpeed.value || 2);
  showBanner(`Barra de Espaço configurada para: ⚡ Acelerar (${spd}x) / Normal (1x) em toda a seleção.`);
});

el.selectAccelSpeed.addEventListener("change", () => {
  setSpaceEffect("accel");
  const spd = Number(el.selectAccelSpeed.value || 2);
  showBanner(`Barra de Espaço configurada para: ⚡ Acelerar (${spd}x) / Normal (1x) em toda a seleção.`);
});

el.btnRestoreSelected.addEventListener("click", () => {
  if (state.selectedIndices.size === 0) return;
  setWordsDeleted(Array.from(state.selectedIndices), false);
  showBanner(`↺ ${state.selectedIndices.size} itens desriscados e restaurados para 1x.`);
});

el.btnClearSelection.addEventListener("click", () => {
  state.selectedIndices.clear();
  updateSelectionUI();
});

// SELECIONAR silêncios na faixa de comprimento escolhida (ou sílabas esticadas 〰 ou relevância)
el.btnSelectPauses.addEventListener("click", () => {
  const val = el.selectPauseRange.value || "0.30-0.65";
  const keepEdges = el.chkKeepEdgeDots.checked;
  state.selectedIndices.clear();

  if (val === "stretch") {
    state.words.forEach((w, idx) => {
      if (w.kind === "stretch") {
        state.selectedIndices.add(idx);
      }
    });
    updateSelectionUI();
    showBanner(`${state.selectedIndices.size} sílabas esticadas (〰) selecionadas. Pressione Espaço para aplicar o efeito ativo.`);
    return;
  }

  if (val === "rel-red" || val === "rel-blue" || val === "rel-red-blue") {
    state.words.forEach((w, idx) => {
      const isBlue = w.relevance_category === "off_topic";
      const isRed = !isBlue && w.relevance_score !== undefined && w.relevance_score !== null && Number(w.relevance_score) <= 0.38;
      if ((val === "rel-red" && isRed) || (val === "rel-blue" && isBlue) || (val === "rel-red-blue" && (isRed || isBlue))) {
        state.selectedIndices.add(idx);
      }
    });
    updateSelectionUI();
    showBanner(`${state.selectedIndices.size} tokens selecionados pelo Medidor de Relevância. Pressione Espaço para aplicar o efeito.`);
    return;
  }

  const [minStr, maxStr] = val.split("-");
  const minDur = Number(minStr || 0.1);
  const maxDur = Number(maxStr || 999);

  const groups = new Map();
  state.words.forEach((w, idx) => {
    if (w.kind === "pause") {
      const gDur = Number(w.pause_group_dur !== undefined ? w.pause_group_dur : w.duration || 0);
      if (gDur >= minDur && gDur <= maxDur) {
        const gid = w.pause_group_id !== undefined ? w.pause_group_id : idx;
        if (!groups.has(gid)) groups.set(gid, []);
        groups.get(gid).push(idx);
      }
    }
  });

  for (const [, idxs] of groups.entries()) {
    if (keepEdges && idxs.length >= 3) {
      for (let j = 1; j < idxs.length - 1; j++) {
        state.selectedIndices.add(idxs[j]);
      }
    } else if (keepEdges && idxs.length === 2) {
      state.selectedIndices.add(idxs[1]);
    } else {
      for (const i of idxs) state.selectedIndices.add(i);
    }
  }

  updateSelectionUI();
  showBanner(
    `${state.selectedIndices.size} partes de silêncio selecionadas (${groups.size} grupos). Pressione Espaço para alternar ${state.spaceEffect === "cut" ? "Riscar" : "Acelerar"}.`
  );
});

el.btnClearRelevance.addEventListener("click", async () => {
  const resp = await fetch("/api/words/relevance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clear: true }),
  });
  applyTranscriptState(await resp.json());
  showBanner("Cores de relevância limpas (texto branco).");
});

el.btnUndo.addEventListener("click", async () => {
  const resp = await fetch("/api/undo", { method: "POST" });
  applyTranscriptState(await resp.json());
});

el.btnRedo.addEventListener("click", async () => {
  const resp = await fetch("/api/redo", { method: "POST" });
  applyTranscriptState(await resp.json());
});

el.searchInput.addEventListener("input", () => {
  const q = el.searchInput.value.trim().toLowerCase();
  state.searchMatches.clear();
  if (q) {
    state.words.forEach((w, idx) => {
      if (w.word.toLowerCase().includes(q)) state.searchMatches.add(idx);
    });
  }
  el.searchCount.textContent = state.searchMatches.size;
  el.btnSelectSearchMatches.classList.toggle("hidden", state.searchMatches.size === 0);
  document.querySelectorAll(".word-tok").forEach((node) => {
    const idx = Number(node.dataset.index);
    node.classList.toggle("is-match", state.searchMatches.has(idx));
  });
});

el.btnSelectSearchMatches.addEventListener("click", () => {
  if (state.searchMatches.size > 0) {
    state.selectedIndices = new Set(state.searchMatches);
    updateSelectionUI();
    showBanner(`${state.selectedIndices.size} ocorrências selecionadas.`);
  }
});

async function pollStatus() {
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();
    if (!data.ok) return;

    renderMemoryMonitor(data.memory);
    renderDriftAssets(data.drift);

    if (state.isTranscribing && data.memory?.transcription?.running && data.memory.transcription.message) {
      showBanner(`⏳ ${data.memory.transcription.message}`, 0);
    }

    if (data.session && data.session.last_updated > state.lastSessionUpdated + 0.01) {
      state.lastSessionUpdated = data.session.last_updated;
      const trResp = await fetch("/api/transcript");
      applyTranscriptState(await trResp.json());
    }
  } catch (e) {
    // ignore
  }
}

async function init() {
  setupVideoPlayer();
  await pollStatus();
  const trResp = await fetch("/api/transcript");
  const trData = await trResp.json();
  if (trData && trData.words && trData.words.length > 0) {
    applyTranscriptState(trData);
  }
  setInterval(pollStatus, 2000);
}

init();
