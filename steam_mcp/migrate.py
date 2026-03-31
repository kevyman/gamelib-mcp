"""Manual database migration entrypoint.

Run with: python -m steam_mcp.migrate
"""

import asyncio

from steam_mcp.data.db import _db_path, migrate_db


async def migrate() -> None:
    print(f"Migrating database: {_db_path()}")
    result = await migrate_db(progress=print)

    if result.changed:
        print(
            f"Migration complete: schema v{result.initial_version} -> v{result.final_version}"
        )
        return

    print(f"No-op: database already at schema v{result.final_version}")


if __name__ == "__main__":
    asyncio.run(migrate())
