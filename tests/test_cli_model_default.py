"""The CLI has no built-in model: ``--model`` falls back to ``$LTX_MODEL`` or errors."""

from __future__ import annotations

import sys

import pytest

from ltx_pipelines_mlx import cli

GENERATE = ["ltx-2-mlx", "generate", "-p", "a fox", "-o", "out.mp4", "--frame-rate", "24", "--distilled"]


def test_model_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.MODEL_ENV, "/models/ltx-2.5")
    args = cli._build_parser().parse_args(GENERATE[1:])
    assert args.model == "/models/ltx-2.5"


def test_explicit_model_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cli.MODEL_ENV, "/models/ltx-2.5")
    args = cli._build_parser().parse_args([*GENERATE[1:], "--model", "/models/other"])
    assert args.model == "/models/other"


@pytest.mark.parametrize(
    "argv",
    [
        GENERATE,
        ["ltx-2-mlx", "info"],
        ["ltx-2-mlx", "preprocess", "--videos", "clips", "-o", "data"],
    ],
)
def test_missing_model_exits_with_hint(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cli.MODEL_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--model is required" in err
    assert cli.MODEL_ENV in err


def test_commands_without_model_are_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.MODEL_ENV, raising=False)
    args = cli._build_parser().parse_args(["enhance", "-p", "a fox"])
    assert not hasattr(args, "model")
