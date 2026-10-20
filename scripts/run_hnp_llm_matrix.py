"""EC-HNP-02 Phase 1: paired A/B matrix with an LLM validator and a fictitious sink.

Arm A: scripted buyer payload + backend controls. Arm B: the same + an LLM validator. No
checkout, payment or merchant is reachable from either arm; nothing here is an operation.

--dry-run uses FakeTransport and opens no connection. A provider is called only when every
gate holds: no --dry-run, HNP_LIVE=1, OPENAI_API_KEY, validator prices, and HNP_MAX_COST_USD
covering the upper-bound estimate printed before anything runs.
"""

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from commerce_agent.governance.canonical import sha256_digest
from commerce_agent.hnp_experiment.evaluation import load_oracles
from commerce_agent.hnp_experiment.evidence import (
    ExperimentalEvidence,
    load_entries,
    verify_entries,
)
from commerce_agent.hnp_experiment.harness import load_cases
from commerce_agent.hnp_experiment.llm.config import HnpSettings
from commerce_agent.hnp_experiment.llm.cost import estimate
from commerce_agent.hnp_experiment.llm.matrix import (
    PlannedEpisode,
    business_cases,
    review_inputs,
    run_matrix,
)
from commerce_agent.hnp_experiment.llm.prompts import PROMPT_DIRECTORY, load_prompt
from commerce_agent.hnp_experiment.llm.scoring import build_summary
from commerce_agent.hnp_experiment.llm.trace import TraceWriter
from commerce_agent.hnp_experiment.llm.transport import (
    BudgetedTransport,
    FakeTransport,
    OpenAIResponsesTransport,
    StructuredLLM,
)
from commerce_agent.hnp_experiment.llm.validator import (
    LlmValidatorAgent,
    dry_run_responder,
    review_input,
    sample_decision,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/commerce_agent/hnp_experiment"
REFUSED = 2


def resolve(path: Path) -> Path:
    return (path if path.is_absolute() else ROOT / path).resolve()


def load_settings(env_file: Path) -> HnpSettings | None:
    try:
        return HnpSettings.load(env_file)
    except ValidationError as error:
        names = sorted({str(e["loc"][0]).upper() for e in error.errors() if e["loc"]})
        print(
            f"Invalid or missing HNP settings in {env_file} or the environment: "
            + ", ".join(n if n == "OPENAI_API_KEY" else f"HNP_{n}" for n in names)
            + ". See the HNP block in .env.example (a dry run can use --env-file .env.example).",
            file=sys.stderr,
        )
        return None


def implementation_hashes() -> dict[str, str]:
    files = sorted([*PACKAGE.rglob("*.py"), *PROMPT_DIRECTORY.glob("*.md")])
    return {str(p.relative_to(PACKAGE)): sha256_digest(p.read_bytes()) for p in files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="use FakeTransport; never call a provider"
    )
    parser.add_argument(
        "--estimate-only", action="store_true", help="print the plan and cost estimate, then exit"
    )
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".env", help="file with the HNP_* settings"
    )
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="abort when arm A or backend results differ from the oracle (harness fault)",
    )
    args = parser.parse_args(argv)
    settings = load_settings(args.env_file)
    if settings is None:
        return REFUSED
    cases = load_cases(resolve(settings.cases_path))
    oracles = load_oracles(resolve(settings.oracle_path))
    prompt = load_prompt(settings.validator_prompt)
    selected, excluded = business_cases(cases)
    plan = estimate(
        review_inputs(cases, review_input, settings.repetitions),
        instructions=prompt.text,
        sample_output=sample_decision(),
        max_output_tokens=settings.max_output_tokens,
        prices=settings.validator_prices,
    )
    mode = (
        "estimate only (no calls)"
        if args.estimate_only
        else "dry-run (FakeTransport, no provider)"
        if args.dry_run
        else "LIVE provider calls"
    )
    print(f"EC-HNP-02 Phase 1 - {mode}; fictitious sink only")
    print(
        f"Business cases: {len(selected)} of {len(cases)} fixtures; excluded scripted "
        f"validator-fault fixtures: {', '.join(excluded)}"
    )
    print(
        f"Plan: {len(selected)} cases x {settings.repetitions} repetitions x arms A/B = "
        f"{2 * len(selected) * settings.repetitions} episodes; seed {settings.seed}"
    )
    print(
        f"Validator: {settings.validator_model} ({prompt.name} {prompt.sha256[:19]}); "
        f"cost estimate at configured prices: {plan.describe()}"
    )
    if args.estimate_only:
        return 0

    transport: StructuredLLM
    live = not args.dry_run
    if live:
        blockers = list(settings.live_blockers())
        if plan.max_usd > settings.max_cost_usd:
            blockers.append(
                f"upper-bound estimate USD {plan.max_usd:.4f} exceeds "
                f"HNP_MAX_COST_USD {settings.max_cost_usd}"
            )
        if blockers or settings.openai_api_key is None:
            print("Live run refused: " + "; ".join(blockers), file=sys.stderr)
            return REFUSED
        provider = OpenAIResponsesTransport(api_key=settings.openai_api_key)
        transport = BudgetedTransport(
            provider, prices=settings.validator_prices, max_usd=settings.max_cost_usd
        )
    else:
        provider = None
        transport = FakeTransport(dry_run_responder(settings.seed))

    output_root = resolve(settings.output_dir)
    if not output_root.is_relative_to((ROOT / ".local").resolve()):
        print("HNP_OUTPUT_DIR must stay under this worktree's .local directory", file=sys.stderr)
        return REFUSED
    kind = "llm_live" if live else "llm_dry_run"
    created = datetime.now(UTC)
    experiment_id = f"hnp02-{kind.removeprefix('llm_')}-{created:%Y%m%dT%H%M%SZ}-s{settings.seed}"
    directory = output_root / experiment_id
    directory.mkdir(parents=True, exist_ok=False)
    evidence = ExperimentalEvidence(directory)
    traces = TraceWriter(directory / "traces.jsonl")

    def validator(episode: PlannedEpisode) -> LlmValidatorAgent:
        return LlmValidatorAgent(
            transport=transport,
            model=settings.validator_model,
            prompt=prompt,
            temperature=settings.temperature,
            reasoning_effort=settings.reasoning_effort,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            prices=settings.validator_prices,
            seed=settings.seed,
            call_prefix=f"{experiment_id}:{episode.episode_id}",
        )

    try:
        run = run_matrix(
            cases=cases,
            repetitions=settings.repetitions,
            seed=settings.seed,
            experiment_id=experiment_id,
            execution_kind=kind,
            validator_factory=validator,
            validator_budget_ns=int(settings.timeout_seconds * 1_000_000_000),
            drain_seconds=settings.timeout_seconds,
            evidence=evidence,
            traces=traces,
        )
    finally:
        if provider is not None:
            provider.close()
    verify_entries(evidence.entries)
    if evidence.path is None or load_entries(evidence.path) != evidence.entries:
        raise ValueError("Durable evidence readback mismatch")
    summary = build_summary(
        run,
        oracles,
        strict=args.strict,
        experiment={
            "phase": "1: LLM validator, scripted buyer payloads",
            "created_at": created.isoformat(),
            "settings": settings.public(),
            "prompts": {"validator": {"name": prompt.name, "sha256": prompt.sha256}},
            "cost_estimate": {
                "calls": plan.calls,
                "input_tokens": plan.input_tokens,
                "min_usd": str(plan.min_usd),
                "max_usd": str(plan.max_usd),
            },
            "corpus_sha256": {
                "cases": sha256_digest(resolve(settings.cases_path).read_bytes()),
                "oracle": sha256_digest(resolve(settings.oracle_path).read_bytes()),
            },
            "implementation_sha256": implementation_hashes(),
            "environment": {"platform": platform.platform(), "python": sys.version},
        },
    )
    (directory / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print_digest(summary, directory)
    return 0


def print_digest(summary: dict, directory: Path) -> None:
    def ratio(metric: dict) -> str:
        rate = "n/a" if metric["rate"] is None else f"{metric['rate']:.2f}"
        low = "-" if metric["low"] is None else f"{metric['low']:.2f}"
        high = "-" if metric["high"] is None else f"{metric['high']:.2f}"
        return f"{metric['k']}/{metric['n']} ({rate}, 95% CI {low}-{high})"

    for arm, metrics in summary["arms"].items():
        line = (
            f"Arm {arm}: effects {metrics['simulated_effects']}, escapes "
            f"{ratio(metrics['escapes'])}, false blocks {ratio(metrics['false_blocks'])}, "
            f"infrastructure errors {metrics['infrastructure_errors']}"
        )
        print(line)
    validator = summary["arms"]["B"]["validator"]
    print(
        f"Validator: captures {ratio(validator['captures'])}, unique captures "
        f"{validator['unique_captures']}, false rejections {ratio(validator['false_rejections'])}, "
        f"invalid outputs {ratio(validator['invalid_outputs'])}, timeouts "
        f"{ratio(validator['timeouts'])}"
    )
    paired = summary["comparison"]
    print(
        f"McNemar A vs B: all p={paired['all']['mcnemar_exact_p']:.3g} "
        f"(only B blocked {paired['all']['only_b_blocked']}, only A blocked "
        f"{paired['all']['only_a_blocked']}); admissible p="
        f"{paired['admissible']['mcnemar_exact_p']:.3g}; unsafe p="
        f"{paired['unsafe']['mcnemar_exact_p']:.3g}"
    )
    cost = summary["cost"]
    print(
        f"Calls: {cost['llm_calls']} ({cost['provider_calls']} provider); tokens in/out "
        f"{cost['tokens']['input_tokens']}/{cost['tokens']['output_tokens']}; USD "
        f"{cost['cost_usd']:.4f} ({cost['basis']})"
    )
    print(f"Wrote {directory / 'summary.json'}, traces.jsonl and evidence.jsonl")


if __name__ == "__main__":
    sys.exit(main())
