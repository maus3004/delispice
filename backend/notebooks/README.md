# Model-development notebooks

The workspace for prototyping models. The cycle:

1. Build and refine the model in a notebook here.
2. When it's ready, distill it into a `.py` module in `../models/` (the
   `backend.models` package the app imports).
3. Wire it into `delispice_app` with `from backend.models import <module>`.

Notes
- Data paths: notebooks here sit two levels below the repo root, so relative
  paths like `../../data_pipeline/wbaserunners/...` work (same depth as the
  old location in `backend/models/`).
- To import the production models from a notebook, start Jupyter from the
  **repo root** (so `backend` is importable), then `from backend.models
  import autotagger, contact_quality, cluster`.
- Notebooks are tracked in git — clear giant outputs before committing so the
  repo stays lean.
