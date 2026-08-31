# Private Dahua P2P worker

This directory contains only source code. Dahua/SmartPSS binaries are proprietary and must stay on the user's NAS.

Build with a 64-bit MinGW toolchain and run the resulting `p2p_relay_multi.exe` through Wine. Mount the vendor directory read-only at `/vendor`; it must contain `P2PDll.dll`, `libdsl.dll`, and `jsonmd.dll` from an authorized SmartPSS/Dahua installation. Microsoft VC runtimes should be installed in the Wine prefix from official Microsoft redistributables (do not commit them here).

The worker opens one authenticated P2P session and maps multiple device ports, for example:

```
wine /usr/local/lib/p2p_relay_multi.exe --serial AK... --user admin --password '...' --dll-dir Z:\vendor --map 554:15540 --map 80:16540
```

It prints `READY` only after each requested channel is connected and then emits `ALIVE` health lines.