# plan2-go.md

## Goal

Implement a small Go CLI for local NixOS development environments.

The tool should:

- use **Incus system containers first**
- optionally support Incus VMs
- use **Nix/NixOS as the provisioning model**
- evaluate Nix configuration into normalized JSON for the Go CLI
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
      Go CLI
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

**Go CLI**

- evaluate Nix
- decode normalized JSON
- create/update/delete instances
- configure host-side resources
- build/copy/activate NixOS closure
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

Do not parse Nix in Go.

Evaluate a dedicated flake output into JSON:

```bash
nix eval --json .#devMachines.default.runtimeConfig
```

The Nix module layer should normalize values and reject invalid configuration before Go sees it.

---

## Runtime Spec

Go should operate on a small provider-neutral model.

```go
type MachineSpec struct {
    Name      string      `json:"name"`
    Kind      MachineKind `json:"kind"`
    Provider  string      `json:"provider"`
    CPUs      *int        `json:"cpus,omitempty"`
    Memory    string      `json:"memory,omitempty"`
    Disk      string      `json:"disk,omitempty"`
    Mounts    []MountSpec `json:"mounts,omitempty"`
    Ports     []PortSpec  `json:"ports,omitempty"`
    NixOSFlake string     `json:"nixosFlake"`
}
```

Prefer explicit enums/constants for machine kind and change classification.

Avoid exposing arbitrary Incus configuration through the common model.

Provider-specific escape hatches may later be added under:

```nix
providerConfig.incus = { ... };
```

but should not be needed for normal use.

---

## Provider Interface

Use a small compiled-in Go interface, not dynamic plugins.

```go
type Provider interface {
    Inspect(ctx context.Context, spec MachineSpec) (MachineState, error)
    Create(ctx context.Context, spec MachineSpec) error
    Update(ctx context.Context, spec MachineSpec, state MachineState) error
    Start(ctx context.Context, name string) error
    Stop(ctx context.Context, name string) error
    Destroy(ctx context.Context, name string) error
    Exec(ctx context.Context, name string, argv []string) error
}
```

Initial implementation:

```text
internal/provider/
    provider.go
    incus/
        incus.go
        commands.go
```

Do not build runtime plugin discovery initially.

A second provider can later validate whether this abstraction is sufficient.

---

## Incus Backend

Prefer invoking the `incus` CLI through `os/exec` initially instead of binding tightly to an Incus client library.

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

Keep command construction isolated in the Incus provider package.

Use `exec.CommandContext` so cancellation and timeouts propagate cleanly.

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

Prefer standard Nix mechanisms rather than reproducing provisioning logic in Go.

Possible implementation strategies:

```text
nix build
nix copy
nixos-rebuild switch
```

Exact transport can evolve later.

Keep Nix command execution behind a small package:

```text
internal/nix/
    eval.go
    build.go
    activate.go
```

---

## CLI

Use Cobra for the CLI.

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

Print normalized runtime configuration for debugging.

---

## Reconciliation

Keep reconciliation deliberately small.

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

```go
type ChangeKind int

const (
    ChangeLive ChangeKind = iota
    ChangeRestart
    ChangeRecreate
    ChangeUnsupported
)
```

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

Use standard `encoding/json`; avoid introducing a database.

---

## Suggested Package Layout

```text
cmd/
  nixant/
    main.go

internal/
  cli/
    root.go
    up.go
    down.go
    shell.go
    rebuild.go
    status.go
    snapshot.go

  config/
    config.go

  model/
    machine.go
    state.go

  planner/
    planner.go

  state/
    state.go

  nix/
    eval.go
    build.go
    activate.go

  provider/
    provider.go
    incus/
      incus.go
      commands.go
      inspect.go

test/
```

Keep `main.go` minimal.

Most logic should live in `internal/` packages and be callable independently of Cobra.

---

## Command Execution Layer

Create one thin wrapper for external processes.

```go
type Runner interface {
    Run(ctx context.Context, name string, args ...string) ([]byte, error)
}
```

Use this for both `nix` and `incus`.

Benefits:

- easy unit testing
- consistent stderr/error handling
- structured logging
- cancellation support
- no shell quoting problems

Do not use `sh -c` except where unavoidable.

---

## Error Model

Wrap errors with operation context:

```go
return fmt.Errorf("create incus instance %q: %w", spec.Name, err)
```

Errors shown to users should include:

- failed operation
- relevant machine/provider
- external command
- useful stderr

Avoid deep custom error hierarchies initially.

---

## Implementation Phases

### Phase 1 — Minimal vertical slice

Implement:

- Cobra CLI
- `nix eval --json`
- JSON decode into `MachineSpec`
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
- shell completion

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

## Testing Strategy

### Unit tests

Test:

- JSON config decoding
- validation
- planner/change classification
- Incus command construction
- state serialization
- Nix command construction

Use a fake `Runner` rather than executing subprocesses.

### Integration tests

Run against a real Incus installation for:

- create
- mount
- limits
- start/stop
- exec
- delete
- NixOS activation

Keep integration tests explicitly opt-in.

---

## Explicit Non-Goals

Do not initially implement:

- arbitrary guest operating systems
- shell/Chef/Ansible provisioners
- dynamic plugin loading
- HashiCorp go-plugin
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

- Go 1.25+
- Cobra
- standard `encoding/json`
- `os/exec` / `exec.CommandContext`
- standard `context`
- standard `slog`
- Go test
- `golangci-lint` or a small selected lint set

Prefer the standard library over framework dependencies.

The first milestone should stay small enough that the complete lifecycle can be understood by reading:

```text
CLI → planner → Incus provider → Nix activation
```

A single static binary is a major advantage over the Python prototype for distributing the tool across development hosts.
