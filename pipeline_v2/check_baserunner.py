"""Accuracy checks for the baserunner model (plan.md §18). Rerun after any change to baserunner.py.

    python -m pipeline_v2.check_baserunner             # new model vs the old one, on the app's current data
    python -m pipeline_v2.check_baserunner --ghost     # extra innings per level, ghost runner on vs off
    python -m pipeline_v2.check_baserunner --data 'pipeline_v2/serving/pitches/**/*.parquet'

TrackMan's ``RunsScored`` is the yardstick (outs always come from TrackMan's ``Outs`` column):

  1. Home-run probe: a home run clears the bases, so RunsScored - 1 = the runners on base before it.
  2. Runs reconciliation: plays where the model's runs differ from RunsScored, by play type.
     "over" = the model scored a runner who didn't score; "under" = it had too few runners on base.
  3. Steal guess: on games WITH steal labels, score the guess as if the labels weren't there.

The old model's numbers come from the base states already stored in the data (what
``data_pipeline/baserunner_state.py`` wrote); they're skipped when that module or those columns
are missing, or when the data is v2 output (games/, serving/).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import polars as pl

from pipeline_v2 import baserunner as br
from pipeline_v2 import config

try:                                           # the old model, for the before/after comparison
    sys.path.insert(0, str(config.REPO / "data_pipeline"))
    import baserunner_state as old_br
except ImportError:
    old_br = None

OLD_STATE = ["on_1b", "on_2b", "on_3b"]


def play_kind(r: dict) -> str:
    pc, kb, pr = r["PitchCall"], r["KorBB"], r["PlayResult"]
    if kb == "Walk" or pc == "HitByPitch":
        return "Walk/HBP"
    if kb == "Strikeout":
        return "Strikeout"
    if pr == "Sacrifice":
        return "Sac fly" if r["TaggedHitType"] in br.SAC_FLY_TYPES else "Sac bunt"
    if pr in br.HIT_BASES and (r["OutsOnPlay"] or 0) > 0:
        return f"{pr} (outs on play)"
    if pr != "Undefined":
        return pr
    return "during an at-bat"


def old_runs(bases: set[int], r: dict, use_heuristic: bool) -> int:
    """The runs the OLD model scored on one pitch, from its stored state before the pitch (the old
    ``_apply_event`` with the runs kept)."""
    pc, kb, pr = r["PitchCall"], r["KorBB"], r["PlayResult"]
    oop, rs = r["OutsOnPlay"] or 0, r["RunsScored"] or 0
    mid_pa = pr == "Undefined" and kb == "Undefined" and pc != "HitByPitch"
    runs = 0
    if pr == "StolenBase":
        bases, n = old_br.apply_steal(bases, rs); runs += n
    elif pr == "CaughtStealing":
        bases, n = old_br.apply_caught_stealing(bases); runs += n
    elif mid_pa and use_heuristic and r["ThrowSpeed"] is not None and bases:
        bases, n = old_br.apply_caught_stealing(bases) if oop > 0 else old_br.apply_steal(bases, rs); runs += n
    elif mid_pa and rs > 0:
        bases, n = old_br._topoff(bases, 0, rs); runs += n
    if kb == "Walk" or pc == "HitByPitch":
        bases, n = old_br.apply_walk(bases); runs += n
    elif kb == "Strikeout":
        pass
    elif pr in old_br._HIT:
        bases, n = old_br.apply_hit(bases, old_br._HIT[pr], rs); runs += n
    elif pr == "Error":
        bases, n = old_br.apply_hit(bases, 1, rs); runs += n
    elif pr == "Sacrifice":
        bases, n = old_br.apply_sacrifice(bases, rs); runs += n
    elif pr == "FieldersChoice":
        bases, n = old_br.apply_fielders_choice(bases, oop, rs); runs += n
    elif pr == "Out":
        bases, n = old_br.apply_out(bases, oop, rs); runs += n
    return runs


def effect_truth(r: dict) -> str:
    if r["PlayResult"] == "StolenBase":
        return "advance"
    if r["PlayResult"] == "CaughtStealing" or (r["OutsOnPlay"] or 0) > 0:
        return "remove"
    return "nothing"


def check_game(args: tuple[pl.DataFrame, bool | None, bool]) -> Counter:
    """All the counts for one game: [model, check, key] -> n."""
    g, ghost, extras_only = args
    c: Counter = Counter()
    level = g["Level"][0]
    has_labels = g["PlayResult"].is_in(["StolenBase", "CaughtStealing"]).any()
    use_heuristic = not has_labels and g["ThrowSpeed"].is_not_null().any()
    new = br.annotate_game(g, ghost=ghost, keep_model_runs=True)
    old_ok = old_br is not None and all(f"old_{col}" in g.columns for col in OLD_STATE) and ghost is None
    for r in new.iter_rows(named=True):
        if extras_only and (r["Inning"] or 0) < br.EXTRA_INNING:
            continue
        rs = r["RunsScored"] or 0
        kind = play_kind(r)
        scope = level if extras_only else "all"
        models = [("new", r["on_1b"] + r["on_2b"] + r["on_3b"], r["model_runs"])]
        if old_ok:
            ob = {b for b, col in zip((1, 2, 3), ("old_on_1b", "old_on_2b", "old_on_3b")) if r[col]}
            models.append(("old", len(ob), old_runs(ob, r, use_heuristic)))
        is_play = kind != "during an at-bat" or rs > 0 or (r["ThrowSpeed"] is not None and r["on_1b"] + r["on_2b"] + r["on_3b"] > 0)
        for m, runners, runs in models:
            if r["PlayResult"] == "HomeRun":
                c[(m, "hr", scope, "total")] += 1
                c[(m, "hr", scope, "match")] += runners == rs - 1
            if is_play:
                c[(m, "runs", kind, "plays")] += 1
                c[(m, "runs", kind, "over")] += runs > rs
                c[(m, "runs", kind, "under")] += runs < rs
                c[(m, "runs", scope, "plays_all")] += 1
                c[(m, "runs", scope, "mismatch_all")] += runs != rs
        # Steal guess, scored on labeled games as if the labels weren't there.
        mid = r["KorBB"] == "Undefined" and r["PitchCall"] != "HitByPitch" and r["PlayResult"] in ("Undefined", "StolenBase", "CaughtStealing")
        if has_labels and not extras_only and mid and r["ThrowSpeed"] is not None and r["on_1b"] + r["on_2b"] + r["on_3b"] > 0:
            truth = effect_truth(r)
            out = (r["OutsOnPlay"] or 0) > 0
            target = br.throw_target(r["BasePositionX"], r["BasePositionZ"])
            old_guess = "remove" if out else "advance"
            new_guess = "remove" if out else ("nothing" if target == 1 else "advance")
            c[("steal", "throws")] += 1
            c[("steal", "old_right")] += old_guess == truth
            c[("steal", "new_right")] += new_guess == truth
    return c


def load(data: str) -> list[pl.DataFrame]:
    lf = pl.scan_parquet(data, include_file_paths="_file")
    cols = lf.collect_schema().names()
    old = [c for c in OLD_STATE if c in cols and "source_file" not in cols]   # v2 output holds the NEW model's states
    df = lf.select(br.COLUMNS + ["GameID", "Level", "_file"] + old).collect().rename({c: f"old_{c}" for c in old})
    return df.partition_by(["GameID", "_file"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Accuracy checks for the baserunner model")
    ap.add_argument("--data", default=str(config.PITCHES_DIR / "**" / "*.parquet"), help="parquet glob")
    ap.add_argument("--ghost", action="store_true", help="extra innings per level: ghost runner on vs off")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    games = load(a.data)
    with ProcessPoolExecutor(a.workers) as pool:
        if a.ghost:
            games = [g for g in games if (g["Inning"] >= br.EXTRA_INNING).any()]
            on = sum(pool.map(check_game, [(g, True, True) for g in games], chunksize=50), Counter())
            off = sum(pool.map(check_game, [(g, False, True) for g in games], chunksize=50), Counter())
            print(f"Extra innings only, {len(games):,} games. Ghost runner ON vs OFF for every level:")
            print(f"{'level':<26}{'home runs':>10}{'probe ON':>10}{'probe OFF':>10}{'plays':>8}{'runs mismatch ON':>18}{'OFF':>7}  in config")
            levels = sorted({k[2] for k in on if k[1] == "hr" or k[1] == "runs"} - {"all"} - set(br.HIT_BASES), key=lambda l: -on[("new", "runs", l, "plays_all")])
            for lv in levels:
                n = on[("new", "runs", lv, "plays_all")]
                if not n:
                    continue
                hr = on[("new", "hr", lv, "total")]
                pr = lambda c: f"{100 * c[('new', 'hr', lv, 'match')] / hr:.0f}%" if hr else "-"
                mm = lambda c: f"{100 * c[('new', 'runs', lv, 'mismatch_all')] / n:.1f}%"
                print(f"{lv:<26}{hr:>10}{pr(on):>10}{pr(off):>10}{n:>8}{mm(on):>18}{mm(off):>7}  {'on' if br.ghost_runner(lv) else 'off'}")
            return 0
        tot = sum(pool.map(check_game, [(g, None, False) for g in games], chunksize=50), Counter())
    models = ["new"] + (["old"] if ("old", "hr", "all", "total") in tot else [])
    print(f"{len(games):,} games from {a.data}\n")
    print("1) Home-run probe (runners on base = RunsScored - 1)")
    for m in models:
        t, ok = tot[(m, "hr", "all", "total")], tot[(m, "hr", "all", "match")]
        print(f"   {m}: {ok:,} of {t:,} = {100 * ok / t:.1f}%")
    print("\n2) Runs reconciliation by play type")
    kinds = sorted({k[2] for k in tot if k[0] == "new" and k[1] == "runs" and k[3] == "plays"}, key=lambda k: -tot[("new", "runs", k, "plays")])
    head = f"   {'play':<26}{'plays':>10}" + "".join(f"{m + ' over':>11}{m + ' under':>11}" for m in models)
    print(head)
    for k in kinds:
        print(f"   {k:<26}{tot[('new', 'runs', k, 'plays')]:>10,}" + "".join(f"{tot[(m, 'runs', k, 'over')]:>11,}{tot[(m, 'runs', k, 'under')]:>11,}" for m in models))
    for m in models:
        o = sum(v for k, v in tot.items() if k[0] == m and k[1] == "runs" and k[3] == "over")
        u = sum(v for k, v in tot.items() if k[0] == m and k[1] == "runs" and k[3] == "under")
        print(f"   {m} total: {o:,} over, {u:,} under")
    n = tot[("steal", "throws")]
    if n:
        print(f"\n3) Steal guess on {n:,} labeled throws: old rule right {100 * tot[('steal', 'old_right')] / n:.1f}%, "
              f"new rule right {100 * tot[('steal', 'new_right')] / n:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
