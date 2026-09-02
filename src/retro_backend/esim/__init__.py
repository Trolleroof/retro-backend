"""Frozen Restaurant E-Sim: environment, benchmark runner, and graders.

Everything under this package is pure Python: no ``js`` or Pyodide imports and
no Cloudflare bindings, so the simulator, graders, and provider adapters run
under CPython in the existing pytest suite and from ``scripts/``. See issue #53
for why the benchmark runner does not execute inside the Worker.
"""
