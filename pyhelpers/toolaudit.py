"""Which host executables do testlists need, and does this container have them.

Two jobs:
  * artifact_fallback(): testlists name tool trees as /testsrc/disk_artifacts/<name>.
    That directory is not in git, so a fresh container lacks it. Each
    disk_artifacts/<name>.json manifest records "dest", where the image installs
    the same tree (watcom-linux -> /opt/watcom); a missing path is rewritten to it.
  * audit_command()/audit_config(): list the executables a test_hostbuild /
    test_hostbashexec command invokes and where each resolves on this host, so a
    missing compiler shows up before a run instead of as "wcl: not found".

CLI: python3 toolaudit.py [--missing]   (audits every testlist the manager lists)
"""
import ast
import json
import os
import re
import shlex
import shutil
import sys

ARTIFACT_ROOT = "/testsrc/disk_artifacts"
HOST_STEPS = ("test_hostbuild", "test_hostbashexec")
_SHELL_WORDS = {
    "", "set", "export", "cd", "rm", "mkdir", "cp", "mv", "ls", "cat", "echo", "test", "[", "if", "then",
    "else", "elif", "fi", "for", "do", "done", "while", "case", "esac", "true", "false", "exit", "source",
    ".", "unset", "printf", "read", "eval", "exec", "bash", "sh", "env", "trap", "wait", "shift", "return",
}
# where the image installs each tree, for when disk_artifacts/<name>.json isn't there either
_DEFAULT_DEST = {"watcom-linux": "/opt/watcom"}
_ARTIFACT_RE = re.compile(re.escape(ARTIFACT_ROOT) + r"/([A-Za-z0-9_.+-]+)")


def artifact_fallback(command):
    """Rewrite disk_artifacts/<name> paths that don't exist here to the manifest's installed dest."""
    def sub(m):
        if os.path.exists(m.group(0)):
            return m.group(0)
        dest = _DEFAULT_DEST.get(m.group(1))
        try:
            with open(os.path.join(ARTIFACT_ROOT, m.group(1) + ".json")) as f:
                dest = json.load(f).get("dest") or dest
        except (OSError, ValueError):
            pass
        return dest if dest and os.path.exists(dest) else m.group(0)
    return _ARTIFACT_RE.sub(sub, command or "")


def audit_command(command, base_path=None):
    """[{tool, path}] for each command word in a shell string; path None = not found.
    Follows `export PATH=...` / `PATH=...` assignments so tools added to PATH inline resolve."""
    command = artifact_fallback(command)
    env = {"PATH": base_path if base_path is not None else os.environ.get("PATH", "")}

    def expand(v):
        return re.sub(r"\$\{?(\w+)\}?", lambda m: env.get(m.group(1), os.environ.get(m.group(1), m.group(0))), v)

    seen, out = set(), []
    for seg in re.split(r"\n|;|&&|\|\||\|", command):
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            continue
        if words and words[0] == "export":
            words.pop(0)
        assigned = False
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            name, _, val = words.pop(0).partition("=")
            env[name] = expand(val)
            assigned = True
        if assigned and not words:
            continue
        if not words or words[0] in _SHELL_WORDS:
            continue
        tool = words[0]
        if tool in seen or not re.match(r"^[A-Za-z0-9_./+][A-Za-z0-9_./+-]*$", tool):
            continue
        seen.add(tool)
        found = tool if os.path.isabs(tool) and os.access(tool, os.X_OK) else shutil.which(tool, path=env['PATH'])
        out.append({"tool": tool, "path": found})
    return out


def missing_tools(command, base_path=None):
    return [t["tool"] for t in audit_command(command, base_path) if not t["path"]
            and not os.path.exists(t["tool"]) and "/" not in t["tool"].lstrip("./")]


def audit_config(config):
    """[{step, tool, path}] for the host-build steps of one CONFIG; {tokens} are filled from top-level scalars."""
    scalars = {k: v for k, v in config.items() if isinstance(v, (str, int))}
    rows = []
    for i, step in enumerate(config.get("steps", [])):
        if step.get("action") not in HOST_STEPS:
            continue
        cmd = str((step.get("param") or {}).get("command") or "")
        for _ in range(3):
            cmd = re.sub(r"\{(\w+)\}", lambda m: str(scalars.get(m.group(1), m.group(0))), cmd)
        for t in audit_command(cmd):
            rows.append({"step": i + 1, **t})
    return rows


def config_from_file(path):
    """CONFIG literal from a testlist file, without importing it."""
    with open(path) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CONFIG" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def audit_tests(tests, root="/testsrc"):
    """tests = /api/v1/tests entries. Returns {tool: {path, missing, used_by: [test ids]}}."""
    tools = {}
    for t in tests:
        for mod in {v for v in (t.get("types") or {}).values()} or {t.get("module")}:
            if not mod:
                continue
            f = os.path.join(root, "sourcedir", mod.replace(".", "/") + ".py")
            try:
                cfg = config_from_file(f)
            except (OSError, SyntaxError, ValueError):
                continue
            for row in audit_config(cfg or {}):
                e = tools.setdefault(row["tool"], {"path": row["path"], "missing": not row["path"], "used_by": []})
                if t["id"] not in e["used_by"]:
                    e["used_by"].append(t["id"])
    return tools


if __name__ == "__main__":
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8080/api/v1/tests") as r:
        res = audit_tests(json.load(r)["data"]["tests"])
    only_missing = "--missing" in sys.argv
    for tool, e in sorted(res.items()):
        if only_missing and not e["missing"]:
            continue
        print(f"{'MISSING' if e['missing'] else 'ok     '} {tool:<16} {e['path'] or '-':<40} {len(e['used_by'])} tests")
