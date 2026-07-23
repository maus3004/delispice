from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import polars as pl
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import NearestNeighbors
from sklearn.model_selection import StratifiedKFold
from itertools import product

ROOT       = Path(__file__).resolve().parent.parent.parent          # repo root (robust to CWD)
PIPELINE   = ROOT / "data_pipeline" / "wbaserunners"
REM_PATH   = ROOT / "data_pipeline" / "re_matrices" / "re288_matrix.parquet"
FEATS      = ["ExitSpeed", "Angle", "Direction"]
LABEL_MAP  = {"Out": 0, "Single": 1, "Double": 2, "Triple": 3, "HomeRun": 4}
DROP_RESULTS = ["Error", "Sacrifice", "StolenBase", "FieldersChoice", "CaughtStealing", "Undefined"]
N_CLASSES  = 5
# default hyperparameter search grid for per-(level, year) CV selection
DEFAULT_K_GRID     = [25, 50, 100, 200, 400, 800, 2000]
DEFAULT_ALPHA_GRID = [0.1, 0.5, 1.0, 2.0, 5.0]
_EVENT_COLS = ["GameID", "Inning", "Top/Bottom", "PlayResult", "RunsScored",
               "Direction", "ExitSpeed", "Angle", "re288_state"]

# P4 is a League-based pseudo-level (Power-4 conferences), physically stored inside the D1 partition;
# the re288_matrix carries matching Level="P4" rows built by re_matrix.py.
P4_LEVEL   = "P4"
P4_LEAGUES = ["SEC", "ACC", "BIG10", "BIG12"]

## We can use compute_run_delta for future 
def compute_run_delta(df: pl.DataFrame, rem: pl.DataFrame) -> pl.DataFrame:
    return (df
        .with_columns(pl.col("re288_state").shift(-1)
                        .over(["GameID","Inning","Top/Bottom"]).alias("next_re288_state"))
        .join(rem.select("re288_state", pl.col("run_expectancy").alias("re_before")),
              on="re288_state", how="left")
        .join(rem.select(pl.col("re288_state").alias("next_re288_state"),
                         pl.col("run_expectancy").alias("re_after")),
              on="next_re288_state", how="left")
        .with_columns(pl.col("re_after").fill_null(0))
        .with_columns((pl.col("RunsScored") + pl.col("re_after") - pl.col("re_before")).alias("run_delta")))

def load_events(level: str, year: str) -> pl.DataFrame:
    """Scan one (level, year)'s wbaserunners parquets -> the columns we need (incl. Direction).
    P4 has no partition dir of its own: read the D1 partition and keep only Power-4 League rows."""
    if level == P4_LEVEL:
        # P4 is League-based: most games sit in D1/, but some P4-League games carry a non-D1 Level
        # and land in Others/, so scan BOTH partitions for the year and filter on League — this keeps
        # the training population identical to what delispice_app scores under a P4 selection.
        files = sorted(PIPELINE.glob(f"*/{year}/**/*.parquet"))
        if not files:
            raise FileNotFoundError(f"no parquets for P4 year={year} under {PIPELINE}")
        return (pl.scan_parquet([str(f) for f in files])
                  .select([*_EVENT_COLS, "League"])
                  .filter(pl.col("League").is_in(P4_LEAGUES))
                  .drop("League").collect())
    files = sorted((PIPELINE / level / year).glob("**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquets for level={level} year={year} under {PIPELINE}")
    return (pl.scan_parquet([str(f) for f in files])
              .select(_EVENT_COLS)
              .collect())

def load_re_matrix(level: str, year: str) -> pl.DataFrame:
    return (pl.read_parquet(REM_PATH)
              .filter((pl.col("Level") == level) & (pl.col("year") == year)))

def filter_batted_balls(df: pl.DataFrame) -> pl.DataFrame:          # cell-0 lines 61-66
    return (df.filter(pl.col("ExitSpeed").is_not_null() & pl.col("Angle").is_not_null()
                      & pl.col("Direction").is_not_null())
              .filter(~pl.col("PlayResult").is_in(DROP_RESULTS)))

# 
def linear_weights(df: pl.DataFrame) -> np.ndarray:                 # cell-0 lines 68-76, ordered
    w = (df.group_by("PlayResult").agg(pl.col("run_delta").mean())
           .with_columns(pl.col("PlayResult").replace_strict(LABEL_MAP).alias("_o")).sort("_o"))
    return w["run_delta"].to_numpy()

def load_training_frame(level: str, year: str) -> tuple[pl.DataFrame, np.ndarray]:
    """One call: (cleaned df with run_delta, linear weights) for a (level, year)."""
    rem = load_re_matrix(level, year)
    df  = filter_batted_balls(compute_run_delta(load_events(level, year), rem))
    return df, linear_weights(df)

# model
def neighbor_labels_sorted(X_ref, y_ref, X_query, max_k, n_jobs=-1):
    nn = NearestNeighbors(n_neighbors=max_k, n_jobs=n_jobs)
    nn.fit(X_ref)
    _, idx = nn.kneighbors(X_query)          # (n_query, max_k), sorted ascending
    return y_ref[idx]                       # (n_query, max_k) 

def smoothed_proba(neighbor_lab, k, alpha, n_classes=N_CLASSES):
    """Laplace-smoothed class probabilities from the first k neighbors.
 
    p_class = (count_class + alpha) / (k + alpha * C)
 
    This is a Dirichlet(alpha,...) posterior: every class keeps nonzero mass,
    encoding the true prior that no launch condition makes any outcome strictly
    impossible. Rows sum to 1; the floor is alpha/(k + alpha*C).
    """
    sub = neighbor_lab[:, :k]                                      # k nearest
    counts = np.stack([(sub == c).sum(axis=1)
                       for c in range(n_classes)], axis=1).astype(float)
    return (counts + alpha) / (k + alpha * n_classes)

def mean_log_loss(proba, y_true):
    p_true = proba[np.arange(len(y_true)), y_true]
    return -np.log(p_true).mean()

def cv_select(X_tr, y_tr, k_grid, alpha_grid, n_splits=5, seed=0):
    """Stratified k-fold CV over the *Cartesian product* of k and alpha.
 
    Loop structure (outer -> inner):
        1. (k, alpha) grid   -- every pair is one candidate configuration
        2. k-fold CV         -- the engine that turns a pair into one score
        3. balls in the fold -- query, smooth, accumulate log-loss
 
    Stratified folds keep rare classes (3B, HR) present in every fold.
    Returns {(k, alpha): mean_cv_log_loss} and the argmin pair.
    """
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    max_k = max(k_grid)
    scores = {(k, a): 0.0 for k, a in product(k_grid, alpha_grid)}
 
    for train_idx, val_idx in skf.split(X_tr, y_tr):     # Loop 2: folds
        X_ref, y_ref = X_tr[train_idx], y_tr[train_idx]
        X_val, y_val = X_tr[val_idx],   y_tr[val_idx]
 
        # one neighbor search per fold, at the largest k in the grid
        lab = neighbor_labels_sorted(X_ref, y_ref, X_val, max_k)
 
        for k in k_grid:                                  # Loop 1a: k
            for alpha in alpha_grid:                       # Loop 1b: alpha
                proba = smoothed_proba(lab, k, alpha)      # Loop 3 inside
                scores[(k, alpha)] += mean_log_loss(proba, y_val)
 
    for key in scores:                                    # average over folds
        scores[key] /= n_splits
 
    best = min(scores, key=scores.get)
    return scores, best


class ContactQualityModel:
    """scaler + reference set + (k, alpha) + weights, bundled so they can't be mismatched."""
    def fit(self, X, y, weights, k, alpha):       # X: (n,3) raw, y: int labels
        self.scaler = StandardScaler().fit(X)
        self.X_ref  = self.scaler.transform(X); self.y_ref = np.asarray(y)
        self.k, self.alpha, self.weights = k, alpha, np.asarray(weights, float)
        self.meta = {}
        return self
    def predict_proba(self, X_new):               # NEW points: reference = full training set
        Xs = self.scaler.transform(np.atleast_2d(np.asarray(X_new, float)))
        return smoothed_proba(neighbor_labels_sorted(self.X_ref, self.y_ref, Xs, self.k), self.k, self.alpha)
    def predict_xrv(self, X_new):
        return self.predict_proba(X_new) @ self.weights
    def training_xrv(self, honest=False, n_splits=5, seed=0):   # xRV for the TRAINING rows
        if not honest:
            proba = smoothed_proba(neighbor_labels_sorted(self.X_ref, self.y_ref, self.X_ref, self.k), self.k, self.alpha)
        else:
            proba = np.empty((len(self.y_ref), N_CLASSES))
            for tr, te in StratifiedKFold(n_splits, shuffle=True, random_state=seed).split(self.X_ref, self.y_ref):
                proba[te] = smoothed_proba(neighbor_labels_sorted(self.X_ref[tr], self.y_ref[tr], self.X_ref[te], self.k), self.k, self.alpha)
        return proba @ self.weights
    def save(self, path):
        """Persist to `{path}.npz` (arrays) + `{path}.json` (hyperparams/meta).

        No pickle: the artifact is plain numeric arrays plus a few scalars, so it
        survives scikit-learn version changes and is human-inspectable.
        """
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            base.with_name(base.name + ".npz"),
            X_ref=self.X_ref, y_ref=self.y_ref, weights=self.weights,
            scaler_mean=self.scaler.mean_, scaler_scale=self.scaler.scale_,
        )
        with open(base.with_name(base.name + ".json"), "w") as f:
            json.dump({"k": int(self.k), "alpha": float(self.alpha),
                       "feats": FEATS, "meta": self.meta}, f, indent=2)
        return base

    @classmethod
    def load(cls, path):
        """Rebuild a fitted model from `{path}.npz` + `{path}.json` (no `fit` call)."""
        base = Path(path)
        with np.load(base.with_name(base.name + ".npz")) as arr:
            X_ref, y_ref, weights = arr["X_ref"], arr["y_ref"], arr["weights"]
            scaler_mean, scaler_scale = arr["scaler_mean"], arr["scaler_scale"]
        with open(base.with_name(base.name + ".json")) as f:
            cfg = json.load(f)

        # reconstruct the StandardScaler from its stored statistics
        scaler = StandardScaler()
        scaler.mean_, scaler.scale_ = scaler_mean, scaler_scale
        scaler.var_ = scaler_scale ** 2
        scaler.n_features_in_ = scaler_mean.shape[0]
        scaler.n_samples_seen_ = int(y_ref.shape[0])

        model = cls()
        model.scaler = scaler
        model.X_ref, model.y_ref, model.weights = X_ref, y_ref, weights
        model.k, model.alpha = int(cfg["k"]), float(cfg["alpha"])
        model.feats, model.meta = cfg.get("feats", FEATS), cfg.get("meta", {})
        return model

def expected_run_values(level: str, year: str, k=None, alpha=None,
                        k_grid=DEFAULT_K_GRID, alpha_grid=DEFAULT_ALPHA_GRID,
                        honest=False, n_splits=5, seed=0):
    """Put in level + year -> (df with an `xRV` column, fitted ContactQualityModel).

    (k, alpha) are CV-selected for THIS (level, year) by default: a stratified
    k-fold search over `k_grid` x `alpha_grid` picks the log-loss argmin on the
    loaded data. Pass an explicit `k` and/or `alpha` to fix that hyperparameter
    and skip its search (pass both to skip CV entirely).
    """
    df, weights = load_training_frame(level, year)
    X = df.select(FEATS).to_numpy().astype(float)
    y = df["PlayResult"].replace_strict(LABEL_MAP, return_dtype=pl.Int64).to_numpy()

    # select the best (k, alpha) for this slice unless both were supplied
    cv_best = None
    if k is None or alpha is None:
        Xs = StandardScaler().fit_transform(X)          # same scaling fit() will use
        cv_scores, (best_k, best_alpha) = cv_select(
            Xs, y, k_grid, alpha_grid, n_splits=n_splits, seed=seed)
        cv_best = float(cv_scores[(best_k, best_alpha)])
        k = best_k if k is None else k
        alpha = best_alpha if alpha is None else alpha

    model = ContactQualityModel().fit(X, y, weights, k, alpha)
    model.meta = {"level": level, "year": year, "n_rows": df.height, "feats": FEATS,
                  "k": int(k), "alpha": float(alpha),
                  "cv_selected": cv_best is not None, "cv_log_loss": cv_best}
    return df.with_columns(pl.Series("xRV", model.training_xrv(honest=honest))), model