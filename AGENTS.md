# Angee messaging bridges

This repository owns Angee's opt-in external messaging bridge folders under
`addons/angee/*`. An addon is a source folder with an `addon.toml`, not a
separately distributed Python package. Keep protocol/vendor behavior, resources,
permissions, and web fragments with the bridge that owns them.

The supported layout is a stack workspace slot: this checkout sits at
`<stack>/workspaces/<ws>/angee-messaging-bridges` with `angee-django`,
`angee-base`, and `angee-react` as sibling slots, and the stack root above owns
the composed host — the `@angee/gql` fixture resolves from the stack root's
`runtime/` (regenerate with `manage.py angee build` + web codegen there).
Shared messaging behavior belongs in the base `angee.messaging` addon, not in
an individual bridge.

Before handing off changes, run `uv run pytest -q`, `pnpm -r typecheck`, and
`pnpm -r test` from the repository root.
