"""Declarative scrape descriptors: defaults in code, overrides in the DB.

Each scraper's *healable* surface — URL templates, CSS selectors, regexes,
JSON paths, cache TTLs, pagination caps, fuzzy cutoffs — lives in a frozen
dataclass here. The imperative parts (auth flows, DOM sibling-walks, score
fusion, reconciliation guards) stay in the provider modules and cannot be
changed through this layer.

Overrides are versioned rows in the ``scrape_config`` table (v17). Code-level
defaults are the implicit version 0: an empty table, a malformed row, or any
load error all resolve to defaults — a bad override can degrade scraping, but
it can never crash a sync. Override dicts may be *partial*; they are merged
over the defaults at load time, so a heal proposal only needs to contain the
fields that actually changed.

The config vocabulary is deliberately data-only. Every field is typed by its
``kind`` metadata and validated accordingly:

- ``url_template``: https only, host frozen to the provider's ALLOWED_HOSTS
  (an override can restyle a path, never redirect the scraper to another
  site), placeholders restricted to the field's declared set.
- ``selector`` / ``selector_tuple``: must compile under soupsieve, length-capped.
- ``regex``: must compile, length-capped, minimum capture-group count enforced.
- ``json_key`` / ``json_key_tuple``: short plain strings (JSON path steps).
- ``slug_map``: lowercase-slug → lowercase-slug string map.
- ``int``: bounds enforced per field.

Nothing here is executable, so the worst a hostile or mistaken override can
express is a wrong-but-plausible extraction — which scrape_validate.py's
fixture replay and live sanity checks exist to catch before a row is trusted.
"""

from __future__ import annotations

import json
import logging
import re
import string
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import soupsieve

logger = logging.getLogger(__name__)

_MAX_SELECTOR_LEN = 300
_MAX_REGEX_LEN = 200
_MAX_TEMPLATE_LEN = 300
_MAX_JSON_KEY_LEN = 64
_MAX_SLUG_LEN = 64
_MAX_TUPLE_ITEMS = 10
_MAX_SLUG_MAP_ITEMS = 40


class ScrapeConfigError(ValueError):
    """A candidate config dict failed structural validation."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class BackloggdScrapeConfig:
    url_template: str = field(
        default="https://backloggd.com/u/{user}/reviews",
        metadata={"kind": "url_template", "placeholders": frozenset({"user"})},
    )
    page_url_template: str = field(
        default="https://backloggd.com/u/{user}/reviews/page/{page}",
        metadata={"kind": "url_template", "placeholders": frozenset({"user", "page"})},
    )
    review_card_selector: str = field(default=".review-card", metadata={"kind": "selector"})
    # The class checked on preceding siblings during the title walk, and the
    # selectors used to pull the title out of (or from within) that sibling.
    title_container_class: str = field(default="game-name", metadata={"kind": "json_key"})
    title_selector: str = field(default=".game-name h3", metadata={"kind": "selector"})
    title_inner_selector: str = field(default="h3", metadata={"kind": "selector"})
    score_selector: str = field(default=".stars-top", metadata={"kind": "selector"})
    score_style_regex: str = field(
        default=r"width:\s*([\d.]+)%", metadata={"kind": "regex", "min_groups": 1}
    )
    review_text_selector: str = field(
        default=".review-body .card-text", metadata={"kind": "selector"}
    )
    pagination_cap: int = field(default=100, metadata={"kind": "int", "min": 1, "max": 500})
    fuzzy_cutoff: int = field(default=85, metadata={"kind": "int", "min": 50, "max": 100})


@dataclass(frozen=True)
class SteamReviewsScrapeConfig:
    url_template: str = field(
        default="https://steamcommunity.com/id/{user}/recommended/",
        metadata={"kind": "url_template", "placeholders": frozenset({"user"})},
    )
    page_url_template: str = field(
        default="https://steamcommunity.com/id/{user}/recommended/?p={page}",
        metadata={"kind": "url_template", "placeholders": frozenset({"user", "page"})},
    )
    review_box_selector: str = field(
        default=".review_box, [class*='review_box']", metadata={"kind": "selector"}
    )
    review_link_selector: str = field(
        default="a[href*='/recommended/']", metadata={"kind": "selector"}
    )
    appid_regex: str = field(
        default=r"/recommended/(\d+)/", metadata={"kind": "regex", "min_groups": 1}
    )
    thumb_up_selector: str = field(
        default=".thumb_up, .thumbsUp, [class*='thumbsUp'], [class*='thumb_up']",
        metadata={"kind": "selector"},
    )
    thumb_down_selector: str = field(
        default=".thumb_down, .thumbsDown, [class*='thumbsDown'], [class*='thumb_down']",
        metadata={"kind": "selector"},
    )
    rating_summary_selector: str = field(
        default=".title, [class*='ratingSummary']", metadata={"kind": "selector"}
    )
    review_text_selector: str = field(
        default=".content, [class*='review_content'] p, [class*='apphub_CardTextContent']",
        metadata={"kind": "selector"},
    )
    pagination_cap: int = field(default=200, metadata={"kind": "int", "min": 1, "max": 500})


def _default_metacritic_platform_query_values() -> dict[str, str]:
    return {
        "steam": "pc",
        "epic": "pc",
        "gog": "pc",
        "ps5": "playstation-5",
        "ps4": "playstation-4",
        "switch": "nintendo-switch",
        "switch2": "nintendo-switch-2",
        "xbox-series-x": "xbox-series-x",
        "xbox-one": "xbox-one",
    }


@dataclass(frozen=True)
class MetacriticScrapeConfig:
    game_url_template: str = field(
        default="https://www.metacritic.com/game/{slug}/",
        metadata={"kind": "url_template", "placeholders": frozenset({"slug"})},
    )
    platform_game_url_template: str = field(
        default="https://www.metacritic.com/game/{platform_slug}/{slug}/",
        metadata={"kind": "url_template", "placeholders": frozenset({"platform_slug", "slug"})},
    )
    platform_query_values: dict[str, str] = field(
        default_factory=_default_metacritic_platform_query_values,
        metadata={"kind": "slug_map"},
    )
    critic_score_selectors: tuple[str, ...] = field(
        default=('[data-testid="score-meta-critic"]', ".metascore_w"),
        metadata={"kind": "selector_tuple"},
    )
    cache_days: int = field(default=30, metadata={"kind": "int", "min": 1, "max": 365})


@dataclass(frozen=True)
class DekuDealsScrapeConfig:
    # The wishlist URL itself comes from DEKUDEALS_WISHLIST_URL (user-specific
    # share link), not from this config — only the JSON item paths live here.
    items_keys: tuple[str, ...] = field(
        default=("items", "games"), metadata={"kind": "json_key_tuple"}
    )
    title_keys: tuple[str, ...] = field(
        default=("title", "name"), metadata={"kind": "json_key_tuple"}
    )
    added_at_key: str = field(default="added_at", metadata={"kind": "json_key"})
    fuzzy_cutoff: int = field(default=85, metadata={"kind": "int", "min": 50, "max": 100})


ScrapeConfig = (
    BackloggdScrapeConfig
    | SteamReviewsScrapeConfig
    | MetacriticScrapeConfig
    | DekuDealsScrapeConfig
)

SCRAPE_PROVIDERS: dict[str, type] = {
    "backloggd": BackloggdScrapeConfig,
    "steam_reviews": SteamReviewsScrapeConfig,
    "metacritic": MetacriticScrapeConfig,
    "dekudeals": DekuDealsScrapeConfig,
}

# Frozen per-provider host allowlists. Overrides may reshape paths and query
# strings but can never point a scraper at a different host — this is the main
# containment line against a prompt-injected "heal" turning into exfiltration
# or a poisoned data source.
ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "backloggd": frozenset({"backloggd.com", "www.backloggd.com"}),
    "steam_reviews": frozenset({"steamcommunity.com"}),
    "metacritic": frozenset({"metacritic.com", "www.metacritic.com"}),
    "dekudeals": frozenset(),  # no configurable URLs
}


def default_config(provider: str) -> Any:
    """Return the code-level default config instance for ``provider``."""
    try:
        cls = SCRAPE_PROVIDERS[provider]
    except KeyError:
        raise ScrapeConfigError([f"Unknown scrape provider '{provider}'. Valid: {sorted(SCRAPE_PROVIDERS)}"]) from None
    return cls()


def config_to_dict(config: Any) -> dict[str, Any]:
    """Serialize a config instance to a JSON-safe dict (tuples become lists)."""
    result: dict[str, Any] = {}
    for f in fields(config):
        value = getattr(config, f.name)
        if isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        result[f.name] = value
    return result


def _template_placeholders(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }


def _validate_selector(name: str, value: Any, problems: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{name}: must be a non-empty CSS selector string")
        return
    if len(value) > _MAX_SELECTOR_LEN:
        problems.append(f"{name}: selector exceeds {_MAX_SELECTOR_LEN} characters")
        return
    try:
        soupsieve.compile(value)
    except Exception as exc:
        problems.append(f"{name}: invalid CSS selector ({exc})")


def _validate_field(provider: str, f: Any, value: Any, problems: list[str]) -> None:
    kind = f.metadata.get("kind")
    name = f.name

    if kind == "url_template":
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{name}: must be a non-empty URL template string")
            return
        if len(value) > _MAX_TEMPLATE_LEN:
            problems.append(f"{name}: template exceeds {_MAX_TEMPLATE_LEN} characters")
            return
        allowed_placeholders = f.metadata["placeholders"]
        try:
            placeholders = _template_placeholders(value)
        except ValueError as exc:
            problems.append(f"{name}: malformed template ({exc})")
            return
        extra = placeholders - allowed_placeholders
        if extra:
            problems.append(
                f"{name}: unexpected placeholders {sorted(extra)}; allowed: {sorted(allowed_placeholders)}"
            )
        # Every declared placeholder is load-bearing: a page_url_template
        # without {page} would re-fetch page 1 until the pagination cap (the
        # live trial only fetches page 1, so it wouldn't catch that), and a
        # template missing {user}/{slug} would scrape the wrong page entirely.
        missing = allowed_placeholders - placeholders
        if missing:
            problems.append(
                f"{name}: missing required placeholders {sorted(missing)}"
            )
        # Substitute dummy values so urlsplit sees a concrete URL.
        concrete = value
        for placeholder in placeholders:
            concrete = concrete.replace("{" + placeholder + "}", "x")
        parts = urlsplit(concrete)
        if parts.scheme != "https":
            problems.append(f"{name}: URL template must use https")
        allowed_hosts = ALLOWED_HOSTS.get(provider, frozenset())
        if parts.hostname not in allowed_hosts:
            problems.append(
                f"{name}: host '{parts.hostname}' not in the {provider} allowlist {sorted(allowed_hosts)}"
            )
    elif kind == "selector":
        _validate_selector(name, value, problems)
    elif kind == "selector_tuple":
        if not isinstance(value, tuple) or not value or len(value) > _MAX_TUPLE_ITEMS:
            problems.append(f"{name}: must be a list of 1-{_MAX_TUPLE_ITEMS} CSS selectors")
            return
        for index, item in enumerate(value):
            _validate_selector(f"{name}[{index}]", item, problems)
    elif kind == "regex":
        if not isinstance(value, str) or not value:
            problems.append(f"{name}: must be a non-empty regex string")
            return
        if len(value) > _MAX_REGEX_LEN:
            problems.append(f"{name}: regex exceeds {_MAX_REGEX_LEN} characters")
            return
        try:
            compiled = re.compile(value)
        except re.error as exc:
            problems.append(f"{name}: invalid regex ({exc})")
            return
        min_groups = f.metadata.get("min_groups", 0)
        if compiled.groups < min_groups:
            problems.append(f"{name}: regex needs at least {min_groups} capture group(s)")
    elif kind == "json_key":
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_JSON_KEY_LEN:
            problems.append(f"{name}: must be a short non-empty string")
    elif kind == "json_key_tuple":
        if not isinstance(value, tuple) or not value or len(value) > _MAX_TUPLE_ITEMS:
            problems.append(f"{name}: must be a list of 1-{_MAX_TUPLE_ITEMS} JSON keys")
            return
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip() or len(item) > _MAX_JSON_KEY_LEN:
                problems.append(f"{name}[{index}]: must be a short non-empty string")
    elif kind == "slug_map":
        if not isinstance(value, dict) or len(value) > _MAX_SLUG_MAP_ITEMS:
            problems.append(f"{name}: must be a map of at most {_MAX_SLUG_MAP_ITEMS} platform slugs")
            return
        for key, item in value.items():
            for label, candidate in ((f"{name} key", key), (f"{name}['{key}']", item)):
                if (
                    not isinstance(candidate, str)
                    or not candidate
                    or len(candidate) > _MAX_SLUG_LEN
                    or not re.fullmatch(r"[a-z0-9-]+", candidate)
                ):
                    problems.append(f"{label}: must be a lowercase slug (a-z, 0-9, '-')")
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{name}: must be an integer")
            return
        minimum = f.metadata.get("min")
        maximum = f.metadata.get("max")
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            problems.append(f"{name}: must be between {minimum} and {maximum}")
    else:  # pragma: no cover - would be a coding error in this module
        problems.append(f"{name}: unknown field kind '{kind}'")


def config_from_dict(provider: str, data: dict[str, Any]) -> Any:
    """Merge a (possibly partial) override dict over the provider defaults.

    Raises ScrapeConfigError listing every problem: unknown keys, wrong types,
    selectors/regexes that don't compile, URL templates off the host allowlist,
    out-of-bounds ints.
    """
    cls = SCRAPE_PROVIDERS.get(provider)
    if cls is None:
        raise ScrapeConfigError(
            [f"Unknown scrape provider '{provider}'. Valid: {sorted(SCRAPE_PROVIDERS)}"]
        )
    if not isinstance(data, dict):
        raise ScrapeConfigError(["config must be a JSON object"])

    known = {f.name: f for f in fields(cls)}
    problems = [f"unknown config key '{key}'" for key in data if key not in known]

    values: dict[str, Any] = {}
    for name, f in known.items():
        if name not in data:
            continue
        value = data[name]
        kind = f.metadata.get("kind")
        if kind in ("selector_tuple", "json_key_tuple") and isinstance(value, list):
            value = tuple(value)
        _validate_field(provider, f, value, problems)
        values[name] = value

    if problems:
        raise ScrapeConfigError(problems)
    return cls(**values)


def validate_config_dict(provider: str, data: dict[str, Any]) -> list[str]:
    """Return the list of structural problems in ``data`` (empty = valid)."""
    try:
        config_from_dict(provider, data)
    except ScrapeConfigError as exc:
        return exc.problems
    return []


# ── DB-backed override lifecycle ─────────────────────────────────────────────


async def get_active_scrape_config_row(provider: str) -> dict | None:
    from .db import get_db

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT * FROM scrape_config WHERE provider = ? AND status = 'active'",
            (provider,),
        )
    return dict(row) if row else None


async def list_scrape_config_rows(provider: str) -> list[dict]:
    from .db import get_db

    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM scrape_config WHERE provider = ? ORDER BY version DESC",
            (provider,),
        )
    return [dict(row) for row in rows]


async def insert_scrape_config_version(
    provider: str,
    config: dict[str, Any],
    *,
    status: str,
    source: str,
    note: str | None = None,
    validation_report: dict | None = None,
) -> int:
    """Append a new config version; if it activates, supersede the previous active."""
    from .db import get_db

    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        if status == "active":
            await db.execute(
                "UPDATE scrape_config SET status = 'superseded' WHERE provider = ? AND status = 'active'",
                (provider,),
            )
        row = await db.execute_fetchone(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM scrape_config WHERE provider = ?",
            (provider,),
        )
        version = int(row["next_version"])
        await db.execute(
            """INSERT INTO scrape_config
                   (provider, version, config_json, status, source, note, validation_report, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                provider,
                version,
                json.dumps(config),
                status,
                source,
                note,
                json.dumps(validation_report) if validation_report is not None else None,
                now,
            ),
        )
        await db.commit()
    return version


async def activate_scrape_config_version(provider: str, version: int) -> None:
    """Promote a pending version to active (superseding the current active)."""
    from .db import get_db

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT status FROM scrape_config WHERE provider = ? AND version = ?",
            (provider, version),
        )
        if row is None:
            raise ScrapeConfigError([f"No {provider} config version {version}"])
        if row["status"] != "pending":
            raise ScrapeConfigError(
                [f"{provider} config version {version} is '{row['status']}', not 'pending'"]
            )
        await db.execute(
            "UPDATE scrape_config SET status = 'superseded' WHERE provider = ? AND status = 'active'",
            (provider,),
        )
        await db.execute(
            "UPDATE scrape_config SET status = 'active' WHERE provider = ? AND version = ?",
            (provider, version),
        )
        await db.commit()


async def rollback_scrape_config_db(provider: str) -> int | None:
    """Retire the active override; reactivate the previous one if any.

    Returns the version now active, or None when the provider is back on the
    code-level defaults. Rolling back with no active override is a no-op that
    returns None (defaults were already in effect).
    """
    from .db import get_db

    async with get_db() as db:
        active = await db.execute_fetchone(
            "SELECT version FROM scrape_config WHERE provider = ? AND status = 'active'",
            (provider,),
        )
        if active is None:
            return None
        await db.execute(
            "UPDATE scrape_config SET status = 'rolled_back' WHERE provider = ? AND status = 'active'",
            (provider,),
        )
        previous = await db.execute_fetchone(
            """SELECT version FROM scrape_config
               WHERE provider = ? AND status = 'superseded' AND version < ?
               ORDER BY version DESC LIMIT 1""",
            (provider, active["version"]),
        )
        restored: int | None = None
        if previous is not None:
            restored = int(previous["version"])
            await db.execute(
                "UPDATE scrape_config SET status = 'active' WHERE provider = ? AND version = ?",
                (provider, restored),
            )
        await db.commit()
    return restored


async def load_scrape_config(provider: str) -> Any:
    """Return the effective config: active DB override merged over defaults.

    Fails open to defaults on any error — a malformed or stale override row
    degrades to default scraping behavior instead of breaking a sync.
    """
    defaults = default_config(provider)
    try:
        row = await get_active_scrape_config_row(provider)
    except Exception:
        logger.exception("Failed to read scrape_config for %s; using defaults", provider)
        return defaults
    if row is None:
        return defaults
    try:
        return config_from_dict(provider, json.loads(row["config_json"]))
    except (ScrapeConfigError, ValueError, TypeError) as exc:
        logger.warning(
            "Ignoring invalid active scrape_config v%s for %s: %s",
            row.get("version"),
            provider,
            exc,
        )
        return defaults
