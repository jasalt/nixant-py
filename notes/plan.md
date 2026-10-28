# Nixant — Python Prototype Implementation Plan

## Goal

Build **nixant**, a small NixOS-focused Incus development-environment CLI derived from `lnussbaum/incant`.

Nixant should behave roughly like a minimal Lima/Vagrant-style frontend, but with a strict division of responsibilities:

- **Incus** manages containers/VMs, storage, networking, mounts, lifecycle.
- **NixOS** defines and applies guest system state.
- **Nixant** provides project discovery, naming, lifecycle orchestration, and developer-friendly commands.

The prototype should aggressively reuse useful Incant code while removing its generic Linux provisioning architecture.

## Core principles

1. NixOS only.
2. Incus only.
3. Containers are the default.
4. `flake.nix` is the source of guest configuration.
5. No `nixant.yaml`, YAML schema, Jinja, or Mako.
6. No generic provisioning subsystem.
7. No SSH provisioning.
8. No custom persistent state database.
9. Prefer invoking `incus`, `nix`, and `nixos-rebuild` over introducing Python libraries for them.
10. Keep implementation small enough to rewrite later if the prototype validates the UX.

Target initial size: approximately **1–2k lines of Python excluding tests**.

---

# CLI

Implement:

```text
nixant up [target]
nixant apply [target]
nixant shell [target]
nixant exec [target] -- COMMAND...
nixant stop [target]
nixant destroy [target]
nixant status [target]
```

Default target:

```text
dev
```

Example:

```bash
nixant up
nixant shell
nixant apply

nixant up backend
nixant exec backend -- systemctl status postgresql
```

Global useful options:

```text
-v, --verbose
--vm
```

Do not add additional configuration options unless required by implementation.

---

# Project model

Discover the project root by walking upward from the current directory.

Prefer:

1. directory containing `flake.nix`
2. stop at filesystem root

Fail clearly if no `flake.nix` exists.

Represent an environment internally with a small immutable/dataclass-style model:

```python
@dataclass
class Environment:
    root: Path
    target: str
    instance_name: str
    vm: bool = False
```

Instance name should be deterministic from:

```text
project directory name + target
```

Example:

```text
supplier-import-dev
supplier-import-test
```

Sanitize names for Incus compatibility.

If necessary, append a short stable hash of the absolute project path to prevent collisions.

Do not create local `.nixant` state.

---

# Flake convention

A target maps directly to:

```text
.#nixosConfigurations.<target>
```

Example:

```bash
nixant up dev
```

uses:

```text
.#nixosConfigurations.dev
```

Do not introduce a custom `nixant` flake output in v1.

Before creating an environment, optionally perform a cheap validation that the target exists using `nix eval` or equivalent.

Failure should state the exact expected flake attribute.

---

# Default guest

Use the current suitable official/community NixOS Incus image, initially equivalent to:

```text
images:nixos/unstable
```

Verify the currently recommended image syntax before implementation.

Container is the default.

Container launch should include the minimum configuration required for NixOS/Nix operation, expected initially to include:

```text
security.nesting=true
```

Do not expose generic Incus config passthrough in v1.

---

# Workspace mount

Mount the project root into every environment as:

```text
/workspace
```

Use an Incus disk device.

Attempt UID/GID shifting where appropriate:

```text
shift=true
```

Use a deterministic device name such as:

```text
nixant-workspace
```

Mount creation must be idempotent.

If an existing managed instance lacks the mount, add it.

Do not implement arbitrary additional mounts yet.

---

# NixOS application

`nixant apply TARGET` is the replacement for Incant provisioning.

Inside the guest, run approximately:

```bash
cd /workspace

nixos-rebuild switch \
  --flake /workspace#TARGET
```

Ensure flake/nix-command support is available during bootstrap even before the project's NixOS configuration enables it.

Prefer setting temporary `NIX_CONFIG` environment configuration over mutating guest files outside NixOS.

After successful activation, future state is entirely managed through NixOS.

Do not:

- install packages imperatively
- configure users imperatively
- configure SSH imperatively
- copy configuration files as provisioning steps
- provide script hooks

Those belong in the NixOS configuration.

---

# `up`

`nixant up TARGET` must be idempotent.

Pseudo-flow:

```text
resolve project
resolve target
derive instance name

if instance does not exist:
    launch NixOS instance
    attach /workspace mount

if instance is stopped:
    start it

wait until Incus exec works

ensure workspace mount exists

apply NixOS configuration

print concise ready message
```

Do not fail because an already-created environment exists.

This differs intentionally from current Incant behavior.

---

# `apply`

Flow:

```text
resolve environment
verify instance exists
verify it is running
run nixos-rebuild switch
```

Do not automatically recreate missing environments; tell the user to run:

```bash
nixant up
```

---

# `shell`

Open an interactive shell in:

```text
/workspace
```

Initially use Incus directly rather than SSH.

Equivalent behavior may use:

```bash
incus exec NAME --cwd /workspace -- bash -l
```

or `incus shell` if it provides better terminal behavior.

Prefer correct interactive terminal behavior over exact command form.

---

# `exec`

Pass everything following `--` through to Incus:

```bash
nixant exec -- systemctl status nginx
```

Default working directory:

```text
/workspace
```

Preserve command exit status.

---

# Lifecycle commands

## stop

Equivalent to:

```bash
incus stop NAME
```

Idempotent if already stopped.

## destroy

Equivalent to:

```bash
incus delete --force NAME
```

Ask no interactive questions in v1.

Fail safely if the resolved instance does not belong to this project unless ownership can be verified.

## status

Print only useful state, e.g.:

```text
supplier-import-dev  RUNNING
target: dev
workspace: /home/user/src/supplier-import
```

Avoid reproducing `incus info`.

---

# Ownership metadata

Store Nixant metadata on the Incus instance using `user.*` configuration keys.

Suggested:

```text
user.nixant.managed=true
user.nixant.project=<absolute-or-stable-project-id>
user.nixant.target=dev
user.nixant.version=1
```

Use these values to distinguish Nixant environments from unrelated Incus instances.

Before mutating or deleting an existing same-named instance, verify ownership.

---

# VM support

Support:

```bash
nixant up --vm
```

but keep it secondary.

Use the appropriate NixOS Incus VM image/boot requirements and verify current Incus/NixOS recommendations while implementing.

Do not add a large abstraction layer to unify VM/container differences.

Isolate differences in the Incus backend.

---

# Package structure

Target approximately:

```text
nixant/
├── __init__.py
├── __main__.py
├── cli.py
├── project.py
├── incus.py
├── nixos.py
├── models.py
└── errors.py
```

Responsibilities:

### `cli.py`

- Click command definitions
- argument parsing
- user-facing errors
- exit codes

Reuse Incant's Click approach where useful.

### `project.py`

- find project root
- validate flake presence
- sanitize project name
- derive deterministic instance name
- construct `Environment`

### `incus.py`

Thin subprocess wrapper around `incus`.

Required operations:

```text
exists
info
launch
start
stop
delete
exec
shell
add_workspace_mount
workspace_mount_exists
set_metadata
get_metadata
```

Reuse and simplify Incant's existing `IncusCLI`.

Do not build a generic Incus API abstraction.

### `nixos.py`

Responsibilities:

```text
target_exists
apply
```

Keep Nix-specific subprocess construction here.

### `models.py`

Only small internal data structures such as `Environment`.

### `errors.py`

Small exception hierarchy for expected user-facing failures.

---

# Code to reuse from Incant

Review and selectively adapt:

```text
incant/cli.py
incant/incus_cli.py
incant/reporter.py
incant/exceptions.py
```

Useful concepts include:

- subprocess error wrapping
- Incus readiness checks
- instance existence/status queries
- interactive shell handling
- shared-folder retry behavior
- Click CLI structure

Do not mechanically fork all Incant code.

---

# Code to remove/not port

Do not port:

```text
config_manager.py
provisioning_manager.py
provisioners/
YAML support
Jinja support
Mako support
generic InstanceConfig schema
copy provisioner
SSH provisioner
LLMNR provisioner
script provisioner
pre-launch command system
generic distro handling
```

NixOS replaces those responsibilities.

---

# Readiness handling

Current Incant contains useful logic for waiting until the Incus agent/guest is usable.

Simplify this.

Nixant only needs to know:

```text
can `incus exec NAME -- true` succeed?
```

Implement bounded retries with a useful timeout/error.

Avoid distro-specific boot-state detection unless actual NixOS behavior requires it.

---

# Command execution

Use `subprocess.run()` with argument arrays.

Never invoke commands through a shell unless strictly necessary.

Provide a single internal command runner supporting:

```python
run(
    args,
    *,
    capture=False,
    check=True,
    interactive=False,
)
```

Verbose mode should print commands before execution.

Keep command output visible for operations such as `nixos-rebuild`.

---

# Error behavior

Expected errors should be concise and actionable.

Examples:

```text
error: no flake.nix found in this directory or its parents
```

```text
error: nixosConfigurations.dev was not found in /path/to/project/flake.nix
```

```text
error: instance supplier-import-dev exists but is not managed by nixant
```

```text
error: environment does not exist; run `nixant up`
```

Preserve subprocess stderr where useful.

---

# Testing

Unit-test logic that does not require Incus:

- project-root discovery
- project-name sanitization
- deterministic instance naming
- target parsing
- command construction
- ownership validation

Mock subprocess/Incus interactions.

Add optional integration tests gated behind an environment variable, e.g.:

```text
NIXANT_INTEGRATION=1
```

Integration smoke test:

```text
create temporary flake
nixant up
nixant exec -- true
nixant apply
nixant stop
nixant up
nixant destroy
```

Do not require integration tests for normal test runs.

---

# Packaging

Use modern `pyproject.toml`.

Provide:

```text
nixant = "nixant.cli:cli"
```

Keep runtime dependencies minimal.

Prefer:

```text
click
```

plus Python standard library.

Remove PyYAML, Jinja2, Mako and other inherited dependencies unless actually needed.

Provide a development flake so the project itself can be run with:

```bash
nix develop
```

and ideally:

```bash
nix run . -- up
```

---

# Prototype exclusions

Explicitly out of scope:

- automatic port forwarding
- port discovery
- arbitrary mounts
- filesystem watching
- background daemon
- Incus remote management
- custom networks
- custom storage pools
- snapshots
- image building
- image caching
- multiple simultaneous project instances for one target
- custom runtime configuration language
- autonomous-agent isolation profiles
- secrets management
- cross-platform/macOS support
- direct Incus REST API integration

Keep extension points obvious, but do not implement these prematurely.

---

# Implementation sequence

1. Create `nixant` package and CLI skeleton.
2. Port minimal Incus subprocess wrapper from Incant.
3. Implement project/flakes discovery and deterministic naming.
4. Implement ownership metadata.
5. Implement container `up`, readiness and workspace mount.
6. Implement NixOS `apply`.
7. Implement `shell` and `exec`.
8. Implement `stop`, `destroy`, and `status`.
9. Add VM mode.
10. Add unit tests.
11. Add one opt-in end-to-end integration test.
12. Remove leftover abstractions/dependencies and document the resulting CLI.

---

# Acceptance scenario

Given a project containing:

```text
flake.nix
```

with:

```text
nixosConfigurations.dev
```

this must work:

```bash
cd project

nixant up
nixant exec -- hostname
nixant shell
nixant apply
nixant stop
nixant up
nixant status
nixant destroy
```

Changing the NixOS configuration and running:

```bash
nixant apply
```

must update the running environment without recreating it.

The project should finish with Nixant clearly behaving as:

```text
Incus lifecycle
      +
workspace mount
      +
NixOS activation
      +
small CLI
```

rather than as a generic provisioning/configuration-management framework.
