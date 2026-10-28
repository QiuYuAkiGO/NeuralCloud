# Misty oral expression v2

This package adds one system-prompt section to the deployed DeepSeek Harness
plugin. It does not change persona, tools, model settings, scheduling, memory,
message aggregation or context compression. The six examples are fictional.

The actual running baseline is `resident-20261028-context-budget-v1`, not the
older `/opt/misty/current` symlink. `source.patch` targets that release's
`MistyAI/plugins/dsh-misty/src/index.mjs`. `oral-expression.mjs` belongs beside
that file. `hashes.json` pins the original plugin, patched plugin and policy.

The local MistyAI checkout lacks this deployed compatibility tree and has
unrelated work in progress. This package therefore records the exact deployed
patch and new module in NeuralCloud, following the existing deployment-evidence
layout, rather than overwriting that checkout.

## Reproduce

1. Obtain the pinned baseline plugin, verify its SHA-256 against `before`, then
   apply `source.patch` from the parent of `MistyAI`. Verify `after`.
2. Upload the patched `index.mjs`, `oral-expression.mjs`,
   `oral-expression.test.mjs`, `hashes.json` and `deploy.py` to
   `/tmp/misty-oral-v2` on the authorized host.
3. Run `python3 /tmp/misty-oral-v2/deploy.py stage` as root. This creates a new
   release, runs the full offline Harness suite and the no-external-action
   deployment gate. It refuses to overwrite an existing release.
4. Run `python3 /tmp/misty-oral-v2/deploy.py activate`. This checks the baseline,
   checkpoints the service, backs up state, installs a systemd override, and
   verifies READY plus unchanged session bindings. Failure restores the previous
   service override. No synthetic QQ messages are sent.

## Roll back

Stop `misty-harness.service`, move only
`/etc/systemd/system/misty-harness.service.d/60-oral-v2.conf` to the backup
directory, run `systemctl daemon-reload`, then start the service. The existing
`50-context-budget.conf` selects the previous release. Retain current state and
new messages; do not restore old databases over live data.

Human preference data is not packaged. This change was authorized for production
after exploratory reviews; it is not a statistically established winner. Future
review rounds remain capped at 30 nonduplicate cases.
