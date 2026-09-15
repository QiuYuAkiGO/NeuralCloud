# Validator tokenizer asset

`qwen3-4b-instruct-2507-tokenizer.json` is the official tokenizer from
[Qwen/Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/resolve/main/tokenizer.json).

SHA-256: `aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4`.

The unmodified asset is distributed under the upstream Apache License 2.0.
Its [original license](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/LICENSE)
is included as `qwen3-4b-instruct-2507-LICENSE.txt` beside this README.

The fixed two-message renderer in `validation_tokens.py` corresponds to the
[official tokenizer configuration](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/resolve/main/tokenizer_config.json)
(SHA-256 `a62ff0a2472a0fa1b8eaabcb57c59b58afa42a22831dc141400b6e0cf2b65ce3`).
It supports system/user text messages followed by the assistant generation
marker, with no tools, BOS, or thinking prefix. It is enabled only for the exact
deployed Ollama model ID `qwen3:4b-instruct-2507-q4_K_M`.

The tokenizer and renderer reproduced both recorded provider input counts
(51,794 and 47,679 tokens) without any adjustments. Model lookup and downloads
are deployment work; they never occur while counting a validator request.
