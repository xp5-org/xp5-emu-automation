"""Steps of a distributed test group member (manager side: /testrunnerapp/helpers/grouphelper.py).

A group testlist is started with POST /api/v1/groups; every member runs a copy of the SAME steps on its own host with {group_member}
(A, B, ...) and the other {group_*} tokens set. Typical shape:
    setup steps ... (component up)
    test_group_ready      -> "this member is up"
    test_group_wait_go    -> blocks until EVERY member is ready, then all return together (peers_file / peers_env hold the peers' data)
    main phase ...
    test_group_barrier name=done   -> nobody tears down while a peer is still testing
    teardown steps (flagged teardown: True; no barriers in teardown)
A member failing (any failed step) aborts the group: peers blocked in a wait step fail at once and run their teardown.
"""
import json
import os
import re
import socket
import time

from dispatch_common import dispatchtest_step, build_vars, resolve_path, as_bool

POLL = 5            # s per server-side long poll: also how soon a local stop is noticed
MANAGER_DOWN = 60   # s the manager may be unreachable before a wait step gives up


def _group(kwargs):
    config = kwargs.get("config") or {}
    grp = config.get("_group")
    if not grp:
        return None, ("not a group member: start this testlist with POST /api/v1/groups "
                      "(a plain run has no group)"), config
    return grp, None, config


def _data(value, vars_map):
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"data must be a JSON object, got {value!r}")
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("data must be an object")
    return {str(k): resolve_path(v, vars_map) for k, v in value.items()}


def _write_peers(reply, grp, peers_file, peers_env):
    peers = reply.get("peers") or {}
    if peers_file:
        os.makedirs(os.path.dirname(os.path.abspath(peers_file)), exist_ok=True)
        with open(peers_file, "w", encoding="utf-8") as f:
            json.dump(peers, f, indent=1)
    if peers_env:
        os.makedirs(os.path.dirname(os.path.abspath(peers_env)), exist_ok=True)
        with open(peers_env, "w", encoding="utf-8", newline="\n") as f:
            f.write(f'GROUP_ID="{grp["id"]}"\nGROUP_MEMBER="{grp["member"]}"\nGROUP_NAMES="{",".join(grp["names"])}"\n')
            for name, p in peers.items():
                tag = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
                f.write(f'PEER_{tag}_HOST="{p["host"]}"\n')
                for k, v in (p.get("data") or {}).items():
                    f.write(f'PEER_{tag}_{re.sub(r"[^A-Za-z0-9]", "_", k).upper()}="{v}"\n')


def _wait(grp, barrier, timeout, context):
    """(reply | None, error): block until `barrier` releases. Error: group aborted, timeout, local stop, manager unreachable too long."""
    import grouphelper
    deadline = time.monotonic() + float(timeout)
    down_since = None
    while True:
        if context is not None and context.get("abort") is True:
            return None, "run stopped while waiting"
        left = deadline - time.monotonic()
        if left <= 0:
            return None, f"timeout: barrier {barrier!r} not released within {timeout}s"
        try:
            r = grouphelper.call("poll", gid=grp["id"], member=grp["member"], barrier=barrier,
                                 wait=min(POLL, left))
            down_since = None
        except OSError as e:
            down_since = down_since or time.monotonic()
            if time.monotonic() - down_since > MANAGER_DOWN:
                return None, f"manager unreachable for {MANAGER_DOWN}s: {e}"
            time.sleep(min(2, max(left, 0)))
            continue
        except (KeyError, ValueError) as e:
            return None, str(e)
        if r["state"] == "released":
            return r, None
        if r["state"] == "aborted":
            return None, f"group aborted: {(r.get('abort') or {}).get('reason')}"


def _arrive(grp, barrier, data, ok=True, error=None):
    import grouphelper
    last = None
    for _ in range(5):      # a blip must not fail the member; the call is idempotent
        try:
            return grouphelper.call("arrive", gid=grp["id"], member=grp["member"], barrier=barrier,
                                    ok=ok, data=data, error=error), None
        except OSError as e:
            last = e
            time.sleep(2)
        except (KeyError, ValueError) as e:
            return None, str(e)
    return None, f"manager unreachable: {last}"


@dispatchtest_step
def test_group_ready(data=None, data_file=None, **kwargs):
    """Report this member READY to its test group (barrier "ready"); returns at once. Publishes {hostname, ip} plus `data` (JSON object, {tokens} resolved) and the JSON object in `data_file` (written by an earlier step: values only known at run time) to the peers."""
    grp, why, config = _group(kwargs)
    if why:
        return False, why
    import clusterhelper
    try:
        pub = {"hostname": socket.gethostname(), "ip": clusterhelper._lan_ip(), **_data(data, build_vars(config))}
        if data_file:
            with open(resolve_path(data_file, build_vars(config))) as f:
                extra = json.load(f)
            if not isinstance(extra, dict):
                raise ValueError("data_file must hold a JSON object")
            pub.update(extra)
    except (ValueError, OSError) as e:
        return False, f"test_group_ready: {e}"
    r, why = _arrive(grp, "ready", pub)
    if why:
        return False, f"test_group_ready: {why}"
    if r["state"] == "aborted":
        return False, f"group aborted: {(r.get('abort') or {}).get('reason')}"
    return True, (f"member {grp['member']} ready; waiting for {', '.join(r['waiting_for']) or 'nobody (all ready)'}")


@dispatchtest_step
def test_group_wait_go(timeout=300, peers_file=None, peers_env=None, **kwargs):
    """Block until EVERY member of the group is ready (the go signal), or the group aborts / `timeout` s pass (step fails; teardown runs). Writes the peers' published data to peers_file (JSON {member: {host, data}}) and/or peers_env (shell PEER_<M>_IP=...)."""
    grp, why, config = _group(kwargs)
    if why:
        return False, why
    vm = build_vars(config)
    r, why = _wait(grp, "ready", timeout, kwargs.get("context"))
    if why:
        return False, f"test_group_wait_go: {why}"
    _write_peers(r, grp, resolve_path(peers_file, vm) or None, resolve_path(peers_env, vm) or None)
    return True, f"go: all {len(grp['names'])} members ready ({', '.join(grp['names'])}); peers {json.dumps(r['peers'])}"


@dispatchtest_step
def test_group_barrier(name="sync", timeout=300, data=None, peers_file=None, peers_env=None, **kwargs):
    """Named sync point: arrive at barrier `name` and block until every member arrived (e.g. name=done before teardown). Not for teardown steps."""
    grp, why, config = _group(kwargs)
    if why:
        return False, why
    vm = build_vars(config)
    name = resolve_path(name, vm)
    try:
        pub = _data(data, vm)
    except ValueError as e:
        return False, f"test_group_barrier: {e}"
    r, why = _arrive(grp, name, pub)
    if why:
        return False, f"test_group_barrier {name}: {why}"
    if r["state"] != "released":
        r, why = _wait(grp, name, timeout, kwargs.get("context"))
        if why:
            return False, f"test_group_barrier {name}: {why}"
    _write_peers(r, grp, resolve_path(peers_file, vm) or None, resolve_path(peers_env, vm) or None)
    return True, f"barrier {name!r} released: {', '.join(grp['names'])} all arrived"


@dispatchtest_step
def test_group_fail(reason="member failed", **kwargs):
    """Tell the group this member failed (aborts the group at once, without waiting for this run to finish). The step itself passes: put a failing step after it, or use it from a step you know has failed."""
    grp, why, config = _group(kwargs)
    if why:
        return False, why
    reason = resolve_path(reason, build_vars(config))
    r, why = _arrive(grp, "ready", {}, ok=False, error=reason)
    return (False, f"test_group_fail: {why}") if why else (True, f"group told: member {grp['member']} failed: {reason}")
