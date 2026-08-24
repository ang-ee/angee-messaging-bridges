"""Guard bridge imports from crossing worker-only vendor boundaries."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_SPEC = importlib.util.find_spec("angee.base")
assert BASE_SPEC is not None and BASE_SPEC.origin is not None
ANGEE = Path(BASE_SPEC.origin).resolve().parents[1]
INTEGRATE_SPEC = importlib.util.find_spec("angee.integrate")
assert INTEGRATE_SPEC is not None and INTEGRATE_SPEC.origin is not None
BASE_ADDONS_ROOT = Path(INTEGRATE_SPEC.origin).resolve().parents[2]
BRIDGE_ADDONS_ROOT = ROOT / "addons"
BRIDGE_PACKAGES_ROOT = BRIDGE_ADDONS_ROOT / "angee"
SOURCE_ROOTS = (ANGEE.parent, BASE_ADDONS_ROOT, BRIDGE_ADDONS_ROOT)

# Derived from this repository's source tree so every new bridge is guarded.
_BRIDGE_PACKAGES = tuple(
    f"angee.{path.name}" for path in sorted(BRIDGE_PACKAGES_ROOT.iterdir()) if path.is_dir()
)


def _module_imports(path: Path) -> set[str]:
    """Return every dotted module name imported by one source file."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    module = _module_name(path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
                prefix = package[: len(package) - node.level + 1]
                imported = ".".join((*prefix, *(node.module or "").split(".")))
            else:
                imported = node.module or ""
            if imported:
                names.add(imported)
                names.update(f"{imported}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def _module_name(path: Path) -> str:
    """Return the importable module name for one repository-owned Python source."""

    for source_root in SOURCE_ROOTS:
        try:
            relative = path.relative_to(source_root).with_suffix("")
        except ValueError:
            continue
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        return ".".join(parts)
    raise ValueError(f"{path} is outside the repository's Python source roots")


def _module_path(module: str) -> Path | None:
    """Resolve one repository-owned module name to its Python source file."""

    parts = module.split(".")
    for source_root in SOURCE_ROOTS:
        base = source_root.joinpath(*parts)
        module_file = base.with_suffix(".py")
        if module_file.is_file():
            return module_file
        package_file = base / "__init__.py"
        if package_file.is_file():
            return package_file
    return None


def _import_closure(entry_modules: tuple[str, ...]) -> set[str]:
    """Walk every repository-owned ``angee.*`` import reachable from entries."""

    closure: set[str] = set()
    visited: set[str] = set()
    pending = list(entry_modules)
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        closure.add(module)
        path = _module_path(module)
        if path is None:
            continue
        for imported in _module_imports(path):
            closure.add(imported)
            if imported.startswith("angee.") and imported not in visited and _module_path(imported) is not None:
                pending.append(imported)
    return closure


def _tree_imports(root: Path) -> set[str]:
    """Return the union of imports across a package subtree."""

    names: set[str] = set()
    for path in root.rglob("*.py"):
        names |= _module_imports(path)
    return names


def test_framework_base_does_not_import_bridge_addons() -> None:
    """The framework model layer stays below every optional messaging bridge."""

    imports = _tree_imports(ANGEE / "base")
    assert not any(name.startswith(prefix) for name in imports for prefix in _BRIDGE_PACKAGES)


def test_live_console_import_path_stays_vendor_free() -> None:
    """The full console import closure excludes worker-only and vendor libraries."""

    forbidden = ("discord", "mautrix", "neonize", "olm", "telethon", "qrcode", "PIL", "Pillow")
    console_entries = (
        "angee.integrate.live",
        "angee.integrate.impl",
        "angee.integrate.tasks",
        "angee.messaging.backends",
        "angee.messaging_integrate_whatsapp.client",
        "angee.messaging_integrate_whatsapp.backend",
        "angee.messaging_integrate_whatsapp.connect",
        "angee.messaging_integrate_whatsapp.schema",
        "angee.messaging_integrate_telegram.backend",
        "angee.messaging_integrate_telegram.identity",
        "angee.messaging_integrate_telegram.connect",
        "angee.messaging_integrate_telegram.schema",
        "angee.messaging_integrate_telegram.autoconfig",
        "angee.messaging_integrate_signal.backend",
        "angee.messaging_integrate_signal.identity",
        "angee.messaging_integrate_signal.connect",
        "angee.messaging_integrate_signal.schema",
        "angee.messaging_integrate_signal.autoconfig",
        "angee.messaging_integrate_matrix.backend",
        "angee.messaging_integrate_matrix.identity",
        "angee.messaging_integrate_matrix.connect",
        "angee.messaging_integrate_matrix.schema",
        "angee.messaging_integrate_matrix.autoconfig",
        "angee.messaging_integrate_discord.backend",
        "angee.messaging_integrate_discord.identity",
        "angee.messaging_integrate_discord.connect",
        "angee.messaging_integrate_discord.schema",
        "angee.messaging_integrate_discord.autoconfig",
        "angee.messaging_integrate_slack.backend",
        "angee.messaging_integrate_slack.identity",
        "angee.messaging_integrate_slack.connect",
        "angee.messaging_integrate_slack.schema",
        "angee.messaging_integrate_slack.autoconfig",
    )
    closure = _import_closure(console_entries)
    assert not any(name == prefix or name.startswith(f"{prefix}.") for name in closure for prefix in forbidden)
    assert "angee.integrate.session" not in closure
    assert "angee.messaging.session" not in closure
    assert "angee.messaging_integrate_signal.session" not in closure
    assert "angee.messaging_integrate_matrix.session" not in closure
    assert "angee.messaging_integrate_discord.session" not in closure
