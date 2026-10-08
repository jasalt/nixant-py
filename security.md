# Security and isolation review

This document records the security review of malicious code running as a nixant
guest user. It describes current limitations and recommended hardening; the
recommendations below are **not implemented guarantees**.

## Security boundary

`nixant.isolation = "agent"` reduces guest privileges, but is not a secure sandbox
for malicious code with access to the writable host checkout. The most immediate
risk is that guest-written files are later consumed by trusted host tools, not a
speculative kernel escape.

The review assumes malicious code can run as the configured guest user and read
or write whatever that user can access. No guest root privilege is needed for the
shared-checkout attacks below. The host operator and Incus daemon are initially
trusted; the documented host operator has `incus-admin` authority.

For deliberately hostile workloads, prefer a VM with a separate disposable
workspace, host-owned security policy, restricted networking, and no credentials.
A VM alone does not fix shared-workspace or host-tool execution attacks.

## 1. Writable Git metadata can cause host execution

**Priority: high — address before relying on agent isolation.**

The default shifted, writable workspace exposes the checkout, including `.git`
in an ordinary clone. The guest user has the host UID and can modify repository
configuration and hooks.

The host-side `evaluation_preflight()` runs `git ls-files` using that repository's
configuration. A safe probe in a disposable repository configured
`core.fsmonitor` to invoke a harmless local callback. Calling the actual
pre-flight caused that callback to write a marker on the host.

Consequently, a malicious guest can arrange host-user execution during a later
`up`, `rebuild`, `config`, or automatic `name` proposal. This does not require a
kernel escape, guest sudo, or malicious Nix evaluation. Host-user execution is
particularly serious when that user has `incus-admin` authority.

**Relevant code:** [project.py](src/nixant/project.py), `evaluation_preflight()`;
[eval.py](src/nixant/nix/eval.py), `evaluate()`.

### Recommended mitigation

- Keep trusted host Git metadata outside guest-writable storage. Account for both
  ordinary clones and linked-worktree metadata; do not assume hiding one path
  protects every host consumer.
- Harden Git invocations, including Git invoked indirectly by Nix, against
  executable repository configuration.
- Prefer a separate guest workspace and explicitly reviewed exports into a
  trusted host checkout.
- Treat Git hooks, editor tasks, build scripts, and other host automation that
  consumes guest-written files as additional execution surfaces. Disabling one
  Git callback does not solve the general problem.

A regression test should use a harmless callback and verify that host pre-flight
cannot execute it from guest-controlled repository metadata.

## 2. Guest-editable configuration can expand access

**Priority: high — separate trusted policy from guest-written configuration.**

The guest can modify tracked `flake.nix` and NixOS module files through the
workspace. The next host `up` or `rebuild` consumes those changes as trusted
configuration. `up` also applies requested host integration changes.

Agent assertions check `nixant.user.sudo`, writable mount names, and declared
ports. They do not enforce the complete effective NixOS privilege configuration.
A safe Nix evaluation confirmed that all assertions can pass with:

- `nixant.isolation = "agent"` and `nixant.user.sudo = false`;
- wheel membership restored through `users.users.dev.extraGroups`;
- Nix trust restored through `nix.settings.trusted-users`;
- an `ALL`/`NOPASSWD` rule added through `security.sudo.extraRules`;
- a writable `workspace` mount with source `/` and target `/workspace`.

No host filesystem was mounted during this probe. The result establishes that
the configuration is accepted, not that an escape was executed.

The writable-mount restriction exempts the **name** `workspace`; it does not
confine that mount to the intended checkout. Python resolves any existing source
path without a trusted allowlist, and the runtime schema does not carry an
independent isolation policy. A subsequent `up` could therefore expose host data
accessible to the mapped user, including their home and credentials.

**Relevant code:** [options.nix](nix/modules/options.nix), agent assertions and
runtime; [common.nix](nix/modules/common.nix), guest privilege configuration;
[project.py](src/nixant/project.py), `resolve_mount_sources()`.

### Recommended mitigation

- Treat project Nix configuration as trusted control-plane code, or introduce a
  host-owned policy outside every guest-writable mount.
- Require explicit review/approval before consuming guest changes to environment
  configuration or relaxing policy.
- Enforce host-side mount allowlists with safe path resolution and attention to
  symlink replacement/races.
- Validate effective privileges, not just the convenience `nixant.user.sudo`
  option. Test ordinary NixOS overrides and broad workspace sources.

Assertions inside attacker-controlled Nix modules cannot themselves enforce a
security boundary against those modules. Effective-configuration checks are
useful defense-in-depth, not a substitute for trusted policy.

## 3. Guest root, nesting, and inherited Incus configuration

**Priority: high — enforce prerequisites and document residual risk.**

The default profile intentionally gives the guest user passwordless sudo.
Setting only `nixant.user.sudo = false` is not sufficient to remove root-equivalent
access: outside agent mode, the user remains a trusted Nix user.

Containers share the host kernel and are created with `security.nesting=true` to
support guest-side Nix sandboxed builds. Malicious code can exercise kernel and
namespace interfaces; kernel, Incus, or confinement vulnerabilities remain
possible attack paths. Agent mode reduces privileges but does not remove this
shared-kernel attack surface.

Creation inherits the default Incus profile without explicitly enforcing
`security.privileged=false` or auditing effective confinement settings and all
inherited devices. Reconciliation deliberately leaves unrelated/profile devices
alone. An inherited host disk, Unix socket, proxy, or weakened confinement setting
can therefore undermine the advertised restrictions.

**Relevant code:** [common.nix](nix/modules/common.nix), sudo and Nix trust;
[incus.py](src/nixant/providers/incus.py), `create()`;
[planner.py](src/nixant/planner.py), reconciliation scope.

### Recommended mitigation

- Fail closed for privileged containers or dangerous effective configuration in a
  restricted mode. Audit both creation and reuse, including profile inheritance.
- Reject inappropriate inherited mounts, control sockets, proxies, and raw
  confinement overrides, or require a dedicated operator-managed restricted
  profile.
- Keep the host kernel and Incus patched; recommend VMs for hostile workloads.
- Document that guest root is not automatically host root in an unprivileged
  container, but it increases the capabilities available to an attacker.

This review did not inspect or mutate the live daemon's profile and did not
attempt any kernel or Incus exploit.

## 4. Secret exposure and unrestricted networking

**Priority: medium — document clearly and provide host-enforced restrictions.**

The workspace exposes gitignored files such as `.env`, tokens, private fixtures,
and repository-local credentials. Git/Nix tracking rules do not hide files from
a bind mount.

Agent mode prohibits declared port-forwarding devices but does not restrict
outbound networking. Malicious code can exfiltrate readable secrets, establish
reverse connections, or access reachable host-bridge, LAN, and other guest
services. Direct access to services on the guest IP depends on the network,
service bindings, and firewall configuration; no published proxy does not mean
network isolation. A service bound only to host loopback is not automatically
reachable through the bridge.

Read-only mounts still expose their contents. They also do not neutralize a
reachable Unix control socket: filesystem read-only status does not make the
socket's protocol read-only.

### Recommended mitigation

- Keep secrets out of guest-visible workspaces and Nix closures.
- Do not expose host home/runtime directories or SSH, Docker, Incus, or desktop
  control sockets. Inspect extra and inherited mounts, not only the workspace.
- Offer operator-managed host/Incus egress and peer-isolation rules, accounting
  for both IPv4 and IPv6, or an offline configuration.
- Do not rely on a guest-controlled firewall to contain a compromised guest.
- Add negative networking tests in a disposable, isolated environment when such
  restrictions are supported.

## 5. Resource exhaustion can affect the host

**Priority: medium — harden limits and document remaining denial-of-service risk.**

Agent defaults provide two CPUs and 4 GiB of memory, but `disk` defaults to null.
There is no nixant-managed PID limit, workspace quota, or I/O/network budget.
Guest code can consume guest storage, host workspace storage, processes, and I/O,
subject to limits inherited from Incus/NixOS.

A quota on the guest root disk does **not** constrain the separate host-mounted
workspace. Host evaluation/build work triggered from guest-modified configuration
is also outside the guest cgroup limits.

**Relevant code:** [options.nix](nix/modules/options.nix), resource defaults;
[planner.py](src/nixant/planner.py), managed limits.

### Recommended mitigation

- Define trusted hard caps, including process limits and quota-capable guest
  storage.
- Put the writable workspace on separately quota-limited storage.
- Consider I/O limits and host-side evaluation/build budgets.
- Keep policy limits outside guest-editable configuration.
- Test exhaustion boundaries only in disposable, quota-limited environments and
  account for inherited limits rather than assuming the host is unlimited.

## 6. Destruction and snapshots do not sanitize the workspace

**Priority: medium — document prominently.**

Instance destruction, ephemeral deletion, and guest snapshot restoration do not
roll back files on host bind mounts. Malicious source/build scripts, Git metadata,
editor tasks, executables, and symlinks can survive and affect later host actions.
Host tools following guest-created symlinks can also access paths outside the
intended artifact directory.

Changing a previously compromised root-capable guest to agent mode is not
reliable remediation. Restoring an instance snapshot is not proof that either
the shared checkout or exposed credentials are safe.

### Recommended mitigation

- Use disposable guest workspaces and review exported changes in a trusted
  context; treat symlinks and executable/configuration files as untrusted input.
- Explicitly document that snapshots do not roll back bind-mounted host files.
- Recreate compromised instances from trusted configuration and rotate exposed
  credentials.
- Add a harmless marker test demonstrating that host workspace changes survive
  snapshot restore and instance destruction, so their scope remains explicit.

## Hardening order

1. Close guest-writable Git metadata paths to host execution.
2. Separate host security policy and trusted environment configuration from guest
   writes.
3. Enforce an audited Incus baseline and recommend VMs for hostile code.
4. Add network, storage, and process restrictions.
5. Document secret exposure, persistent workspace changes, and snapshot limits.

## Review evidence and limitations

The review was performed against revision `e0a1e7f`.

- 405 unit tests passed.
- A harmless temporary-repository probe confirmed host fsmonitor callback
  execution during evaluation pre-flight.
- A Nix evaluation confirmed accepted agent-mode privilege overrides and a broad
  writable workspace source. It did not build or deploy that configuration.
- Existing isolation tests cover baseline sudo/group/trust behavior and a
  read-only mount, not the adversarial transitions described here.
- No live escape attempts, host root mounts, network scans, or exfiltration tests
  were performed. No claim is made that kernel or hypervisor isolation was
  penetration-tested.

These are review findings and proposed mitigations, not evidence that any
particular deployed host has already been compromised.
