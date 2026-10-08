## Verdict

**Feasible, and a good match for nixant’s current architecture.** A single nixant project can already manage multiple named NixOS targets, each with its own container and workspace mount.

I would revise the earlier proposal in two important ways:

1. **Layer reusable modules, not successive template invocations.** Templates scaffold projects; they are not an inheritance or upgrade mechanism.
2. **Keep a small shared site declaration.** Removing `client.nix` outright would duplicate ports between NixOS and devenv, since these are separate evaluations.

The basic design needs little or no CLI work. The main work is packaging, integration testing, and defining state and repository boundaries.

## 1. What already works

The current implementation supports the necessary building blocks:

| Requirement | Current support |
|---|---|
| Multiple containers from one flake | Multiple `nixosConfigurations.<target>` |
| Independent lifecycle operations | `up`, `rebuild`, `shell`, `destroy`, etc. accept targets |
| Per-target ownership and operation locking | Already implemented |
| Mount only one site into each container | Override `nixant.mounts.workspace.source` |
| Relative site paths | Resolved relative to the enclosing nixant project root |
| Different host ports per site | `nixant.ports` |
| Read-only shared modules | Additional read-only mounts |
| List existing project instances | `nixant status` |

For example, each target can contain:

```nix
nixant = {
  instanceName = "wp-client-a";

  mounts.workspace = {
    source = "sites/client-a";  # String, not ./sites/client-a
    target = "/workspace";
  };

  ports = [
    { host = 8081; guest = 8081; }
    { host = 8025; guest = 8025; }
  ];
};
```

**The workspace override is essential.** Keeping the default `"."` source would expose the entire consolidated checkout—including sibling sites—to every container.

These details are supported by `nix/modules/options.nix` and `src/nixant/project.py`, rather than requiring new functionality.

## 2. Recommended structure

I would separate the design into three layers:

```text
nixant
  Container lifecycle, mounts, users, resources, port forwarding
    │
    └── generic devenv guest module
          Installs the devenv CLI and basic tools
            │
            └── WordPress workspace
                  Site declarations and reusable devenv WordPress module
```

A consolidated project could look like:

```text
wp-workspace/
├── flake.nix
├── flake.lock
├── nix/
│   └── devenv-guest.nix
├── shared/
│   └── wordpress-devenv/
└── sites/
    ├── client-a/
    │   ├── client.nix
    │   ├── devenv.nix
    │   ├── devenv.yaml
    │   ├── devenv.lock
    │   ├── site.nix
    │   ├── plugins/
    │   └── themes/
    └── client-b/
        └── ...
```

The root flake creates `client-a` and `client-b` targets using a small Nix helper. Each imports the same guest module but mounts only its own site.

Usage would be:

```sh
nixant up client-a
nixant up client-b

nixant shell client-a

# Or run application operations from the host:
nixant exec --target client-a -- devenv up -d
nixant exec --target client-a -- devenv tasks run site:setup

nixant status
```

There is no need to make nixant understand WordPress or devenv processes.

## 3. How template layering should work

The earlier suggestion could imply:

```sh
nixant init devenv
nixant init wordpress
```

That is **not how the current initializer works**. When `flake.nix` exists, `init` prints a merge snippet and writes nothing.

Instead:

- **`devenv` template:** complete minimal project using a reusable guest module.
- **`wordpress` or `wordpress-workspace` template:** complete project using that same guest module, plus WordPress declarations.
- **Reusable WordPress devenv module:** maintained separately from generated site files.

This makes the layering real at the module level, rather than relying on copying one template over another.

For maintainability, I would start with a generic template in nixant and a WordPress workspace example or external template. The sizable WordPress provisioning/task implementation need not become part of nixant itself.

Also, the correct existing syntax is **`nixant init devenv`**, not the `-t` form in my previous answer.

## 4. Retain a common source for ports

My earlier recommendation to remove `client.nix` was too strong.

The current WordPress arrangement already solves a useful problem:

- Host configuration needs instance identity and forwarded ports.
- devenv needs the public WordPress URL and service ports.
- Both can import the same plain Nix data.

Keep that pattern:

```nix
# sites/client-a/client.nix
{
  instance = "wp-client-a";
  ports = {
    http = 8081;
    mailpit = 8025;
  };
}
```

The root flake imports it to configure nixant; `site.nix` imports it to configure WordPress.

For the first version, preserving the existing **host port = guest port** convention is simplest. Separating public and internal ports is possible later, but the existing WordPress module uses `wordpress.url` as a Caddy virtual-host address, so it is not just a port-forwarding change.

Two caveats:

- nixant checks duplicate host ports **within a target**, not across all targets. A workspace helper should validate uniqueness across sites.
- The WordPress module uses devenv’s port allocation facilities. Externally forwarded services must stay on their declared ports; an automatically shifted Mailpit port would leave the Incus proxy pointing at the wrong listener. This needs a runtime test.

## 5. Repository consolidation is a separate decision

There are two viable arrangements.

### A. One repository containing all sites

**Simplest for shared configuration and evaluation.**

- One root flake imports site declarations directly.
- Shared WordPress module updates can affect all sites in one commit.
- Each container still mounts only its site.
- Sites lose some independent-clone convenience.

The shared module can be mounted read-only into each guest at a stable path. Site `devenv.nix` then needs an explicit import arrangement that works there; a relative import into a parent directory will not work if only the site directory is mounted.

### B. One orchestration repository, independent site repositories

**Closest to the existing workspace.**

- The orchestration repository tracks container configuration.
- Site repositories retain their own code, devenv declarations and locks.
- Mount sources can refer to local site checkouts without including them in the root flake source.

However, **a filesystem mount reference is not the same as a Nix import**: a root flake cannot simply import declarations from ignored nested repositories and expect Git-based flake evaluation to include them.

Options include keeping host declarations in the orchestration repository, using explicit inputs, or deliberately configuring submodule-aware sourcing.

There is another practical trap: making entire sites Git submodules often leaves their `.git` files pointing into the parent repository’s `.git/modules`. Mounting only the site into the guest can break Git and the WordPress submodule-check task. Avoid solving that by exposing all parent Git metadata to every guest.

**Recommendation:** use a monorepo for the simplest demonstration; preserve standalone site clones if independent repositories are a requirement. Do not make nested-site submodules the default without testing their guest-visible Git layout.

## 6. Runtime issues the template must address

### Guest-side builds remain necessary

nixant builds the NixOS system on the host. Installing `pkgs.devenv` does **not** prebuild each site’s PHP, MariaDB and shell environment.

Those are evaluated and realised by devenv inside each guest, with their own:

- Nix store usage;
- downloads and cache configuration;
- `devenv.lock`;
- first-start latency.

This is a valid trade-off, but should be documented. One outer `flake.lock` does not automatically consolidate every site’s devenv dependencies.

### State and snapshot semantics

The existing WordPress module stores WordPress and MariaDB state under devenv state, normally within the mounted checkout.

That means:

- container replacement can preserve site state;
- a container snapshot does **not** back up the externally mounted site state;
- running the same checkout’s devenv stack on both host and guest can cause state conflicts and stale absolute paths/store references.

The initial policy should be: **run a site’s devenv environment only inside its assigned guest**, and use explicit database dumps/state backups.

Moving state into the guest is possible, but changes persistence and the existing host-side production-import scripts.

### Service lifecycle

Keep these distinct:

- `nixant up`: start/configure the box;
- `devenv up`: start application services.

Detached devenv processes should not be assumed to restart after a container reboot. Automatic startup could later be an opt-in systemd integration, not an implicit property of the template.

### Privileges and networking

- Test devenv under `nixant.isolation = "agent"` without making the user a trusted Nix user merely to suppress cache warnings.
- Prefer high HTTP ports initially. The existing WordPress role explicitly adjusts a sysctl for unprivileged Caddy on port 80; installing devenv alone does not reproduce that.
- Keep `.test` DNS optional and host-managed.
- Use containers for this first version: nixant currently rejects forwarded ports for VM targets.
- Separate containers and mounts do not imply network isolation between clients.

## 7. Suggested scope

| Deliverable | Assessment |
|---|---|
| Generic devenv template | Small, straightforward addition |
| Two-site WordPress workspace example | Feasible with existing target/mount support |
| Shared WordPress module distribution | Requires an explicit pinning/import policy |
| Automatic app startup and health reporting | Separate enhancement |
| Bulk `up`/`down` or site-generation commands | Convenience features, not prerequisites |

Before calling it supported, I would validate a two-site example that demonstrates:

- both containers can run simultaneously;
- neither sees the other’s source;
- Git/submodule checks work inside each guest;
- both HTTP and Mailpit forwards work;
- state survives container recreation as documented;
- reboot behaviour is explicit;
- the intended agent-isolation profile can build and run the stack.

**Bottom line:** proceed with a generic devenv template and a two-target WordPress workspace proof of concept. Consolidate orchestration first; do not couple that to consolidating every repository, dependency lock, and application lifecycle at once.

This was a source review only—no files changed, and no runtime feasibility tests were performed.