"""HTTP layer: FastAPI routers, schemas, middleware.

Everything here is a thin adapter over ``app.core``, ``app.indexing``,
``app.jobs``, ``app.search`` and ``app.services``.  No business logic and no SQL
lives in this package - the ``services/queries/*`` modules already return
contract-shaped dicts (API_CONTRACT 3-6), so a router's job is limited to
parsing/validating input, choosing the right service call, and shaping the
envelope.
"""
