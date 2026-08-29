# Angee messaging bridges

This opt-in source repository contains Angee's external messaging bridge addon
folders: Matrix, WhatsApp, Telegram, Discord, Signal, iMessage, Facebook, and
Meta export support. Each addon lives under `addons/angee/<name>` with its
`addon.toml` contract and any colocated web fragment.

The repository is a development environment, not a Python distribution. It
uses a sibling `angee` checkout — the framework monorepo (core, `addons/`,
`packages/`).

Run the checks from this directory:

```sh
uv run pytest -q
pnpm install
pnpm -r typecheck
pnpm -r test
```
