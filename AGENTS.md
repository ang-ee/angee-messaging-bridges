# Angee messaging bridges

This repository owns Angee's opt-in external messaging bridge folders under
`addons/angee/*`. An addon is a source folder with an `addon.toml`, not a
separately distributed Python package. Keep protocol/vendor behavior, resources,
permissions, and web fragments with the bridge that owns them.

The supported sibling layout places `angee-django`, `angee-base`, and
`angee-react` beside this checkout. Shared messaging behavior belongs in the
base `angee.messaging` addon, not in an individual bridge.

Before handing off changes, run `uv run pytest -q`, `pnpm -r typecheck`, and
`pnpm -r test` from the repository root.
