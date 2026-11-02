<h1 align="center">QEMU + DOS + Flask Automation Toolkit</h1>


This is a Python Flask tool used for automating QEMU, MS-DOS, and Pacific-C workflows.

I like writing software and tools for DOS, but it’s tedious and takes many steps. This project is an effort to reduce the process to a button click or API call to:

- Copy and build the code, validating whether the compiler succeeded or failed  
- Play the saved-state VM with the built executable and automatically capture the screen to observe the program's behavior

### In its current state, it:

- Copies data into the `hdd.img` DOS filesystem  
- Converts `hdd.img` into `hdd.qcow2` (for `savevm` support)  
- Boots MS-DOS using `qemu-system-i386`  
- Performs OCR against screenshots  
- Sends key inputs  
- Takes VM-state snapshots  
- Attaches and copies data to a floppy image  
- Records the runtime and results as an HTML report

### Working on:

- Separating out floppy build/package steps into a separate test  
- Making the build test solely responsible for checking if code compiles




## Build & Run

### Repositories

The toolkit is two checkouts that sit side by side on a shared filesystem. Both are bind-mounted into the container:

| Repo | Checkout | Mounted at | Holds |
|---|---|---|---|
| [flask-testrunner](https://github.com/xp5-org/flask-testrunner) | `testrunnerapp` | `/testrunnerapp` | Flask app, cluster manager, `cluster/` queue |
| [xp5-emu-automation](https://github.com/xp5-org/xp5-emu-automation) | `testsrc` | `/testsrc` | `sourcedir/` testlists, `pyhelpers/`, Dockerfiles, `docker-bake.hcl` |

```bash
export SHARED_FS_PATH=/mnt/bughabitat
cd "$SHARED_FS_PATH"
git clone https://github.com/xp5-org/flask-testrunner.git testrunnerapp
git clone https://github.com/xp5-org/xp5-emu-automation.git testsrc
```
### Building the Image

## 0. List what is available
here is the command to run against the git dirs to see what the current version has as buildable docker layers

## 1. prep bakebuilder

Run from `testsrc/`, the directory that holds `docker-bake.hcl`, `Dockerfile`, `Dockerfile.base` and `docker/` together (the build context). Needs Docker with buildx.

```bash
cd "$SHARED_FS_PATH/testsrc"
docker buildx use bakebuilder 2>/dev/null || docker buildx create --name bakebuilder --driver docker-container --use
COMPONENTS=qemu,86box docker buildx bake --load
```

The second line selects the `bakebuilder` builder if it already exists and creates it otherwise, so it is safe to re-run. The third line is one command: `COMPONENTS=...` is an environment variable for `docker buildx bake`, and `--load` is a `bake` flag. For everything, use `COMPONENTS=all docker buildx bake --load` (or just `docker buildx bake --load`).


## 2. Copy the components in
`COMPONENTS` is a comma-separated list (default `all`). One command lists everything that can be selected, with a one-line description. `stage` means the component has its own `docker/<name>.Dockerfile`, compiled (or downloaded prebuilt) during `docker build`; `apt` would mean distro packages only, and none are left:

```bash
cd "$SHARED_FS_PATH/testsrc/docker" && for f in pkgs/*.apt; do n=$(basename $f .apt); [ -f $n.Dockerfile ] && t=stage || t=apt; printf '%-10s %-6s %s\n' "$n" "$t" "$(sed -n 's/^# desc: //p' $f)"; done
```

Anything not in that list cannot be installed. Pass any subset, e.g. `COMPONENTS=qemu,86box,verilator,eda`, or leave it unset for everything. `eda` is the Icarus Verilog / Yosys / ngspice / GTKWave toolchain; `eda`, `verilator`, `simh` and `dosbox` are all compiled from their git heads at build time (`VERILATOR_REF`, default `stable`; `SIMH_REF` and `DOSBOXX_REF`, default `master`). `gnuradio` builds gr-lora_sdr and gr-osmosdr into a conda env (`/opt/conda/envs/gr310`) that is not on `PATH` (`gnuradio-companion` and `grcc` are wrappers that activate it) and is slow to build.

Each component builds in its own stage and apt packages shared between components are installed once, so `COMPONENTS=86box` does not pull in or build QEMU. Components that compile from source must also appear in the `matrix` in `docker-bake.hcl`; `docker buildx bake --print` shows the resolved plan. Other variables: `APT_PROXY=http://host:3142`, `QEMU_VERSION=v10.1.0`, `TAG=myimage:latest` (default `xp5-rdp-test:latest`; use the same name when running).

Bake's `target:` contexts need a `docker-container` builder (the default `docker` driver fails with `unsupported context source target`), which is what the `buildx create` line sets up. `--load` puts the finished image into the local Docker image store.


### running built image
## 1. once image is built, run docker image from CLI

```bash
docker run -d --name testnode -p 3389:3389 -p 8080:8080 \
  -e USERNAME=user -e USERPASSWORD=a \
  -v "$SHARED_FS_PATH/testrunnerapp":/testrunnerapp \
  -v "$SHARED_FS_PATH/testsrc":/testsrc \
  xp5-rdp-test:latest
```

RDP to `localhost:3389`, web UI at `http://localhost:8080`. If `USERPASSWORD` is omitted a random one is printed in `docker logs`. The `/testsrc` mount provides `sourcedir/` and `pyhelpers/`; without it the web UI starts but lists no tests. The container's entrypoint `chown`s `/testrunnerapp` recursively, so that mount must be writable.

## 1. Alternative - use docker compose to manage multiple nodes

Five identical containers that all bind-mount the same host paths, so they share the app code, the `cluster/` queue directory, the test sources and the SSH keys. Each node gets its own hostname plus its own RDP and web ports.

```yaml
# docker-compose.yml  (image built beforehand with: docker buildx bake --load)
x-node: &node
  image: xp5-rdp-test:latest
  restart: unless-stopped
  shm_size: 1gb
  environment:
    USERNAME: user
    USERPASSWORD: ${USERPASSWORD:-changeme}
  volumes:
    - ${SHARED_FS_PATH:-/mnt/bughabitat}/testrunnerapp:/testrunnerapp   # app + cluster/ queue shared by every node
    - ${SHARED_FS_PATH:-/mnt/bughabitat}/testsrc:/testsrc               # testlists, pyhelpers, optional m68k/ BasiliskII override
    - ${SHARED_FS_PATH:-/mnt/bughabitat}/keys:/keys                     # shared sshd host keys + agent keypair

services:
  node1: { <<: *node, hostname: node1, container_name: node1, ports: ["3391:3389", "8101:8080"] }
  node2: { <<: *node, hostname: node2, container_name: node2, ports: ["3392:3389", "8102:8080"] }
  node3: { <<: *node, hostname: node3, container_name: node3, ports: ["3393:3389", "8103:8080"] }
  node4: { <<: *node, hostname: node4, container_name: node4, ports: ["3394:3389", "8104:8080"] }
  node5: { <<: *node, hostname: node5, container_name: node5, ports: ["3395:3389", "8105:8080"] }
```

```bash
mkdir -p "$SHARED_FS_PATH/keys"
USERPASSWORD=secret docker compose up -d
```

Node N is at RDP `:339N` and web `:810N`. Because `/testrunnerapp` is shared, all nodes read and write the same `cluster/` directory; register each node's URL in `cluster/host_urls.json` (e.g. `"node1": "http://<docker-host-ip>:8101"`) so the manager can reach it. Use a separate bind mount per node for anything that must not be shared (e.g. `./state/node1:/home/user`).

Build-test

https://github.com/user-attachments/assets/dd1ac9d7-7b0d-412a-85af-f96541620758

Play-test

https://github.com/user-attachments/assets/f65638ed-ffad-45da-8fd2-869d3140ef0a







## Screenshots

**Screenshot 1: Initial Build View**  
the tests are separated as its unreliable to write to the msdos FAT disk image while the vm is in a powered on state or may want to have separate build vs run actions

![Screenshot 2025-06-19 at 9 01 03 PM](https://github.com/user-attachments/assets/7a13753e-6e48-406e-83aa-ca8ca55fb3bf)

**Screenshot 2: Post-Build Summary**  
report generation with screenshots included inline

![Screenshot 2025-06-19 at 9 03 01 PM](https://github.com/user-attachments/assets/9e73eca0-c5af-4fb0-8399-b1f3c781d9eb)


Example Build-test failure:
![Screenshot 2025-06-21 at 9 35 01 AM](https://github.com/user-attachments/assets/657d8669-f6fc-494a-8ff8-7d2c50135f7a)


Example Build-test success detailed report:
![image](https://github.com/user-attachments/assets/7e6e57a9-1df3-4c98-ad75-cf9fd291ae7a)




