"""The learning loop (docs/gym.md section 9): playbooks evolve against the euro score by reflection and population search."""
from __future__ import annotations

import difflib
import json
import os
import random
import re
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from gtl.contracts import EVOLVING_AGENTS

SECTIONS = ("Principles", "When to act alone", "When to escalate", "Checks", "Message style", "Lessons")
MAX_WORDS = 800
MAX_CHANGES = 3
TRACE_LIMIT = 40

REFLECT_SYSTEM = (
    "You improve the playbook of one agent in a supply chain team. The playbook is markdown and is added to "
    "the agent's instructions. You get the current playbook, the agent's worst episode (score, euro "
    "breakdown, problems found and missed, its actions, and the remedies a perfect-information oracle used) "
    "and a one-line summary of its best episode. Outcomes show what really happened after each commitment.\n"
    "Edit the playbook: add, sharpen or delete at most 3 rules so the agent would do better next time. "
    "Write GENERAL rules that hold for any scenario. Do not copy scenario facts such as ids, names, "
    "quantities or dates from the trace. Keep exactly these sections: " + ", ".join(SECTIONS) + ". "
    f"Stay under {MAX_WORDS} words.\n"
    'Answer with JSON only: {"playbook": "<the full new markdown>", "changes": ["<at most 3 short sentences>"]}.')

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {"playbook": {"type": "string"},
                   "changes": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_CHANGES}},
    "required": ["playbook", "changes"],
}

EvaluateFn = Callable[..., "Member"]


@dataclass
class Member:
    id: str
    playbooks: dict[str, str]
    parents: tuple[str, ...] = ()
    origin: str = "seed"  # seed | reflect | merge | survivor
    results: list[dict] = field(default_factory=list)
    mean_score: float = 0.0


def load_seed_playbooks(directory: str | Path = "playbooks") -> dict[str, str]:
    out: dict[str, str] = {}
    for agent in EVOLVING_AGENTS:
        path = Path(directory) / f"{agent}.md"
        if path.exists():
            out[agent] = path.read_text(encoding="utf-8")
    return out


def real_llm_factory() -> Any:
    """Builds the real client inside a worker process (zero arguments, so it pickles)."""
    from gtl.config import load_env
    from gtl.llm import LLMClient

    load_env()
    return LLMClient()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _failed(seed: int, error: str) -> dict:
    return {"seed": seed, "score": 0.0, "reward": 0.0, "breakdown": {}, "found": [], "missed": [],
            "trace": [], "remedies": [], "types": [], "llm_calls": {}, "error": error}


def _episode(job: tuple) -> dict:
    """One episode in a worker. Any exception becomes a do-nothing record with the error."""
    seed, split, tenant, base, playbooks, llm_factory, agents, tier = job
    try:
        from gtl.gym.baseline import baseline_agents
        from gtl.gym.runner import run_episode
        from gtl.gym.scenario import generate
        from gtl.gym.score import oracle

        scenario = generate(base, seed, split, tenant=tenant, tier=tier)
        llm = llm_factory() if llm_factory is not None else None
        res = run_episode(scenario, tenant, playbooks=playbooks, llm=llm, trace=True,
                          agents=baseline_agents() if agents == "baseline" else None)
        return {"seed": seed, "score": res.score, "reward": res.reward, "breakdown": res.breakdown,
                "found": res.problems_found, "missed": res.problems_missed, "trace": res.trace,
                "remedies": oracle(scenario, tenant).remedies_used,
                "types": sorted({d.type for d in scenario.disruptions}), "llm_calls": res.llm_calls}
    except Exception as exc:  # noqa: BLE001 - a failing episode scores like do-nothing and the run goes on
        return _failed(seed, f"{type(exc).__name__}: {exc}")


def evaluate(member: Member, seeds: list[int] | range, split: str, tenant: dict, base: dict, *,
             llm_factory: Callable[[], Any] | None, workers: int, agents: str = "default",
             tier: str = "base") -> Member:
    """The member played on each seed; a copy with its results and mean normalized score."""
    seeds = list(seeds)
    jobs = [(s, split, tenant, base, member.playbooks, llm_factory, agents, tier) for s in seeds]
    if workers <= 1 or len(jobs) <= 1:
        results = [_episode(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_episode, j) for j in jobs]
            results = []
            for seed, fut in zip(seeds, futures):
                try:
                    results.append(fut.result())
                except Exception as exc:  # noqa: BLE001 - a dead worker is a failed episode
                    results.append(_failed(seed, f"{type(exc).__name__}: {exc}"))
    mean = statistics.fmean(r["score"] for r in results) if results else 0.0
    return replace(member, results=results, mean_score=mean)


# ---------------------------------------------------------------------------
# Reflection, merge, selection
# ---------------------------------------------------------------------------
def _valid_playbook(text: Any) -> bool:
    if not isinstance(text, str) or len(text.split()) > MAX_WORDS:
        return False
    return all(re.search(rf"^#{{1,6}}\s*{re.escape(s)}\s*$", text, re.MULTILINE | re.IGNORECASE) for s in SECTIONS)


def _episode_view(result: dict, agent: str) -> dict:
    trace = [t for t in result.get("trace", []) if t.get("type") == "action" and t.get("by") == agent]
    view = {"score": result.get("score"), "breakdown": result.get("breakdown", {}).get("terms", {}),
            "problems_found": result.get("found", []), "problems_missed": result.get("missed", []),
            "actions": trace[:TRACE_LIMIT], "oracle_remedies": result.get("remedies", []),
            "outcomes": result.get("breakdown", {}).get("outcomes", [])}
    if agent == "resolver":  # the offers it accepted or passed over, with what it knew about each
        view["offers"] = [{k: t[k] for k in ("subject", "status", "net_value_eur", "cost_eur", "context")}
                          for t in result.get("trace", []) if t.get("type") == "option"][:TRACE_LIMIT]
    return view


def reflect(member: Member, llm: Any, *, rng: random.Random, member_id: str | None = None,
            generation: int = 0) -> tuple[Member, list[dict]]:
    """One reflection call per agent on the member's worst episode. A bad reply keeps the old playbook."""
    child = replace(member, id=member_id or f"{member.id}r", parents=(member.id,), origin="reflect",
                    playbooks=dict(member.playbooks), results=[], mean_score=0.0)
    usable = [r for r in member.results if not r.get("error")]
    if not usable:
        return child, []
    low = min(r["score"] for r in usable)
    worst = rng.choice([r for r in usable if r["score"] == low])
    best = max(usable, key=lambda r: r["score"])
    best_line = (f"best episode: score {best['score']:.2f}, found {len(best['found'])} of "
                 f"{len(best['found']) + len(best['missed'])} problems")
    lessons: list[dict] = []
    for agent, playbook in member.playbooks.items():
        user = json.dumps({"agent": agent, "playbook": playbook, "worst_episode": _episode_view(worst, agent),
                           "best_episode": best_line}, default=str)
        try:
            data = llm.complete("reflect", REFLECT_SYSTEM, user, schema=REFLECT_SCHEMA).data
            if not isinstance(data, dict) or not _valid_playbook(data.get("playbook")):
                continue
            changes = [str(c) for c in data.get("changes", [])][:MAX_CHANGES]
        except Exception:  # noqa: BLE001 - the reflect model may be down; the old playbook stays
            continue
        if data["playbook"].strip() == playbook.strip():
            continue
        child.playbooks[agent] = data["playbook"]
        lessons.append({"agent": agent, "changes": changes, "parent": member.id, "generation": generation})
    return child, lessons


def merge(a: Member, b: Member, *, rng: random.Random, member_id: str | None = None) -> Member:
    """Each agent from the better parent, except one random agent taken from the other."""
    better, other = (a, b) if a.mean_score >= b.mean_score else (b, a)
    agents = sorted(better.playbooks)
    swapped = rng.choice(agents) if agents else None
    playbooks = {ag: (other if ag == swapped else better).playbooks.get(ag, better.playbooks[ag]) for ag in agents}
    return Member(member_id or f"{a.id}+{b.id}", playbooks, (a.id, b.id), "merge")


def _type_means(member: Member) -> dict[str, float]:
    by_type: dict[str, list[float]] = {}
    for r in member.results:
        for t in r.get("types", []):
            by_type.setdefault(t, []).append(r["score"])
    return {t: statistics.fmean(v) for t, v in by_type.items()}


def select(population: list[Member], keep: int) -> list[Member]:
    """The best overall, the best per disruption type, then the next best overall, up to `keep`."""
    ranked = sorted(population, key=lambda m: -m.mean_score)  # stable: ties keep population order
    if not ranked or keep <= 0:
        return []
    chosen = [ranked[0]]
    means = {m.id: _type_means(m) for m in population}
    for t in sorted({t for m in means.values() for t in m}):
        holders = [m for m in ranked if t in means[m.id]]
        top = max(holders, key=lambda m: means[m.id][t])
        if top not in chosen:
            chosen.append(top)
    chosen = chosen[:keep]
    for m in ranked:
        if len(chosen) >= keep:
            break
        if m not in chosen:
            chosen.append(m)
    return chosen


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, data: Any) -> None:
    _write(path, json.dumps(data, indent=2, default=str))


def _member_dict(m: Member) -> dict:
    return asdict(m)


def _write_playbooks(directory: Path, playbooks: dict[str, str]) -> None:
    for agent, text in playbooks.items():
        _write(directory / f"{agent}.md", text)


def _publish_checkpoint(root: Path, commit: dict) -> None:
    """Replay a complete generation. Every write is a replacement, including the lessons log."""
    _write_json(root / "state.json", commit["state"])
    _write_playbooks(root / "best", commit["best_playbooks"])
    if commit["lesson_text"] is not None:
        _write(root / "lessons.jsonl", commit["lesson_text"])
    _write_json(root / "curve.json", commit["curve"])
    (root / "pending.json").unlink()


def _rng_state(rng: random.Random) -> list:
    version, internal, gauss = rng.getstate()
    return [version, list(internal), gauss]


def _set_rng_state(rng: random.Random, state: list) -> None:
    rng.setstate((state[0], tuple(state[1]), state[2]))


def _deadline(until: str | None, now: datetime) -> datetime | None:
    """`until` is HH:MM (next time it occurs, in the tenant timezone) or a full ISO datetime."""
    if not until:
        return None
    if re.fullmatch(r"\d{1,2}:\d{2}", until):
        hour, minute = (int(x) for x in until.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target if target > now else target + timedelta(days=1)
    when = datetime.fromisoformat(until)
    return when if when.tzinfo else when.replace(tzinfo=now.tzinfo)


def _safety(val_results: list[dict]) -> tuple[int, float]:
    violations = sum(int(r.get("breakdown", {}).get("violations", 0)) for r in val_results)
    fraud = sum(float(r.get("breakdown", {}).get("terms", {}).get("fraud", 0.0)) for r in val_results)
    return violations, fraud


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _reward_eur(results: list[dict]) -> float:
    """Mean reward in EUR over the episodes."""
    return _mean([float(r.get("reward", 0.0)) for r in results])


def _business(results: list[dict]) -> dict[str, float]:
    """Per-episode means of what the team cost: requests to people, supplier emails, euros committed, model calls."""
    def per_episode(get: Callable[[dict], float]) -> float:
        return _mean([float(get(r)) for r in results])

    return {
        "human_items": per_episode(lambda r: r.get("breakdown", {}).get("human_items", 0)),
        "emails": per_episode(lambda r: r.get("breakdown", {}).get("emails", 0)),
        "spend_eur": per_episode(lambda r: r.get("breakdown", {}).get("terms", {}).get("spend", 0.0)),
        "agent_calls_mean": per_episode(lambda r: sum(r.get("llm_calls", {}).values())),
    }


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def train(run_id: str, *, population: int = 4, batch: int = 4, generations: int | None = None,
          until: str | None = None, val_seeds: list[int] | range = range(1, 9), tenant: dict, base: dict,
          llm_factory: Callable[[], Any] | None, reflect_llm: Any = None, workers: int = 4,
          out_dir: str | Path = "var/gym", resume: bool = False, rng_seed: int = 0,
          playbooks_dir: str | Path = "playbooks", evaluate_fn: EvaluateFn = evaluate, tier: str = "base") -> dict:
    if generations is None and until is None:
        raise ValueError("give generations, until, or both")
    val_seeds = list(val_seeds)
    root = Path(out_dir) / run_id
    tz = ZoneInfo(tenant.get("timezone", "UTC"))
    deadline = _deadline(until, datetime.now(tz))
    rng = random.Random(rng_seed)
    seen: dict[str, Member] = {}  # identical playbooks on identical seeds are played once

    def ev(m: Member, seeds: list[int], split: str, agents: str = "default") -> Member:
        key = json.dumps([m.playbooks, seeds, split, agents, tier], sort_keys=True)
        if key not in seen:
            extra = {"tier": tier} if tier != "base" else {}  # a custom evaluate_fn need not know about tiers
            seen[key] = evaluate_fn(m, seeds, split, tenant, base, llm_factory=llm_factory, workers=workers,
                                    agents=agents, **extra)
        return replace(m, results=seen[key].results, mean_score=seen[key].mean_score)

    if resume and (root / "pending.json").exists():
        pending = json.loads((root / "pending.json").read_text(encoding="utf-8"))
        _publish_checkpoint(root, pending)
    if resume:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        if state.get("initializing"):
            if state["tier"] != tier:
                raise ValueError(f"run {run_id} was started on the {state['tier']} tier, not {tier}")
            resume = False  # killed before generation zero committed: repeat the initial evaluation
    if resume:
        curve = json.loads((root / "curve.json").read_text(encoding="utf-8"))
        if curve.get("tier", "base") != tier:
            raise ValueError(f"run {run_id} was started on the {curve.get('tier', 'base')} tier, not {tier}")
        pop = [Member(m["id"], m["playbooks"], tuple(m["parents"]), m["origin"]) for m in state["population"]]
        gen, best = state["generation"], state["best"]
        _set_rng_state(rng, state["rng"])
        ref = curve["reference"]
        if "untrained_reward_eur" not in ref:  # a curve written before the EUR columns: fill them in once
            seed_pb = load_seed_playbooks(playbooks_dir)
            ref["untrained_reward_eur"] = _reward_eur(ev(Member("untrained", dict(seed_pb)), val_seeds, "val").results)
            ref["baseline_reward_eur"] = _reward_eur(ev(Member("baseline", {}), val_seeds, "val", "baseline").results)
    else:
        _write_json(root / "state.json", {"initializing": True, "tier": tier})
        seed_pb = load_seed_playbooks(playbooks_dir)
        pop = [Member(f"g0m{i}", dict(seed_pb)) for i in range(population)]
        untrained = ev(Member("untrained", dict(seed_pb)), val_seeds, "val")
        scripted = ev(Member("baseline", {}), val_seeds, "val", "baseline")
        curve = {"run_id": run_id, "tier": tier, "generations": [],
                 "reference": {"do_nothing": 0.0, "baseline": scripted.mean_score, "untrained": untrained.mean_score,
                               "untrained_reward_eur": _reward_eur(untrained.results),
                               "baseline_reward_eur": _reward_eur(scripted.results)}}
        gen, best = 0, {"val_mean": None, "generation": None}
        _write_json(root / "curve.json", curve)

    while generations is None or gen < generations:
        seeds = [gen * batch + i for i in range(1, batch + 1)]
        evaluated = [ev(m, seeds, "train") for m in pop]
        survivors = select(evaluated, max(1, population // 2))
        top = survivors[0]
        validated = ev(top, val_seeds, "val")
        violations, fraud = _safety(validated.results)

        lessons: list[dict] = []
        nxt = [replace(s, origin="survivor", results=[], mean_score=0.0) for s in survivors]
        children: list[Member] = []
        while len(nxt) + len(children) < population:
            cid = f"g{gen + 1}m{len(nxt) + len(children)}"
            if len(survivors) >= 2 and len(children) >= len(survivors):
                a, b = rng.sample(survivors, 2)
                children.append(merge(a, b, rng=rng, member_id=cid))
                continue
            parent = survivors[len(children) % len(survivors)]
            if reflect_llm is None:
                children.append(Member(cid, dict(parent.playbooks), (parent.id,), "reflect"))
            else:
                child, new = reflect(parent, reflect_llm, rng=rng, member_id=cid, generation=gen + 1)
                children.append(child)
                lessons += new
        nxt += children

        val_reward = _reward_eur(validated.results)
        curve["generations"].append({
            "generation": gen, "val_mean": validated.mean_score,
            "val_reward_eur": val_reward, "val_gain_eur": val_reward - curve["reference"]["untrained_reward_eur"],
            **_business(validated.results),
            "val_scores": [r["score"] for r in validated.results],
            "train_mean": statistics.fmean(m.mean_score for m in evaluated), "train_best": top.mean_score,
            "best_member": top.id, "violations": violations, "fraud_eur": fraud,
            "agent_calls": sum(sum(r.get("llm_calls", {}).values()) for r in validated.results),
            "at": datetime.now(tz).isoformat(timespec="seconds")})
        best_playbooks = {}
        if best["val_mean"] is None or validated.mean_score > best["val_mean"]:
            best = {"val_mean": validated.mean_score, "generation": gen, "member": top.id}
            best_playbooks = top.playbooks

        _write_json(root / f"gen_{gen}.json", {"generation": gen, "members": [_member_dict(m) for m in evaluated]})
        for m in evaluated:
            _write_playbooks(root / "pop" / str(gen) / m.id, m.playbooks)
        lesson_text = None
        if lessons:
            path = root / "lessons.jsonl"
            previous = path.read_text(encoding="utf-8") if path.exists() else ""
            lesson_text = previous + "".join(json.dumps(x) + "\n" for x in lessons)
        gen += 1
        state = {"generation": gen, "best": best, "rng": _rng_state(rng),
                 "population": [{"id": m.id, "playbooks": m.playbooks, "parents": list(m.parents), "origin": m.origin}
                                for m in nxt]}
        commit = {"state": state, "curve": curve, "best_playbooks": best_playbooks, "lesson_text": lesson_text}
        _write_json(root / "pending.json", commit)
        _publish_checkpoint(root, commit)
        pop = nxt
        if deadline is not None and datetime.now(tz) >= deadline:
            break
    return {"run_id": run_id, "dir": str(root), "generations_done": gen, "best_val_mean": best["val_mean"],
            "best_generation": best["generation"], "reference": curve["reference"]}


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------
def promote(run_id: str, out_dir: str | Path = "var/gym", playbooks_dir: str | Path = "playbooks") -> dict:
    """Copy the best playbooks into `playbooks_dir`, only when the best generation was clean on validation."""
    root = Path(out_dir) / run_id
    curve = json.loads((root / "curve.json").read_text(encoding="utf-8"))
    gens = curve["generations"]
    if not gens:
        return {"promoted": False, "reason": "no generation finished", "diffs": {}}
    best = gens[0]
    for g in gens[1:]:
        if g["val_mean"] > best["val_mean"]:
            best = g
    if best["violations"] or best["fraud_eur"]:
        return {"promoted": False, "generation": best["generation"], "diffs": {},
                "reason": f"generation {best['generation']} had {best['violations']} violations and "
                          f"{best['fraud_eur']:.0f} EUR fraud on validation"}
    diffs: dict[str, str] = {}
    for src in sorted((root / "best").glob("*.md")):
        dst = Path(playbooks_dir) / src.name
        old = dst.read_text(encoding="utf-8") if dst.exists() else ""
        new = src.read_text(encoding="utf-8")
        diffs[src.stem] = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                       f"a/{src.name}", f"b/{src.name}"))
        _write(dst, new)
    return {"promoted": True, "generation": best["generation"], "val_mean": best["val_mean"], "diffs": diffs,
            "reason": ""}
