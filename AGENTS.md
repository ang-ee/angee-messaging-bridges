# Angee messaging bridges

This repository owns Angee's opt-in external messaging bridge folders under
`src/angee/*`. Each addon is a distribution with its own `pyproject.toml`;
the root file defines the collection and uv workspace. Keep protocol/vendor behavior, resources,
permissions, and web fragments with the bridge that owns them.

The supported layout is a stack workspace slot: this checkout sits at
`<stack>/workspaces/<ws>/angee-messaging-bridges` with the consolidated
`angee/` repository (`src/` and `packages/`) as a sibling slot. The stack root owns
the composed host — the `@angee/gql` fixture resolves from the stack root's
`runtime/`. Native composition and provisioning prepare the host after uv
dependency installation; the shared Vite integration generates web artifacts
for development and builds. See the framework's [checks](../angee/docs/checks.md)
for explicit CLI generation and verification.
Shared messaging behavior belongs in the base `angee.messaging` addon, not in
an individual bridge.

The stack root owns the JS installation; never run `pnpm install` in a source
slot. Before handing off changes, run `uv run pytest -q`,
`pnpm --config.verify-deps-before-run=false -r typecheck`, and
`pnpm --config.verify-deps-before-run=false -r test` from the repository root.
