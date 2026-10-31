"""CC65 build steps: staging, disk sets, cc65/ca65/ld65 compile, ZX02 packing, host shell scripts.

Loaded through dispatch_functions (star import); shared primitives are in dispatch_common.
"""
import os
import time
import threading
from dispatch_common import dispatchtest_step, resolve_path as _common_resolve_path, as_bool
from vicehelpers import ViceInstance
from vicehelpers import compile_cc65, assemble_ca65, assemble_object, link_ld65, create_blank_d64, format_and_copyd64


def _resolve_path(path, config):
    """Same call shape as the old per-project resolver: '' for an empty path, CONFIG scalars as the token map."""
    if not path:
        return ""
    return _common_resolve_path(path, config)


@dispatchtest_step
def test_make_disk_set(script=None, src_bin=None, out_dir=None, expect=None,
                       extra_args=None, depends=None, force=False, **kwargs):
    """Build a project's data disk set by running its own maker script.

    For tests whose disks carry a large data payload rather than just the PRG,
    the images are generated artifacts and must not be committed or hand-built:
    test_compiletheprogram reformats only its own d64, so anything else the run
    needs has to be produced here.

    Runs `script src_bin out_dir` (with the interpreter running the app) when
    any image named in `expect` is missing, or when an image is older than
    src_bin. Otherwise it is a no-op, so re-runs stay fast.
    """
    import subprocess as _subp
    import sys as _sys

    log = []
    config = kwargs.get("config", {})

    script = _resolve_path(script or config.get("diskmaker_script"), config)
    src_bin = _resolve_path(src_bin or config.get("diskmaker_src"), config)
    out_dir = _resolve_path(out_dir or config.get("out_dir"), config)
    if not script or not os.path.exists(script):
        return False, f"disk maker script not found: {script!r}"
    if not src_bin or not os.path.exists(src_bin):
        return False, f"disk maker source not found: {src_bin!r}"
    if not out_dir:
        return False, "no output directory for the disk set"

    # "a.d64, b.d64" from a testlist, or a real list
    if isinstance(expect, str):
        wanted = [w.strip() for w in expect.replace(",", " ").split() if w.strip()]
    else:
        wanted = list(expect or [])
    targets = [os.path.join(out_dir, w) for w in wanted]

    # Anything else the images are built from -- typically the freshly compiled
    # PRG that gets written onto the first disk -- so a rebuilt program lands on
    # the disk instead of silently leaving a stale copy there.
    if isinstance(depends, str):
        dep_list = [_resolve_path(d.strip(), config)
                    for d in depends.replace(",", " ").split() if d.strip()]
    else:
        dep_list = [_resolve_path(d, config) for d in (depends or [])]
    sources = [src_bin] + [d for d in dep_list if d]

    stale = as_bool(force)
    if not stale:
        if not targets:
            stale = True
            log.append("no expected images listed: building unconditionally")
        else:
            newest_src = max(os.path.getmtime(s) for s in sources if os.path.exists(s))
            for t in targets:
                if not os.path.exists(t):
                    stale = True
                    log.append(f"missing: {t}")
                elif os.path.getmtime(t) < newest_src:
                    stale = True
                    log.append(f"older than its sources: {t}")

    def _artifacts(lines):
        # the report harvests "ARTIFACT: <path>" lines, so the disks show up in
        # the build-artifacts table whether or not they were rebuilt this run
        for t in targets:
            if os.path.exists(t):
                lines.append(f"ARTIFACT: {t}")
        return lines

    if not stale:
        return True, "\n".join(_artifacts(log + [f"disk set already current in {out_dir}"]))

    os.makedirs(out_dir, exist_ok=True)
    cmd = [_sys.executable, script, src_bin, out_dir]
    if extra_args:
        extra = extra_args if isinstance(extra_args, (list, tuple)) else str(extra_args).split()
        cmd += [_resolve_path(str(x), config) for x in extra]
    log.append("running: " + " ".join(cmd))
    proc = _subp.run(cmd, stdout=_subp.PIPE, stderr=_subp.STDOUT, text=True,
                     cwd=os.path.dirname(script) or None)
    log.append(proc.stdout.strip())
    if proc.returncode != 0:
        return False, "\n".join(log + [f"disk maker failed (rc={proc.returncode})"])

    missing = [t for t in targets if not os.path.exists(t)]
    if missing:
        return False, "\n".join(log + ["still missing after build: " + ", ".join(missing)])

    for t in targets:
        log.append(f"built {os.path.basename(t)}: {os.path.getsize(t)} bytes")
    return True, "\n".join(_artifacts(log))


@dispatchtest_step
def test_stage_file(src_file=None, dst_file=None, **kwargs):
    """Copy src_file -> dst_file before a build.

    Used by 'variant' tests that share one source tree: each variant stages its
    own data file (e.g. polishcow.bin) into the fixed name the assembler
    .incbin's (sidmusic1.bin), so variants don't clobber each other's data.
    """
    import shutil
    config = kwargs.get("config", {})
    src = _resolve_path(src_file or config.get("sidbin_src"), config)
    dst = _resolve_path(dst_file or config.get("sidbin_dst"), config)
    if not src or not dst:
        return False, f"stage: missing src/dst (src={src!r} dst={dst!r})"
    if not os.path.exists(src):
        return False, f"stage: source not found: {src}"
    shutil.copyfile(src, dst)
    return True, f"staged {src} -> {dst} ({os.path.getsize(dst)} bytes)"


@dispatchtest_step
def test_compiletheprogram(out_dir=None, src_dir=None, d64_path=None, driver1_path=None, driver1_label=None,
                           driver2_path=None, driver2_label=None,
                           drivers=None, linkerconf=None,
                           prg_filepath=None, archtype=None, **kwargs):
    import os
    import glob
    import traceback

    def _nullify(v):
        """Convert string 'None'/'null'/'' to Python None."""
        if not v or str(v).strip().lower() in ("none", "null", ""):
            return None
        return v

    log = []
    context = None

    try:
        context = kwargs.get("context")
        config = kwargs.get("config")

        log.append(f"config present: {bool(config)}")
        log.append(f"context present: {bool(context)}")

        if not config:
            raise ValueError("Missing or empty config")

        archtype = _nullify(archtype) or _nullify(config.get("archtype", ""))
        log.append(f"archtype: {archtype!r}")

        # optional custom ld65 linker config
        linker_cfg = _nullify(linkerconf) or _nullify(config.get("linkerconf"))
        if linker_cfg:
            linker_cfg = _resolve_path(linker_cfg, config)
            log.append(f"linker_cfg: {linker_cfg!r}")
            if not linker_cfg:
                raise ValueError("Could not resolve linkerconf")

        drivers_list = []
        if drivers:
            for drv in drivers:
                if isinstance(drv, dict):
                    drivers_list.append(drv)
                else:
                    log.append(f"Skipping invalid driver entry (not a dict): {drv!r}")

        for path_key, label_key, path_arg, label_arg in [
            ("linker_driver1", "driver1_label", driver1_path, driver1_label),
            ("linker_driver2", "driver2_label", driver2_path, driver2_label),
        ]:
            drv_path = _nullify(path_arg) or _nullify(config.get(path_key))
            drv_label = _nullify(label_arg) or _nullify(config.get(label_key))
            if drv_path and drv_label:
                drivers_list.append({"path": drv_path, "label": drv_label})

        res_out_dir = _resolve_path(_nullify(out_dir) or _nullify(config.get("out_dir")), config)
        log.append(f"res_out_dir: {res_out_dir!r}")
        if not res_out_dir:
            raise ValueError("Invalid output directory")

        res_src_dir = _resolve_path(_nullify(src_dir) or _nullify(config.get("src")), config)
        log.append(f"res_src_dir: {res_src_dir!r}")
        if not res_src_dir:
            raise ValueError("Invalid source directory")

        os.makedirs(res_out_dir, exist_ok=True)

        # Resolve drivers once — they are shared across every disk built here.
        resolved_drivers = []
        log.append(f"drivers: {len(drivers_list)}")
        for drv in drivers_list:
            drv_path = _nullify(drv.get("path"))
            drv_label = _nullify(drv.get("label"))
            if not drv_path or not drv_label:
                log.append(f"Skipping driver with missing path or label: {drv!r}")
                continue
            resolved_drv = _resolve_path(drv_path, config)
            if not resolved_drv:
                raise ValueError(f"Could not resolve driver path: {drv_path!r}")
            resolved_label = _resolve_path(drv_label, config)
            if not resolved_label:
                raise ValueError(f"Could not resolve driver label: {drv_label!r}")
            base = os.path.splitext(os.path.basename(resolved_drv))[0]
            resolved_drivers.append((base, resolved_drv, resolved_label))

        # if src_dir has subdirectories build each into its own disk image named after the subdir 
        # if no subdirs, collect every .c/.s directly in src_dir and link into a single disk
        # disk spec = (label, build_src, inter_dir, prg_file, d64_file)
        disk_specs = []
        subdirs = [
            entry for entry in sorted(os.listdir(res_src_dir))
            if os.path.isdir(os.path.join(res_src_dir, entry))
            and glob.glob(os.path.join(res_src_dir, entry, "*.c"))
        ]

        if subdirs:
            log.append(f"multi-disk mode: {len(subdirs)} client subdir(s): {subdirs}")
            for name in subdirs:
                build_src = os.path.join(res_src_dir, name)
                inter_dir = os.path.join(res_out_dir, name)
                prg_file = os.path.join(res_out_dir, name + ".prg")
                d64_file = os.path.join(res_out_dir, name + ".d64")
                disk_specs.append((name, build_src, inter_dir, prg_file, d64_file))
        else:
            res_prg_file = _resolve_path(_nullify(prg_filepath) or _nullify(config.get("prg_filepath")), config)
            log.append(f"res_prg_file: {res_prg_file!r}")
            if not res_prg_file:
                raise ValueError("Invalid PRG output path")

            d64_filename = config.get("cmainfile", "disk") + ".d64"
            _d64_input = _nullify(d64_path) or os.path.join(res_out_dir, d64_filename)
            log.append(f"d64 input before resolve: {_d64_input!r}")
            res_d64_file = _resolve_path(_d64_input, config)
            log.append(f"res_d64_file: {res_d64_file!r}")
            if not res_d64_file:
                raise ValueError("Invalid D64 output path")

            disk_label = config.get("cmainfile", "disk")
            log.append("flat mode: single disk from src_dir")
            disk_specs.append((disk_label, res_src_dir, res_out_dir, res_prg_file, res_d64_file))

        steps = []
        artifact_paths = []

        for disk_label, build_src, inter_dir, prg_file, d64_file in disk_specs:
            os.makedirs(inter_dir, exist_ok=True)
            link_objects = []

            c_files = glob.glob(os.path.join(build_src, "*.c"))
            log.append(f"[{disk_label}] c_files found: {len(c_files)}")
            for src_c in c_files:
                base_name = os.path.splitext(os.path.basename(src_c))[0]
                asm_file = os.path.join(inter_dir, f"{base_name}.s")
                obj_file = os.path.join(inter_dir, f"{base_name}.o")
                steps.extend([
                    (compile_cc65,  [src_c, asm_file, archtype], {"optimize": True}, f"[{disk_label}] Compile cc65 {base_name}"),
                    (assemble_ca65, [asm_file, obj_file, archtype], {},              f"[{disk_label}] Assemble ca65 {base_name}"),
                ])
                link_objects.append(obj_file)
                artifact_paths.extend([asm_file, obj_file])

            s_files = glob.glob(os.path.join(build_src, "*.s"))
            log.append(f"[{disk_label}] s_files found: {len(s_files)}")
            for src_s in s_files:
                base_name = os.path.splitext(os.path.basename(src_s))[0]
                obj_file = os.path.join(inter_dir, f"{base_name}_asm.o")
                steps.append(
                    (assemble_ca65, [src_s, obj_file, archtype], {}, f"[{disk_label}] Assemble ca65 {base_name} (source)")
                )
                link_objects.append(obj_file)
                artifact_paths.append(obj_file)

            for base, resolved_drv, resolved_label in resolved_drivers:
                driver_s = os.path.join(inter_dir, base + ".s")
                driver_o = os.path.join(inter_dir, base + ".o")
                steps.extend([
                    (assemble_object, [resolved_drv, driver_s, resolved_label], {}, f"[{disk_label}] Assemble driver object {base}"),
                    (assemble_ca65,   [driver_s, driver_o, archtype],            {}, f"[{disk_label}] Assemble driver ca65 {base}"),
                ])
                link_objects.append(driver_o)
                artifact_paths.extend([driver_s, driver_o])

            if not link_objects:
                raise RuntimeError(f"No linkable objects for disk '{disk_label}' — no .c or .s files found in {build_src}")

            steps.extend([
                (link_ld65,          [link_objects, prg_file, archtype, linker_cfg], {}, f"[{disk_label}] Link ld65"),
                (create_blank_d64,   [d64_file],                                     {}, f"[{disk_label}] Create blank d64"),
                (format_and_copyd64, [d64_file, prg_file],                           {}, f"[{disk_label}] Format and copy to d64"),
            ])
            artifact_paths.extend([prg_file, d64_file])

        log.append(f"steps to run: {len(steps)}")

        for func, args, kwargs_func, label in steps:
            log.append(f"Running: {label}")
            try:
                success, out = func(*args, **kwargs_func)
            except Exception as e:
                tb = traceback.extract_tb(e.__traceback__)
                origin = tb[-1] if tb else None
                location = f"{origin.filename}:{origin.lineno} in {origin.name}" if origin else "unknown location"
                raise RuntimeError(f"Exception in step '{label}' [{location}]: {type(e).__name__}: {e}") from e
            log.append(f"{label}:\n{out}")
            if not success:
                raise RuntimeError(f"Step returned failure: {label}")

        for art in artifact_paths:
            log.append(f"ARTIFACT: {art}")

    except Exception as e:
        tb_lines = traceback.format_exc().splitlines()
        log.append(f"EXCEPTION ({type(e).__name__}): {e}")
        log.append(f"TRACEBACK:\n" + "\n".join(tb_lines))
        if isinstance(context, dict):
            context["abort"] = True
        return False, "\n".join(log)

    return True, "\n".join(log)


@dispatchtest_step
def test_compress_zx02(enabled=True, out_dir=None, src_dir=None, archtype=None, linkerconf=None,
                       zx02_prg_filepath=None, zx02_d64_path=None, mode="relink",
                       prg_filepath=None, **kwargs):
    """Build a parallel, self-extracting zx02 PRG+D64 alongside the normal
    build -- same src_dir/archtype/linkerconf test_compiletheprogram would
    use, run after it and before test_emulator_start. Produces separate
    artifacts (zx02_prg_filepath/zx02_d64_path); it never touches the
    normal prg_filepath/d64_path, so a testlist points test_emulator_start
    at whichever pair it wants to actually boot.

    mode picks the loader shape:

      "relink" (default) recompiles the program at a shifted start address so
        the compressed blob sits entirely below it. Smallest loader, but it
        needs a program that can be relinked and that leaves room underneath.

      "inplace" compresses the PRG test_compiletheprogram already built, as
        is, and relies on ZX02's in-place guarantee instead of on disjoint
        ranges. Use it when "relink" cannot apply: a linker cfg that pins
        segments to absolute addresses, or a program that already fills low
        memory. Reads prg_filepath rather than src_dir.

    enabled defaults on; set "enabled": "False" in a step's param to make
    this a no-op without removing the step (so a testlist can toggle the
    zx02 variant per run/variant without editing its step list).
    """
    from testsrc.pyhelpers.zx02build import build_zx02_variant, build_zx02_inplace_variant

    context = kwargs.get("context", {})
    config = kwargs.get("config", {})

    if not as_bool(enabled, default=True):
        return True, "zx02 compression skipped (enabled=False)"

    if not config:
        return False, "Missing config"

    archtype = archtype or config.get("archtype")
    res_out_dir = _resolve_path(out_dir or config.get("out_dir"), config)
    res_src_dir = _resolve_path(src_dir or config.get("src"), config)
    res_prg_out = _resolve_path(zx02_prg_filepath or config.get("zx02_prg_filepath"), config)
    res_d64_out = _resolve_path(zx02_d64_path or config.get("zx02_d64_path"), config)
    linker_cfg = _resolve_path(linkerconf or config.get("linkerconf") or "", config) or None

    mode = (str(mode).strip().lower() or "relink")
    if mode not in ("relink", "inplace"):
        return False, f"zx02 compression: unknown mode {mode!r} (expected 'relink' or 'inplace')"

    if mode == "inplace":
        res_prg_in = _resolve_path(prg_filepath or config.get("prg_filepath"), config)
        if not res_out_dir or not res_prg_in or not res_prg_out or not res_d64_out:
            return False, (f"zx02 compression (inplace): missing path(s) -- out_dir={res_out_dir!r} "
                           f"prg_filepath={res_prg_in!r} zx02_prg_filepath={res_prg_out!r} "
                           f"zx02_d64_path={res_d64_out!r}")
    elif not res_out_dir or not res_src_dir or not res_prg_out or not res_d64_out:
        return False, (f"zx02 compression: missing path(s) -- out_dir={res_out_dir!r} "
                       f"src_dir={res_src_dir!r} zx02_prg_filepath={res_prg_out!r} "
                       f"zx02_d64_path={res_d64_out!r}")

    try:
        if mode == "inplace":
            success, log = build_zx02_inplace_variant(
                res_prg_in, res_out_dir, archtype, res_prg_out, res_d64_out,
                linker_cfg=linker_cfg,
            )
        else:
            success, log = build_zx02_variant(
                res_src_dir, res_out_dir, archtype, res_prg_out, res_d64_out,
                linker_cfg=linker_cfg,
            )
    except Exception as e:
        import traceback
        if isinstance(context, dict):
            context["abort"] = True
        return False, f"EXCEPTION ({type(e).__name__}): {e}\n{traceback.format_exc()}"

    if not success and isinstance(context, dict):
        context["abort"] = True
    return success, log


@dispatchtest_step
def test_shellscript(script=None, args=None, cwd=None, timeout=None, **kwargs):
    """Run an arbitrary shell script as a test step.

    Not tied to any particular archtype -- any testlist can shell out to a
    build/convert/setup script this way, the same as the qemu testlists do.
    `args` is a testlist string (shlex-split) or a real list; both go through
    resolve_path so {placeholders} from the testlist config work same as
    elsewhere. Runs with /bin/sh if the script isn't marked executable.
    """
    import subprocess as _subp
    import shlex as _shlex

    config = kwargs.get("config", {})
    context = kwargs.get("context", {})

    res_script = _resolve_path(script, config)
    if not res_script or not os.path.exists(res_script):
        context["abort"] = True
        return False, f"shell script not found: {res_script!r}"

    if isinstance(args, str):
        arg_list = _shlex.split(args)
    else:
        arg_list = [str(a) for a in (args or [])]
    arg_list = [_resolve_path(a, config) for a in arg_list]

    res_cwd = _resolve_path(cwd, config) if cwd else (os.path.dirname(res_script) or None)

    cmd = [res_script] + arg_list
    if not os.access(res_script, os.X_OK):
        cmd = ["/bin/sh", res_script] + arg_list

    log = ["running: " + " ".join(cmd)]
    try:
        proc = _subp.run(cmd, stdout=_subp.PIPE, stderr=_subp.STDOUT, text=True,
                         cwd=res_cwd, timeout=float(timeout) if timeout else None)
    except _subp.TimeoutExpired as e:
        context["abort"] = True
        return False, "\n".join(log + [f"timed out after {timeout}s", (e.output or "").strip()])
    except Exception as e:
        context["abort"] = True
        return False, "\n".join(log + [str(e)])

    log.append((proc.stdout or "").strip())
    if proc.returncode != 0:
        context["abort"] = True
        return False, "\n".join(log + [f"shell script failed (rc={proc.returncode})"])

    return True, "\n".join(log)


# only the steps leave this module; dispatch_functions star-imports it
__all__ = [n for n, f in list(globals().items()) if getattr(f, "_is_teststep", False)]
