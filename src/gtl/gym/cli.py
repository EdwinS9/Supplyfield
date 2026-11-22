"""`gtl gym run | batch | baseline` (docs/gym.md section 8)."""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from gtl.config import load_env, load_tenant
from gtl.gym.diagnose import CAUSES, causes_table, episode_report
from gtl.gym.runner import play_episode, run_episode
from gtl.gym.scenario import Scenario, generate, soony_real
from gtl.gym.score import Result

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BASE = ROOT / "tests" / "fixtures" / "soony_base.json"


def parse_seeds(text: str) -> list[int]:
    """`3` or `1-8` (inclusive)."""
    lo, _, hi = text.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


def load_playbooks(directory: str | None) -> dict[str, str]:
    if not directory:
        return {}
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(Path(directory).glob("*.md"))}


def _scenario(spec: dict, tenant: dict) -> Scenario:
    base = json.loads(Path(spec["base"]).read_text(encoding="utf-8"))
    if spec["scenario"] == "soony_real":
        return soony_real(base)
    return generate(base, int(spec["scenario"]), spec["split"], tenant=tenant, tier=spec["tier"])


def run_one(spec: dict) -> dict:
    """One episode from plain data, so it can run in a worker process. Returns the Result as a dict."""
    from gtl.gym.baseline import baseline_agents

    tenant = load_tenant(spec["tenant"], root=str(ROOT / "tenants"))
    llm = None
    if spec["llm"] == "real":
        from gtl.llm import LLMClient
        load_env()
        llm = LLMClient()
    agents = baseline_agents() if spec["agents"] == "baseline" else None
    result = run_episode(_scenario(spec, tenant), tenant, playbooks=load_playbooks(spec["playbooks"]), llm=llm,
                         agents=agents, trace=spec["trace"])
    return dataclasses.asdict(result)


def diagnose_one(spec: dict) -> dict:
    """One episode with its diagnosis, from plain data so it can run in a worker process."""
    from gtl.gym.baseline import baseline_agents

    tenant = load_tenant(spec["tenant"], root=str(ROOT / "tenants"))
    agents = baseline_agents() if spec["agents"] == "baseline" else None
    scenario = _scenario(spec, tenant)
    result, board, _ = play_episode(scenario, tenant, playbooks=load_playbooks(spec["playbooks"]), agents=agents)
    return episode_report(result, scenario, board)


def _spec(args: argparse.Namespace, scenario: str, agents: str | None = None, trace: bool = False) -> dict:
    return {"scenario": scenario, "split": getattr(args, "split", "train"), "tenant": args.tenant,
            "base": args.base, "playbooks": getattr(args, "playbooks", None), "llm": args.llm,
            "agents": agents or getattr(args, "agents", "default"), "trace": trace,
            "tier": getattr(args, "tier", "base")}


def _run_many_with(fn: Any, specs: list[dict], workers: int) -> list[dict]:
    if workers <= 1 or len(specs) == 1:
        return [fn(s) for s in specs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, specs))


def _run_many(specs: list[dict], workers: int) -> list[dict]:
    return _run_many_with(run_one, specs, workers)


def _money(x: float) -> str:
    return f"{x:,.0f} EUR"


def cmd_run(args: argparse.Namespace) -> int:
    res = run_one(_spec(args, args.scenario, trace=bool(args.trace)))
    bd = res["breakdown"]
    print(f"scenario {args.scenario}, agents {args.agents}")
    for product, copies in sorted(bd["packed"].items()):
        print(f"  packed {product}: {copies:,.0f} copies")
    print(f"reward     {_money(res['reward'])}")
    print(f"do-nothing {_money(res['do_nothing'])}")
    print(f"oracle     {_money(res['oracle'])}")
    print(f"score      {res['score']:.3f}")
    print("breakdown  " + ", ".join(f"{k} {v:,.0f}" for k, v in bd["terms"].items()))
    print(f"found {len(res['problems_found'])}, missed {len(res['problems_missed'])}: "
          + (", ".join(res["problems_missed"]) or "none"))
    if bd["agent_errors"]:
        print(f"agent errors: {bd['agent_errors']}")
    if args.trace:
        Path(args.trace).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    seeds = parse_seeds(args.seeds)
    results = _run_many([_spec(args, str(s)) for s in seeds], args.workers)
    for seed, res in zip(seeds, results):
        print(f"{args.split}-{seed}: score {res['score']:.3f}, reward {_money(res['reward'])}, "
              f"found {len(res['problems_found'])}/{len(res['problems_found']) + len(res['problems_missed'])}")
    print(f"mean score {statistics.fmean(r['score'] for r in results):.3f} over {len(results)} episodes")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    seeds = parse_seeds(args.seeds)
    print(f"{args.split} seeds {args.seeds}")
    print(f"{'do-nothing':<22}{0.0:.3f}")
    for label, agents in (("scripted baseline", "baseline"), ("default team (rules)", "default")):
        results = _run_many([_spec(args, str(s), agents) for s in seeds], args.workers)
        print(f"{label:<22}{statistics.fmean(r['score'] for r in results):.3f}")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from gtl.gym import evolve

    if args.generations is None and args.until is None:
        print("give --generations, --until, or both", file=sys.stderr)
        return 2
    tenant = load_tenant(args.tenant, root=str(ROOT / "tenants"))
    base = json.loads(Path(args.base).read_text(encoding="utf-8"))
    reflect_llm = None
    if args.llm == "real":
        load_env()
        from gtl.llm import LLMClient
        reflect_llm = LLMClient()
    out = evolve.train(
        args.run_id, population=args.population, batch=args.batch, generations=args.generations,
        until=args.until, val_seeds=parse_seeds(args.val_seeds), tenant=tenant, base=base,
        llm_factory=evolve.real_llm_factory if args.llm == "real" else None, reflect_llm=reflect_llm,
        workers=args.workers, out_dir=args.out_dir, resume=args.resume, playbooks_dir=str(ROOT / "playbooks"),
        tier=args.tier)
    print(f"run {out['run_id']}: {out['generations_done']} generations, best validation "
          f"{out['best_val_mean']:.3f} (generation {out['best_generation']}), files in {out['dir']}")
    return 0


def cmd_curve(args: argparse.Namespace) -> int:
    curve = json.loads((Path(args.out_dir) / args.run_id / "curve.json").read_text(encoding="utf-8"))
    ref = curve["reference"]
    print(f"run {curve['run_id']}")
    print(f"reference  do-nothing {ref['do_nothing']:.3f}  baseline {ref['baseline']:.3f}  "
          f"untrained {ref['untrained']:.3f}")
    if "untrained_reward_eur" in ref:
        print(f"reference EUR  baseline {ref['baseline_reward_eur']:.0f}  untrained {ref['untrained_reward_eur']:.0f}")
    for g in curve["generations"]:
        eur = f"  net {g['val_reward_eur']:.0f} EUR  gain {g['val_gain_eur']:+.0f} EUR" if "val_reward_eur" in g else ""
        print(f"gen {g['generation']:>3}  val {g['val_mean']:.3f}{eur}  train {g['train_mean']:.3f}  "
              f"best {g['best_member']}  violations {g['violations']}  fraud {g['fraud_eur']:.0f} EUR")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    from gtl.gym import evolve

    out = evolve.promote(args.run_id, args.out_dir, args.playbooks_dir)
    for name, diff in out["diffs"].items():
        print(diff or f"{name}: no change")
    if not out["promoted"]:
        print(f"refused: {out['reason']}")
        return 1
    print(f"promoted generation {out['generation']} (validation {out['val_mean']:.3f}) to {args.playbooks_dir}")


def _print_report(rep: dict) -> None:
    print(f"== {rep['scenario']}: do-nothing {_money(rep['do_nothing'])}, team {_money(rep['team'])}, "
          f"oracle {_money(rep['oracle'])}, score {rep['score']:.3f}, stake {_money(rep['stake_eur'])}")
    if rep["below_do_nothing"]:
        print(f"  BELOW DO-NOTHING by {_money(rep['below_do_nothing']['short_by_eur'])}: "
              f"{rep['below_do_nothing']['reason']}")
    print("  disruptions: " + ", ".join(rep["disruptions"]))
    print("  oracle remedies: " + (", ".join(rep["oracle_remedies"]) or "none"))
    print("  team actions:")
    for a in rep["actions"]:
        print(f"    hour {a['hour']:>5}  {a['kind']:<18} {a['status']:<15} {a['key']}")
    print("  misses:")
    for m in rep["misses"]:
        print(f"    {m['cause']:<13} {m['remedy'] or m['problem']}: {m['detail']}")
    if not rep["misses"]:
        print("    none")


def cmd_diagnose(args: argparse.Namespace) -> int:
    scenarios = [args.scenario] if args.scenario else [str(s) for s in parse_seeds(args.seeds)]
    reports = _run_many_with(diagnose_one, [_spec(args, s) for s in scenarios], args.workers)
    for rep in reports:
        _print_report(rep)
    misses = [m for rep in reports for m in rep["misses"]]
    table, costly = causes_table(misses), causes_table(misses, costly_only=True)
    stakes = sorted(rep["stake_eur"] for rep in reports)
    print(f"stake per episode: min {_money(stakes[0])}, median {_money(stakes[len(stakes) // 2])}, "
          f"max {_money(stakes[-1])}; mean score {statistics.fmean(rep['score'] for rep in reports):.3f}")
    print(f"causes over {len(reports)} episodes: {'all misses':>13}{'costly':>8}  (costly: the oracle uses the remedy)")
    for cause in CAUSES:
        print(f"  {cause:<13}{table[cause]:>25}{costly[cause]:>8}")
    if args.out:
        Path(args.out).write_text(json.dumps({"episodes": reports, "causes": table, "costly_causes": costly}, indent=2, default=str),
                                  encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gtl gym")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser, playbooks: bool = True) -> None:
        p.add_argument("--tenant", default="soony")
        p.add_argument("--base", default=str(DEFAULT_BASE))
        p.add_argument("--llm", choices=("none", "real"), default="none")
        p.add_argument("--tier", choices=("base", "hard"), default="base",
                       help="hard: rival offers from suppliers with different track records")
        if playbooks:
            p.add_argument("--playbooks", default=None)
            p.add_argument("--agents", choices=("default", "baseline"), default="default")

    run = sub.add_parser("run", help="one episode")
    run.add_argument("--scenario", default="soony_real", help="soony_real or a seed number")
    run.add_argument("--split", choices=("train", "val"), default="train")
    run.add_argument("--trace", default=None, help="write the result with the action trace to this file")
    common(run)
    run.set_defaults(fn=cmd_run)

    batch = sub.add_parser("batch", help="many episodes in a process pool")
    batch.add_argument("--split", choices=("train", "val"), required=True)
    batch.add_argument("--seeds", required=True, help="3 or 1-8")
    batch.add_argument("--workers", type=int, default=4)
    batch.add_argument("--out", default=None, help="write the results as a JSON list")
    common(batch)
    batch.set_defaults(fn=cmd_batch)

    base = sub.add_parser("baseline", help="do-nothing, scripted baseline and default team")
    base.add_argument("--split", choices=("train", "val"), default="val")
    base.add_argument("--seeds", required=True)
    base.add_argument("--workers", type=int, default=4)
    common(base, playbooks=False)
    base.set_defaults(fn=cmd_baseline)

    train = sub.add_parser("train", help="evolve the playbooks against the euro score")
    train.add_argument("--run-id", required=True)
    train.add_argument("--population", type=int, default=4)
    train.add_argument("--batch", type=int, default=4)
    train.add_argument("--generations", type=int, default=None)
    train.add_argument("--until", default=None, help="HH:MM in the tenant timezone, or an ISO datetime")
    train.add_argument("--workers", type=int, default=4)
    train.add_argument("--resume", action="store_true")
    train.add_argument("--val-seeds", default="1-8")
    train.add_argument("--out-dir", default=str(ROOT / "var" / "gym"))
    common(train, playbooks=False)
    train.set_defaults(fn=cmd_train)

    curve = sub.add_parser("curve", help="reference lines and validation score per generation")
    curve.add_argument("--run-id", required=True)
    curve.add_argument("--out-dir", default=str(ROOT / "var" / "gym"))
    curve.set_defaults(fn=cmd_curve)

    promote = sub.add_parser("promote", help="copy the best playbooks into playbooks/ when validation was clean")
    promote.add_argument("--run-id", required=True)
    promote.add_argument("--out-dir", default=str(ROOT / "var" / "gym"))
    promote.add_argument("--playbooks-dir", default=str(ROOT / "playbooks"))
    promote.set_defaults(fn=cmd_promote)
    diag = sub.add_parser("diagnose", help="why the team missed each problem, with one cause per miss")
    diag.add_argument("--scenario", default=None, help="soony_real; or use --split with --seeds")
    diag.add_argument("--split", choices=("train", "val"), default="val")
    diag.add_argument("--seeds", default="1-16", help="3 or 1-8, used without --scenario")
    diag.add_argument("--workers", type=int, default=4)
    diag.add_argument("--out", default=None, help="write the episodes and the cause table as JSON")
    common(diag)
    diag.set_defaults(fn=cmd_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
