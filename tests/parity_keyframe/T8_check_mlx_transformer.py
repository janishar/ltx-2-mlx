"""T8 (MLX side): LTXModel forward parity, num_layers=1.

Loads same PT bf16 weights from upstream (model.diffusion_model.* prefix
needs remapping to MLX's transformer.* prefix). Runs forward with same
seeded keyframe-conditioned input, compares against PT dump.
"""

from __future__ import annotations

import os
import sys

import mlx.core as mx
import numpy as np

from ltx_core_mlx.model.transformer.model import LTXModel, LTXModelConfig

PT_WEIGHTS = os.path.expanduser(
    "~/.cache/huggingface/hub/models--Lightricks--LTX-2.3/"
    "snapshots/76730e634e70a28f4e8d51f5e29c08e40e2d8e74/ltx-2.3-22b-dev.safetensors"
)
MLX_WEIGHTS = os.path.join(
    os.path.expanduser(os.environ.get("LTX_PARITY_BF16_PACK_DIR", "")), "transformer-dev.safetensors"
)
PT = "/tmp/T8_transformer_pt.npz"


def load_mlx_block_0_weights() -> dict:
    """Load only block 0 + adaln + finale weights from MLX bf16 transformer-dev."""
    raw = mx.load(MLX_WEIGHTS)
    out: dict[str, mx.array] = {}
    for k, v in raw.items():
        if not k.startswith("transformer."):
            continue
        new_k = k.removeprefix("transformer.")
        # Filter: keep only block 0 + non-block keys
        if "transformer_blocks." in new_k and not new_k.startswith("transformer_blocks.0."):
            continue
        out[new_k] = v.astype(mx.float32)
    return out


def main() -> None:
    pt = dict(np.load(PT))
    cfg = LTXModelConfig(num_layers=1)
    model = LTXModel(cfg)

    weights = load_mlx_block_0_weights()
    print(f"Loading {len(weights)} MLX weight keys...")
    model.load_weights(list(weights.items()), strict=False)

    # Build MLX-style inputs from the saved PT inputs.
    # Positions: PT shape (B, 3, T, 2) where last is [start, end]; MLX wants (B, T, num_axes) midpoint.
    video_pos_pt = pt["video_pos"]  # (1, 3, T, 2)
    video_pos_mid = ((video_pos_pt[..., 0] + video_pos_pt[..., 1]) / 2.0).transpose(0, 2, 1)
    audio_pos_pt = pt["audio_pos"]  # (1, 1, T, 2)
    audio_pos_mid = ((audio_pos_pt[..., 0] + audio_pos_pt[..., 1]) / 2.0).transpose(0, 2, 1)

    video_latent = mx.array(pt["video_latent"])
    audio_latent = mx.array(pt["audio_latent"])
    sigma = mx.array(pt["sigma"])
    video_ts = mx.array(pt["video_ts"])
    audio_ts = mx.array(pt["audio_ts"])
    video_pos = mx.array(video_pos_mid)
    audio_pos = mx.array(audio_pos_mid)
    video_ctx = mx.array(pt["video_ctx"])
    audio_ctx = mx.array(pt["audio_ctx"])

    print("Running forward...")
    vx, ax = model(
        video_latent=video_latent,
        audio_latent=audio_latent,
        timestep=sigma,
        video_text_embeds=video_ctx,
        audio_text_embeds=audio_ctx,
        video_positions=video_pos,
        audio_positions=audio_pos,
        video_timesteps=video_ts,
        audio_timesteps=audio_ts,
    )

    vx_np = np.asarray(vx).astype(np.float32)
    ax_np = np.asarray(ax).astype(np.float32)
    print(f"MLX video out shape: {vx_np.shape}")
    print(f"MLX audio out shape: {ax_np.shape}")

    pt_v, pt_a = pt["video_out"], pt["audio_out"]
    print(f"PT  video out shape: {pt_v.shape}")
    print(f"PT  audio out shape: {pt_a.shape}")

    if vx_np.shape != pt_v.shape:
        print("FAIL: video shape mismatch")
        sys.exit(1)
    if ax_np.shape != pt_a.shape:
        print("FAIL: audio shape mismatch")
        sys.exit(1)

    v_delta = float(np.max(np.abs(vx_np - pt_v)))
    a_delta = float(np.max(np.abs(ax_np - pt_a)))
    v_rel = v_delta / max(float(np.max(np.abs(pt_v))), 1e-9)
    a_rel = a_delta / max(float(np.max(np.abs(pt_a))), 1e-9)
    print(f"video max_abs={v_delta:.4e}  rel={v_rel:.4%}")
    print(f"audio max_abs={a_delta:.4e}  rel={a_rel:.4%}")
    print(f"PT  video mean={pt_v.mean():.4f} std={pt_v.std():.4f}")
    print(f"MLX video mean={vx_np.mean():.4f} std={vx_np.std():.4f}")
    print(f"PT  audio mean={pt_a.mean():.4f} std={pt_a.std():.4f}")
    print(f"MLX audio mean={ax_np.mean():.4f} std={ax_np.std():.4f}")

    fail = max(v_delta, a_delta) > 5e-2
    print()
    print("FAIL" if fail else "OK")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
