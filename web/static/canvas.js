// ltx studio canvas sizing — pure helpers, no DOM. Loaded before app.js and
// also require()-able from Node for tests.
//
// LTX encodes 32× spatially, so every dimension must be a multiple of 32.
// Two-stage pipelines (distilled, two-stage, HQ, a2v, keyframe, IC-LoRA,
// lip dub) render stage 1 at half size and floor that to 32, so their output
// is effectively a multiple of 64. Only `generate --one-stage` renders at the
// requested size directly.

"use strict";

const ASPECT_RATIOS = [
  ["16:9", "16:9 · Widescreen", 16 / 9],
  ["9:16", "9:16 · Vertical", 9 / 16],
  ["1:1", "1:1 · Square", 1],
  ["4:3", "4:3 · Standard", 4 / 3],
  ["3:4", "3:4 · Portrait", 3 / 4],
  ["3:2", "3:2 · Photo", 3 / 2],
  ["2:3", "2:3 · Portrait photo", 2 / 3],
  ["21:9", "21:9 · Ultrawide", 21 / 9],
];

const CANVAS_MIN_MP = 0.1;
const CANVAS_MAX_MP = 2.1;
// 1920×1088 — the largest size the studio's presets offer.
const CANVAS_MAX_PIXELS = 1920 * 1088;

/** Pixel multiple the output snaps to for a subcommand and generate pipeline. */
function canvasGrid(cmd, pipeline) {
  return cmd === "generate" && pipeline === "one-stage" ? 32 : 64;
}

/** Nearest legal dimension on the grid (at least two cells). */
function snapDimension(value, grid) {
  return Math.max(2 * grid, Math.round(value / grid) * grid);
}

/**
 * Legal width/height closest to `ratio` at about `megapixels`.
 *
 * Scans widths and heights on the grid around the ideal (the other side
 * rounded to the grid), so a ratio and its inverse give mirrored sizes. Scores
 * aspect error (weighted) plus relative pixel-count error; sizes above
 * CANVAS_MAX_PIXELS are skipped. The weight of 4 keeps the pixel count close
 * on the coarse ×64 grid and keeps the size non-decreasing as megapixels rise.
 */
function solveCanvas(ratio, megapixels, grid, { aspectWeight = 4 } = {}) {
  if (!(ratio > 0) || !(megapixels > 0)) return null;
  const target = Math.min(megapixels * 1e6, CANVAS_MAX_PIXELS);
  const idealW = Math.sqrt(target * ratio);
  const candidates = [];
  for (let k = Math.round(idealW / grid) - 8; k <= Math.round(idealW / grid) + 8; k++) {
    if (k >= 2) candidates.push([k * grid, snapDimension((k * grid) / ratio, grid)]);
  }
  const idealH = idealW / ratio;
  for (let k = Math.round(idealH / grid) - 8; k <= Math.round(idealH / grid) + 8; k++) {
    if (k >= 2) candidates.push([snapDimension(k * grid * ratio, grid), k * grid]);
  }
  let best = null;
  for (const [width, height] of candidates) {
    const pixels = width * height;
    if (pixels > CANVAS_MAX_PIXELS) continue;
    const score = aspectWeight * Math.abs(Math.log(width / height / ratio)) + Math.abs(pixels - target) / target;
    if (!best || score < best.score - 1e-12) best = { width, height, pixels, score };
  }
  return best;
}

/** Key of the named aspect ratio within `tolerance` of width/height, or null. */
function matchAspect(width, height, tolerance = 0.03) {
  if (!width || !height) return null;
  const found = ASPECT_RATIOS.find(([, , r]) => Math.abs(width / height / r - 1) <= tolerance);
  return found ? found[0] : null;
}

function aspectRatioFor(key) {
  const found = ASPECT_RATIOS.find(([k]) => k === key);
  return found ? found[2] : null;
}

function clampMegapixels(mp) {
  return Math.min(CANVAS_MAX_MP, Math.max(CANVAS_MIN_MP, Math.round(mp * 100) / 100));
}

if (typeof module !== "undefined") {
  module.exports = {
    ASPECT_RATIOS, CANVAS_MIN_MP, CANVAS_MAX_MP, CANVAS_MAX_PIXELS,
    canvasGrid, snapDimension, solveCanvas, matchAspect, aspectRatioFor, clampMegapixels,
  };
}
