"""Pydantic v2 request/response models mirroring API_CONTRACT.md.

Response models are attached with ``responses={200: {"model": ...}}`` on the
pass-through endpoints (the ``services/queries`` layer already produces the
contract shape, so re-validating 100 rows per request would only add latency and
a way to fail) and as a real ``response_model`` where the router constructs the
object itself.  Either way ``/openapi.json`` describes every route.
"""
