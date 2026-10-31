"""Atari ST (Hatari) steps.

Loaded through dispatch_functions (star import); shared primitives are in dispatch_common.
"""
import os
import time
from dispatch_common import dispatchtest_step, resolve_path as _common_resolve_path, as_bool, register_terminable
from atarihelpers import HatariInstance

register_terminable(HatariInstance)


def _resolve_path(path, config):
    """Same call shape as the old per-project resolver: '' for an empty path, CONFIG scalars as the token map."""
    if not path:
        return ""
    return _common_resolve_path(path, config)


@dispatchtest_step
def test_hatari_start(name=None, port=6502, config_path=None,
                      mountpath=None,
                      prg_filename=None,
                      romfile_path=None,
                      fastboot=True,
                      **kwargs):
    log = []
    context = kwargs.get("context", {})
    config = kwargs.get("config", {})
    
    config_path = _resolve_path(config_path or config.get("hatari_config"), config)
    mountpath = _resolve_path(mountpath or config.get("mountpath"), config)
    prg_filename = config.get("prg_filename")
    rom_path = _resolve_path(romfile_path or config.get("rom_path"), config)
    archtype = config.get("archtype", "st")
    
    if not name:
        name = "hatari1"
    
    name = "hatari1"

    instance = HatariInstance(
        name=name,
        port=port,
        archtype=archtype,
        config_path=config_path,
        prg_filename=prg_filename,
        rom_path=rom_path,
        mountpath=mountpath,
        fastboot=fastboot
    )
    started = instance.start()
    
    log.append(f"Launching {name} mount={mountpath} rom={rom_path} config={config_path}")
    log.append("Command: " + " ".join(instance.cmd))
    
    stdout_lines = instance.get_output()
    if stdout_lines:
        log.append("Hatari stdout:")
        log.append("".join(stdout_lines))
        
    if not started:
        log.append(f"Failed to start {instance.name}: {instance.startup_error}")
        return False, "\n".join(log)
        
    context[name] = instance
    time.sleep(0.5)
    instance.take_screenshot()
    
    return True, "\n".join(log)


@dispatchtest_step
def test_hatariterminate_all(**kwargs):
    log = []
    context = kwargs.get("context", {})
    
    stopped_names = []
    for name, instance in context.items():
        if hasattr(instance, "stop") and callable(instance.stop):
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


@dispatchtest_step
def test_compile4atari(out_dir=None, src_dir=None, prg_filepath=None, config=None, **kwargs):
    import os
    import subprocess
    context = kwargs.get("context", {})

    if not config:
        return False, "Missing config"

    res_out_dir = _resolve_path(out_dir if out_dir != 'None' else config.get("out_dir"), config)
    res_src_dir = _resolve_path(src_dir if src_dir != 'None' else config.get("src"), config)
    res_prg_path = _resolve_path(prg_filepath if prg_filepath != 'None' else config.get("prg_filepath"), config)
    
    os.makedirs(res_out_dir, exist_ok=True)

    cmainfile = config.get("cmainfile", "")
    source_file = os.path.normpath(os.path.join(res_src_dir, cmainfile))

    log = []
    try:
        result = subprocess.run(
            ["m68k-atari-mintelf-gcc", source_file, "-lgem", "-o", res_prg_path],
            cwd=res_out_dir,
            capture_output=True,
            text=True
        )
        log.append(result.stdout)
        log.append(result.stderr)
        
        if result.returncode != 0:
            context["abort"] = True
            return False, "\n".join(log)
            
    except Exception as e:
        context["abort"] = True
        return False, str(e)

    if not os.path.exists(res_prg_path):
        context["abort"] = True
        log.append(f"Output file not found: {res_prg_path}")
        return False, "\n".join(log)

    log.append(f"ARTIFACT: {res_prg_path}")

    return True, "\n".join(log)


# only the steps leave this module; dispatch_functions star-imports it
__all__ = [n for n, f in list(globals().items()) if getattr(f, "_is_teststep", False)]
