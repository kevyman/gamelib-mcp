"""Read-only integration status tools."""

from ..integrations.inspectors import inspect_all_integrations_dict


async def get_integration_status(
    platforms: list[str] | None = None,
    verbose: bool = True,
) -> dict:
    payload = inspect_all_integrations_dict()
    if platforms is not None:
        wanted = set(platforms)
        payload = {name: item for name, item in payload.items() if name in wanted}
    if not verbose:
        return {
            name: {
                "overall_status": item["overall_status"],
                "summary": item["summary"],
                "active_backend": item["active_backend"],
            }
            for name, item in payload.items()
        }
    return payload
