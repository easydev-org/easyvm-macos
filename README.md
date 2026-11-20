# easyvm-macos

Walkthroughs and CI for **macOS** guests, driven by
[`easydev-org/easyvm`](https://github.com/easydev-org/easyvm).

## What lives here

| Directory | Release |
|---|---|
| `15/` | macOS Sequoia 15 |
| `26/` | macOS Tahoe 26 |

Each holds `steps/`: the `.walk` files for `setup`, `ssh`, `vm-configure` and `dev`, plus the shell
scripts those stages run inside the guest and the reference images they compare against.

`macos.yml` and `macos26.yml` are two chains rather than one, because the two releases pull DIFFERENT
assets - a different recovery image and a different firmware, with different identities baked into it.

```sh
gh workflow run macos26.yml -f disks_repository=easydev-org/easyvm-macos -f from_tag=macos26
```

## What lives elsewhere, on purpose

The **engine** - the daemon, the qemu argument builder, the OpenCore assembly - is in
`easydev-org/easyvm`. The **CI machinery** is too: both workflows call
`easydev-org/easyvm/.github/workflows/stage.yml@main`.

Disks pull from and publish to this repository's own ghcr package, `easydev-org/easyvm-macos`.
