# Angee messaging bridges

This opt-in source repository contains Angee's external messaging bridge addon
folders: Matrix, WhatsApp, Telegram, Discord, Signal, iMessage, Facebook, and
Meta export support. Each addon lives under `src/angee/<name>` with its
`pyproject.toml` package contract and any colocated web fragment.

Each addon is a Python distribution; the root `pyproject.toml` defines their
`angee-messaging-bridges` collection and uv workspace. The checkout is a source
slot, not a runnable host. It uses the consolidated sibling `angee/` checkout
(`src/` and `packages/`).
The stack root owns the JS installation; never run `pnpm install` in a source slot.

Run the checks from this directory:

```sh
uv run pytest -q
pnpm --config.verify-deps-before-run=false -r typecheck
pnpm --config.verify-deps-before-run=false -r test
```
