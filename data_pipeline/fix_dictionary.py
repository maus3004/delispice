"""Fix dictionary for cleaning raw Trackman CSVs before (re)building parquets.

Derived from the full-dataset value scan (30,233 files / 9,188,422 rows) vs the canonical
sets in trackman_pandera_schema.py. Rows-affected counts (whole dataset) noted per column.

NOTE: the stray "," / "." tokens and cross-column-leakage values are literal bad cells from a few
source-export files (NOT row shifts — every file is well-formed, 0 ragged rows). apply_fixes() maps
ALL of them to "Undefined" (a valid value in every affected column) so the non-nullable event columns
(KorBB / TaggedHitType / PlayResult) stay populated and nothing is set to null.

Usage:
    import polars as pl
    from trackman_schema import pl_csv_schema
    from fix_dictionary import apply_fixes
    raw = pl.read_csv(path, schema_overrides=pl_csv_schema)
    clean = apply_fixes(raw)          # remap typos + junk->'Undefined', drop corrupt-int rows
"""
import polars as pl

# bad value -> canonical value  (string remaps)
FIX_MAP = {
    "PitcherThrows": {   # 57 bad rows
        'Left ': 'Left',
        'RIght': 'Right',
    },
    "BatterSide": {   # 5 bad rows
        'left': 'Left',
        'RIght': 'Right',
    },
    "CatcherThrows": {   # 21065 bad rows
        ',': 'Undefined',
        'TCU_HFG': 'Undefined',
        'TEX_LON': 'Undefined',
        'R': 'Right',
        'L': 'Left',
    },
    "TaggedPitchType": {   # 307 bad rows
        ',': 'Undefined',
        'Changeup': 'ChangeUp',
        'FastBall': 'Fastball',
        'Four-Seam': 'FourSeamFastBall',
    },
    "PitchCall": {   # 74 bad rows
        'Inplay': 'InPlay',
        'BalIntentional': 'BallIntentional',
        'StirkeCalled': 'StrikeCalled',
        'FouldBallNotFieldable': 'FoulBallNotFieldable',
        'ballCalled': 'BallCalled',
        'inPlay': 'InPlay',
        'BatterInterference': 'BattersInterference',
        'SrikeCalled': 'StrikeCalled',
        'HitbyPitch': 'HitByPitch',
        'StrkeSwinging ': 'StrikeSwinging',
        'CatchersInterfernece': 'CatchersInterference',
        'StriekSwinging': 'StrikeSwinging',
        'Hitbypitch': 'HitByPitch',
        'Strikecalled': 'StrikeCalled',
        'BallIntentional ': 'BallIntentional',
        'CatcherInterference': 'CatchersInterference',
        'foulBallNotFieldable': 'FoulBallNotFieldable',
        'BallInDirt': 'BallinDirt',
        'BallAutomatic': 'AutomaticBall',
        'StriekC': 'StrikeCalled',
        'SwinginStrike': 'StrikeSwinging',
        'Fastball': 'Undefined',
        'FlyBall': 'Undefined',
        'PickOff': 'Undefined',
        'Sinker': 'Undefined',
        'Slider': 'Undefined',
    },
    "KorBB": {   # 385 bad rows
        ',': 'Undefined',
        '.': 'Undefined',
        'InPlay': 'Undefined',
        'walk': 'Walk',
        'StrikeOut': 'Strikeout',
    },
    "TaggedHitType": {   # 371 bad rows
        ',': 'Undefined',
        '.': 'Undefined',
        'BattersInterference': 'Undefined',
        'FieldersChoice': 'Undefined',
        'Interference': 'Undefined',
        'PassedBall': 'Undefined',
        'PopUp': 'Popup',
        'Groundball': 'GroundBall',
        'Flyball': 'FlyBall',
        'groundBall': 'GroundBall',
        'popup': 'Popup',
        'Popout': 'Popup',
    },
    "PlayResult": {   # 456 bad rows
        ',': 'Undefined',
        '.': 'Undefined',
        'FlyBall': 'Undefined',
        'GroundBall': 'Undefined',
        'Popup': 'Undefined',
        'Sacrifice ': 'Sacrifice',
        'Single ': 'Single',
        'Fielderschoice': 'FieldersChoice',
        'Homerun': 'HomeRun',
        'SIngle': 'Single',
        'error': 'Error',
        'Error ': 'Error',
        'Out ': 'Out',
        'OUt': 'Out',
        'FieldersChoice ': 'FieldersChoice',
        'sacrifice': 'Sacrifice',
        'Sacrificie': 'Sacrifice',
        'Nothing': 'Undefined',
    },
}

# Nothing is set to null now: all former TO_NULL junk/leakage maps to 'Undefined' in FIX_MAP
# above (a valid value in every affected column), so the non-nullable event columns stay
# populated. Kept (empty) so apply_fixes still works and for any future additions.
TO_NULL: dict[str, list[str]] = {}

# integer columns: rows OUTSIDE these inclusive ranges are corrupt -> quarantine
INT_RANGE = {'Outs': (0, 2), 'Balls': (0, 3), 'Strikes': (0, 2), 'PitchofPA': (1, 25), 'OutsOnPlay': (0, 3), 'RunsScored': (0, 4)}

def apply_fixes(df: pl.DataFrame, drop_corrupt_ints: bool = True) -> pl.DataFrame:
    """Remap typos + junk->'Undefined', and (optionally) drop rows with corrupt integer counts."""
    combined = {}
    for c, m in FIX_MAP.items():
        combined.setdefault(c, {}).update(m)
    for c, vals in TO_NULL.items():
        combined.setdefault(c, {}).update({v: None for v in vals})
    df = df.with_columns([pl.col(c).replace(m) for c, m in combined.items() if c in df.columns])
    if drop_corrupt_ints:
        for c, (lo, hi) in INT_RANGE.items():
            if c in df.columns:
                df = df.filter(pl.col(c).is_null() | pl.col(c).is_between(lo, hi))
    return df
