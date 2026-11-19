"""System Advisor - "what can this machine actually run, and how safely?"

Five small modules, each with one job:

``probe``        the only module here that may start a process, and only from a
                 frozen table of read-only inventory commands;
``hardware``     turns probe output and OS facts into one hardware profile, plus
                 the live resource sample the dashboard draws;
``catalog``      the reference dataset of models: a seed shipped with the app,
                 overlaid by whatever the updater has refreshed;
``fit``          memory arithmetic - what a model needs against what this
                 machine can give without being driven to 100%;
``security``     an explainable 0-100 safety score for a catalog entry or a
                 file already on disk;
``safe_config``  launch flags and environment values that keep headroom;
``sources``      where catalog data comes from, and the fetchers for each;
``updater``      the refresh job - manual, or self-initiated when stale.

Provenance, stated once.  Hardware figures are **measured**.  Everything the
advisor concludes from them - a requirement, a verdict, a speed class, a safety
score - is an **estimate**, and every payload says so, so the interface can
paint it with the inferred (violet) convention rather than as a fact.
"""
