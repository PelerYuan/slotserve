"""Container specs: what SlotServe's containers are made of, as plain data (no database, no Docker client).

``build_spec(ctx)`` runs the contributors in a fixed order over a ``SessionContext``; ``render()`` turns the spec into
a Docker create body; ``harden()`` is the last step before Docker and cannot be skipped (``DockerClient.create``
refuses a body it has not seen). ``redact`` removes secrets from previews and diagnostics. The control plane and,
later, node agents use the same code (architecture A01).
"""
