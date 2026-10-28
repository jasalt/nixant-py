# plan2.md

## Goal

Implement a small Python CLI for local NixOS development environments.

The tool should:

- use **Incus system containers first**
- optionally support Incus VMs
- use **Nix/NixOS as the provisioning model**
- evaluate Nix configuration into normalized JSON for the Python CLI
- keep the runtime backend modular enough to add libvirt, Docker, or another VM/container backend later
- avoid becoming a general-purpose Vagrant replacement

The CLI owns machine lifecycle and host integration. NixOS owns guest system state.

---

## High-Level Architecture

```text
flake.nix / devmachine.nix
        |
        v
   Nix evaluation
        |
        | JSON machine spec
        v
    Python CLI
        |
        +--> lifecycle planner
        |
        +--> provider interface
        |       |
        |       +--> IncusProvider
        |       +--> future providers
        |
        +--> NixOS deploy/activate
        |
        +--> state/cache
```

### Responsibility split

**Nix**

- configuration syntax
- defaults and validation
- composition through modules
- NixOS guest configuration
- reproducible system build

**Python CLI**

- evaluate Nix
- parse normalized JSON
- create/update/delete instances
- configure host-side resources
- copy/build/activate NixOS closure
- provide ergonomic developer commands

**Provider backend**

- create instance
- start/stop/restart
- destroy
- exec
- configure CPU/memory/disk
- mounts
- port/network mappings
- snapshots where supported

---

## Nix Configuration Model

Keep one project-level Nix definition containing both outer machine configuration and inner NixOS configuration.

Example shape:

```nix
{
  devMachines.default = {
    provider = "incus";
    type = "container";

    machine = {
      memory = "4GiB";
      cpus = 4;

      mounts = {
        workspace = {
          source = ./.;
          target = "/workspace";
        };
      };

      ports = [
        { host = 8080; guest = 80; }
      ];
    };

    nixos = {
      services.openssh.enable = true;
      services.mysql.enable = true;
    };
  };
}
```

Do not parse Nix in Python.

Evaluate a dedicated flake output into JSON:

```bash
nix eval --json .#devMachines.default.runtimeConfig
```

The Nix module layer should normalize values and reject invalid configuration before Python sees it.

---

## Runtime Spec

Python should operate on a small provider-neutral model.

```python
@dataclass
class MachineSpec:
    name: str
    kind: Literal["container", "vm"]
    provider: str
    cpus: int | None
    memory: str | None
    disk: str | None
    mounts: list[MountSpec]
    ports: list[PortSpec]
    nixos_flake: str
```

Avoid exposing arbitrary Incus configuration through the common model.

Provider-specific escape hatches may later be added under:

```nix
providerConfig.incus = { ... };
```

but should not be needed for normal use.

---

## Provider Interface

Use a normal Python protocol/ABC, not dynamic plugins.

```python
class Provider(Protocol):
    def inspect(self, spec: MachineSpec) -> MachineState: ...
    def create(self, spec: MachineSpec) -> None: ...
    def update(self, spec: MachineSpec, state: MachineState) -> None: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def destroy(self, name: str) -> None: ...
    def exec(self, name: str, argv: list[str]) -> int: ...
```

Initial implementation:

```text
providers/
    base.py
    incus.py
```

Do not build runtime plugin discovery yet.

A second provider can later validate whether this abstraction is sufficient.

---

## Incus Backend

Prefer invoking the `incus` CLI through `subprocess` initially instead of depending on an Incus Python SDK.

Responsibilities:

- launch NixOS image
- distinguish container vs VM
- configure limits
- configure proxy devices / networking
- configure disk devices for mounts
- start/stop/delete
- execute commands
- inspect instance state
- snapshots

Keep command construction isolated in `providers/incus.py`.

---

## NixOS Activation

Initial flow:

1. evaluate project machine configuration
2. ensure Incus instance exists
3. apply outer machine configuration
4. start instance
5. build the matching NixOS system
6. copy closure into the instance
7. activate it
8. run readiness checks

Prefer standard Nix mechanisms rather than reproducing provisioning logic in Python.

Possible implementation strategies:

```text
nix build
nix copy
nixos-rebuild switch
```

Exact transport can evolve later.

---

## CLI

Use `typer` or `click`. Prefer `typer` for the prototype.

Core commands:

```text
tool up [name]
tool down [name]
tool restart [name]
tool destroy [name]

tool shell [name]
tool exec [name] -- <command>

tool rebuild [name]
tool status [name]

tool snapshot [name] [snapshot-name]
tool restore [name] <snapshot-name>

tool config [name]
```

### `up`

- evaluate Nix
- create machine if missing
- reconcile outer machine settings
- start machine
- activate NixOS configuration

### `rebuild`

Only rebuild and activate NixOS state.

### `config`

Print the normalized JSON/runtime configuration for debugging.

---

## Reconciliation

Keep reconciliation deliberately small.

For each `up`:

```text
desired spec
    +
actual provider state
    ↓
small diff
    ↓
safe provider mutations
```

Classify changes:

- live-updateable
- requires restart
- requires recreation
- unsupported

Do not implement a generic Terraform-style planner.

For the first version, explicit recreation warnings are acceptable.

---

## State

Avoid a large persistent state database.

Store only local metadata that cannot be derived cheaply:

```text
.nixant/
    state.json
```

Possible contents:

- machine name
- provider
- instance identifier
- last applied config hash
- last NixOS closure
- tool schema version

Incus and Nix remain authoritative for actual machine and system state.

---

## Suggested Package Layout

```text
src/
  nixant/
    __main__.py
    cli.py
    config.py
    models.py
    planner.py
    state.py

    nix/
      eval.py
      build.py
      activate.py

    providers/
      base.py
      incus.py

    commands/
      up.py
      down.py
      shell.py
      rebuild.py
      status.py
      snapshot.py

tests/
  unit/
  integration/
```

---

## Implementation Phases

### Phase 1 — Minimal vertical slice

Implement:

- `nix eval --json`
- `MachineSpec`
- Incus container creation
- memory + CPU limits
- one workspace mount
- `up`
- `shell`
- `down`
- `destroy`

Use an existing NixOS Incus image.

### Phase 2 — NixOS provisioning

Add:

- build system closure
- copy/deploy into instance
- activate configuration
- `rebuild`
- configuration hash
- useful error reporting

### Phase 3 — Development ergonomics

Add:

- port mappings
- multiple mounts
- `exec`
- `status`
- restart/recreate detection
- project-specific machine names

### Phase 4 — Isolation features

Add:

- snapshots
- restore
- ephemeral instances
- agent-oriented isolated profiles
- read-only or restricted mounts

### Phase 5 — Backend validation

Add one second backend only when useful.

Use it to refine the provider interface rather than designing a generic provider system prematurely.

---

## Explicit Non-Goals

Do not initially implement:

- arbitrary guest operating systems
- shell/Chef/Ansible provisioners
- dynamic plugin loading
- remote cloud orchestration
- generic infrastructure planning
- full networking abstraction
- provider feature parity
- automatic abstraction of every Incus option
- a replacement for NixOS modules

The project should remain:

> a thin local machine lifecycle layer around NixOS, with Incus as the first provider.

---

## Prototype Technology Choices

- Python 3.12+
- Typer
- `dataclasses` or Pydantic for runtime models
- `subprocess` for `nix` and `incus`
- pytest
- Ruff
- mypy or pyright

Prefer standard library code where practical.

The first milestone should stay small enough that the complete lifecycle can be understood by reading the CLI, planner, and Incus provider modules.
