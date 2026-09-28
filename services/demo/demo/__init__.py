"""Demo 'production' system that RootSignal investigates.

gateway -> checkout -> inventory (+ Postgres, Redis). Every service emits Prometheus
metrics and JSON logs (to Loki). fault-control injects realistic failures.
"""
