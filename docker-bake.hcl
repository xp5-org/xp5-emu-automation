// Composes the base + selected components into the final image.
//   docker buildx bake                      -> all components
//   COMPONENTS=86box,simh docker buildx bake
// Every component with a docker/<name>.Dockerfile is a bake target; only the
// selected ones are wired into the final image, the rest are left as
// empty `FROM scratch` stages in the Dockerfile, so unselected builds never run.

variable "COMPONENTS" { default = "all" }
variable "APT_PROXY" { default = "" }
variable "QEMU_VERSION" { default = "latest" }
variable "TAG" { default = "xp5-rdp-test:latest" }
variable "BASE_TAG" { default = "xp5-rdp-testbase:latest" }

group "default" {
  targets = ["final"]
}

target "base" {
  dockerfile = "Dockerfile.base"
  context    = "."
  args       = { APT_PROXY = APT_PROXY }
  tags       = [BASE_TAG]
}

target "comp" {
  name       = "comp-${c}"
  matrix     = { c = ["qemu", "86box", "retro68", "basilisk", "watcom", "ps2sdk", "pcsx2", "nxdk", "xemu", "vice", "hatari", "linapple", "webtools", "kicad"] }
  dockerfile = "docker/${c}.Dockerfile"
  context    = "."
  contexts   = { base = "target:base" }
  args       = c == "qemu" ? { APT_PROXY = APT_PROXY, QEMU_VERSION = QEMU_VERSION } : { APT_PROXY = APT_PROXY }
}

target "final" {
  dockerfile = "Dockerfile"
  context    = "."
  // ctx-<name> is only passed for selected components; for the rest the
  // Dockerfile's own `FROM scratch AS ctx-<name>` stage stays in place.
  contexts = merge(
    { base = "target:base" },
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "qemu") ? { ctx-qemu = "target:comp-qemu" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "86box") ? { ctx-86box = "target:comp-86box" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "retro68") ? { ctx-retro68 = "target:comp-retro68" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "basilisk") ? { ctx-basilisk = "target:comp-basilisk" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "watcom") ? { ctx-watcom = "target:comp-watcom" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "ps2sdk") ? { ctx-ps2sdk = "target:comp-ps2sdk" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "pcsx2") ? { ctx-pcsx2 = "target:comp-pcsx2" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "nxdk") ? { ctx-nxdk = "target:comp-nxdk" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "xemu") ? { ctx-xemu = "target:comp-xemu" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "vice") ? { ctx-vice = "target:comp-vice" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "hatari") ? { ctx-hatari = "target:comp-hatari" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "linapple") ? { ctx-linapple = "target:comp-linapple" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "webtools") ? { ctx-webtools = "target:comp-webtools" } : {},
    COMPONENTS == "all" || contains(split(",", COMPONENTS), "kicad") ? { ctx-kicad = "target:comp-kicad" } : {}
  )
  args = { COMPONENTS = COMPONENTS, APT_PROXY = APT_PROXY }
  tags = [TAG]
}
