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

Run the checks from this directory. The bridge source tests use `tests.settings`
and a checkout-local SQLite database. Use the managed Python version pinned by
the sibling framework checkout to get the SQLite version its tests require.
The bridge lockfile must match the sibling's dependency manifests. The
`--locked` flag reports any mismatch.

```sh
uv run --locked --managed-python --python "$(cat ../angee/.python-version)" python -m pytest -q
pnpm --config.verify-deps-before-run=false -r typecheck
pnpm --config.verify-deps-before-run=false -r test
```

To check affected tests locally, use the bridge test environment with the
framework's locked check tools and a sibling framework checkout whose
`scripts/changed_path_selection.py` supports the `pytest` subcommand. Run from the
bridge source root and set `base_sha` and `checked_sha` to the commits you want to
compare. The checked commit must match `HEAD`.

```sh
python ../angee/scripts/changed_path_selection.py pytest \
  --root "$PWD" --base "$base_sha" --head "$checked_sha" -- -q
```

The runner uses the same pytest configuration and reports which paths it selected
or why it ran the full suite.
