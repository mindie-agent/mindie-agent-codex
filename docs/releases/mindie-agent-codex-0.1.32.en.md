# mindie-agent-codex 0.1.32

[中文](mindie-agent-codex-0.1.32.md)

- **Headless Windows PowerShell 5.1 Hook UTF-8 & No-Popup Hardening (`native_entry.py`, `hooks/hooks.json`)**: Launches hook helpers through `System.Diagnostics.Process` (`CreateNoWindow=$true` with raw UTF-8 byte-stream I/O and `WindowsApps` stub filtering), eliminating child console flashes, PowerShell `ErrorRecord` wrapping, and OEM `CP936`/`CP437` mojibake when `CREATE_NO_WINDOW` leaves no console handle, with bounded 800-char `Stop` diagnostics and explicit Windows PowerShell 5.1 CI coverage.
- **WSL2 `/mnt/<drive>` Binary Shadowing Guard & Transport Classification Parity (`native_context.py`, `bootstrap.py`, `component_update.py`, `harness_update.py`)**: Prevents WSL2 `/mnt/<drive>` DrvFS shims and PE `.exe` binaries from shadowing Linux `codex`, `uv`, `gh`, and `mindie-*` CLIs, and classifies mid-stream `ssl.SSLError`, `SSLCertVerificationError`, `http.client.IncompleteRead`/`RemoteDisconnected`, and DNS errors as non-reportable transport/certificate failures while keeping local URL/state bugs reportable.
