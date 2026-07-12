"""Production models used by delispice_app.

Import as a package (the repo root is on sys.path in every launch mode):

    from backend.models import cluster, contact_quality

Workflow: prototype in backend/notebooks/*.ipynb; when a model is ready, distill
it into a .py module here and import it from delispice_app. Keep heavy imports
(sklearn) inside functions so importing the package stays fast.
"""
