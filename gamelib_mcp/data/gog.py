"""GOG owned games sync.

Primary backend: GOG's own account listing (``embed.gog.com``
``/account/getFilteredProducts``, the endpoint lgogdownloader itself reads),
authenticated with the lgogdownloader session via ``gog_session.py``. It
carries the real catalog title AND the stable per-item **product id**, so GOG
is no longer an identifier-less store: a re-sync resolves by
``gog_product_id`` first and a catalog rename is a rename, not a fork.

Fallback backend: ``lgogdownloader --list``, whose output is one slug per line
(ANSI color codes and optional ``[N]`` update indicators). Slugs title-case
into degraded names ("Legacy Of Kain Defiance", "Mount Blade") and carry no
product id, so that path keeps resolving by title only. It exists because the
account API can fail on auth/network/payload, and losing the sync entirely is
worse than a degraded one. ``--list j`` (JSON mode) is not an option: it
crashes on lgogdownloader 3.12.

One-time local setup:
  1. Install lgogdownloader (apt install lgogdownloader)
  2. Run: lgogdownloader --login
  3. Mount ~/.config/lgogdownloader/ into Docker (see deploy.md)

Playtime is not available from either backend.
"""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from gamelib_mcp.data.db import (
    GOG_PRODUCT_ID,
    adopt_platform_identifier,
    get_db,
    get_game_by_identifier,
    get_manual_overrides,
    get_platform_game_by_normalized_name,
    load_fuzzy_candidates,
    upsert_game_alias,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.gog_session import (
    authenticated_get_json,
    load_gog_session,
    missing_session_error,
    stale_session_message,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB, resolve_and_link_game
from gamelib_mcp.data.title_normalization import (
    normalize_search_text,
    prepare_catalog_title,
)

_ROMAN_RE = re.compile(r"\b([IiVvXx]{2,})\b")
_ORDINAL_RE = re.compile(r"(\d+)(St|Nd|Rd|Th)\b")

logger = logging.getLogger(__name__)

_LGOGDOWNLOADER_BIN = "lgogdownloader"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_UPDATE_INDICATOR = re.compile(r"\s+\[\d+\]$")
_AUTH_FILE_TOKENS = ("cookie", "token", "auth", "session", "galaxy")


def _config_dir() -> Path:
    """Return the lgogdownloader config directory (where auth session is stored)."""
    override = os.getenv("LGOGDOWNLOADER_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "lgogdownloader"


def _subprocess_env() -> dict:
    """
    Build env dict for lgogdownloader subprocess.

    lgogdownloader stores its session in XDG_CONFIG_HOME/lgogdownloader/.
    We set XDG_CONFIG_HOME to the parent of _config_dir() so lgogdownloader
    finds its session at the expected path.
    """
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(_config_dir().parent)
    return env


def _slug_to_title(slug: str) -> str:
    """Convert a lgogdownloader slug to a human-readable title."""
    if slug.endswith("_game") and "_" in slug[:-5]:
        slug = slug[:-5]
    # GOG convention: trailing _the → "The " prefix (e.g. elder_scrolls_the → The Elder Scrolls)
    if slug.endswith("_the") and "_" in slug[:-4]:
        slug = "the_" + slug[:-4]
    title = slug.replace("_", " ").title()
    # Fix Roman numerals mangled by .title() (Ii→II, Iii→III, Iv→IV, Vi→VI …)
    title = _ROMAN_RE.sub(lambda m: m.group(0).upper(), title)
    # Fix ordinal suffixes (20Th→20th, 1St→1st, 2Nd→2nd, 3Rd→3rd)
    title = _ORDINAL_RE.sub(lambda m: m.group(1) + m.group(2).lower(), title)
    return title


def _parse_lgogdownloader_output(stdout: str) -> list[str]:
    """
    Parse lgogdownloader --list plain text output into a list of game titles.

    Each line is a slug with optional ANSI color codes and trailing [N] update
    indicator. Strips both, then title-cases for fuzzy matching.

    Example input line: "\x1b[01;34mcyberpunk_2077 [1]\x1b[0m"
    Example output: "Cyberpunk 2077"
    """
    titles = []
    for line in stdout.splitlines():
        line = _ANSI_ESCAPE.sub("", line).strip()
        if not line:
            continue
        line = _UPDATE_INDICATOR.sub("", line).strip()
        if not line:
            continue
        titles.append(_slug_to_title(line))
    return titles


def _has_auth_files(config_path: Path) -> bool:
    if not config_path.is_dir():
        return False
    return any(
        path.is_file() and any(token in path.name.lower() for token in _AUTH_FILE_TOKENS)
        for path in config_path.iterdir()
    )


_ACCOUNT_PRODUCTS_URL = "https://embed.gog.com/account/getFilteredProducts"
# GOG serves 50 products a page, so this cap sits an order of magnitude past
# any real library — it exists so a malformed totalPages cannot spin forever.
_MAX_LISTING_PAGES = 200
_LISTING_TIMEOUT_SECONDS = 30

_LISTING_AUTH_ERROR = stale_session_message("account-listing")


@dataclass(frozen=True)
class GogProduct:
    """One owned catalog item as GOG's account listing reports it."""

    product_id: str
    title: str
    slug: str


def _parse_listing_page(payload: dict) -> list[GogProduct]:
    """Owned games on one listing page; movies and non-game rows dropped."""
    products: list[GogProduct] = []
    raw = payload.get("products")
    if not isinstance(raw, list):
        return products
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Absent flags mean "unknown", not "not a game" — only an explicit
        # isGame=false / isMovie=true disqualifies a row.
        if item.get("isGame") is False or item.get("isMovie") is True:
            continue
        product_id = item.get("id")
        title = item.get("title")
        if product_id is None or product_id == "":
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        slug = item.get("slug")
        products.append(
            GogProduct(
                product_id=str(product_id),
                title=title.strip(),
                slug=slug.strip() if isinstance(slug, str) else "",
            )
        )
    return products


async def fetch_gog_library_products(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> list[GogProduct]:
    """Owned GOG products from the account listing lgogdownloader itself reads.

    Auth reuses the lgogdownloader session (``gog_session``): bearer token
    first, cookie jar on a 401/403 or an HTML login bounce. Raises RuntimeError
    with the re-login advice when no session is stored or neither credential
    authenticates; transport/HTTP/parse failures propagate. The caller treats
    every one of those as "fall back to the CLI listing".
    ``transport`` exists for tests (httpx.MockTransport).
    """
    # Credentials are read from disk ONCE per fetch and a rejected bearer
    # token is remembered on the session object, so a multi-page listing does
    # not re-parse the token file and re-try the dead token on every page.
    session = load_gog_session()
    if session.token is None and session.cookies is None:
        raise missing_session_error()

    products: list[GogProduct] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_LISTING_TIMEOUT_SECONDS, transport=transport
    ) as client:
        total_pages = 1
        page = 1
        while page <= min(total_pages, _MAX_LISTING_PAGES):
            payload = await authenticated_get_json(
                client,
                _ACCOUNT_PRODUCTS_URL,
                {
                    "hiddenFlag": 0,
                    "isUpdated": 0,
                    "mediaType": 1,
                    "sortBy": "title",
                    "system": "",
                    "page": page,
                },
                session=session,
            )
            if payload is None:
                raise RuntimeError(_LISTING_AUTH_ERROR)
            for product in _parse_listing_page(payload):
                if product.product_id in seen:
                    continue
                seen.add(product.product_id)
                products.append(product)
            reported = payload.get("totalPages")
            if isinstance(reported, int) and reported > 0:
                total_pages = reported
            page += 1
    return products


def _short_error(exc: Exception) -> str:
    """One short line describing a listing failure, for the sync result."""
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:200]


def _check_preconditions() -> dict | None:
    """The skip/failure result when GOG cannot be synced at all, else None."""
    if not shutil.which(_LGOGDOWNLOADER_BIN):
        logger.info("lgogdownloader not in PATH — skipping GOG sync")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "degraded",
            "error_summary": "lgogdownloader not in PATH",
            "error_classification": "missing_runtime_dependency",
        }

    config_path = _config_dir()
    if not config_path.exists():
        logger.info(
            "lgogdownloader config dir not found (%s) — skipping GOG sync", config_path
        )
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": f"lgogdownloader config dir not found: {config_path}",
            "error_classification": "missing_configuration",
        }

    if not _has_auth_files(config_path):
        logger.info("lgogdownloader session files missing in %s — skipping GOG sync", config_path)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": f"lgogdownloader session files missing in {config_path}; run lgogdownloader --login",
            "error_classification": "missing_configuration",
        }
    return None


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms.

    Primary backend is GOG's account listing (product ids + real catalog
    titles); any failure there — auth, network, unexpected payload — logs a
    warning and falls back to ``lgogdownloader --list`` unchanged, because a
    degraded sync beats no sync.

    Silent skip conditions:
    - lgogdownloader binary not in PATH
    - lgogdownloader config dir does not exist (no session stored)
    - no session files in the config dir

    Returns: {"added", "matched", "skipped", "listing_backend"}, plus
    "renamed" on the account-API path and "listing_error" when it fell back,
    plus sync failure metadata when the CLI cannot run either.
    """
    precondition = _check_preconditions()
    if precondition is not None:
        return precondition

    try:
        products = await fetch_gog_library_products()
    except Exception as exc:
        logger.warning(
            "GOG account listing unavailable (%s) — falling back to lgogdownloader --list",
            exc,
        )
        result = await _sync_from_lgogdownloader()
        result["listing_backend"] = "lgogdownloader"
        result["listing_error"] = _short_error(exc)
        return result

    return await _sync_from_account_listing(products)


async def _rename_to_catalog_title(game_id: int, catalog_title: str, slug_title: str | None) -> bool:
    """Rewrite a row's name to the GOG catalog title where that is safe.

    Two rows qualify: one still carrying the legacy slug title this sync used
    to store ("Legacy Of Kain Defiance"), and a GOG-only row, whose name has
    no other platform's source behind it. A name the user set by hand
    (``manual_overrides``) is never touched.
    """
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT name FROM games WHERE id = ?", (game_id,))
        if row is None:
            return False
        current = row["name"] or ""
        if current == catalog_title:
            return False
        if "name" in await get_manual_overrides(db, game_id):
            return False
        if not (slug_title and current.casefold() == slug_title.casefold()):
            other_platform = await db.execute_fetchone(
                "SELECT 1 FROM game_platforms WHERE game_id = ? AND platform != ? LIMIT 1",
                (game_id, "gog"),
            )
            if other_platform is not None:
                return False
        await db.execute(
            "UPDATE games SET name = ?, name_normalized = ? WHERE id = ?",
            (catalog_title, normalize_search_text(catalog_title), game_id),
        )
        await db.commit()
    return True


async def _identifierless_gog_row(name: str) -> sqlite3.Row | None:
    """The oldest same-normalized-name games row owning a gog platform row
    that carries NO gog_product_id — the only shape the account-listing name
    fallback may attach to (see the caller for why)."""
    normalized = normalize_search_text(name)
    if not normalized:
        return None
    async with get_db() as db:
        return await db.execute_fetchone(
            """SELECT g.*
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'gog'
               WHERE COALESCE(g.name_normalized, '') = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM game_platform_identifiers gpi
                     WHERE gpi.game_platform_id = gp.id
                       AND gpi.identifier_type = ?
                 )
               ORDER BY g.id
               LIMIT 1""",
            (normalized, GOG_PRODUCT_ID),
        )


async def _sync_from_account_listing(products: list[GogProduct]) -> dict:
    """Resolve account-listing products identifier-first and write them."""
    added = matched = skipped = renamed = 0
    candidates = await load_fuzzy_candidates()
    igdb_platform_id = PLATFORM_TO_IGDB.get("gog")

    for product in products:
        prepared_title = prepare_catalog_title(product.title)
        if prepared_title is None:
            skipped += 1
            continue
        # The name this sync stored before the account listing existed — the
        # only key a legacy row can be found by.
        slug_title = _slug_to_title(product.slug) if product.slug else None
        legacy_title = slug_title if slug_title and slug_title != prepared_title else None

        igdb_game = None
        existing = await get_game_by_identifier(GOG_PRODUCT_ID, product.product_id)
        if existing is not None:
            game_id = existing["id"]
            matched += 1
        else:
            # Identifier miss: adopt onto an identifier-less gog row of the
            # same name rather than letting the fuzzy fallback's
            # exclude_platform guard fork a stranded duplicate. The legacy
            # slug title is tried too, so the first account-API run adopts
            # every row the CLI path created.
            adopted = await adopt_platform_identifier(
                name=prepared_title,
                platform="gog",
                identifier_type=GOG_PRODUCT_ID,
                identifier_value=product.product_id,
            )
            if adopted is None and legacy_title:
                adopted = await adopt_platform_identifier(
                    name=legacy_title,
                    platform="gog",
                    identifier_type=GOG_PRODUCT_ID,
                    identifier_value=product.product_id,
                )
            if adopted is not None:
                game_id = adopted
                matched += 1
            else:
                # Adoption refused only when the same-name gog rows were
                # AMBIGUOUS (several identifier-less ones) — the one other
                # reason, a same-name row that already carries a DIFFERENT
                # product id, must NOT be matched here: that is a distinct GOG
                # product ("Alone in the Dark" 2008 vs 2024), and attaching a
                # second id would be the within-platform name collapse the
                # identity rules forbid. So the fallback accepts only a row
                # whose gog platform row has no product id at all.
                row = await _identifierless_gog_row(prepared_title)
                if row is None and legacy_title:
                    row = await _identifierless_gog_row(legacy_title)
                if row is not None:
                    game_id = row["id"]
                    matched += 1
                else:
                    game_id, igdb_game = await resolve_and_link_game(
                        prepared_title, igdb_platform_id, candidates, platform="gog"
                    )
                    if game_id in candidates:
                        matched += 1
                    else:
                        candidates[game_id] = prepared_title
                        added += 1

        if await _rename_to_catalog_title(game_id, prepared_title, slug_title):
            renamed += 1

        if product.title != prepared_title:
            await upsert_game_alias(
                game_id,
                product.title,
                alias_type="edition",
                source="gog",
                source_key=product.product_id,
            )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
            from_source=True,
        )
        await upsert_game_platform_identifier(platform_id, GOG_PRODUCT_ID, product.product_id)

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info(
        "GOG sync (account API): added=%d matched=%d skipped=%d renamed=%d",
        added, matched, skipped, renamed,
    )
    return {
        "added": added,
        "matched": matched,
        "skipped": skipped,
        "renamed": renamed,
        "listing_backend": "account_api",
    }


async def _sync_from_lgogdownloader() -> dict:
    """The legacy slug listing: no product ids, so the title is the only key."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _LGOGDOWNLOADER_BIN,
            "--list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
    except Exception as exc:
        logger.warning("GOG sync failed (subprocess error): %s", exc)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": f"GOG sync failed: {exc}",
        }

    if proc.returncode != 0:
        stderr = stderr_bytes.decode(errors="replace")[:300]
        logger.warning(
            "lgogdownloader --list failed (rc=%d): %s",
            proc.returncode,
            stderr,
        )
        summary = f"lgogdownloader --list failed (rc={proc.returncode})"
        if stderr:
            summary = f"{summary}: {stderr}"
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": summary,
        }

    titles = _parse_lgogdownloader_output(stdout_bytes.decode())
    if not titles:
        logger.info("GOG sync: no games found in lgogdownloader output")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for title in titles:
        prepared_title = prepare_catalog_title(title)
        if prepared_title is None:
            skipped += 1
            continue
        igdb_platform_id = PLATFORM_TO_IGDB.get("gog")

        # This backend reports no product id, so the title is the only stable
        # key it has: a same-normalized-name row that already owns gog is this
        # exact catalog item re-syncing. Match it directly — re-running the
        # title through IGDB can land on a *different* same-named IGDB
        # candidate whose conflicting release year makes the fuzzy fallback
        # refuse the existing row and fork a duplicate (observed in prod:
        # "Agony", "Sigma Theory", "Under The Moon" pairs).
        existing = await get_platform_game_by_normalized_name(prepared_title, "gog")
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        else:
            game_id, igdb_game = await resolve_and_link_game(
                prepared_title, igdb_platform_id, candidates
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = prepared_title
                added += 1

        if title != prepared_title:
            await upsert_game_alias(
                game_id,
                title,
                alias_type="edition",
                source="gog",
                source_key=None,
            )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
            from_source=True,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
