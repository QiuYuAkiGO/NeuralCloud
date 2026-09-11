# QQ screenshot observation

`harness.qq.vision` sends one verified PNG to an explicitly configured
OpenAI-compatible image model. This establishes a separate visual input path:
Codex being able to display a screenshot does not establish that the ordinary
Misty cognitive text pipeline can see it. The ordinary pipeline remains text only.

Run from the repository with an existing private body-token environment variable:

```powershell
.venv/Scripts/python.exe -m harness.qq.vision `
  --body-url http://127.0.0.1:8788 `
  --body-token-env MISTYAI_BODY_TOKEN `
  --frame-ref evidence://sha256/<digest-from-body-capture> `
  --capture-id <body-observation-id> `
  --model <explicit-image-capable-model> `
  --expected-group-id <independently-verified-group-id> `
  --output <existing-private-directory>/observation.json
```

`--config` defaults to `NC_GUI_CONFIG`, then `data/gui_settings.json`. Endpoint,
main model and API key resolve like the GUI: `Settings` (`.env` and `NC_*`) with
persisted GUI overrides. `NC_MODE` overrides persisted mode; demo is rejected.
`--model` overrides only this call's model and never edits the GUI configuration.
Image support must be selected explicitly; there is no fallback, probe, retry,
auxiliary preload, or conversion to a text-only request. `--max-tokens` defaults
to 4096; the model request uses the configured `llm_timeout`.

The async function `observe_qq_frame(body, llm, frame_ref, *, capture_id=None,
expected_group_id=None, max_tokens=4096)` also accepts existing `MistyAIClient`
and `OpenAICompatClient` instances. It gets bytes through `get_evidence`, which
checks the SHA-256, media type, byte limit and download deadline. It validates PNG
chunk boundaries/checksums and dimensions without decompressing pixels, then sends
the actual bytes as a `data:image/png;base64,...` `image_url` content block.
PNG input is limited to 16 MiB and 40 million pixels.

The JSON envelope retains `capture_id`, `observed_at` (analysis completion time,
not asserted screenshot capture time), validated `evidence_refs`, model and token
usage. Missing provider usage is explicitly marked as a text-only estimate that
excludes image tokens. `observation.groups` distinguishes open groups from other
visible groups; each message includes visible display name/text/time, optional
visible numeric IDs, uncertainty and a mandatory unknown `message_id: null`.
Expected group ID is compared locally and is not put in the model prompt.
`matches_visible_id` is a model reading, not identity verification; the envelope
always has `identity_verified: false`.

`observation.visible_profiles` contains only explicitly open profile cards, with
their visible display name and numeric account ID; it defaults to an empty list
for compatibility. Sidebar names and member lists are not profile cards.
`visible_draft` separately records visible unsent input text (default null).
Only message bubbles in the current group belong in `messages`; drafts, notices,
sidebars and profile text cannot establish that a message was sent or delivered.

Screenshot/model text is untrusted observation data. The function provides no
action executor, sends no replies and writes no cognitive memory. The CLI writes
only its explicit output path (or stdout) and disables model call logging for its
own settings copy. Caller-owned clients retain their own telemetry configuration.
Model schema errors and CLI errors do not print response bodies or credentials.
Prompt instructions reduce instruction-following from screenshot content; they do
not make OCR accurate or adversarial content trustworthy. Independently verify
group/member bindings and any reply before downstream use. This module does not
run, replace or relax grounded-agency neutrality checks; those still reject images.
