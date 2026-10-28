I've read incus-spawn's (https://github.com/Sanne/incus-spawn) README, DESIGN.md and VISION.md from a fresh clone (HEAD `2187023`, 2026-10-09). Here's the comparison.

## How they differ

The project is now called **isx**. Both tools run agents in Incus system containers (or VMs), but they make opposite choices on the central question.

|                   | nixant                                                                | isx (incus-spawn)                                                                                                                  |
| ----------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| What it is        | A declarative NixOS guest per project, built from the project's flake | A workstation for agents: Fedora template images (YAML), with disposable branches made by copy-on-write cloning                    |
| Getting work out  | A live read-write bind mount of `/workspace` (`shift=true`)           | **No read-write project mount, deliberately.** Changes come out through `git fetch` from an `isx://` remote                        |
| Root in the guest | Passwordless sudo by default; `isolation = "agent"` removes it        | Passwordless sudo is always on: "the container is the security boundary", and no capabilities are dropped                          |
| Credentials       | Not handled. Whatever is in the workspace is exposed                  | A host-side TLS-intercepting proxy adds the real API and GitHub tokens on the way out. The container only holds placeholder values |
| Network           | Not restricted                                                        | Three modes: full, `--proxy-only`, `--airgap`                                                                                      |
| Reproducibility   | Strong: pinned flake, `nixant rebuild` reconciles the guest           | Template images built from definitions, with layered parent images                                                                 |
| Scope             | Small CLI, an extension modules API (WordPress)                       | A large product: TUI, MCP server, IDE remote access, GUI passthrough, per-agent accounts, packaging                                |

So nixant gives a reproducible environment and limits privileges inside the guest. isx gives up privilege limits inside the guest. It relies instead on keeping credentials out, isolating the filesystem and controlling egress. The holes that `security.md` flags for nixant are exactly what isx's design is built to close:
- §1 and §6, the guest-writable `.git/hooks`, `.envrc` and fsmonitor → isx's FAQ makes the same argument and concludes there should be no RW mount.
- §4, secrets and the network → isx has the proxy and network modes.

## Worth adopting, in order of value

1. **A git-fetch exit path as an alternative to the RW workspace.** For example, add an `isolation = "agent"` sub-mode (or a new mode) where the guest gets a clone, not a bind mount. The host then runs `git fetch` through a remote helper (`nixant exec -- git ...` over Incus exec, like `isx://`) and reviews an immutable commit. This removes the top item in nixant's hardening order and the time-of-check/time-of-use problem. It costs live editing on the host, which isx replaces with VS Code Remote-SSH and JetBrains Gateway.

2. **Overlay mounts for caches and shared inputs.** isx's `mode: overlay` gives the guest a writable view of a host directory, with writes going to a container-local layer. That fits nixant's rule that "only the workspace may be writable" better than forcing `readOnly = true` on mounts that tools want to write to.

3. **Network modes enforced by the host.** The airgap trick is solid and easy to copy: mask each NIC with a `type: none` device, because Incus can't remove a NIC that comes from a profile. That's the only way to make the profile's NIC really go away. **Don't copy their proxy-only mode as it is.** It uses iptables rules inside the container, and the agent has passwordless sudo there, so it can flush them. nixant's `security.md` already says not to rely on a firewall the guest controls. Incus network ACLs or nftables rules on the bridge, applied on the host, would do it properly. Agent mode makes even guest-side rules meaningful, since the agent has no root to undo them, but host-side is still better.

4. **Keeping credentials out of the guest with a proxy** is the most valuable idea, and also the biggest one. A full TLS-intercepting proxy is probably out of scope for nixant. A small version could work: keep secrets out of the guest, and document or offer a way to route the agent's API traffic through a host-side process that adds the token. At minimum, the agent-mode docs should say plainly that the API key the agent uses lives in the guest.

5. **Smaller ideas:**
   - Copy-on-write branching from a built, stopped base instance (`incus copy` on btrfs/zfs) for fast, disposable parallel agents. This fits well with `ephemeral`.
   - An `isx doctor`-style preflight check: idmapped-mount support, storage driver, and an audit of whether the inherited profile is privileged. That last check is §3's "fail closed" recommendation.
   - Confining project-local config. isx stops templates from a cloned repo from reaching outside it (host resources must be inside the project, and repos are always cloned from the network). That is the same trust problem as nixant's §2, "project Nix config is guest-writable control-plane code".

**What nixant already does better:** declarative, pinned NixOS guests that reconcile in place; a real privilege-reduced agent mode, whereas isx always grants root; and a lighter tool. isx's README itself says containers are for semi-trusted code and VMs are for hostile code. nixant's README makes the same point.

My recommendation is items 1 and 3 first (git-fetch exit path, host-side airgap and egress modes). They close nixant's two highest-priority documented gaps without taking on isx's size. If you want, I can file them as beads.
