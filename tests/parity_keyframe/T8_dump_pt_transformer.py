"""T8 (PT side): LTXModel forward parity, num_layers=1 to fit in memory.

Tests the FULL forward path (prelude + 1 block + finale) on a tiny
keyframe-conditioned input shape. Only loads weights for block 0 +
adaln + finale + connector etc. — about ~1.5 GB instead of 22 GB.

Run from upstream venv:
    cd "$LTX_REFERENCE_DIR"  # Lightricks/LTX-2 checkout
    uv run python "$LTX_MLX_REPO"/tests/parity_keyframe/T8_dump_pt_transformer.py
"""

from __future__ import annotations

import json
import os

import numpy as np
import safetensors
import torch
from ltx_core.guidance.perturbations import BatchedPerturbationConfig
from ltx_core.model.transformer.attention import AttentionFunction
from ltx_core.model.transformer.modality import Modality
from ltx_core.model.transformer.model import LTXModel, LTXModelType
from ltx_core.model.transformer.rope import LTXRopeType

PT_WEIGHTS = os.path.expanduser(
    "~/.cache/huggingface/hub/models--Lightricks--LTX-2.3/"
    "snapshots/76730e634e70a28f4e8d51f5e29c08e40e2d8e74/ltx-2.3-22b-dev.safetensors"
)
EMBEDDED_CFG = os.path.join(os.path.expanduser(os.environ.get("LTX_TEST_MODEL_DIR", "")), "embedded_config.json")
WEIGHT_PREFIX = "model.diffusion_model."


def load_weights_for_one_block() -> dict:
    """Load only weights needed for num_layers=1: block 0 + adaln + finale + caption_proj.

    Strips the 'model.diffusion_model.' prefix to match the model's state_dict keys.
    """
    state: dict[str, torch.Tensor] = {}
    with safetensors.safe_open(PT_WEIGHTS, framework="pt") as f:
        for k in f.keys():  # noqa: SIM118 (safe_open is not a dict)
            if not k.startswith(WEIGHT_PREFIX):
                continue
            new_k = k.removeprefix(WEIGHT_PREFIX)
            # Filter: keep only block 0 + non-block keys
            if "transformer_blocks." in new_k and not new_k.startswith("transformer_blocks.0."):
                continue
            state[new_k] = f.get_tensor(k).to(torch.float32)
    return state


def build_model() -> LTXModel:
    with open(EMBEDDED_CFG) as f:
        cfg_full = json.load(f)
    cfg = cfg_full["transformer"]

    return LTXModel(
        model_type=LTXModelType.AudioVideo,
        num_attention_heads=cfg.get("num_attention_heads", 32),
        attention_head_dim=cfg.get("attention_head_dim", 128),
        in_channels=cfg.get("in_channels", 128),
        out_channels=cfg.get("out_channels", 128),
        num_layers=1,  # ← only 1 block instead of 48
        cross_attention_dim=cfg.get("cross_attention_dim", 4096),
        norm_eps=cfg.get("norm_eps", 1e-06),
        attention_type=AttentionFunction(cfg.get("attention_type", "default")),
        positional_embedding_theta=cfg.get("positional_embedding_theta", 10000.0),
        positional_embedding_max_pos=cfg.get("positional_embedding_max_pos", [20, 2048, 2048]),
        timestep_scale_multiplier=cfg.get("timestep_scale_multiplier", 1000),
        use_middle_indices_grid=cfg.get("use_middle_indices_grid", True),
        audio_num_attention_heads=cfg.get("audio_num_attention_heads", 32),
        audio_attention_head_dim=cfg.get("audio_attention_head_dim", 64),
        audio_in_channels=cfg.get("audio_in_channels", 128),
        audio_out_channels=cfg.get("audio_out_channels", 128),
        audio_cross_attention_dim=cfg.get("audio_cross_attention_dim", 2048),
        audio_positional_embedding_max_pos=cfg.get("audio_positional_embedding_max_pos", [20]),
        av_ca_timestep_scale_multiplier=cfg.get("av_ca_timestep_scale_multiplier", 1),
        rope_type=LTXRopeType(cfg.get("rope_type", "interleaved")),
        double_precision_rope=cfg.get("frequencies_precision", False) == "float64",
        apply_gated_attention=cfg.get("apply_gated_attention", False),
        caption_projection=None,
        audio_caption_projection=None,
        cross_attention_adaln=cfg.get("cross_attention_adaln", False),
    )


def make_inputs(seed: int) -> dict:
    """Seeded keyframe-conditioned inputs."""
    rng = np.random.default_rng(seed)
    # Tiny shape: F=1, H=2, W=2 -> 4 generation tokens; 2 keyframes appended -> 8 kf tokens
    n_gen, n_kf = 4, 8
    n_total = n_gen + n_kf

    video_latent = rng.standard_normal((1, n_total, 128)).astype(np.float32)
    audio_latent = rng.standard_normal((1, 4, 128)).astype(np.float32)
    # positions shape (B, num_axes, T, 2) where last dim = [start, end].
    # For midpoint equivalence with MLX scalars, use [start, start+1] so
    # midpoint = start + 0.5. Generate scalar starts and stack [s, s+1].
    video_pos_starts = rng.standard_normal((1, 3, n_total)).astype(np.float32)
    video_pos = np.stack([video_pos_starts, video_pos_starts + 1.0], axis=-1)
    audio_pos_starts = rng.standard_normal((1, 1, 4)).astype(np.float32)
    audio_pos = np.stack([audio_pos_starts, audio_pos_starts + 1.0], axis=-1)
    # Cross-attention text context: (B, 1024 tokens, dim)
    video_ctx = rng.standard_normal((1, 1024, 4096)).astype(np.float32) * 0.1
    audio_ctx = rng.standard_normal((1, 1024, 2048)).astype(np.float32) * 0.1
    sigma = np.array([0.5], dtype=np.float32)
    # Per-token timesteps: gen tokens at sigma, kf tokens at 0 (keyframe-conditioning regime)
    video_ts = np.array([[0.5] * n_gen + [0.0] * n_kf], dtype=np.float32)
    audio_ts = np.array([[0.5] * 4], dtype=np.float32)
    return dict(
        video_latent=video_latent,
        audio_latent=audio_latent,
        video_pos=video_pos,
        audio_pos=audio_pos,
        video_ctx=video_ctx,
        audio_ctx=audio_ctx,
        sigma=sigma,
        video_ts=video_ts,
        audio_ts=audio_ts,
    )


def main() -> None:
    print("Building LTXModel(num_layers=1)...")
    model = build_model()
    print("Loading weights (block 0 + adaln + finale)...")
    state = load_weights_for_one_block()
    print(f"  loaded {len(state)} keys")
    missing, unexpected = model.load_state_dict(state, strict=False)
    print(f"  missing: {len(missing)} unexpected: {len(unexpected)}")
    if missing[:5]:
        print(f"  first missing: {missing[:5]}")
    if unexpected[:5]:
        print(f"  first unexpected: {unexpected[:5]}")

    model.train(False)

    inputs = make_inputs(seed=42)

    video_mod = Modality(
        latent=torch.from_numpy(inputs["video_latent"]),
        sigma=torch.from_numpy(inputs["sigma"]),
        timesteps=torch.from_numpy(inputs["video_ts"]),
        positions=torch.from_numpy(inputs["video_pos"]),
        context=torch.from_numpy(inputs["video_ctx"]),
        enabled=True,
    )
    audio_mod = Modality(
        latent=torch.from_numpy(inputs["audio_latent"]),
        sigma=torch.from_numpy(inputs["sigma"]),
        timesteps=torch.from_numpy(inputs["audio_ts"]),
        positions=torch.from_numpy(inputs["audio_pos"]),
        context=torch.from_numpy(inputs["audio_ctx"]),
        enabled=True,
    )
    perts = BatchedPerturbationConfig.empty(1)

    print("Running forward...")
    with torch.no_grad():
        vx, ax = model(video_mod, audio_mod, perts)

    print(f"video out shape: {vx.shape} mean={vx.mean().item():.6f} std={vx.std().item():.6f}")
    print(f"audio out shape: {ax.shape} mean={ax.mean().item():.6f} std={ax.std().item():.6f}")

    np.savez(
        "/tmp/T8_transformer_pt.npz",
        **inputs,
        video_out=vx.detach().cpu().numpy().astype(np.float32),
        audio_out=ax.detach().cpu().numpy().astype(np.float32),
    )
    print("wrote /tmp/T8_transformer_pt.npz")


if __name__ == "__main__":
    main()
