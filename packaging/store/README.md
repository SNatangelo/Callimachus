# Microsoft Store build records

See [the recorded build procedure](../../docs/deployment/microsoft-store-build.md).

`source-inputs.json` is the public v1.1.0 supplementary source publication
inventory. Its hashes identify the published archives, not a new release.
`native-recipes/` preserves the corresponding small build scripts and source /
dependency inventories. Copy them to `build/desktop/native-14.40` before use.

`prepare_sources.py` recovers 22 hash-pinned public source archives and original
upstream wheel inputs and places them in the recipe layout. Its `--list` mode
prints the recovery plan without touching the build directory. It does not use
the optional retained artifacts under `output/` and does not compile programs.

`prepare_tools.py` recovers public Microsoft SDK/framework inputs and portable
build tools into `build/store-tools`. The linked guide records installation of
the exact Visual Studio component and compiler version checks. The native
driver prints its plan by default; only `-Build` executes compilation. It
preserves the historical `R:` short-path requirement and checks an existing
mapping before using it. The CPython script's MSVC directory is 14.39.33519;
the actual compiler/linker build is 14.39.33523. Framework DLLs are 14.40.33728.0.

The framework manifest on this branch corrects a publication receipt error:
`appx_sha256` binds the selected Microsoft framework Appx, not Callimachus.msix.
Its eight DLL hashes are retained unchanged.

Never copy development signing keys, provider credentials, run databases,
compiler installations, virtual environments, wheels or frozen distributions
into this directory. This directory records the build, not certification.
