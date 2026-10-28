"""The CLI's evaluation expression against real Nix; no Incus needed.

Unit tests feed evaluate() hand-written JSON, so only these prove that the
--apply expression and the module's runtime agree.
"""

import json
import shutil
from pathlib import Path

import pytest

from nixant.errors import NixantError, UsageError
from nixant.models import MachineSpec
from nixant.nix.eval import evaluate, evaluate_spec
from nixant.nix.store import is_drv_path
from nixant.project import flake_reference
from nixant.run import Runner

from .conftest import REPO

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
    (root / ".nixant").mkdir(parents=True)
    (root / ".nixant/flake.nix").write_text(
        "{ outputs = _: { nixosConfigurations.plain = { config = { }; }; }; }\n"
    )
    return root


def test_unknown_target_lists_the_available_ones(plain_flake: Path) -> None:
    with pytest.raises(UsageError, match="'nope' not found; available targets: plain"):
        evaluate_spec(plain_flake, "nope", Runner())


def test_target_without_the_module_is_reported(plain_flake: Path) -> None:
    with pytest.raises(NixantError, match="must import nixant.nixosModules"):
        evaluate(plain_flake, "plain", Runner())


def test_broken_flake_fails_in_nix(plain_flake: Path) -> None:
    (plain_flake / ".nixant/flake.nix").write_text("{ this is not nix\n")
    with pytest.raises(NixantError):
        evaluate_spec(plain_flake, "plain", Runner())


def test_source_boundary_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nix = shutil.which("nix")
    assert nix is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nix").symlink_to(nix)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert shutil.which("git") is None
    root = tmp_path / "workspace #1"
    config = root / ".nixant"
    config.mkdir(parents=True)
    (config / "flake.nix").write_text(
        "{ outputs = { self }: { source = self.outPath; }; }"
    )
    (config / "untracked.nix").write_text("{}")
    (root / "project-a/.git").mkdir(parents=True)
    (root / "secret").write_text("sentinel")
    runner = Runner()
    argv = ["nix", "eval", "--raw", f"{flake_reference(root)}#source"]
    source = Path(runner.run(argv, capture=True).stdout.decode())
    assert sorted(p.name for p in source.iterdir()) == ["flake.nix", "untracked.nix"]
    (root / "secret").write_text("changed")
    assert runner.run(argv, capture=True).stdout.decode() == str(source)
    # A parent work tree must not widen or filter the explicit path source.
    (root / ".git").mkdir()
    assert runner.run(argv, capture=True).stdout.decode() == str(source)
