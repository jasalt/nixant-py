"""The CLI's evaluation expression against real Nix; no Incus needed.

Unit tests feed evaluate() hand-written JSON, so only these prove that the
--apply expression and the module's runtime agree.
"""

import json
from pathlib import Path

import pytest

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.nix.eval import evaluate, evaluate_spec
from nixant.nix.store import is_drv_path
from nixant.run import Runner

from .conftest import REPO, git

pytestmark = pytest.mark.integration

EXAMPLE = REPO / "examples" / "basic"


def test_example_evaluates_to_its_golden_runtime() -> None:
    golden = MachineSpec.from_runtime(
        json.loads((EXAMPLE / "runtime.json").read_text())
    )
    evaluated = evaluate(EXAMPLE, "dev", Runner())
    assert evaluated.spec == golden
    assert is_drv_path(evaluated.drv_path)
    assert evaluate_spec(EXAMPLE, "dev", Runner()) == golden


@pytest.fixture
def plain_flake(tmp_path: Path) -> Path:
    """A flake with no inputs whose only target lacks the nixant module."""
    root = tmp_path / "plain"
    root.mkdir()
    (root / "flake.nix").write_text(
        "{ outputs = _: { nixosConfigurations.plain = { config = { }; }; }; }\n"
    )
    git(root, "init", "-q")
    git(root, "add", "flake.nix")
    return root


def test_unknown_target_lists_the_available_ones(plain_flake: Path) -> None:
    with pytest.raises(UsageError, match="'nope' not found; available targets: plain"):
        evaluate_spec(plain_flake, "nope", Runner())


def test_target_without_the_module_is_reported(plain_flake: Path) -> None:
    with pytest.raises(NixantError, match="must import nixant.nixosModules"):
        evaluate(plain_flake, "plain", Runner())


def test_broken_flake_fails_in_nix(plain_flake: Path) -> None:
    (plain_flake / "flake.nix").write_text("{ this is not nix\n")
    with pytest.raises(NixantError):
        evaluate_spec(plain_flake, "plain", Runner())
