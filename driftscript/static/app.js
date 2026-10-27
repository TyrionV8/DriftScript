/**
 * DriftScript — Descript-style Text Video Editor for CutWire Drift
 * - Snug text layout with wide square left-side scrollbar
 * - Granular silence tokens: • (1 pontinho = 0.05s), — (1 traço = 10 pontinhos), • (tempo) • (> 4 traços)
 * - Stretched syllable tokens: 〰0.35s attached to dragged-out words
 * - Standard Text-Editor Selection & Cursor:
 *   * 1x Click: places cursor & starts playing audio from that word (does not select)
 *   * 2x Click: selects the word | 3x Click: selects the paragraph
 *   * Click & Drag: continuous range selection
 *   * Ctrl + Click / Drag: non-contiguous multi-selection
 *   * Arrow keys & Shift + Arrows: cursor navigation and continuous selection adjustment
 * - Spacebar toggles the active mode (Riscar or Acelerar 2x/4x) across the entire selection
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
  baseSelectedIndices: new Set(),
  anchorIndex: null,
  caretIndex: null,
  isMouseDown: false,
  hasDragged: false,
  mouseDownX: 0,
  mouseDownY: 0,
  mouseDownIdx: null,
  mouseDownCtrl: false,
  mouseDownShift: false,
  singleClickTimer: null,
  spaceEffect: "cut", // "cut" (Riscar) | "accel" (Acelerar 2x/4x)
  loopSelectionMode: false,
  isTranscribing: false,
  activeWordIndex: -1,
  searchMatches: new Set(),
  auditionUntilSec: 0,
  lastSessionUpdated: 0,
  isSpeed2x: false,
  previewScale: 1,
  lastSkipWallMs: 0,
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
        const clampedSpd = Math.min(4.0, Number(w.speed || 1.0));
        const prevSpd = prevW ? Math.min(4.0, Number(prevW.speed || 1.0)) : 1.0;
        if (!prevW || prevW.deleted || Math.abs(prevSpd - clampedSpd) > 0.05) {
          const badge = document.createElement("span");
          badge.className = "accel-badge";
          badge.textContent = `⚡${clampedSpd}x`;
          span.prepend(badge);
        }
      }
      if (state.selectedIndices.has(wIdx)) span.classList.add("is-selected");
      if (state.searchMatches.has(wIdx)) span.classList.add("is-match");
      if (wIdx === state.activeWordIndex) span.classList.add("is-active");
      if (wIdx === state.caretIndex) span.classList.add("has-caret");

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

  document.querySelectorAll(".word-tok.has-caret").forEach((node) => {
    const idx = Number(node.dataset.index);
    if (idx !== state.caretIndex) node.classList.remove("has-caret");
  });
  if (state.caretIndex !== null && state.caretIndex >= 0) {
    const caretNode = document.getElementById(`word-${state.caretIndex}`);
    if (caretNode) caretNode.classList.add("has-caret");
  }
}

async function seekToWord(wordIndex, autoPlay = false) {
  const w = state.words[wordIndex];
  if (!w) return;

  state.auditionUntilSec = w.deleted ? w.end + 0.08 : 0;

  if (el.videoPlayer.src) {
    el.videoPlayer.currentTime = w.start;
    if (autoPlay) {
      state.loopSelectionMode = false;
      el.videoPlayer.play().catch(() => {});
    }
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
      speed: Math.min(4.0, Number(speed || 2.0)),
    }),
  });
  const data = await resp.json();
  applyTranscriptState(data);
}

function getTargetIndices() {
  if (state.selectedIndices.size > 0) {
    return Array.from(state.selectedIndices);
  }
  if (state.caretIndex !== null && state.caretIndex >= 0 && state.caretIndex < state.words.length) {
    return [state.caretIndex];
  }
  return [];
}

/**
 * Corta / Risca (atalho C, X, Delete ou Backspace)
 */
async function triggerCutAction() {
  const indices = getTargetIndices();
  if (!indices.length) return;
  await setWordsDeleted(indices, true);
  showBanner(`✂ ${indices.length} itens riscados [C].`);
}

/**
 * Acelera (atalho A): aplica a velocidade escolhida e, se apertar A novamente na mesma seleção já acelerada,
 * alterna entre 2x e 4x.
 */
async function triggerAccelerateAction() {
  const indices = getTargetIndices();
  if (!indices.length) return;

  const allAt2x = indices.every((idx) => {
    const w = state.words[idx];
    return w && !w.deleted && Math.abs(Math.min(4.0, Number(w.speed || 1.0)) - 2.0) < 0.05;
  });
  const allAt4x = indices.every((idx) => {
    const w = state.words[idx];
    return w && !w.deleted && Math.abs(Math.min(4.0, Number(w.speed || 1.0)) - 4.0) < 0.05;
  });

  let nextSpd = Math.min(4.0, Number(el.selectAccelSpeed.value || 2.0));
  if (allAt2x) {
    nextSpd = 4.0;
    if (el.selectAccelSpeed) el.selectAccelSpeed.value = "4";
  } else if (allAt4x) {
    nextSpd = 2.0;
    if (el.selectAccelSpeed) el.selectAccelSpeed.value = "2";
  }

  await setWordsSpeed(indices, nextSpd);
  showBanner(`⚡ ${indices.length} itens acelerados em ${nextSpd}x [A].`);
}

/**
 * Desrisca / Restaura para 1x (atalho D ou R)
 */
async function triggerRestoreAction() {
  const indices = getTargetIndices();
  if (!indices.length) return;
  await setWordsDeleted(indices, false);
  showBanner(`↺ ${indices.length} itens desriscados e restaurados para 1x [D].`);
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
 * Localiza o índice da palavra/token mais próximo da coordenada (clientX, clientY),
 * permitindo arrastar o mouse de forma contínua exatamente como num editor de texto.
 */
function findClosestWordIndexAtPoint(clientX, clientY) {
  const hit = document.elementFromPoint(clientX, clientY);
  const directTok = hit ? hit.closest(".word-tok") : null;
  if (directTok) {
    return Number(directTok.dataset.index);
  }

  const nodes = el.transcriptContainer.querySelectorAll(".word-tok");
  if (!nodes.length) return null;

  let bestIdx = null;
  let bestLineDist = Infinity;
  let bestHorizDist = Infinity;

  for (let i = 0; i < nodes.length; i++) {
    const r = nodes[i].getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;

    let lineDist = 0;
    if (clientY < r.top) lineDist = r.top - clientY;
    else if (clientY > r.bottom) lineDist = clientY - r.bottom;

    let horizDist = 0;
    if (clientX < r.left) horizDist = r.left - clientX;
    else if (clientX > r.right) horizDist = clientX - r.right;

    if (lineDist < bestLineDist - 4 || (Math.abs(lineDist - bestLineDist) <= 4 && horizDist < bestHorizDist)) {
      bestLineDist = lineDist;
      bestHorizDist = horizDist;
      bestIdx = Number(nodes[i].dataset.index);
    }
  }
  return bestIdx;
}

/**
 * Encontra o token na linha visual imediatamente acima (dir = -1) ou abaixo (dir = +1)
 * para navegação vertical com Seta Cima / Seta Baixo e Shift + Seta.
 */
function findVerticalNeighborIndex(curIdx, dir) {
  const curNode = document.getElementById(`word-${curIdx}`);
  if (!curNode) return curIdx;
  const curRect = curNode.getBoundingClientRect();
  const targetX = curRect.left + curRect.width * 0.5;
  const curMidY = curRect.top + curRect.height * 0.5;

  const nodes = el.transcriptContainer.querySelectorAll(".word-tok");
  let bestIdx = curIdx;
  let bestLineY = null;
  let bestXDist = Infinity;

  for (let i = 0; i < nodes.length; i++) {
    const r = nodes[i].getBoundingClientRect();
    const midY = r.top + r.height * 0.5;
    const dy = midY - curMidY;

    if (dir < 0 && dy >= -6) continue; // Precisa estar numa linha acima
    if (dir > 0 && dy <= 6) continue;  // Precisa estar numa linha abaixo

    const absDy = Math.abs(dy);
    if (bestLineY === null || absDy < bestLineY - 8) {
      bestLineY = absDy;
      bestXDist = Math.abs(r.left + r.width * 0.5 - targetX);
      bestIdx = Number(nodes[i].dataset.index);
    } else if (Math.abs(absDy - bestLineY) <= 8) {
      const xDist = Math.abs(r.left + r.width * 0.5 - targetX);
      if (xDist < bestXDist) {
        bestXDist = xDist;
        bestIdx = Number(nodes[i].dataset.index);
      }
    }
  }
  return bestIdx;
}

function applyContiguousSelection(anchor, focus, baseSet = null) {
  const nextSet = baseSet ? new Set(baseSet) : new Set();
  const a = Math.min(anchor, focus);
  const b = Math.max(anchor, focus);
  for (let k = a; k <= b; k++) {
    nextSet.add(k);
  }
  state.selectedIndices = nextSet;
  state.caretIndex = focus;
  updateSelectionUI();
}

/**
 * Mouse & Keyboard events on the text editor (Comportamento Padrão de Editor de Texto):
 * - 1x Clique Esquerdo: NÃO seleciona por padrão; posiciona o cursor e começa a tocar o áudio a partir dali.
 * - 2x Clique Esquerdo: seleciona a palavra clicada.
 * - 3x Clique Esquerdo: seleciona o parágrafo inteiro.
 * - Clicar e Arrastar: seleciona de forma contínua do ponto inicial até o ponto atual.
 * - Segurar Ctrl + Clicar ou Arrastar: seleciona partes diferentes (seleção múltipla descontínua).
 * - Setas e Shift + Setas: move o cursor ou expande/reduz a seleção contínua.
 */
el.transcriptContainer.addEventListener("mousedown", (e) => {
  if (e.button === 2) return; // Botão direito tratado pelo contextmenu
  el.transcriptContainer.focus();

  const idx = findClosestWordIndexAtPoint(e.clientX, e.clientY);
  if (idx === null) return;

  const isCtrl = e.ctrlKey || e.metaKey;
  const isShift = e.shiftKey;

  // 3x Clique: seleciona o parágrafo inteiro
  if (e.detail >= 3) {
    if (state.singleClickTimer) {
      clearTimeout(state.singleClickTimer);
      state.singleClickTimer = null;
    }
    el.videoPlayer.pause();
    const seg = state.segments.find((s) => s.word_indices && s.word_indices.includes(idx));
    if (!isCtrl) state.selectedIndices.clear();
    if (seg && seg.word_indices.length > 0) {
      for (const wi of seg.word_indices) state.selectedIndices.add(wi);
      state.anchorIndex = seg.word_indices[0];
      state.caretIndex = seg.word_indices[seg.word_indices.length - 1];
    } else {
      state.selectedIndices.add(idx);
      state.anchorIndex = idx;
      state.caretIndex = idx;
    }
    state.baseSelectedIndices = new Set(state.selectedIndices);
    state.isMouseDown = false;
    updateSelectionUI();
    return;
  }

  // 2x Clique: seleciona a palavra clicada (e eventual sílaba esticada 〰 anexada)
  if (e.detail === 2) {
    if (state.singleClickTimer) {
      clearTimeout(state.singleClickTimer);
      state.singleClickTimer = null;
    }
    el.videoPlayer.pause();
    if (!isCtrl) state.selectedIndices.clear();
    state.selectedIndices.add(idx);
    if (state.words[idx + 1]?.kind === "stretch") {
      state.selectedIndices.add(idx + 1);
    }
    state.anchorIndex = idx;
    state.caretIndex = idx;
    state.baseSelectedIndices = new Set(state.selectedIndices);
    state.isMouseDown = false;
    updateSelectionUI();
    seekToWord(idx, false);
    return;
  }

  // 1x Clique ou início de Clicar e Arrastar
  state.isMouseDown = true;
  state.hasDragged = false;
  state.mouseDownX = e.clientX;
  state.mouseDownY = e.clientY;
  state.mouseDownIdx = idx;
  state.mouseDownCtrl = isCtrl;
  state.mouseDownShift = isShift;

  if (isShift) {
    const anchor = state.anchorIndex !== null ? state.anchorIndex : (state.caretIndex !== null ? state.caretIndex : idx);
    state.anchorIndex = anchor;
    applyContiguousSelection(anchor, idx, isCtrl ? state.baseSelectedIndices : null);
  } else if (isCtrl) {
    state.baseSelectedIndices = new Set(state.selectedIndices);
    state.anchorIndex = idx;
    state.caretIndex = idx;
    updateSelectionUI();
  } else {
    state.baseSelectedIndices.clear();
    state.anchorIndex = idx;
    state.caretIndex = idx;
    updateSelectionUI();
  }
});

window.addEventListener("mousemove", (e) => {
  if (!state.isMouseDown || state.anchorIndex === null) return;

  const dx = e.clientX - state.mouseDownX;
  const dy = e.clientY - state.mouseDownY;
  const distSq = dx * dx + dy * dy;
  const curIdx = findClosestWordIndexAtPoint(e.clientX, e.clientY);

  if (!state.hasDragged && (distSq >= 25 || (curIdx !== null && curIdx !== state.mouseDownIdx))) {
    state.hasDragged = true;
    if (state.singleClickTimer) {
      clearTimeout(state.singleClickTimer);
      state.singleClickTimer = null;
    }
  }

  if (state.hasDragged && curIdx !== null) {
    applyContiguousSelection(
      state.anchorIndex,
      curIdx,
      state.mouseDownCtrl ? state.baseSelectedIndices : null
    );
  }
});

window.addEventListener("mouseup", (e) => {
  if (!state.isMouseDown) return;
  const wasDrag = state.hasDragged;
  const idx = state.mouseDownIdx;
  const isCtrl = state.mouseDownCtrl;
  const isShift = state.mouseDownShift;
  state.isMouseDown = false;

  if (wasDrag) {
    state.baseSelectedIndices = new Set(state.selectedIndices);
    return;
  }

  if (e.detail === 1 && idx !== null) {
    if (isCtrl) {
      // Ctrl + Clique alterna apenas a palavra clicada mantendo as outras partes selecionadas
      if (state.selectedIndices.has(idx)) state.selectedIndices.delete(idx);
      else state.selectedIndices.add(idx);
      state.baseSelectedIndices = new Set(state.selectedIndices);
      state.anchorIndex = idx;
      state.caretIndex = idx;
      updateSelectionUI();
    } else if (isShift) {
      // Shift + Clique seleciona continuamente do cursor/âncora até idx
      state.baseSelectedIndices = new Set(state.selectedIndices);
      seekToWord(idx, false);
    } else {
      // 1x Clique simples: NÃO seleciona, posiciona o cursor e começa a tocar o áudio a partir dali
      state.selectedIndices.clear();
      state.baseSelectedIndices.clear();
      state.anchorIndex = idx;
      state.caretIndex = idx;
      updateSelectionUI();
      if (state.singleClickTimer) clearTimeout(state.singleClickTimer);
      state.singleClickTimer = setTimeout(() => {
        state.singleClickTimer = null;
        seekToWord(idx, true);
      }, 170);
    }
  }
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

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;

  if (e.key === "Escape") {
    state.selectedIndices.clear();
    state.baseSelectedIndices.clear();
    updateSelectionUI();
    return;
  }

  // Navegação por cursor e seleção contínua com Setas / Shift + Setas (igual editor de texto)
  if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key) && state.words.length > 0) {
    e.preventDefault();
    const cur =
      state.caretIndex !== null
        ? state.caretIndex
        : state.activeWordIndex >= 0
        ? state.activeWordIndex
        : 0;

    let nextIdx = cur;
    if (e.key === "ArrowLeft") {
      if (!e.shiftKey && state.selectedIndices.size > 0) {
        nextIdx = Math.min(...state.selectedIndices);
      } else {
        nextIdx = Math.max(0, cur - 1);
      }
    } else if (e.key === "ArrowRight") {
      if (!e.shiftKey && state.selectedIndices.size > 0) {
        nextIdx = Math.max(...state.selectedIndices);
      } else {
        nextIdx = Math.min(state.words.length - 1, cur + 1);
      }
    } else if (e.key === "ArrowUp") {
      nextIdx = findVerticalNeighborIndex(cur, -1);
    } else if (e.key === "ArrowDown") {
      nextIdx = findVerticalNeighborIndex(cur, 1);
    } else if (e.key === "Home") {
      nextIdx = 0;
    } else if (e.key === "End") {
      nextIdx = state.words.length - 1;
    }

    if (e.shiftKey) {
      if (state.anchorIndex === null) state.anchorIndex = cur;
      applyContiguousSelection(state.anchorIndex, nextIdx, e.ctrlKey || e.metaKey ? state.baseSelectedIndices : null);
    } else {
      state.selectedIndices.clear();
      state.baseSelectedIndices.clear();
      state.anchorIndex = nextIdx;
      state.caretIndex = nextIdx;
      updateSelectionUI();
    }

    const node = document.getElementById(`word-${nextIdx}`);
    if (node) node.scrollIntoView({ block: "nearest" });
    seekToWord(nextIdx, false);
    return;
  }

  // Ctrl+A / Cmd+A = Selecionar todo o texto da transcrição em vez da página HTML toda
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    e.preventDefault();
    state.selectedIndices.clear();
    for (let i = 0; i < state.words.length; i++) {
      state.selectedIndices.add(i);
    }
    state.baseSelectedIndices = new Set(state.selectedIndices);
    if (state.words.length > 0) {
      state.anchorIndex = 0;
      state.caretIndex = state.words.length - 1;
    }
    updateSelectionUI();
    showBanner(`Todos os ${state.words.length} itens da transcrição selecionados (Ctrl+A).`);
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
    e.preventDefault();
    triggerCutAction();
    return;
  }

  // Atalhos de letras (sem Ctrl/Meta/Alt): C/X = Riscar, A = Acelerar (alterna 2x ⇄ 4x), D/R = Desriscar
  if (!e.ctrlKey && !e.metaKey && !e.altKey) {
    const keyLower = e.key.toLowerCase();
    if (keyLower === "c" || keyLower === "x") {
      e.preventDefault();
      triggerCutAction();
      return;
    }
    if (keyLower === "a") {
      e.preventDefault();
      triggerAccelerateAction();
      return;
    }
    if (keyLower === "d" || keyLower === "r") {
      e.preventDefault();
      triggerRestoreAction();
      return;
    }
  }

  // Barra de Espaço = Play / Pause
  if (e.key === " " || e.code === "Space") {
    e.preventDefault();
    el.btnPlayPause.click();
    return;
  }
});

/**
 * Video Player Sync, Selection Loop Playback, Restart from Zero, 2x Speed, Dynamic Segment Acceleration (2x/4x), and Resolution Switcher
 */
function setupVideoPlayer() {
  const video = el.videoPlayer;

  const tick = () => {
    const nowMs = performance.now();

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

      // Aplica aceleração dinâmica quando o playhead entra num trecho acelerado (até 4x)
      const baseRate = state.isSpeed2x ? 2.0 : 1.0;
      let segMult = 1.0;
      for (const sr of state.speedRanges) {
        if (t >= sr.start && t < sr.end) {
          segMult = Math.min(4.0, Number(sr.speed || 1.0));
          break;
        }
      }
      const targetRate = Math.min(8.0, Math.max(0.25, baseRate * segMult));
      if (Math.abs(video.playbackRate - targetRate) > 0.02) {
        try {
          video.playbackRate = targetRate;
        } catch (err) {
          // ignore
        }
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
    "5. Acelerar trechos (2x ou 4x) em vez de cortar:",
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
    state.baseSelectedIndices.clear();
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

// Botões de Riscar [C], Acelerar [A] (alterna 2x ⇄ 4x) e Desriscar [D]
el.btnCutSelected.addEventListener("click", () => {
  triggerCutAction();
});

el.btnAccelSelected.addEventListener("click", () => {
  triggerAccelerateAction();
});

el.selectAccelSpeed.addEventListener("change", async () => {
  const spd = Math.min(4.0, Number(el.selectAccelSpeed.value || 2));
  const indices = getTargetIndices();
  if (indices.length > 0) {
    await setWordsSpeed(indices, spd);
    showBanner(`⚡ ${indices.length} itens acelerados em ${spd}x.`);
  }
});

el.btnRestoreSelected.addEventListener("click", () => {
  triggerRestoreAction();
});

el.btnClearSelection.addEventListener("click", () => {
  state.selectedIndices.clear();
  state.baseSelectedIndices.clear();
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
    state.baseSelectedIndices = new Set(state.selectedIndices);
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
    state.baseSelectedIndices = new Set(state.selectedIndices);
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

  state.baseSelectedIndices = new Set(state.selectedIndices);
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
    state.baseSelectedIndices = new Set(state.selectedIndices);
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
