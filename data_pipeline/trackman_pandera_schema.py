"""
Pandera schema for Trackman college-baseball pitch-level CSVs.

Column-count variants handled by `required=False`:
  167 cols  = common core           (no SpinAxis3d*, no bat-tracking)
  170 cols  = core + BatSpeed/VerticalAttackAngle/HorizontalAttackAngle
  201 cols  = full (adds 31 SpinAxis3d* columns)

STRICT mode: allowed-value sets contain only canonical values. Typos, casing
variants, trailing spaces, stray ','/'.' and cross-column leakage FAIL validation
so you can find & clean them. `nullable=True` lets genuinely-missing (empty) pass.

Requires: polars, pandera>=0.32, numpy, pandas (failure-case reporting).

NOTE: columns are NOT coerced (pandera's polars coercion is ~100x slower here). Read with the
dtype dict so columns already have the right type, otherwise you'll get dtype-mismatch failures:
Usage:
    import polars as pl
    from trackman_schema import pl_csv_schema
    from trackman_pandera_schema import TRACKMAN_SCHEMA
    df = pl.read_csv(path, schema_overrides=pl_csv_schema)   # dtypes already correct
    TRACKMAN_SCHEMA.validate(df, lazy=True)                  # lazy=True collects ALL failures
"""
import pandera.polars as pa
import pandera.backends.polars.builtin_checks  # noqa: F401  (registers isin/in_range/str_matches for polars)
from pandera.polars import Column, DataFrameSchema
import polars as pl

# ==========================================================================
# Allowed value sets  --  EDIT THESE as we review each column
# ==========================================================================
PITCHERTHROWS = ['Left', 'Right', 'Both', 'Undefined']
BATTERSIDE = ['Left', 'Right', 'Undefined']
CATCHERTHROWS = ['Left', 'Right', 'Both', 'Undefined']
PITCHERSET = ['Undefined']
TOP_BOTTOM = ['Top', 'Bottom']
TAGGEDPITCHTYPE = ['Fastball', 'Sinker', 'Cutter', 'Slider', 'Sweeper', 'Curveball', 'ChangeUp', 'Splitter', 'Knuckleball', 'FourSeamFastBall', 'TwoSeamFastBall', 'OneSeamFastBall', 'Other', 'Undefined']
AUTOPITCHTYPE = ['Four-Seam', 'Sinker', 'Cutter', 'Slider', 'Curveball', 'Changeup', 'Splitter', 'Other', 'Undefined']
PITCHCALL = ['BallCalled', 'StrikeCalled', 'StrikeSwinging', 'FoulBall', 'FoulBallNotFieldable', 'FoulBallFieldable', 'InPlay', 'HitByPitch', 'BallinDirt', 'BallIntentional', 'AutomaticBall', 'AutomaticStrike', 'CatchersInterference', 'BattersInterference', 'WildPitch', 'Undefined']
KORBB = ['Undefined', 'Strikeout', 'Walk']
TAGGEDHITTYPE = ['Undefined', 'GroundBall', 'FlyBall', 'LineDrive', 'Popup', 'Bunt']
PLAYRESULT = ['Undefined', 'Out', 'Single', 'Double', 'Triple', 'HomeRun', 'Sacrifice', 'FieldersChoice', 'Error', 'StolenBase', 'CaughtStealing']
AUTOHITTYPE = ['GroundBall', 'LineDrive', 'FlyBall', 'Popup']

# ==========================================================================
# Schema
# ==========================================================================
TRACKMAN_SCHEMA = DataFrameSchema(
    {
    # --- Identifiers & game keys ---
    "PitchUID":                          Column(pl.Utf8),
    "PlayID":                            Column(pl.Utf8, nullable=True),
    "GameID":                            Column(pl.Utf8),
    "GameUID":                           Column(pl.Utf8),
    "GameForeignID":                     Column(pl.Utf8, nullable=True),
    "HomeTeamForeignID":                 Column(pl.Utf8, nullable=True),
    "AwayTeamForeignID":                 Column(pl.Utf8, nullable=True),
    "PitcherId":                         Column(pl.Utf8, nullable=True),
    "BatterId":                          Column(pl.Utf8, nullable=True),
    "CatcherId":                         Column(pl.Utf8, nullable=True),

    # --- Date / time (free strings — not format-validated) ---
    "Date":                              Column(pl.Utf8, nullable=True),
    "Time":                              Column(pl.Utf8, nullable=True),
    "UTCDate":                           Column(pl.Utf8, nullable=True),
    "UTCTime":                           Column(pl.Utf8, nullable=True),
    "LocalDateTime":                     Column(pl.Utf8, nullable=True),
    "UTCDateTime":                       Column(pl.Utf8, nullable=True),

    # --- People & teams (names/codes — no value enum) ---
    "Pitcher":                           Column(pl.Utf8),
    "Batter":                            Column(pl.Utf8, nullable=True),
    "Catcher":                           Column(pl.Utf8, nullable=True),
    "PitcherTeam":                       Column(pl.Utf8),
    "BatterTeam":                        Column(pl.Utf8),
    "CatcherTeam":                       Column(pl.Utf8),
    "HomeTeam":                          Column(pl.Utf8),
    "AwayTeam":                          Column(pl.Utf8),
    "Stadium":                           Column(pl.Utf8),

    # --- Game state — integers ---
    "PitchNo":                           Column(pl.Int64, nullable=True),   # type-only
    "Inning":                            Column(pl.Int64),   # type-only
    "Top/Bottom":                        Column(pl.Utf8, pa.Check.isin(TOP_BOTTOM)),
    "PAofInning":                        Column(pl.Int64, nullable=True),   # type-only
    "PitchofPA":                         Column(pl.Int64, pa.Check.in_range(1, 25), nullable=True),
    "Outs":                              Column(pl.Int64, pa.Check.in_range(0, 2)),
    "Balls":                             Column(pl.Int64, pa.Check.in_range(0, 3)),
    "Strikes":                           Column(pl.Int64, pa.Check.in_range(0, 2)),
    "OutsOnPlay":                        Column(pl.Int64, pa.Check.in_range(0, 3)),
    "RunsScored":                        Column(pl.Int64, pa.Check.in_range(0, 4)),

    # --- Handedness / set ---
    "PitcherThrows":                     Column(pl.Utf8, pa.Check.isin(PITCHERTHROWS), nullable=True),
    "BatterSide":                        Column(pl.Utf8, pa.Check.isin(BATTERSIDE), nullable=True),
    "CatcherThrows":                     Column(pl.Utf8, pa.Check.isin(CATCHERTHROWS), nullable=True),
    "PitcherSet":                        Column(pl.Utf8, pa.Check.isin(PITCHERSET)),

    # --- Pitch & play classification (categorical) ---
    "TaggedPitchType":                   Column(pl.Utf8, pa.Check.isin(TAGGEDPITCHTYPE)),
    "AutoPitchType":                     Column(pl.Utf8, pa.Check.isin(AUTOPITCHTYPE), nullable=True),
    "PitchCall":                         Column(pl.Utf8, pa.Check.isin(PITCHCALL), nullable=True),
    "KorBB":                             Column(pl.Utf8, pa.Check.isin(KORBB)),
    "TaggedHitType":                     Column(pl.Utf8, pa.Check.isin(TAGGEDHITTYPE)),
    "PlayResult":                        Column(pl.Utf8, pa.Check.isin(PLAYRESULT)),
    "AutoHitType":                       Column(pl.Utf8, pa.Check.isin(AUTOHITTYPE), nullable=True),
    "Tilt":                              Column(pl.Utf8, nullable=True),   # free string (clock face) — not validated
    "Notes":                             Column(pl.Utf8, nullable=True),

    # --- Competition (free strings — not value-validated) ---
    "Level":                             Column(pl.Utf8, nullable=True),
    "League":                            Column(pl.Utf8, nullable=True),
    "System":                            Column(pl.Utf8, nullable=True),

    # --- Confidence flags (free strings — not value-validated) ---
    "PitchReleaseConfidence":            Column(pl.Utf8, nullable=True),
    "PitchLocationConfidence":           Column(pl.Utf8, nullable=True),
    "PitchMovementConfidence":           Column(pl.Utf8, nullable=True),
    "HitLaunchConfidence":               Column(pl.Utf8, nullable=True),
    "HitLandingConfidence":              Column(pl.Utf8, nullable=True),
    "CatcherThrowCatchConfidence":       Column(pl.Utf8, nullable=True),
    "CatcherThrowReleaseConfidence":     Column(pl.Utf8, nullable=True),
    "CatcherThrowLocationConfidence":    Column(pl.Utf8, nullable=True),

    # --- Measured numeric metrics (Float64 — type + nullable) ---
    "RelSpeed":                          Column(pl.Float64, nullable=True),
    "VertRelAngle":                      Column(pl.Float64, nullable=True),
    "HorzRelAngle":                      Column(pl.Float64, nullable=True),
    "SpinRate":                          Column(pl.Float64, nullable=True),
    "SpinAxis":                          Column(pl.Float64, nullable=True),
    "RelHeight":                         Column(pl.Float64, nullable=True),
    "RelSide":                           Column(pl.Float64, nullable=True),
    "Extension":                         Column(pl.Float64, nullable=True),
    "VertBreak":                         Column(pl.Float64, nullable=True),
    "InducedVertBreak":                  Column(pl.Float64, nullable=True),
    "HorzBreak":                         Column(pl.Float64, nullable=True),
    "PlateLocHeight":                    Column(pl.Float64, nullable=True),
    "PlateLocSide":                      Column(pl.Float64, nullable=True),
    "ZoneSpeed":                         Column(pl.Float64, nullable=True),
    "VertApprAngle":                     Column(pl.Float64, nullable=True),
    "HorzApprAngle":                     Column(pl.Float64, nullable=True),
    "ZoneTime":                          Column(pl.Float64, nullable=True),
    "ExitSpeed":                         Column(pl.Float64, nullable=True),
    "Angle":                             Column(pl.Float64, nullable=True),
    "Direction":                         Column(pl.Float64, nullable=True),
    "HitSpinRate":                       Column(pl.Float64, nullable=True),
    "PositionAt110X":                    Column(pl.Float64, nullable=True),
    "PositionAt110Y":                    Column(pl.Float64, nullable=True),
    "PositionAt110Z":                    Column(pl.Float64, nullable=True),
    "Distance":                          Column(pl.Float64, nullable=True),
    "LastTrackedDistance":               Column(pl.Float64, nullable=True),
    "Bearing":                           Column(pl.Float64, nullable=True),
    "HangTime":                          Column(pl.Float64, nullable=True),
    "pfxx":                              Column(pl.Float64, nullable=True),
    "pfxz":                              Column(pl.Float64, nullable=True),
    "x0":                                Column(pl.Float64, nullable=True),
    "y0":                                Column(pl.Float64, nullable=True),
    "z0":                                Column(pl.Float64, nullable=True),
    "vx0":                               Column(pl.Float64, nullable=True),
    "vy0":                               Column(pl.Float64, nullable=True),
    "vz0":                               Column(pl.Float64, nullable=True),
    "ax0":                               Column(pl.Float64, nullable=True),
    "ay0":                               Column(pl.Float64, nullable=True),
    "az0":                               Column(pl.Float64, nullable=True),
    "EffectiveVelo":                     Column(pl.Float64, nullable=True),
    "MaxHeight":                         Column(pl.Float64, nullable=True),
    "MeasuredDuration":                  Column(pl.Float64, nullable=True),
    "SpeedDrop":                         Column(pl.Float64, nullable=True),
    "PitchLastMeasuredX":                Column(pl.Float64, nullable=True),
    "PitchLastMeasuredY":                Column(pl.Float64, nullable=True),
    "PitchLastMeasuredZ":                Column(pl.Float64, nullable=True),
    "ContactPositionX":                  Column(pl.Float64, nullable=True),
    "ContactPositionY":                  Column(pl.Float64, nullable=True),
    "ContactPositionZ":                  Column(pl.Float64, nullable=True),
    "PitchTrajectoryXc0":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryXc1":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryXc2":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryYc0":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryYc1":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryYc2":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryZc0":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryZc1":                Column(pl.Float64, nullable=True),
    "PitchTrajectoryZc2":                Column(pl.Float64, nullable=True),
    "HitSpinAxis":                       Column(pl.Float64, nullable=True),
    "HitTrajectoryXc0":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc1":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc2":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc3":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc4":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc5":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc6":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc7":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryXc8":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc0":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc1":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc2":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc3":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc4":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc5":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc6":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc7":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryYc8":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc0":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc1":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc2":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc3":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc4":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc5":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc6":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc7":                  Column(pl.Float64, nullable=True),
    "HitTrajectoryZc8":                  Column(pl.Float64, nullable=True),
    "ThrowSpeed":                        Column(pl.Float64, nullable=True),
    "PopTime":                           Column(pl.Float64, nullable=True),
    "ExchangeTime":                      Column(pl.Float64, nullable=True),
    "TimeToBase":                        Column(pl.Float64, nullable=True),
    "CatchPositionX":                    Column(pl.Float64, nullable=True),
    "CatchPositionY":                    Column(pl.Float64, nullable=True),
    "CatchPositionZ":                    Column(pl.Float64, nullable=True),
    "ThrowPositionX":                    Column(pl.Float64, nullable=True),
    "ThrowPositionY":                    Column(pl.Float64, nullable=True),
    "ThrowPositionZ":                    Column(pl.Float64, nullable=True),
    "BasePositionX":                     Column(pl.Float64, nullable=True),
    "BasePositionY":                     Column(pl.Float64, nullable=True),
    "BasePositionZ":                     Column(pl.Float64, nullable=True),
    "ThrowTrajectoryXc0":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryXc1":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryXc2":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryYc0":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryYc1":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryYc2":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryZc0":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryZc1":                Column(pl.Float64, nullable=True),
    "ThrowTrajectoryZc2":                Column(pl.Float64, nullable=True),

    # --- Optional: bat tracking (170-col files only) ---
    "BatSpeed":                          Column(pl.Float64, nullable=True, required=False),
    "VerticalAttackAngle":               Column(pl.Float64, nullable=True, required=False),
    "HorizontalAttackAngle":             Column(pl.Float64, nullable=True, required=False),

    # --- Optional: SpinAxis3d block (201-col files only) ---
    "SpinAxis3dTransverseAngle":         Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dLongitudinalAngle":       Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dActiveSpinRate":          Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSpinEfficiency":          Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dTilt":                    Column(pl.Utf8, nullable=True, required=False),   # clock-face string, not float
    "SpinAxis3dVectorX":                 Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dVectorY":                 Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dVectorZ":                 Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationRotationX": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationRotationY": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationRotationZ": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleHorizontalAmb1": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleVerticalAmb1": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallXAmb1": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallYAmb1": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallZAmb1": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleHorizontalAmb2": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleVerticalAmb2": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallXAmb2": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallYAmb2": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallZAmb2": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleHorizontalAmb3": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleVerticalAmb3": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallXAmb3": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallYAmb3": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallZAmb3": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleHorizontalAmb4": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallAngleVerticalAmb4": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallXAmb4": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallYAmb4": Column(pl.Float64, nullable=True, required=False),
    "SpinAxis3dSeamOrientationBallZAmb4": Column(pl.Float64, nullable=True, required=False),

    },
    strict=False,   # tolerate the 167/170/201 column-count differences
    name="trackman_csv",
)
