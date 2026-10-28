"""Portable command line. CPU commands do not import model dependencies."""
from __future__ import annotations

import argparse
import json
import sys

from .checkpoints import write_json
from .config import EncoderConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description="Qwen3.8 native memory encoder")
    parser.add_argument("--config", help="EncoderConfig JSON; defaults are the approved 27B architecture")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="inspect environment without downloading weights")
    doctor.add_argument("--out")
    prepare = commands.add_parser("prepare-data", help="write deterministic synthetic train/validation/test and timeline")
    prepare.add_argument("--out", required=True)
    profile = commands.add_parser("profile", help="run real GPU forward/backward and cache/reload feasibility gates")
    profile.add_argument("--out", required=True)
    train = commands.add_parser("train", help="train adapters; requires a matching passing GPU profile")
    train.add_argument("--data", required=True)
    train.add_argument("--out", required=True)
    train.add_argument("--profile", required=True)
    train.add_argument("--resume")
    train.add_argument("--arm", choices=("native", "text"), default="native")
    evaluate = commands.add_parser("evaluate", help="evaluate native and frozen baselines; optional trained text LoRA")
    evaluate.add_argument("--data", required=True)
    evaluate.add_argument("--checkpoint", required=True)
    evaluate.add_argument("--text-lora")
    evaluate.add_argument("--out", required=True)
    serve = commands.add_parser("serve", help="serve an accepted paired checkpoint; production routing unchanged")
    serve.add_argument("--service-config", required=True)
    serve.add_argument("--checkpoint", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8091)
    replay = commands.add_parser("replay", help="replay synthetic timeline against isolated NeuralCloud storage")
    replay.add_argument("--data", required=True)
    replay.add_argument("--out", required=True)
    source = commands.add_parser("source-check", help="real source and generation checks on explicitly prepared test stores")
    source.add_argument("--service-config", required=True)
    source.add_argument("--cases", required=True)
    source.add_argument("--checkpoint", required=True)
    source.add_argument("--out", required=True)
    accept = commands.add_parser("accept", help="bind all passing reports to a trained checkpoint")
    for name in ("checkpoint", "profile", "evaluation", "sources", "replay"):
        accept.add_argument("--" + name, required=True)
    bundle = commands.add_parser("bundle", help="export a code/docs-only allowlisted handoff zip")
    bundle.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    config = EncoderConfig.read(args.config)
    try:
        if args.command == "doctor":
            from .experiments import doctor as inspect
            result = inspect()
            if args.out:
                write_json(args.out, result)
        elif args.command == "prepare-data":
            from .data import prepare_data
            result = prepare_data(args.out, seed=config.seed)
        elif args.command == "profile":
            from .experiments import profile as run_profile
            result = run_profile(config, args.out)
        elif args.command == "train":
            from .experiments import train as run_train
            result = run_train(config, args.data, args.out, args.profile, resume=args.resume, arm=args.arm)
        elif args.command == "evaluate":
            from .experiments import evaluate as run_evaluation
            result = run_evaluation(config, args.data, args.checkpoint, args.out, lora_checkpoint=args.text_lora)
        elif args.command == "serve":
            import uvicorn
            from .service import configured_app
            uvicorn.run(configured_app(args.service_config, args.checkpoint, config), host=args.host, port=args.port)
            return 0
        elif args.command == "replay":
            from .validation import replay_timeline
            result = replay_timeline(args.data, args.out)
        elif args.command == "source-check":
            from .validation import check_sources
            result = check_sources(config, args.service_config, args.cases, args.checkpoint, args.out)
        elif args.command == "accept":
            from .validation import accept_release
            result = accept_release(args.checkpoint, args.profile, args.evaluation, args.sources, args.replay)
        else:
            from .validation import export_bundle
            result = export_bundle(args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("passed") is False or result.get("accepted") is False else 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
