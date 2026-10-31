"""Apple II (linapple) steps.

Loaded through dispatch_functions (star import); shared primitives are in dispatch_common.
"""
import os
import time
from dispatch_common import dispatchtest_step, resolve_path as _common_resolve_path, as_bool, register_terminable
from applehelpers import AppleInstance

register_terminable(AppleInstance)


def _resolve_path(path, config):
    """Same call shape as the old per-project resolver: '' for an empty path, CONFIG scalars as the token map."""
    if not path:
        return ""
    return _common_resolve_path(path, config)


@dispatchtest_step
def test_apple_start(name=None, disk1_path=None, disk2_path=None,
                     autoboot=True, fullscreen=False, config_path=None, **kwargs):
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})

    disk1_path = _resolve_path(disk1_path or config.get("disk1_path"), config)
    disk2_path = _resolve_path(disk2_path or config.get("disk2_path"), config)
    config_path = _resolve_path(config_path or config.get("apple_config"), config)

    if not name or str(name).strip().lower() in ("none", "null", ""):
        name = "apple1"
    name = str(name).strip()

    instance = AppleInstance(
        name=name,
        disk1_path=disk1_path or None,
        disk2_path=disk2_path or None,
        autoboot=as_bool(autoboot, default=True),
        fullscreen=as_bool(fullscreen, default=False),
        config_path=config_path or None,
    )
    started = instance.start()

    log.append(f"Launching {name} disk1={disk1_path} disk2={disk2_path} config={config_path}")
    log.append("Command: " + " ".join(instance.cmd))

    stdout_lines = instance.get_output()
    if stdout_lines:
        log.append("LinApple stdout:")
        log.append("".join(stdout_lines))

    if not started:
        log.append(f"Failed to start {instance.name}: {instance.startup_error}")
        return False, "\n".join(log)

    context[name] = instance
    time.sleep(0.5)
    instance.take_screenshot()

    return True, "\n".join(log)


@dispatchtest_step
def test_apple_sendkeys(text=None, name="apple1", delay_ms=50, **kwargs):
    """Type a literal string into a running Apple ][ emulator window."""
    context = kwargs.get("context", {})
    instance = context.get(name)

    if not isinstance(instance, AppleInstance):
        return False, f"No AppleInstance named '{name}' found in context"

    if text is None:
        return False, "No text specified"

    log = []
    try:
        success, out = instance.send_keys(str(text), delay_ms=int(delay_ms))
        instance.take_screenshot()
        log.append(f"send_keys to {name}: {out}")
        return success, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send keys to {name}: {e}"


@dispatchtest_step
def test_apple_sendkey(key=None, name="apple1", **kwargs):
    """Send a single xdotool keysym (e.g. Return, Escape, Up/Down/Left/Right)
    to a running Apple ][ emulator window."""
    context = kwargs.get("context", {})
    instance = context.get(name)

    if not isinstance(instance, AppleInstance):
        return False, f"No AppleInstance named '{name}' found in context"

    if not key or not str(key).strip():
        return False, "No key specified"

    log = []
    try:
        success, out = instance.send_key(str(key).strip())
        instance.take_screenshot()
        log.append(f"send_key to {name}: {out}")
        return success, "\n".join(log)
    except Exception as e:
        return False, f"Failed to send key to {name}: {e}"


@dispatchtest_step
def test_apple_terminate_all(**kwargs):
    log = []
    context = kwargs.get("context", {})

    stopped_names = []
    for name, instance in context.items():
        if isinstance(instance, AppleInstance):
            log.append(f"Stopping {name}...")
            instance.take_screenshot()
            instance.stop()
            stopped_names.append(name)
            log.append(f"{name} has been terminated.")

    for name in stopped_names:
        del context[name]

    if not log:
        log.append("No active instances found to stop.")

    return True, "\n".join(log)


# only the steps leave this module; dispatch_functions star-imports it
__all__ = [n for n, f in list(globals().items()) if getattr(f, "_is_teststep", False)]
