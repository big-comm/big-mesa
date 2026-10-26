# mesa-big — the BigCommunity Mesa

**mesa-big** is an optional build of [Mesa](https://www.mesa3d.org), the open
source OpenGL and Vulkan drivers, for [BigCommunity](https://communitybig.org).
It is built from the official sources at mesa3d.org, always the latest stable
release (the `.0` of a new series included), with its own copy of LLVM built
in, and it is tested during the build on simulated AMD and Intel GPUs. It
comes in two packages: **mesa-big** (64-bit) and **lib32-mesa-big**, the same
Mesa for 32-bit games (Steam, Wine).

It is for whoever wants new Mesa releases sooner: games, recent GPUs, new
Vulkan extensions. It is not the default. The ISO and everyone who does not
install it keep Manjaro's `mesa`.

> **Status: first release in testing.** mesa-big 26.2.5 is being validated on
> Intel (Arrow Lake) before it is offered to everyone. See the
> [roadmap](#roadmap).

---

## Contents

- [What is inside](#what-is-inside)
- [How it differs from Manjaro's mesa](#how-it-differs-from-manjaros-mesa)
- [Installing](#installing)
- [Checking that it works](#checking-that-it-works)
- [Going back to Manjaro's mesa](#going-back-to-manjaros-mesa)
- [Tested during the build](#tested-during-the-build)
- [Updating to a new release](#updating-to-a-new-release)
- [Building it yourself](#building-it-yourself)
- [Roadmap](#roadmap)
- [Credits and licenses](#credits-and-licenses)

---

## What is inside

| | Where it comes from |
|---|---|
| **Mesa** | [archive.mesa3d.org](https://archive.mesa3d.org), latest stable release, pinned by checksum |
| **LLVM** | [llvm-project](https://github.com/llvm/llvm-project/releases) stable release, built here and linked in statically |
| **Shader test set** | [shader-db](https://gitlab.freedesktop.org/mesa/shader-db), real shaders from games and apps |

| Drivers | |
|---|---|
| **OpenGL** | radeonsi (AMD), iris (Intel Gen8+), crocus (older Intel), zink (OpenGL on Vulkan), virgl, llvmpipe (software) |
| **Vulkan** | RADV (AMD), ANV (Intel), hasvk (older Intel), NVK (NVIDIA), venus (virtual machines) |
| **Video** | VA-API for AMD, all codecs Mesa supports |
| **Virtual machines** | AMD and Intel native contexts (the host's native driver used inside the guest), plus virgl and venus |
| **Vulkan layers** | device-select, overlay, anti-lag, screenshot, vram-report-limit |

Left out: softpipe (llvmpipe replaces it whenever LLVM is present, and here it
always is), lavapipe (Manjaro's `vulkan-swrast` keeps working alongside)
and OpenCL (see the [roadmap](#roadmap)). The 32-bit package has the same
drivers, without Intel ray tracing (as Arch's `lib32-mesa`).

## How it differs from Manjaro's mesa

- **Newer, sooner.** Each Mesa stable release, as it comes out; Manjaro's mesa
  follows Arch, through its testing and stable branches.
- **Its own LLVM.** Manjaro's mesa links the system LLVM (`llvm-libs`), so it
  has to be rebuilt every time Manjaro moves LLVM, and stable and testing do
  not always agree. mesa-big carries LLVM inside: it does not depend on
  `llvm-libs` and keeps working whatever LLVM the system has. AMD shaders are
  compiled by ACO, Mesa's own compiler (radeonsi's default since Mesa 26.0), so
  LLVM is only used by llvmpipe.
- **One package per architecture.** `mesa`, `vulkan-intel`, `vulkan-radeon`,
  `vulkan-nouveau`, `vulkan-virtio`, `vulkan-mesa-layers`,
  `vulkan-mesa-implicit-layers` and `libva-mesa-driver` are all in `mesa-big`,
  which provides and replaces them; their `lib32-` twins are all in
  `lib32-mesa-big`.
- **Same compiler and flags as Arch.** GCC, `-O2`, the system's
  `makepkg.conf`. Clang was measured and compiled shaders 3–4% slower; Mesa
  refuses LTO. Faster builds (`-O3`, `x86-64-v3`, PGO) will only be used once
  they are measured, see the [roadmap](#roadmap).

The version shown by `glxinfo` and `vulkaninfo` ends in `-big<pkgrel>`, for
example `Mesa 26.2.5-big1`.

## Installing

From the **BigCommunity Driver Manager**: *Mesa* page, the **BigCommunity**
row, *Switch*. It plans the switch first and refuses it if any installed
package would lose a dependency.

Or with pacman, accepting the removal of the packages they replace:

```bash
sudo pacman -S mesa-big lib32-mesa-big
```

Without the multilib repository (no 32-bit programs), `mesa-big` alone.

Then restart the session (or the computer): programs already running keep the
Mesa they started with.

## Checking that it works

```bash
glxinfo -B | grep "OpenGL version"          # ... Mesa 26.2.5-big1
vulkaninfo --summary | grep driverInfo     # Mesa 26.2.5-big1
```

## Going back to Manjaro's mesa

From the Driver Manager, the **Stable** row. Or with pacman, naming the Vulkan
drivers for your GPU:

```bash
sudo pacman -S mesa vulkan-intel vulkan-mesa-implicit-layers \
  lib32-mesa lib32-vulkan-intel lib32-vulkan-mesa-implicit-layers     # Intel
sudo pacman -S mesa vulkan-radeon vulkan-mesa-implicit-layers \
  lib32-mesa lib32-vulkan-radeon lib32-vulkan-mesa-implicit-layers    # AMD
```

If the desktop does not start, do it from a text console (Ctrl+Alt+F3).

## Tested during the build

`check()` runs in the CI on every build, and a package that fails it is not
published:

1. **No LLVM from the system.** No library in the package may link
   `libLLVM`.
2. **The drivers load.** RADV and ANV run on simulated GPUs (Mesa's
   `drm-shim`, no hardware needed) and must report this exact version.
3. **They compile real shaders.** shader-db compiles about 8,500 shaders for
   AMD and 13,500 for Intel on the simulated GPUs; a crash, or fewer than 8,000
   shaders, fails the build.
4. **Performance control.** The code generated for those shaders (size,
   registers, instructions, cycles, spills) is compared with
   [`shaderdb-baseline.txt`](mesa-big/shaderdb-baseline.txt), the numbers of
   the last published release. Anything more than 2% worse, or any new spill,
   is reported as a warning in the build log, to be looked at before the
   release goes to stable.

`lib32-mesa-big` runs what a 32-bit game does first, with a small 32-bit
program built in its `check()` ([`test-lib32.c`](lib32-mesa-big/test-lib32.c)):
OpenGL through EGL on llvmpipe, which must report the static LLVM, and Vulkan
on simulated AMD and Intel GPUs, which must report this exact version. It also
fails if any library links `libLLVM`, or if anything outside `/usr/lib32` ends
up in the package.

## Updating to a new release

For now, by hand, in `mesa-big/PKGBUILD` and `lib32-mesa-big/PKGBUILD`, which
must stay on the same Mesa and LLVM:

1. Set `pkgver` (Mesa) and, when there is a new one, `_llvmver` (LLVM).
   `pkgrel` back to 1.
2. Download the tarballs and their signatures, and check them with the release
   managers' keys kept in [`mesa-big/keys/pgp`](mesa-big/keys/pgp):

   ```bash
   gpg --import mesa-big/keys/pgp/*.asc
   gpg --verify mesa-26.2.6.tar.xz.sig mesa-26.2.6.tar.xz
   ```

   The CI does not import keys, so the PKGBUILD pins the tarballs by checksum
   instead of downloading the signatures: this is where they are checked.
3. Update the NVK crate list if Mesa changed it (`_gencrates` in
   `mesa-big/PKGBUILD` prints it; both PKGBUILDs carry it) and the checksums
   (`updpkgsums` in each directory).
4. Build. After the release is published, copy the new
   `src/shaderdb-summary.metrics` over `shaderdb-baseline.txt`, so the next
   release is compared with this one.

`mesa-big` links `libSPIRV-Tools.so` (and `lib32-mesa-big` its 32-bit twin),
whose file name carries no version: when Manjaro moves `spirv-tools` to a new
release, rebuild both (`pkgrel + 1`).

## Building it yourself

```bash
git clone https://github.com/big-comm/big-mesa
cd big-mesa/mesa-big
makepkg -s
cd ../lib32-mesa-big     # needs the multilib repository
makepkg -s
```

LLVM is built first, then Mesa, then the tests run. Each LLVM compile job
needs about 1.5 GB of memory; on a desktop, limit the jobs to keep it usable:

```bash
MESA_BIG_JOBS=4 nice makepkg -s
```

Each needs about 7 GB of free disk space. Then:

```bash
sudo pacman -U mesa-big/mesa-big-*.pkg.tar.zst lib32-mesa-big/lib32-mesa-big-*.pkg.tar.zst
```

### How the official packages are built

Official packages are built by the BigCommunity CI
([build-package](https://github.com/big-comm/build-package)), which builds
the directory it is asked for (`pkgbuild_dir: mesa-big` or
`pkgbuild_dir: lib32-mesa-big`).

```
big-mesa/
├── mesa-big/
│   ├── PKGBUILD                 # Mesa, LLVM, the build and its tests
│   ├── shaderdb-baseline.txt    # shader-db numbers of the last release
│   └── keys/pgp/                # Mesa and LLVM release managers' keys
└── lib32-mesa-big/
    ├── PKGBUILD                 # the same, for i686
    └── test-lib32.c             # check(): OpenGL and Vulkan from a 32-bit program
```

---

## Roadmap

- [x] mesa-big 26.2.5 — upstream stable, static LLVM 23.1.4, tested on simulated GPUs
- [x] **lib32-mesa-big** — the 32-bit libraries, for Steam and Wine, with their own static LLVM
- [x] Selectable in the BigCommunity Driver Manager
- [ ] Validation on Intel (Arrow Lake) and AMD machines
- [ ] **opencl-mesa-big** — Rusticl, with its own Clang built in
- [ ] A `-debug` package, for useful backtraces when a driver crashes
- [ ] gamescope frame limiter patch
- [ ] Faster builds, only if measured: `-O3`, `x86-64-v3`, profile-guided optimisation
- [ ] New Mesa and LLVM releases picked up, checked and built for testing automatically
- [ ] Published benchmarks against Manjaro's mesa on the same hardware

Claims about speed will be made here with numbers, once the benchmarks exist.

## Credits and licenses

- **Mesa** — the Mesa developers; MIT and other permissive licenses
  (`/usr/share/licenses/mesa-big/license.rst`).
- **LLVM** — the LLVM project; Apache-2.0 with LLVM exception
  (`/usr/share/licenses/mesa-big/LLVM-LICENSE.TXT`).
- **shader-db** — used only in the build's tests, not shipped.
- The packaging follows Arch Linux's `mesa` PKGBUILD (Jan Alexander Steffens,
  Laurent Carlier and contributors), including the NVK crate list.
- This repository: MIT, see [LICENSE](LICENSE).
