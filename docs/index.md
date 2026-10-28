# nixant

NixOS development environments on local [Incus](https://linuxcontainers.org/incus/).

Describe an environment as a `nixosConfiguration` in your project's flake. nixant builds it on the host, transfers the closure into an Incus container, activates it, and provides a shell inside. Your project checkout is mounted into the guest, so host editors and tools keep working.

## Getting started

Install nixant from its flake:

```console
nix run github:jasalt/nixant-py -- init
```

Then, from the generated project directory:

```console
nixant doctor
nixant up
nixant shell
```

See the [README](../README.md) for host requirements, project setup, templates, commands, and limitations.

## How it works

- Nix evaluates and builds the guest NixOS configuration on the host.
- Incus runs an isolated system container (VM support is also available).
- nixant transfers and activates the system closure, mounts the workspace, and forwards configured ports.
- Your project's flake defines the environment; nixant does not modify Incus server configuration.
