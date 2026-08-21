from eval.rp.judges.accuracy import extract_choice, score_accuracy, score_timechara
from eval.rp.judges.character_rm import CharacterRMJudge
from eval.rp.schema import RPEpisode, RPSample


def apply_judges(
    episode: RPEpisode,
    sample: RPSample,
    *,
    character_rm: CharacterRMJudge | None = None,
) -> RPSample:
    scores: dict[str, float] = dict(sample.scores)
    skipped = list(sample.skipped_scores)
    if episode.bench == "roleeval" or episode.choices:
        scored = score_accuracy(episode, sample.response)
        if scored:
            scores.update(scored)
        elif episode.answer is None:
            skipped.append("accuracy")
    if episode.bench == "timechara":
        scores.update(score_timechara(episode, sample))
    if episode.bench == "charactereval":
        if character_rm is None:
            character_rm = CharacterRMJudge()
        if character_rm.ensure():
            scores.update(character_rm.score_episode(episode, sample.response))
        else:
            skipped.append("character_rm")
            sample.meta["judge_note"] = character_rm.unavailable_reason
    sample.scores = scores
    sample.skipped_scores = skipped
    return sample


__all__ = ["CharacterRMJudge", "apply_judges", "extract_choice", "score_accuracy", "score_timechara"]
