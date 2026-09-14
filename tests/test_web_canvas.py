"""ltx studio canvas sizing (web/static/canvas.js), run through Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CANVAS_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "canvas.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _node(script: str) -> object:
    """Run ``script`` with canvas.js loaded as ``c`` and return what it prints as JSON."""
    code = f"const c = require({json.dumps(str(CANVAS_JS))});\n{script}"
    out = subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_grid_is_64_except_generate_one_stage() -> None:
    grids = _node(
        """console.log(JSON.stringify([
          c.canvasGrid("generate", "distilled"), c.canvasGrid("generate", "two-stage"),
          c.canvasGrid("generate", "one-stage"), c.canvasGrid("a2v", "one-stage"), c.canvasGrid("ic-lora", undefined),
        ]));"""
    )
    assert grids == [64, 64, 32, 64, 64]


def test_every_solution_is_on_grid_under_ceiling_and_non_decreasing() -> None:
    result = _node(
        """const bad = [];
        for (const grid of [64, 32]) for (const [key, , ratio] of c.ASPECT_RATIOS) {
          let prev = 0;
          for (let mp = c.CANVAS_MIN_MP; mp <= c.CANVAS_MAX_MP + 1e-9; mp += 0.01) {
            const s = c.solveCanvas(ratio, mp, grid);
            if (!s || s.width % grid || s.height % grid || s.pixels > c.CANVAS_MAX_PIXELS || s.pixels < prev)
              bad.push([grid, key, mp.toFixed(2), s]);
            if (Math.abs(s.width / s.height / ratio - 1) > 0.08) bad.push([grid, key, mp.toFixed(2), "aspect", s]);
            prev = s.pixels;
          }
        }
        console.log(JSON.stringify(bad.slice(0, 5)));"""
    )
    assert result == []


def test_known_sizes_and_mirrored_orientations() -> None:
    sizes = _node(
        """const size = (r, mp, g) => { const s = c.solveCanvas(r, mp, g); return `${s.width}x${s.height}`; };
        console.log(JSON.stringify({
          wide: size(16 / 9, 0.92, 64), tall: size(9 / 16, 0.92, 64),
          max: size(16 / 9, 2.1, 64), square: size(1, 0.92, 64), oneStage: size(16 / 9, 0.5, 32),
        }));"""
    )
    assert sizes == {
        "wide": "1280x704",
        "tall": "704x1280",
        "max": "1920x1088",
        "square": "960x960",
        "oneStage": "960x544",
    }


def test_snap_match_and_clamp_helpers() -> None:
    values = _node(
        """console.log(JSON.stringify([
          c.snapDimension(1000, 64), c.snapDimension(10, 64), c.snapDimension(740, 32),
          c.matchAspect(768, 768, 0.005), c.matchAspect(1280, 704, 0.005), c.matchAspect(1920, 1080, 0.005),
          c.clampMegapixels(5), c.clampMegapixels(0.01), c.clampMegapixels(0.456),
        ]));"""
    )
    assert values == [1024, 128, 736, "1:1", None, "16:9", 2.1, 0.1, 0.46]
