# Migrate a project to `.nixant/`

Current nixant releases discover environment configuration in the nearest
`.nixant/flake.nix`. Root-level project flakes are no longer a supported nixant
layout. This is a manual, one-time move; do not run `nixant init` over an
existing root-level environment and expect it to migrate automatically.

## Before you move the files

1. Stop any nixant operation for the workspace and back up the workspace and
   `flake.lock`.
2. Keep the workspace at its current absolute path while migrating. nixant's
   ownership identity, target locks, and mount roots are based on the workspace
   path, not on the configuration directory. Moving the workspace changes that
   identity; use `nixant adopt` only when you intentionally move a workspace.
3. For every target, identify the *effective* instance name currently in use.
   If the old `nixant name` command was used, its value was stored in Git config
   as `nixant.<target>.instanceName` and was not visible to Nix. Copy that value
   into the target's Nix configuration before upgrading. For example, inspect
   repository-local config with:

   ```sh
   git config --local --get-all nixant.dev.instanceName
   git config --worktree --get-all nixant.dev.instanceName
   ```

   The old worktree command may report an error when worktree config was never
   enabled. Check all targets that had overrides, and use the value shown by
   the old `nixant config` or `nixant status` when in doubt. Put the active
   value in that target's configuration as `nixant.instanceName = "...";`.
   For example, if the `dev` target was named `shop-work`, preserve that exact
   name in the module that defines `nixosConfigurations.dev`:

   ```nix
   nixant.instanceName = "shop-work";
   ```

   Do this before the first `up` with the new release. Keeping the workspace
   path and each existing instance name unchanged lets nixant recognize and
   reuse the existing instances. If the configured name differs, nixant will
   refuse rather than silently retarget or replace an instance. To intentionally
   rename, explicitly destroy the old instance first (this deletes its
   guest-only data), then create the new one.

## Move the environment configuration

From the workspace root, move the flake and its configuration modules into the
isolated source. Keep runtime/project files such as application code, databases,
secrets, and caches at the workspace root.

```sh
mkdir -p .nixant
mv flake.nix .nixant/
if [ -f flake.lock ]; then mv flake.lock .nixant/; fi
if [ -d nix ]; then mv nix .nixant/; fi
```

The paths from `flake.nix` to modules moved with `nix/` stay the same (for
example `./nix/dev.nix`). Other files imported using relative Nix paths must
also be moved into `.nixant/` and their references kept within that source, or
restructured as explicit flake inputs. Nix evaluates the configuration as an
explicit path source; references to arbitrary files elsewhere in the workspace
are not a way to bring the whole workspace into the store.

Mounts are separate: keep `nixant.mounts.<name>.source` as a **string** resolved
from the workspace root, not as a Nix path. For example:

```nix
nixant.mounts.workspace.source = ".";
```

The source remains the checkout root even though the flake now lives in
`.nixant/`. Existing string mount paths therefore remain workspace-relative.

Then refresh the lock from the isolated source:

```sh
nix flake lock "path:$PWD/.nixant"
```

A lock made for the old root can contain relative path inputs that no longer
resolve after the move. Review the refreshed `.nixant/flake.lock`; use explicit
remote inputs for shared dependencies. To test or develop against a local nixant
checkout, override the input explicitly:

```sh
nix flake lock "path:$PWD/.nixant" \
  --override-input nixant "path:/absolute/path/to/nixant"
```

The override is local to this lock operation; update the flake's nixant input
URL as appropriate if the project should persistently use that source. Nix
commands run directly for this project (for example `nix flake check`) should
run from `.nixant/`, or name that directory as the flake source.

## Source boundary and Git

nixant evaluates `.nixant/` with an explicit `path:` reference, even when the
workspace is inside a Git repository. Files elsewhere in the workspace,
sibling repositories, and Git's tracked/untracked-file rules do not widen or
filter that source. All ordinary files placed inside `.nixant/` are potential
Nix build inputs and can be copied into the Nix store. Keep credentials,
secrets, generated data, caches, and runtime content outside `.nixant/`.

A Git repository is no longer required by nixant: initialization does not stage
files, evaluation does not require tracked files, and `nixant doctor` no longer
requires Git on the host. This does not remove Git requirements of your own Nix
inputs or tools; those are governed by the inputs and software you choose.

## Verify

Install/use the new nixant release, then check discovery and evaluation before
changing the running instance:

```sh
nixant doctor
nixant config
nixant up --dry-run
```

`nixant config` should show the original workspace root and the intended
`instanceName`. A dry run does not apply changes. Once the configuration and
name are correct, run `nixant up` to reconcile the existing instance.

If you have not moved a legacy project yet, `nixant` reports that it found a
root-level flake and points here; it will not initialize a replacement over
that layout.
