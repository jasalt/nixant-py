A **normal full-VM Linux VPS without nested KVM** should work well: run Incus system containers on it. Containers use the VPS’s kernel, so they do not need `/dev/kvm`.

The distinction matters: a KVM-backed VPS with nested virtualization disabled is fine; an OpenVZ/LXC-based VPS may prohibit the namespaces, mounts, or cgroups Incus needs.

## Minimal setup

```text
Your laptop                         VPS
┌─────────────────┐                ┌──────────────────────────┐
│ nixant container│── transfer ────▶│ Incus                    │
│ + workspace     │                │  └─ copied NixOS container│
└─────────────────┘                │      ├─ workspace        │
                                   │      ├─ database/state   │
Teammate ───── SSH :2222 ──────────▶│      └─ sshd             │
                                   └──────────────────────────┘
```

### VPS host

- A supported Linux distribution with a modern kernel.
- Root access to install and initialize Incus.
- Incus bridge networking and persistent storage.
- Enough disk for the container, checkout, and temporary transfer artifacts.
- Firewall allowing administrative SSH and the guest SSH endpoint.
- Same architecture as the source—currently x86_64 for nixant.

**Nix on the VPS is optional for merely running the imported container.** Install Nix and nixant if the VPS should also rebuild and manage it through nixant.

A basic Incus `dir` storage pool is sufficient for a prototype, although nixant’s explicit disk quota setting needs a quota-capable pool.

## Preparing the guest

Enable OpenSSH declaratively before exporting:

```nix
services.openssh = {
  enable = true;
  settings = {
    PasswordAuthentication = false;
    KbdInteractiveAuthentication = false;
    PermitRootLogin = "no";
    AllowAgentForwarding = false;
  };
};

users.users.dev.openssh.authorizedKeys.keys = [
  "ssh-ed25519 AAAA... teammate"
];
```

Use the actual nixant guest username instead of `dev`.

For a full handoff, letting the teammate use the existing guest account is simplest: they get the current home directory, tools, and permissions. Decide explicitly whether that account should retain passwordless sudo.

**Their SSH access does not require any Incus or VPS administrative privileges.**

## Copying the full state

There are two separate things to transfer:

1. **Container filesystem:** home directories, `/var/lib` databases, installed Nix store paths, system configuration, etc.
2. **Host-mounted data:** especially the checkout mounted at `/workspace`.

Incus export/import handles the first, not the second.

For a first implementation, I would use a stopped transfer:

- Stop the container cleanly.
- Stop any host processes writing to the checkout.
- Export it using Incus’s portable backup format.
- Archive the checkout separately, preserving permissions and relevant metadata.
- Transfer both over SSH.
- Import the container on the VPS.
- Restore the checkout at a VPS-local path and replace its workspace disk device.
- Remove Wayland/GPU devices and review all other host-dependent mounts and proxy devices.
- Start and verify the guest.

Avoid storage-optimized exports initially: the default portable format is more suitable across different storage backends. Explicitly reject ephemeral containers before stopping them.

### Where should the checkout live remotely?

**Option A: VPS-host directory mounted into the guest — recommended initially**

```text
/srv/nixant/shop → /workspace
```

This matches nixant’s existing design and supports later rebuilds on the VPS. The directory ownership and shifted mount must be configured correctly.

**Option B: Put the checkout inside the container**

This makes future container backups genuinely self-contained. However, the workspace mount must be removed and disabled in the configuration, and the checkout must be copied into the guest filesystem—not accidentally into the mounted host directory.

This is attractive for a standalone handoff, but departs from nixant’s current host-checkout workflow.

## Exposing SSH

For one container, a dedicated port on the VPS is enough. For example, an Incus TCP proxy device can forward:

```text
VPS_PUBLIC_IP:2222 → container:22
```

An illustrative host command:

```bash
incus config device add shop guest-ssh proxy \
  listen=tcp:0.0.0.0:2222 \
  connect=tcp:127.0.0.1:22
```

Then:

```bash
ssh -p 2222 dev@vps.example.com
```

Keep the VPS’s administrative SSH on its own port/account. Restrict port 2222 to teammate source addresses where practical; alternatively expose guest SSH only through a private VPN.

Verify the guest SSH host-key fingerprint through a trusted channel. An exact move can preserve the original key; an independent clone should normally get new host keys.

## Main caveats

- **No running process transfer:** filesystem state survives, but shells, agents, and in-memory work restart.
- **The container shares the VPS kernel:** suitable for trusted teammates, not equivalent to a VM boundary against hostile users.
- **Full state includes secrets:** API tokens, Git credentials, shell history, `.env` files, and database contents may all travel.
- **Imported Incus profiles and devices need review:** source storage pools, networks, and absolute paths may not exist remotely.
- **Keep the source stopped initially:** two copies running background jobs or using the same external credentials can cause problems.

**For this use case, nixant mainly needs a reliable “export container + workspace, then import and remap” workflow.** No web platform, KVM, public Incus API, or always-running nixant service is necessary.