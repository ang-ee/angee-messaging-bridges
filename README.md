# Angee messaging bridges

This opt-in source repository contains Angee's external messaging bridge addon
folders: Matrix, WhatsApp, Telegram, Discord, Signal, iMessage, Facebook, and
Meta export support. Each addon lives under `addons/angee/<name>` with its
`addon.toml` contract and any colocated web fragment.

The repository is a development environment, not a Python distribution. It
uses the consolidated sibling `angee/` checkout. The stack root owns the JS
installation; never run `pnpm install` in a source slot.

Run the checks from this directory:

```sh
uv run pytest -q
pnpm --config.verify-deps-before-run=false -r typecheck
pnpm --config.verify-deps-before-run=false -r test
```
