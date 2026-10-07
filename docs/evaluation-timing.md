# Single-evaluation timing

Measured on the local Fedora Lima VM with Nix 2.34.8, using nixpkgs
`151fa4e8ddfdd8dd25d945ad94ed54a13de9f6e4` and the minimal nixant container
module. Reproduce with `python3 scripts/benchmark_eval.py`.

| Operation | Fresh eval cache | Warm 1 | Warm 2 |
|---|---:|---:|---:|
| runtime + toplevel drvPath | 13.223 s | 11.776 s | 10.253 s |
| runtime only | 0.861 s | 0.847 s | 0.863 s |

Input locking took 0.405 s. Each form starts with an isolated `XDG_CACHE_HOME`;
subsequent runs reuse it. Inputs already existed in the Nix store. These are
**not empty-store/network cold timings** and do not flush the OS page cache.
Fetching on a genuinely new host adds network-dependent latency. No system
build or Incus mutation is included.

The full NixOS derivation evaluation dominates latency even warm. Keep the
planned single `--apply` evaluation for runtime + drvPath, then build the drv
output without re-evaluation. Keep `config` runtime-only. Print progress before
evaluation and inherit Nix stderr. Do not introduce a second runtime evaluation
or a bespoke cache: the limited warm improvement does not justify correctness
risk from stale configuration. Re-measure against a larger example when needed.
