"""Local CharacterRM judge. Lazy-loads torch; missing weights are a skip, not a crash."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eval.rp.paths import CHARRM_DIR
from eval.rp.schema import RPEpisode

_HF_ID = "morecry/BaichuanCharRM"


class CharacterRMJudge:
    def __init__(self, model_path: Path | str | None = None) -> None:
        self.model_path = Path(model_path) if model_path else CHARRM_DIR
        self.available = False
        self.unavailable_reason = ""
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None

    def ensure(self) -> bool:
        if self.available:
            return True
        if self.unavailable_reason and self._model is None:
            return False
        if not self.model_path.exists():
            self.unavailable_reason = (
                f"未找到 CharacterRM 权重（{self.model_path}）。"
                f"请下载 {_HF_ID} 到该目录。评测继续，但不打 13 维分。"
            )
            return False
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError:
            self.unavailable_reason = "未安装 torch/transformers。pip install -e \".[eval]\""
            return False
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_path), trust_remote_code=True)
            self._model = AutoModel.from_pretrained(str(self.model_path), trust_remote_code=True)
            self._model.eval()
            self._torch = torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model.to(device)
            self.available = True
            self.unavailable_reason = ""
            return True
        except Exception as exc:  # noqa: BLE001 — judge must not fail the run
            self.unavailable_reason = f"CharacterRM 加载失败：{exc}"
            self._model = None
            return False

    def score_episode(self, episode: RPEpisode, response: str) -> dict[str, float]:
        if not self.ensure():
            return {}
        metrics = episode.meta.get("metrics") or []
        profile = str(episode.meta.get("profile_text") or episode.profile)
        context = str(episode.meta.get("context") or _rebuild_context(episode, response))
        clipped = response.split("\n")[0].strip() or response.strip()
        scores: dict[str, float] = {}
        values: list[float] = []
        for item in metrics:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                metric_en, metric_zh = str(item[0]), str(item[1])
            else:
                continue
            value = self._score_one(profile, context, clipped, metric_zh)
            scores[metric_en] = value
            values.append(value)
        if values:
            scores["character_rm_mean"] = sum(values) / len(values)
        return scores

    def _score_one(self, profile: str, context: str, response: str, metric_zh: str) -> float:
        text = (
            "角色信息：\n\n"
            f"{profile}\n\n对话：\n\n{context}\n\n回复：\n\n{response}\n\n评分标准：\n\n{metric_zh}"
        )
        tokenizer = self._tokenizer
        torch = self._torch
        encoded = tokenizer.encode(text=text, add_special_tokens=False)
        eos = tokenizer.eos_token_id
        if eos is not None:
            encoded = encoded + [eos]
        encoded = encoded[-4096:]
        device = next(self._model.parameters()).device
        input_ids = torch.tensor(encoded, device=device).unsqueeze(0)
        with torch.no_grad():
            out = self._model(input_ids=input_ids)
        raw = _extract_score(out)
        if 0.0 <= raw <= 1.0:
            return raw * 4.0 + 1.0
        return raw


def _extract_score(out: Any) -> float:
    tensor = None
    if isinstance(out, (tuple, list)) and len(out) > 1:
        tensor = out[1]
    elif hasattr(out, "logits"):
        tensor = out.logits
    elif hasattr(out, "score"):
        tensor = out.score
    else:
        tensor = out
    if hasattr(tensor, "detach"):
        tensor = tensor.detach().float().reshape(-1)[0]
    if hasattr(tensor, "item"):
        return float(tensor.item())
    return float(tensor)


def _rebuild_context(episode: RPEpisode, response: str) -> str:
    lines = [f"{turn.speaker}：{turn.text}" for turn in episode.history]
    lines.append(episode.query)
    lines.append(f"{episode.role_id}：{response}")
    return "\n".join(lines)
