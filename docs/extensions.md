# External extension development

An extension is an ordinary NixOS module distributed by a flake. There is no plugin loader or registration step: a consumer imports both nixant's container (or VM) module and the extension module into its `nixosConfiguration`.

See the [public extension interface](../README.md#extension-modules) for the supported `nixant.*` options. The in-tree [WordPress extension](../extensions/wordpress/README.md) is a larger example. This guide describes an independent repository; it does not move WordPress out of nixant.

## One checkout or separate repositories?

| Concern | In-tree extension | Separate repository |
|---|---|---|
| Versioning | Core and extension normally share one pinned revision. | Consumers can update and roll back them independently. |
| Coordinated changes | Core, extension and tests can change atomically. | Cross-repository changes require compatible revisions and coordination. |
| Interface discipline | Easy to accidentally depend on internals or change both sides without exposing a compatibility break. | Encourages an explicit compatibility contract, but still needs tests. |
| Maintenance | Shared CI, ownership and release cadence. | Independent ownership and releases, with additional CI and dependency maintenance. |
| Setup | One input and a bundled `nixant init` template. | Another input and a separately distributed Nix template. |

Directory placement does not determine modularity. An in-tree module can stay strictly on the public interface. Nor does exporting a module enable its services: importing `nixosModules.container` does not enable WordPress.

There is a packaging cost in the current checkout: the CLI package uses `src = self` and its wrapper references `${self}`. Extension-only changes can therefore change the CLI derivation/source reference even when its Python code is unchanged. A separate repository avoids that particular coupling, at the cost of managing another release and lockfile.

## Minimal repository

The following is a copyable starter, not an existing published extension. Replace `YOUR-ORG/nixant-example` with your repository before using its template.

```text
nixant-example/
├── flake.nix
├── flake.lock
├── nix/module.nix
└── template/
    ├── flake.nix
    └── nix/dev.nix
```

### Extension flake

Create `flake.nix`:

```nix
{
  description = "Example nixant extension";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "github:jasalt/nixant-py";
    nixant.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixant, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      testGuest = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          nixant.nixosModules.container
          self.nixosModules.default
          {
            nixant.instanceName = "extension-check";
            nixant.user = { name = "dev"; uid = 1000; };
            nixant.mounts.workspace.source = "/tmp/extension-check";
            example.enable = true;
            system.stateVersion = "26.05";
          }
        ];
      };
    in {
      nixosModules.default = import ./nix/module.nix;
      templates.default = {
        path = ./template;
        description = "nixant container with the example extension";
      };
      checks.${system}.eval = pkgs.runCommand "example-eval" {
        # Force evaluation to the system derivation without building the guest.
        systemDrv = testGuest.config.system.build.toplevel.drvPath;
      } ''
        printf '%s\n' "$systemDrv" > "$out"
      '';
    };
}
```

The extension's nixant input is used for its own tests. Exporting the module does not automatically import that copy of nixant into a consumer's system.

### Module

Create `nix/module.nix`:

```nix
{ config, lib, pkgs, ... }:
{
  options.example.enable = lib.mkEnableOption "the example development tools";

  config = lib.mkIf config.example.enable {
    environment.systemPackages = [ pkgs.ripgrep ];
    environment.variables.EXAMPLE_WORKDIR = config.nixant.workdir;
  };
}
```

This module requires nixant when enabled. Use your own option namespace, rather than adding private options under `nixant`. Read only the documented public interface; do not read or write `nixant.runtime`. If supporting operation without nixant, detect its presence with `options ? nixant` and guard access to `config.nixant` accordingly.

For services writing the shared workspace, use the configured guest user and respect the isolation model. Do not assume root access, a hard-coded UID, or a fixed `/workspace` path.

## Consumer template

Create `template/flake.nix`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixant.url = "github:jasalt/nixant-py";
    nixant.inputs.nixpkgs.follows = "nixpkgs";

    extension.url = "github:YOUR-ORG/nixant-example";
    extension.inputs.nixpkgs.follows = "nixpkgs";
    extension.inputs.nixant.follows = "nixant";
  };

  outputs = { nixpkgs, nixant, extension, ... }: {
    nixosConfigurations.dev = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        nixant.nixosModules.container
        extension.nixosModules.default
        ./nix/dev.nix
      ];
    };
  };
}
```

Create `template/nix/dev.nix`:

```nix
{
  nixant = {
    instanceName = "example-dev";
    user = { name = "dev"; uid = 1000; sudo = true; };
    mounts.workspace.source = "/absolute/path/to/project";
  };
  example.enable = true;
  system.stateVersion = "26.05";
}
```

The consumer owns its nixpkgs and nixant versions. The nested `follows` declarations align the extension's inputs with those versions; they do not prove compatibility. They assume the extension declares inputs with these names. nixant currently requires guest nixpkgs 26.05 or newer.

Use a real published URL in external templates. The `nixant-template-url` marker in nixant's bundled templates is rewritten by `nixant init`; plain `nix flake init` does not perform that substitution.

## Initialize a consumer

With the extension published:

```bash
mkdir demo && cd demo
git init
nix flake init -t github:YOUR-ORG/nixant-example#default
# Edit nix/dev.nix: set the absolute host checkout path, host UID (id -u),
# and a unique Incus instance name.
git add flake.nix nix/dev.nix
nix flake lock
git add flake.lock
nixant up
nixant exec -- rg --version
```

Install/run the CLI as described in [Running nixant](../README.md#running-nixant), and satisfy the host's Nix/Incus requirements first. For reproducibility, use a CLI revision matching the consumer's locked nixant revision.

`nixant init --list` lists bundled templates, not arbitrary external extensions. External projects use `nix flake init -t …` and then the same `nixant up`, `rebuild`, `shell` and other lifecycle commands as bundled projects. `nix flake init` copies template files; it does not automatically make the consumer depend on the revision from which the template was copied. The consumer's inputs and lockfile determine that.

## Develop across local checkouts

Keep three directories: nixant, the extension, and a disposable consumer. To initialize from an unpublished extension, run this in an empty consumer directory instead of the published-template command:

```bash
nix flake init -t path:/absolute/path/to/nixant-example#default
```

Edit the consumer settings and stage its new Nix files as above. Then, from the **consumer** directory, lock both inputs to your working copies:

```bash
nix flake lock \
  --override-input nixant path:/absolute/path/to/nixant \
  --override-input extension path:/absolute/path/to/nixant-example

# First deployment:
nix run path:/absolute/path/to/nixant -- up
# Subsequent deployments:
nix run path:/absolute/path/to/nixant -- rebuild
```

Repeat the lock command after editing either checkout, then rebuild. Locked inputs are snapshots, not live links. `path:` references include working-tree content; Git-backed local references generally exclude untracked files, so stage new files when using those.

To run the extension's own checks against local core changes, run this from the **extension** directory:

```bash
nix flake check --override-input nixant path:/absolute/path/to/nixant
```

Local overrides can change lockfiles and make them machine-specific. Do not commit them as release or consumer pins. Before publishing, remove the overrides by updating the affected inputs using their declared published URLs:

```bash
# Consumer directory, after the extension is published:
nix flake update nixant extension
# Extension directory, if its nixant input was overridden:
nix flake update nixant
```

These commands select current published revisions, not necessarily the old pins. To return to earlier pins, restore the intended lockfile from version control instead, preserving unrelated changes. Review the lockfile diff and test again with the published inputs. Commit the extension's own `flake.lock` and each real consumer's lockfile; a starter template can leave lock creation to the consumer.

## Validation and release workflow

There are three different levels of confidence:

1. **Evaluation checks:** exercise enabled/disabled options, assertions, defaults, and the public interface. The starter's check evaluates one enabled guest to its system derivation; it is only a smoke check. Add negative tests and evaluate the actual consumer template, not just a hand-written fixture. With `nixosModules.options` alone, explicitly enable nixant and define the guest user's `users.users` entry; that module does not create a guest.
2. **NixOS VM tests:** boot services and verify behavior. Export an optional package such as `packages.x86_64-linux.vm-test`, backed by `pkgs.testers.runNixOSTest`, and run `nix build .#vm-test -L`. This output is not provided by the starter. Keep KVM-dependent tests separate from lightweight checks when CI lacks KVM.
3. **Real Incus integration tests:** verify mount ownership, port forwarding, activation, isolation, and lifecycle operations on a supported host. Use disposable projects and unique instance names, with cleanup on failure. The WordPress extension's [test documentation](../extensions/wordpress/README.md#tests) is a reference for this split.

Run checks in the extension repository:

```bash
nix flake check
```

Then validate a configured consumer separately:

```bash
# Build the complete guest; no Incus deployment yet.
nix build .#nixosConfigurations.dev.config.system.build.toplevel -L
# Deploy and exercise the actual environment.
nixant up
nixant exec -- rg --version
# Deletes the disposable guest and its guest-local data, not host workspace files.
nixant destroy
```

A successful evaluation does not establish that services build or run. Likewise, a VM test does not establish that Incus mounts or networking work.

For each extension release, document the tested nixant and nixpkgs revisions, supported host architecture and container/VM support. Test locked supported dependencies in CI; optionally test newer core revisions in a separate compatibility job. Publish core changes before extension releases that require them, then update consumer pins explicitly. Independent releases are useful only if consumers can identify a supported combination.
