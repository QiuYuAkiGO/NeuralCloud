"""Temporary resource constraints preserve durable maintenance work."""


class ProductionMaintenanceDeferred(ValueError):
    """The job is still valid, but cannot spend autonomous resources yet."""

    def __init__(self, error_code="production_maintenance_deferred", *, retry_after_seconds=30):
        super().__init__(error_code)
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds


def read_maintenance_context(meter):
    """Translate transport failure at the infrastructure boundary."""
    import httpx
    try:
        return meter.context(scope="owner", autonomous=True)
    except httpx.TransportError as exc:
        raise ProductionMaintenanceDeferred("production_maintenance_runtime_unavailable") from exc
