# Windows x64 release CRT: local or authorized private packaging

This recipe is deliberately **absent from public Forge dispatch and ingest**.
The Microsoft runtime installation license does not grant standalone public
redistribution. Licensed Visual Studio users have separate, conditional rights
to distribute listed DLLs with their programs. This repository already uses a
[private-bytes policy](../../docs/ASSET_CATALOG.md#why-msvc-is-like-this) for
Microsoft toolchains. A private destination alone does not establish entitlement.

Pinned Visual Studio manifest package
`Microsoft.VC.14.52.CRT.Redist.X64.base`, version **14.52.37029**, SHA256
`40d09d7e3ee369b84c7fb47cb76e773c54f2614e24d72fbcbf38a3821f67f86b`.
This is the same official Microsoft manifest/payload source family used by xwin.
The archive's installation directory is `14.52.37025`; its manifest/release
version is `14.52.37029`. This runtime is newer than the installed xwin snapshot's
14.44.35220 CRT headers and supports Windows 10+/Server 2016+.

Only the ten release DLLs from `x64/Microsoft.VC145.CRT/` are copied to `bin/`.
The helper rejects unknown/debug DLL names, duplicate names, path traversal and
non-AMD64/non-DLL PE files. All DLL bytes remain unmodified. `meta.json` records
the upstream source, hashes, license acknowledgement and per-DLL hashes. The
package includes the pinned Microsoft runtime license document.

For local use after reading the linked Microsoft license terms:

```sh
uv run python recipes/_windows_cross.py vc-runtime \
  --work /path/to/private-work --accept-microsoft-license
# Conan equivalent: SOLDR_ACCEPT_MICROSOFT_LICENSE=1 uv run --with conan conan create \
#   recipes/vc-runtime-windows-x64 --version 14.52.37029
```

The flag acknowledges local installation/use terms; it does not grant rights to
publish the result. Do not upload the resulting DLLs to public Actions artifacts,
public releases, LFS or a public catalogue mirror without established permission.

UCRT is supplied by Windows 10+ itself. The recipe intentionally does not copy
`ucrtbase.dll` from a Linux host cache or another Windows installation. Consumers
must audit the test closure against an explicit Windows OS/API-set baseline and
stage VC runtime DLLs alongside the test executables under an authorized route.
See [the Windows producer/consumer contract](../../docs/LINUX_WINDOWS_CROSS.md).
