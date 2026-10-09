"""Base state for every pitch, walked pitch by pitch through each half-inning (plan.md §8, §18).

``annotate_game(df)`` takes one game and adds, for each pitch, the state AT THE START of that pitch:

    on_1b, on_2b, on_3b   Int8: 1 if that base is occupied
    base_state            "101" = runners on 1st and 3rd
    re288_state           "101|1|3-2": bases | outs | balls-strikes (8 x 3 x 12 = 288 states)
    next_re288_state      the state on the next pitch of the same half-inning; null on its last pitch
    half_complete         True if the half-inning recorded 3 outs (run value stays blank otherwise)

Run value is computed when the data is read, never stored (plan.md §8):
    RunsScored + RE(next_re288_state) - RE(re288_state), with RE = 0 after a half-inning's last pitch.

Copied from ``data_pipeline/baserunner_state.py`` (which stays as is until cleanup) and fixed per
plan.md §18, where each fix was measured on 9.3M pitches:

  1. Ghost runner on 2nd in extra innings only at the levels that use it (config.GHOST_RUNNER_LEVELS);
     the old model put one in every D1 regular-season extra inning.
  2. A strikeout the batter reached on (dropped third strike) puts him on 1st.
  3. Every out on the play takes one runner off: pickoffs, runners thrown out on a hit or a wild
     pitch, strike-'em-out-throw-'em-out. Before, only the batter's outs and labeled caught
     stealing removed anyone.
  4. A fielder's choice removes exactly OutsOnPlay runners (it removed one even when nobody was out).
  5. The steal guess (games without steal labels) uses the base the catcher threw to: a throw to
     1st with nobody out is a back-pick, not a steal, and the target picks which runner moved.
  6. A sacrifice fly is handled like an out (the run scores, the others hold); a bunt still
     advances every runner.
  7. Hits, errors and bunts score exactly RunsScored runners, lead runners first; walks, steals and
     everything else score extra runners when RunsScored says so.

Bases are a set drawn from {1, 2, 3}. The play helpers return ``(bases, runs)``: ``runs`` is what the
model scored, used only to reconcile with RunsScored (TrackMan's count, which is what run value
uses). ``check_baserunner.py`` measures how often the two disagree.
"""
from __future__ import annotations

import polars as pl

from pipeline_v2 import config

HIT_BASES = {"Single": 1, "Double": 2, "Triple": 3, "HomeRun": 4}
SAC_FLY_TYPES = {"FlyBall", "LineDrive", "Popup"}     # a "Sacrifice" with these hit types is a sac fly
EXTRA_INNING = 10
HALF = ["Inning", "_tb"]
ORDER = ["Inning", "_tb", "PAofInning", "PitchofPA"]
COLUMNS = ["Inning", "Top/Bottom", "PAofInning", "PitchofPA", "Outs", "Balls", "Strikes", "PitchCall",
           "KorBB", "PlayResult", "OutsOnPlay", "RunsScored", "ThrowSpeed", "BasePositionX",
           "BasePositionZ", "TaggedHitType"]


# ── Small helpers ─────────────────────────────────────────────────────────────────────────────────
def throw_target(x: float | None, z: float | None) -> int | None:
    """The base the catcher threw to (1, 2 or 3), from TrackMan's ``BasePositionX/Z``; None when the
    throw wasn't measured. 2nd base sits ~127 ft out on the X axis, 1st and 3rd ~64 ft out on either
    side (Z > 0 is the 1st-base side)."""
    if x is None or z is None:
        return None
    if x > 95:
        return 2
    return 1 if z > 0 else 3


def runner_at(target: int | None) -> int | None:
    """The base of the runner a throw was aimed at: a throw to 1st is a pickoff of the runner on
    1st; a throw to 2nd or 3rd is at the runner stealing it, who came from the base before."""
    if target is None:
        return None
    return 1 if target == 1 else target - 1


def remove_runner(bases: set[int], base: int | None = None, lead: bool = False) -> set[int]:
    """Take one runner off: the one on ``base`` if given and occupied, otherwise the lead runner
    (``lead=True``) or the trailing one."""
    bases = set(bases)
    if not bases:
        return bases
    if base in bases:
        bases.discard(base)
    else:
        bases.discard(max(bases) if lead else min(bases))
    return bases


def score_lead_runners(bases: set[int], n: int) -> tuple[set[int], int]:
    """The lead runners score, one at a time, until ``n`` runs are in or nobody is left. Everyone
    else holds."""
    bases, runs = set(bases), 0
    while runs < n and bases:
        bases.discard(max(bases))
        runs += 1
    return bases, runs


# ── One kind of play each ─────────────────────────────────────────────────────────────────────────
def apply_walk(bases: set[int]) -> tuple[set[int], int]:
    """Walk, hit by pitch, or reaching on a dropped third strike: the batter takes 1st and pushes
    runners only through occupied bases (a forced advance)."""
    bases, runs = set(bases), 0
    if 1 in bases:
        if 2 in bases:
            if 3 in bases:
                runs += 1                            # bases loaded: the runner on 3rd is forced home
            bases.add(3)
        bases.add(2)
    bases.add(1)
    return bases, runs


def advance(bases: set[int], by: int, runs_scored: int, batter_base: int | None) -> tuple[set[int], int]:
    """Hits, errors and bunts. The batter takes ``batter_base`` (None when he's out, as on a bunt).
    Exactly ``runs_scored`` runners score, lead runners first; each runner left goes as far as ``by``
    bases ahead, but never past 3rd or past the runner ahead of him. A runner with no base left
    ahead of the batter must have scored, even if RunsScored says otherwise."""
    if batter_base is not None and batter_base >= 4:     # home run: everybody scores
        return set(), len(bases) + 1
    runners = sorted(bases, reverse=True)                # lead runner first
    n_score = min(max(runs_scored, 0), len(runners))
    runs = n_score
    new = set() if batter_base is None else {batter_base}
    floor = batter_base or 0                             # runners must end up ahead of the batter
    cap = 3
    for b in runners[n_score:]:
        t = min(b + by, cap)
        if t <= floor or t < b:
            runs += 1                                    # no room: he scored after all
            continue
        new.add(t)
        cap = t - 1
    return new, runs


def apply_out(bases: set[int], runner_outs: int) -> set[int]:
    """The batter is out. Each extra out on the play takes the trailing runner (the force at 2nd on
    a double play). Survivors hold; runs are added by the RunsScored reconciliation."""
    for _ in range(runner_outs):
        bases = remove_runner(bases)
    return set(bases)


def apply_fielders_choice(bases: set[int], outs_on_play: int) -> tuple[set[int], int]:
    """The batter is safe at 1st. Exactly ``outs_on_play`` runners are out (trailing first: usually
    the force at 2nd); then the batter takes 1st, forcing anyone still in his way."""
    for _ in range(outs_on_play):
        bases = remove_runner(bases)
    return apply_walk(bases)


def apply_steal(bases: set[int], target: int | None) -> set[int]:
    """A stolen base. If the throw's target is known, the runner from the base before it moved up;
    otherwise the trailing runner with an open base ahead (as before). A steal of home is the
    RunsScored reconciliation scoring the runner on 3rd."""
    bases = set(bases)
    if target in (2, 3) and target - 1 in bases and target not in bases:
        bases.discard(target - 1)
        bases.add(target)
        return bases
    for b in sorted(bases):
        if b + 1 <= 3 and b + 1 not in bases:
            bases.discard(b)
            bases.add(b + 1)
            break
    return bases


# ── One pitch ─────────────────────────────────────────────────────────────────────────────────────
def apply_pitch(bases: set[int], r: dict, use_heuristic: bool) -> tuple[set[int], int]:
    """The bases after one pitch, and the runs the model scored on it. ``r`` holds the pitch's columns
    plus ``_k_reached`` (the batter reached on a strikeout, worked out from the next pitch's outs)."""
    pc, kb, pr = r["PitchCall"], r["KorBB"], r["PlayResult"]
    oop = r["OutsOnPlay"] or 0
    rs = r["RunsScored"] or 0
    target = throw_target(r["BasePositionX"], r["BasePositionZ"])
    ends_pa = kb != "Undefined" or pc == "HitByPitch" or pr not in ("Undefined", "StolenBase", "CaughtStealing")
    # Outs on runners: OutsOnPlay includes the batter's own out only on an Out or a Sacrifice
    # (strikeouts carry OutsOnPlay = 0 in TrackMan's data).
    runner_outs = max(oop - 1, 0) if pr in ("Out", "Sacrifice") else oop
    runs = 0

    # 1) Baserunning during the pitch: steals, caught stealing, pickoffs. It comes first because it
    #    can share a pitch with a strikeout or a walk.
    if pr == "StolenBase":
        bases = apply_steal(bases, target)
    elif pr == "CaughtStealing":
        bases = remove_runner(bases, runner_at(target))
        runner_outs -= 1
    elif use_heuristic and not ends_pa and r["ThrowSpeed"] is not None and bases:
        # No steal labels in this game: a measured catcher throw while the at-bat goes on.
        if runner_outs > 0:
            bases = remove_runner(bases, runner_at(target))      # caught stealing or picked off
            runner_outs -= 1
        elif target != 1:
            bases = apply_steal(bases, target)                   # a throw to 1st with nobody out: back-pick
    if pr not in HIT_BASES and pr not in ("Error", "Out", "Sacrifice", "FieldersChoice"):
        # Any other out on a runner (a pitcher's pickoff, a runner thrown out on a wild pitch,
        # strike-'em-out-throw-'em-out). The plays below handle their own outs.
        for _ in range(max(runner_outs, 0)):
            bases = remove_runner(bases, runner_at(target))

    # 2) What the batter did.
    if kb == "Walk" or pc == "HitByPitch":
        bases, runs = apply_walk(bases)
    elif kb == "Strikeout":
        if r["_k_reached"]:
            bases, runs = apply_walk(bases)                      # dropped third strike
    elif pr in HIT_BASES or pr == "Error":
        hit = HIT_BASES.get(pr, 1)                               # an error puts the batter on 1st
        bases, runs = advance(bases, hit, rs, batter_base=hit)
        for _ in range(runner_outs):
            bases = remove_runner(bases, lead=True)              # thrown out taking an extra base
    elif pr == "Sacrifice":
        if r["TaggedHitType"] in SAC_FLY_TYPES:
            bases = apply_out(bases, runner_outs)                # sac fly: the run scores, others hold
        else:
            bases, runs = advance(bases, 1, rs, batter_base=None)    # bunt: everyone moves up one
            for _ in range(runner_outs):
                bases = remove_runner(bases)
    elif pr == "FieldersChoice":
        bases, runs = apply_fielders_choice(bases, oop)
    elif pr == "Out":
        bases = apply_out(bases, runner_outs)

    # 3) RunsScored is the authority: if the model scored fewer runs, the lead runners scored.
    if runs < rs:
        bases, extra = score_lead_runners(bases, rs - runs)
        runs += extra
    return bases, runs


# ── One game ──────────────────────────────────────────────────────────────────────────────────────
def ghost_runner(level: str | None) -> bool:
    return level in config.GHOST_RUNNER_LEVELS


def annotate_game(df: pl.DataFrame, level: str | None = None, ghost: bool | None = None,
                  keep_model_runs: bool = False) -> pl.DataFrame:
    """Add the base-state columns to one game's pitches (any column order; rows come back sorted by
    Inning, Top/Bottom, PAofInning, PitchofPA). ``level`` defaults to the game's ``Level`` column;
    ``ghost`` overrides config.GHOST_RUNNER_LEVELS (the check script uses it). ``keep_model_runs``
    adds ``model_runs``, the runs the model scored on each pitch, for the checks."""
    df = (df.with_columns(pl.when(pl.col("Top/Bottom") == "Top").then(0).otherwise(1).alias("_tb"))
            .sort(ORDER, nulls_last=True))
    if level is None and "Level" in df.columns:
        lv = df["Level"].drop_nulls().head(1).to_list()
        level = lv[0] if lv else None
    if ghost is None:
        ghost = ghost_runner(level)
    # Steal labels anywhere in the game -> trust them; none, but catcher throws measured -> guess.
    has_labels = df["PlayResult"].is_in(["StolenBase", "CaughtStealing"]).any()
    use_heuristic = not has_labels and df["ThrowSpeed"].is_not_null().any()

    # A strikeout the batter reached on: the next pitch of the half shows no new out for him.
    oop = pl.col("OutsOnPlay").fill_null(0)
    next_outs = pl.col("Outs").shift(-1).over(HALF)
    df = df.with_columns((pl.col("KorBB").eq("Strikeout") & next_outs.is_not_null()
                          & (next_outs - pl.col("Outs") - oop <= 0)).fill_null(False).alias("_k_reached"))

    on1, on2, on3, base, re288, model_runs = [], [], [], [], [], []
    cur_half, bases = None, set()
    for r in df.select(COLUMNS + ["_tb", "_k_reached"]).iter_rows(named=True):
        half = (r["Inning"], r["_tb"])
        if half != cur_half:
            cur_half = half
            extra = r["Inning"] is not None and r["Inning"] >= EXTRA_INNING
            bases = {2} if (ghost and extra) else set()
        b1, b2, b3 = int(1 in bases), int(2 in bases), int(3 in bases)
        on1.append(b1); on2.append(b2); on3.append(b3)
        base.append(f"{b1}{b2}{b3}")
        re288.append(f"{b1}{b2}{b3}|{r['Outs']}|{r['Balls']}-{r['Strikes']}")
        bases, runs = apply_pitch(bases, r, use_heuristic)
        model_runs.append(runs)

    # Outs the half-inning recorded: OutsOnPlay, plus strikeouts the batter didn't reach on.
    outs_made = oop + (pl.col("KorBB").eq("Strikeout") & ~pl.col("_k_reached")).cast(pl.Int64)
    out = df.with_columns(
        pl.Series("on_1b", on1, dtype=pl.Int8),
        pl.Series("on_2b", on2, dtype=pl.Int8),
        pl.Series("on_3b", on3, dtype=pl.Int8),
        pl.Series("base_state", base, dtype=pl.Utf8),
        pl.Series("re288_state", re288, dtype=pl.Utf8),
    ).with_columns(
        pl.col("re288_state").shift(-1).over(HALF).alias("next_re288_state"),
        (outs_made.sum().over(HALF) >= 3).alias("half_complete"),
    )
    if keep_model_runs:
        out = out.with_columns(pl.Series("model_runs", model_runs, dtype=pl.Int16))
    return out.drop("_tb", "_k_reached")
