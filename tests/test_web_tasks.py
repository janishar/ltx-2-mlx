"""ltx studio task catalog (web/static/tasks.js), run through Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TASKS_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "tasks.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

MODEL_25 = {"configured": True, "local": True, "is_25": True, "has_distilled": True, "has_dev": True}
MODEL_23 = {**MODEL_25, "is_25": False}
MODEL_HF = {**MODEL_25, "local": False, "is_25": False}


def _node(script: str) -> object:
    """Run ``script`` with tasks.js loaded as ``t`` and return what it prints as JSON."""
    code = f"const t = require({json.dumps(str(TASKS_JS))});\n{script}"
    out = subprocess.run(["node", "-e", code], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_generated_keyframes_flag_on_every_generate_task() -> None:
    result = _node(
        """const ctx = {frames: 49, fps: 24, width: 704, height: 448, seed: 1, inputMeta: () => ({})};
        const out = {};
        for (const task of t.LTX_TASKS.filter((x) => x.cmd === "generate")) {
          const fields = task.fields.map((f) => f.key);
          const args = task.build({generatedKeyframes: 3, beats: [], anchors: []}, ctx);
          const off = task.build({generatedKeyframes: 0, beats: [], anchors: []}, ctx);
          out[task.id] = [fields.includes("generatedKeyframes"),
                          args.join(" ").includes("--num-generated-keyframes 3"),
                          off.includes("--num-generated-keyframes")];
        }
        console.log(JSON.stringify(out));"""
    )
    assert result and all(v == [True, True, False] for v in result.values()), result


def test_generated_keyframes_requirements() -> None:
    cases = _node(
        f"""const r = (v, model, ctx) => t.generateRequires(v, model, ctx);
        const m25 = {json.dumps(MODEL_25)}, m23 = {json.dumps(MODEL_23)}, hf = {json.dumps(MODEL_HF)};
        console.log(JSON.stringify({{
          off23: r({{generatedKeyframes: 0}}, m23, {{frames: 49}}),
          blank23: r({{generatedKeyframes: ""}}, m23, {{frames: 49}}),
          ok25: r({{generatedKeyframes: 3}}, m25, {{frames: 49}}),
          on23: r({{generatedKeyframes: 3}}, m23, {{frames: 49}}),
          hf: r({{generatedKeyframes: 3}}, hf, {{frames: 49}}),
          tooShort: r({{generatedKeyframes: 8}}, m25, {{frames: 9}}),
          autoDuration: r({{generatedKeyframes: 8}}, m25, {{frames: 9, autoDuration: true}}),
          fractional: r({{generatedKeyframes: 1.5}}, m25, {{frames: 49}}),
          negative: r({{generatedKeyframes: -1}}, m25, {{frames: 49}}),
          noCtx: r({{generatedKeyframes: 2}}, m25),
        }}));"""
    )
    assert cases["off23"] is None and cases["blank23"] is None
    assert cases["ok25"] is None and cases["hf"] is None and cases["autoDuration"] is None and cases["noCtx"] is None
    assert "LTX-2.5" in cases["on23"]
    assert "at least 10 frames" in cases["tooShort"]
    assert "whole number" in cases["fractional"] and "whole number" in cases["negative"]
