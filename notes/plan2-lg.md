# plan2-lg.md

## Goal

Implement a small **let-go** CLI for local NixOS development environments.

Use:

- **Incus system containers first**
- optional Incus VMs
- **Nix/NixOS as the provisioning model**
- Nix evaluation → normalized JSON → let-go data
- a small provider boundary so another VM/container backend can be added later
- a daemonless architecture similar in spirit to `lgcr`

Avoid turning the project into a general Vagrant replacement.

The CLI owns machine lifecycle and host integration. NixOS owns guest system state.

Reference implementations:

- let-go: https://github.com/nooga/let-go
- lgcr: https://github.com/nooga/lgcr

---

## Why let-go fits

let-go is suitable here because the program is mostly:

- process orchestration
- maps/vectors representing desired and actual state
- JSON parsing
- filesystem state
- command dispatch
- a small amount of reconciliation logic

This matches the style demonstrated by `lgcr`: keep most logic as pure Clojure data transformations and isolate host effects at the boundary.

The result can be bundled as a standalone executable:

```bash
lg -b nixant src/nixant/main.lg
```

No JVM should be required at runtime.

---

## High-Level Architecture

```text
flake.nix / devmachine.nix
        |
        v
   nix eval --json
        |
        | JSON
        v
     let-go CLI
        |
        +--> config normalization
        |
        +--> lifecycle planner
        |
        +--> provider protocol
        |       |
        |       +--> Incus provider
        |       +--> future providers
        |
        +--> Nix build/deploy/activate
        |
        +--> small local state file
```

### Responsibility split

**Nix**

- configuration language
- schema/defaults through Nix modules
- validation
- composition
- NixOS guest configuration
- reproducible system builds

**let-go CLI**

- execute `nix eval`
- parse evaluated JSON into maps
- inspect provider state
- calculate required lifecycle actions
- invoke Incus
- build/copy/activate NixOS
- expose developer-oriented commands

**Provider implementation**

- create
- inspect
- start
- stop
- destroy
- exec
- limits
- mounts
- ports
- snapshots

---

## Nix Configuration Model

Keep the machine definition and NixOS definition in the same project.

Example:

```nix
{
  devMachines.default = {
    provider = "incus";
    type = "container";

    machine = {
      memory = "4GiB";
      cpus = 4;

      mounts.workspace = {
        source = ./.;
        target = "/workspace";
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

Do **not** parse Nix syntax in let-go.

Evaluate a normalized output:

```bash
nix eval --json .#devMachines.default.runtimeConfig
```

Treat Nix as the configuration compiler and JSON as the CLI boundary.

---

## Runtime Data

Represent configuration with ordinary immutable maps and vectors.

Example normalized value:

```clojure
{:name "default"
 :provider "incus"
 :kind "container"
 :cpus 4
 :memory "4GiB"
 :mounts [{:name "workspace"
           :source "/home/user/project"
           :target "/workspace"
           :read-only? false}]
 :ports [{:host 8080
          :guest 80
          :protocol "tcp"}]
 :nixos-flake ".#devMachines.default.nixos"}
```

Prefer plain data over records unless records clearly improve protocol dispatch.

Validation functions should return normalized maps or fail with a clear message.

Example:

```clojure
(defn normalize-machine [m]
  (-> m
      normalize-kind
      normalize-memory
      normalize-mounts
      normalize-ports
      validate-machine))
```

---

## Provider Abstraction

Use a small Clojure protocol.

```clojure
(defprotocol Provider
  (inspect-machine [provider spec])
  (create-machine! [provider spec])
  (update-machine! [provider spec state])
  (start-machine! [provider name])
  (stop-machine! [provider name])
  (destroy-machine! [provider name])
  (exec-machine! [provider name argv]))
```

Initial provider:

```text
src/nixant/provider/incus.lg
```

Provider selection can initially be a simple map:

```clojure
(def providers
  {"incus" incus/provider})
```

Do not implement dynamic plugin loading yet.

Add a second provider only when a real use case exists; use that implementation to test whether the protocol is actually general enough.

---

## Process Execution

Centralize external command execution.

Use let-go's built-in OS facilities:

- `os/sh` for buffered machine-readable commands
- `os/exec*` for interactive commands such as `shell`
- lower-level `os/exec` only when custom process behavior is needed

Example boundary:

```clojure
(defn run-captured! [cmd & args]
  (let [{:keys [exit out err]} (apply os/sh cmd args)]
    (when-not (zero? exit)
      (throw (ex-info (str cmd " failed")
                      {:command cmd
                       :args args
                       :exit exit
                       :stderr err})))
    out))
```

Keep all shell/process behavior in one namespace.

Avoid `sh -c`; pass argv directly wherever possible.

For long-running or cancellation-sensitive operations, verify whether the higher-level process API provides sufficient signal/process-group behavior. If not, use the lower-level let-go/Go process primitives rather than hiding this behind shell scripts.

---

## Incus Provider

Prefer invoking the existing `incus` CLI rather than embedding Incus internals.

The provider should translate neutral machine data into Incus commands.

Responsibilities:

- launch NixOS image
- choose container or VM
- CPU limits
- memory limits
- disk limits where applicable
- mounts via Incus disk devices
- ports/proxy devices
- start/stop/delete
- inspect state
- exec
- snapshots

Example conceptual translation:

```clojure
{:kind "container"
 :memory "4GiB"
 :cpus 4}
```

becomes approximately:

```text
incus launch <image> <name>
incus config set <name> limits.memory 4GiB
incus config set <name> limits.cpu 4
```

Keep Incus command construction inside the provider namespace.

---

## Nix Integration

Use a dedicated namespace:

```text
src/nixant/nix.lg
```

Functions:

```clojure
(eval-machine project machine-name)
(build-system spec)
(copy-system! spec closure)
(activate-system! spec closure)
```

### Evaluation

```text
nix eval --json .#devMachines.<name>.runtimeConfig
```

Parse JSON:

```clojure
(json/read-json output)
```

### Activation flow

1. evaluate desired machine
2. inspect current Incus state
3. create/update outer machine
4. start instance
5. build NixOS closure
6. copy closure into instance
7. activate closure
8. verify readiness

Prefer standard Nix mechanisms:

```text
nix build
nix copy
nixos-rebuild switch
```

Do not reimplement package installation or service configuration in let-go.

---

## Reconciliation

Keep reconciliation as a pure data transformation.

```clojure
(defn plan [desired actual]
  ...)
```

Output a vector of actions:

```clojure
[{:op :set-memory
  :value "8GiB"
  :class :live}
 {:op :restart
  :class :restart}]
```

Possible classes:

```clojure
:live
:restart
:recreate
:unsupported
```

Execution is separate:

```clojure
(defn apply-plan! [provider spec actions]
  ...)
```

This separation is important:

```text
desired + actual
      |
      v
   pure plan
      |
      v
 effectful execution
```

Do not build a Terraform-style dependency graph.

---

## CLI

Follow the simple explicit dispatch style used by `lgcr`; a framework is unnecessary initially.

Commands:

```text
nixant up [name]
nixant down [name]
nixant restart [name]
nixant destroy [name]

nixant shell [name]
nixant exec [name] -- <command>

nixant rebuild [name]
nixant status [name]

nixant snapshot [name] [snapshot-name]
nixant restore [name] <snapshot-name>

nixant config [name]
```

Command dispatch can remain plain Clojure:

```clojure
(defn dispatch [args]
  (let [cmd (first args)
        tail (vec (rest args))]
    (case cmd
      "up"      (cmd-up tail)
      "down"    (cmd-down tail)
      "shell"   (cmd-shell tail)
      "rebuild" (cmd-rebuild tail)
      "status"  (cmd-status tail)
      "config"  (cmd-config tail)
      (print-help))))
```

### `up`

```text
evaluate
  → inspect
  → plan
  → create/reconcile
  → start
  → NixOS activate
```

### `rebuild`

Only rebuild and activate the inner NixOS configuration.

### `config`

Print the evaluated normalized runtime data, ideally as JSON or EDN.

---

## State

Keep the CLI mostly stateless.

Store only metadata that is not conveniently derivable:

```text
.nixant/
    state.json
```

Example:

```clojure
{:schema 1
 :machines
 {"default"
  {:provider "incus"
   :instance "project-default"
   :config-hash "..."
   :closure "/nix/store/..."}}}
```

Incus remains authoritative for machine runtime state.

Nix remains authoritative for system configuration.

Write state atomically:

```text
state.json.tmp
    ↓ rename
state.json
```

Do not add SQLite or a daemon.

---

## Suggested Source Layout

```text
src/
  nixant/
    main.lg
    cli.lg
    config.lg
    model.lg
    planner.lg
    state.lg
    process.lg
    nix.lg

    provider/
      core.lg
      incus.lg

test/
  config_test.lg
  planner_test.lg
  incus_test.lg
  state_test.lg
```

Keep effect-free parsing/planning code separate from subprocess/filesystem code.

A useful target is the `lgcr` pattern:

```text
pure helpers
    +
small effectful runtime boundary
```

---

## Error Handling

Use `ex-info` with structured data.

```clojure
(throw
  (ex-info "Incus create failed"
           {:operation :create
            :machine (:name spec)
            :provider :incus
            :stderr err}))
```

Top-level CLI catches failures, renders a concise error, and exits non-zero.

Preserve structured context internally rather than converting failures into strings too early.

---

## Testing

### Pure unit tests

Prioritize:

- Nix JSON → normalized machine map
- configuration validation
- mount normalization
- port normalization
- desired/actual diff
- change classification
- Incus argv construction
- state serialization

Command construction should be a pure function:

```clojure
(incus/create-argv spec)
```

so tests do not need Incus.

### Effect tests

Use a replaceable runner function:

```clojure
(def ^:dynamic *runner* process/run!)
```

Tests can bind a fake runner and capture calls.

### Integration tests

Opt-in tests against real Incus:

- create
- limits
- mount
- port forwarding
- start/stop
- exec
- destroy
- NixOS activation

---

## Implementation Phases

### Phase 1 — Minimal vertical slice

Implement:

- let-go executable
- manual CLI dispatcher
- Nix JSON evaluation
- configuration normalization
- Incus container create
- CPU/memory
- one workspace mount
- `up`
- `shell`
- `down`
- `destroy`

Use an existing NixOS Incus image.

### Phase 2 — NixOS activation

Add:

- `nix build`
- closure deployment
- system activation
- `rebuild`
- config hash
- structured errors

### Phase 3 — Development ergonomics

Add:

- port mappings
- multiple mounts
- `exec`
- `status`
- restart/recreate detection
- deterministic project instance names
- better help output

### Phase 4 — Isolation

Add:

- snapshots
- restore
- ephemeral instances
- autonomous-agent profile
- read-only/restricted mounts
- separate persistent and disposable storage policies

### Phase 5 — Backend validation

Implement one second backend only when genuinely useful.

Do not add a generic plugin architecture first.

Use the second backend to discover which pieces are truly provider-neutral.

---

## let-go-Specific Design Guidance

Lean into let-go rather than imitating Go classes.

Prefer:

```clojure
maps
vectors
keywords
pure functions
protocols only at effect boundaries
```

Avoid:

```text
large mutable object graphs
deep wrapper abstractions
runtime dependency injection frameworks
generic plugin registries
complex macro DSLs
```

Use `go`/`core.async` only where concurrency is actually valuable, for example concurrently inspecting several machines later.

The first implementation should be almost entirely synchronous.

This CLI does not need the low-level namespace/cgroup machinery demonstrated by `lgcr`; Incus already provides that. The useful lesson from `lgcr` is architectural: **small daemonless CLI, persistent data, pure transformations, narrow syscall/process boundary**.

---

## Explicit Non-Goals

Do not initially implement:

- arbitrary guest operating systems
- Chef/Ansible/shell provisioner plugins
- dynamic provider plugins
- OCI/container runtime functionality
- remote cloud orchestration
- generic infrastructure graphs
- full networking abstraction
- backend feature parity
- every Incus option
- another configuration language
- a NixOS module replacement

The project should remain:

> a small let-go lifecycle orchestrator around NixOS, with Incus as the first machine provider.

---

## Prototype Technology Choices

- let-go
- `.lg` source files
- built-in `json`
- built-in `os`
- `ex-info` for structured failures
- Clojure protocols for provider boundaries
- maps/vectors for runtime state
- let-go `test` namespace
- standalone bundle via `lg -b`

Avoid external dependencies unless they remove substantial implementation work.

The first milestone should stay small enough that the complete lifecycle can be understood as:

```text
CLI
 → Nix eval
 → pure planner
 → Incus commands
 → NixOS activation
```
