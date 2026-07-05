"""delispice_app — a standalone Dash + DuckDB desktop app (pitcher/batter reports).

Modules:
    data     DuckDB serving layer (cached picker indexes + targeted per-player scans); loads the
             AutoCluster adapter from backend/models/cluster.py by file path
    report   pure report builders + Plotly figures (ported from interactive_pitcher.ipynb)
    app      Dash UI (layout + callbacks); run browser-mode with ``python -m delispice_app.app``
    launch   opens the app in a native pop-up window via pywebview (``python -m delispice_app.launch``)
"""
