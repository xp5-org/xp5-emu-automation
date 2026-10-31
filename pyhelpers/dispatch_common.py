"""Primitives shared by every dispatch module.

The runner (helpers/dispatchhelper.py) loads ONE file, dispatch_functions.py,
and exposes every public function in its namespace as a step. That file
star-imports the per-backend dispatch_*.py modules, so the backends can live in
separate files; they import the decorator and resolvers from here (never from
dispatch_functions, which would load a second copy of it).
"""
import re
import socket


# only decorated defs are steps
def dispatchtest_step(func):
    func._is_teststep = True
    return func


# instance classes test_terminate_all should stop in addition to its built-in
# ones; a backend module appends its own at import
EXTRA_TERMINABLE = []


def register_terminable(*classes):
    EXTRA_TERMINABLE.extend(c for c in classes if c not in EXTRA_TERMINABLE)


def as_bool(value, default=False):
    """Parse a testlist yes/no param. Steps get params as strings, so plain truthiness reads "False" as True; parse the word instead."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "0", "false", "no", "none", "null", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    return default


def build_vars(config):
    """Flat {token} lookup for a CONFIG: top-level config + leaves of structure.project + projbasedir/projdir/src_dir aliases."""
    project_cfg = config.get("structure", {}).get("project", {})
    vars_map = dict(config)

    # scalar leaves of structure.project (e.g. floppy1_path, config_path)
    for k, v in project_cfg.items():
        if isinstance(v, (str, int)):
            vars_map.setdefault(k, v)

    src_node = project_cfg.get("sourcecode_dir", "")
    if isinstance(src_node, dict):
        src_node = src_node.get("_rel", "")
    vars_map.setdefault("src_dir", src_node)
    vars_map.setdefault("projbasedir", config.get("projbasedir", ""))
    vars_map.setdefault("projdir", config.get("projdir", ""))
    # this host's path-safe name: .../vmrun/<test>/{host_id}/<vm_id> keeps hosts from clobbering each other
    vars_map.setdefault("host_id", re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname()) or "host")
    return vars_map


def resolve_path(val, vars_map):
    """Iteratively resolve {tokens} in val against vars_map."""
    if not isinstance(val, str):
        return val
    atomic = {k: v for k, v in vars_map.items() if isinstance(v, (str, int))}
    resolved = val
    while "{" in resolved:
        try:
            previous = resolved
            resolved = resolved.format(**atomic)
            if previous == resolved:
                break
        except (KeyError, ValueError, IndexError):
            break
    return resolved
