"""Measure fresh-eval-cache and warm Nix evaluations without building a system.

Run from the checkout: python3 scripts/benchmark_eval.py
Input fetching/locking is timed separately. Existing store paths are not deleted;
"fresh" does not mean an empty Nix store or a cold OS page cache.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def timed(argv: list[str], cwd: Path, env: dict[str, str]) -> float:
    started = time.monotonic()
    subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL, check=True)
    return round(time.monotonic() - started, 3)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nixant-eval-") as directory:
        root = Path(directory)
        (root / "flake.nix").write_text(
            "{ inputs.nixant.url = " + json.dumps(f"path:{ROOT}") + ";\n"
            'inputs.nixpkgs.follows = "nixant/nixpkgs";\n'
            "outputs = { nixant, nixpkgs, ... }: { nixosConfigurations.dev = "
            'nixpkgs.lib.nixosSystem { system = "x86_64-linux"; modules = [ '
            'nixant.nixosModules.container { nixant.instanceName = "benchmark-dev"; '
            'system.stateVersion = "25.05"; } ]; }; }; }\n'
        )
        env = dict(os.environ)
        results = {"lock_seconds": timed(["nix", "flake", "lock"], root, env)}
        expressions = {
            "combined": "c: { runtime = c.config.nixant.runtime; "
            "drvPath = c.config.system.build.toplevel.drvPath; }",
            "runtime": "c: c.config.nixant.runtime",
        }
        for name, expression in expressions.items():
            env["XDG_CACHE_HOME"] = str(root / f"cache-{name}")
            argv = [
                "nix",
                "eval",
                "--json",
                ".#nixosConfigurations.dev",
                "--apply",
                expression,
            ]
            for label in ("fresh", "warm1", "warm2"):
                results[f"{name}_{label}_seconds"] = timed(argv, root, env)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
