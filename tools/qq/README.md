# Private QQ session launcher

`Start-QQSession.ps1` launches only `python -m harness.qq.session`. It does not
start the standard Runtime, capture a screen, run a speech decision, send a QQ
message, or stop any existing process.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Projects\NeuralCloud\tools\qq\Start-QQSession.ps1
```

Defaults:

- Configuration: `%LOCALAPPDATA%\NeuralCloud\QQDesktop\session.json`.
- Loopback port: `8789`.
- Python: the launcher's repository `.venv\Scripts\python.exe`.
- Child working directory: that repository root.
- Logs: unique stdout/stderr files below the existing private root's `logs` folder.

Optional arguments are `-ConfigPath <absolute-path>` and `-Port <1024..65535>`.
The configuration, its `token_file`, and its `memory_db` must already exist
inside `%LOCALAPPDATA%\NeuralCloud\QQDesktop`. The launcher rejects paths outside
that root and junction/symlink components between LOCALAPPDATA and each file.
The operator prepares the root and files with private permissions beforehand;
this script does not create tokens, initialize databases, or change ACLs.

Startup authenticates `GET /v1/status` and compares service name, healthy status,
persistent database path, allowed groups, self account and decision-enabled flag.
It checks the automatic activity fields are booleans and standard Runtime startup
is disabled. Automatic activity may be reported from a separate authenticated,
scoped watcher heartbeat. These are the startup identity checks; this
is not proof of model reachability or a model invocation.

A matching listener returns `already_running`. An occupied port without matching
authenticated status returns an error and is left untouched. Otherwise one hidden
process starts, with authentication health polling bounded to 20 seconds. A local
named mutex prevents competing launchers from starting the same port concurrently.
No credentials appear in command arguments or launcher JSON output; requests go
directly to loopback without proxies or redirects. Parent environment variables
and working directory are not modified.

The JSON result contains status, exit code, port, safe reason, `started_at`, and
private log paths. `launch_pid` identifies the process created by this invocation;
`listener_pid` identifies the current port owner when available. Python's Windows
venv launcher can make these IDs different. Existing instances have no
`launch_pid`. Neither PID is automatically terminated, including after a timeout.
The operator must resolve a failed or late startup before retrying; do not stop
an unrelated port occupant. No stop script is supplied.

Exit codes: `0` for started/already running, `10` for another launcher holding
the startup lock, and `20` for configuration, port conflict, startup or health
failure. Private configuration parse errors are deliberately not echoed.

`tests/test_qq_session_launcher.py` exercises native Windows PowerShell 5.1 using
synthetic private files and mocked HTTP/process effects. It never starts the real
session or calls a model.

The authorized desktop loop has separate `Start-QQWatcher.ps1` and
`Stop-QQWatcher.ps1` entry points. See [watcher operation](../../docs/QQ_WATCHER.md)
for private configuration, startup baseline, evidence checks and pause behavior.
