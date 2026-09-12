"""Golden corpus for "is this the same title?" — the one decision that matters.

Two layers, both table-driven:

1. The KEY layer exercises the comparators directly (``match_key``,
   ``normalize_edition_comparison_title``, ``titles_conflict_on_identity``) through
   ``_gate_accepts``, which is the resolver's acceptance surface minus the
   network: the identity check and the edition-stripped equality gate, tried
   against the query and against each identity-preserving rung of the query
   ladder (the rungs that are allowed to vouch for identity themselves).

2. The RESOLVER layer runs the same corpus through the real
   ``_resolve_game_with_status`` with IGDB's two network calls patched, so a
   pair that "should match" is only green when a library row spelled one way
   actually links to an IGDB record spelled the other way.

Six normalizers used to fold six different subsets of this variation; that is
how the "&" bug (#184) and the Humble folding mismatch happened. Everything
here is a regression test for the single key that replaced them.
"""

import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import igdb
from gamelib_mcp.data.db.fuzzy import titles_conflict_on_identity
from gamelib_mcp.data.igdb import _generate_resolve_query_variants
from gamelib_mcp.data.title_normalization import (
    match_key,
    normalize_edition_comparison_title,
)


def _gate_accepts(query: str, candidate: str) -> bool:
    """Would the resolver's name gate accept ``candidate`` for ``query``?

    Mirrors _select_best_match's two conditions (no sequel/version identity
    conflict AND edition-stripped equality), applied to the query itself and to
    every identity-preserving ladder rung — the transformations that are
    allowed to vouch for identity, so their hits are gated against the rung
    rather than the original ("Sea of Thieves: 2026 Edition").
    """
    gates = [query] + [
        variant
        for variant, identity_preserving in _generate_resolve_query_variants(query)
        if identity_preserving
    ]
    return any(
        not titles_conflict_on_identity(gate, candidate)
        and normalize_edition_comparison_title(gate)
        == normalize_edition_comparison_title(candidate)
        for gate in gates
    )


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

# Two spellings of ONE title. Symmetric: neither side is the "decorated" one,
# so the gate must accept in both directions.
SPELLING_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("ampersand", "Rabbit and Steel", "Rabbit & Steel"),
    ("ampersand", "Ratchet & Clank", "Ratchet and Clank"),
    ("roman ii", "Hades II", "Hades 2"),
    ("roman iii", "Persona III", "Persona 3"),
    ("roman iv", "Diablo IV", "Diablo 4"),
    ("roman v", "Grand Theft Auto V", "Grand Theft Auto 5"),
    ("roman vii", "Final Fantasy VII", "Final Fantasy 7"),
    ("roman ix", "Dragon Quest IX", "Dragon Quest 9"),
    ("number word", "Left 4 Dead", "Left Four Dead"),
    ("number word", "Nine Sols", "9 Sols"),
    ("apostrophe", "Death's Door", "Deaths Door"),
    ("apostrophe", "Marvel's Spider-Man", "Marvels Spider Man"),
    ("curly apostrophe", "Marvel’s Spider-Man", "Marvel's Spider-Man"),
    ("dotted acronym", "F.E.A.R.", "FEAR"),
    ("dotted acronym", "Q.U.B.E. 2", "QUBE 2"),
    ("dotted acronym", "S.T.A.L.K.E.R.: Shadow of Chernobyl", "STALKER: Shadow of Chernobyl"),
    ("separators", "Half-Life", "Half Life"),
    ("separators", "Sid Meier's Civilization: Beyond Earth", "Sid Meiers Civilization Beyond Earth"),
    ("diacritics", "Pokémon Legends: Arceus", "Pokemon Legends: Arceus"),
    ("trademark", "Sekiro™: Shadows Die Twice", "Sekiro: Shadows Die Twice"),
    ("registered", "DOOM® Eternal", "DOOM Eternal"),
    ("versus", "Marvel vs. Capcom", "Marvel versus Capcom"),
    ("versus", "Marvel vs Capcom 3", "Marvel Versus Capcom 3"),
)

# A decorated library title and the base game IGDB actually holds. Directional:
# the gate strips the decoration off the QUERY, never off the candidate.
EDITION_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("goty", "Borderlands Game of the Year Edition", "Borderlands"),
    ("goty short", "Deus Ex GOTY Edition", "Deus Ex"),
    ("enhanced", "The Witcher: Enhanced Edition", "The Witcher"),
    ("definitive", "Age of Empires: Definitive Edition", "Age of Empires"),
    ("remastered", "BioShock Remastered", "BioShock"),
    ("ultimate", "Mortal Kombat 11 Ultimate Edition", "Mortal Kombat 11"),
    ("ps5 marker", "God of War (PS5)", "God of War"),
    ("switch marker", "Hollow Knight for Nintendo Switch", "Hollow Knight"),
    ("switch 2 edition", "Hollow Knight - Nintendo Switch 2 Edition", "Hollow Knight"),
    ("year edition", "Sea of Thieves: 2026 Edition", "Sea of Thieves"),
    # Generation 3: the gate's edition-stripped tier is
    # normalize_edition_comparison_title, whose generic "<up to 3 words>
    # Edition" tail reaches SKU names no curated list enumerates. Under the
    # narrow series-gap strip these three never met their base game.
    ("named edition", "Watch Dogs: Day One Edition", "Watch Dogs"),
    ("named edition", "DARK SOULS: Prepare To Die Edition", "Dark Souls"),
    ("decorated qualifier", "Marvel's Midnight Suns Digital+ Edition", "Marvel's Midnight Suns"),
)

# Different games that a looser key would collapse. Symmetric: neither
# direction may match.
DISTINCT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("sequel number", "Xenoblade Chronicles", "Xenoblade Chronicles 2"),
    ("sequel number", "Alan Wake", "Alan Wake 2"),
    ("letter is not ten", "Mega Man X", "Mega Man 10"),
    ("subtitled re-release", "Persona 3", "Persona 3 Reload"),
    ("remake is its own game", "Final Fantasy VII", "Final Fantasy VII Remake"),
    ("leading article", "The Forest", "Forest"),
    ("vr port", "PAYDAY 2", "Payday 2 VR"),
    ("decorated sibling", "Counter-Strike", "Counter-Strike Nexon"),
    ("prefixed sequel", "Tales from the Borderlands", "New Tales from the Borderlands"),
    ("episode", "Half-Life 2", "Half-Life 2: Episode One"),
    ("same series", "Ori and the Blind Forest", "Ori and the Will of the Wisps"),
    ("numbered sequel", "Star Wars: Battlefront", "Star Wars Battlefront II"),
    # The broader strip's boundary: a subtitle is not an edition suffix, and a
    # differently-decorated SKU is not the same product.
    ("collection", "Halo", "Halo: The Master Chief Collection"),
    ("subtitle", "Star Wars", "Star Wars: The Force Unleashed"),
    ("re-release subtitle", "Persona 5", "Persona 5 Royal"),
    # Both sides strip, and differently: "Sacred 2 Gold" collapses to "Sacred
    # 2" (a known qualifier) while "Sacred 2: Fallen Angel" keeps its subtitle,
    # so the two never meet — which is the honest answer for two different
    # products.
    ("different products", "Sacred 2: Fallen Angel", "Sacred 2 Gold"),
)


class MatchKeyTests(unittest.TestCase):
    """What the key folds, stated directly."""

    def test_folds_every_spelling_variation_in_the_corpus(self) -> None:
        for label, a, b in SPELLING_PAIRS:
            with self.subTest(label=label, a=a, b=b):
                self.assertEqual(match_key(a), match_key(b))

    def test_keeps_distinct_games_distinct(self) -> None:
        for label, a, b in DISTINCT_PAIRS:
            with self.subTest(label=label, a=a, b=b):
                self.assertNotEqual(match_key(a), match_key(b))

    def test_does_not_fold_x_or_a_lone_i(self) -> None:
        # "Mega Man X" is not "Mega Man 10", and a lone "I" is too ambiguous to
        # read as a number at all ("Warhammer 40,000: Dawn of War I").
        self.assertEqual(match_key("Mega Man X"), "mega man x")
        self.assertEqual(match_key("Rocket League I"), "rocket league i")

    def test_apostrophes_join_rather_than_split(self) -> None:
        self.assertEqual(match_key("Death's Door"), "deaths door")
        self.assertEqual(match_key("Marvel's Spider-Man"), "marvels spider man")

    def test_dotted_acronyms_join_but_abbreviations_do_not(self) -> None:
        self.assertEqual(match_key("F.E.A.R."), "fear")
        self.assertEqual(match_key("Q.U.B.E. 2"), "qube 2")
        self.assertEqual(
            match_key("S.T.A.L.K.E.R.: Shadow of Chernobyl"), "stalker shadow of chernobyl"
        )
        # A two-letter group is an abbreviation, not an acronym run.
        self.assertEqual(match_key("Mr. Driller"), "mr driller")

    def test_versus_folds_to_vs_as_a_whole_token(self) -> None:
        self.assertEqual(match_key("Marvel vs. Capcom"), "marvel vs capcom")
        self.assertEqual(match_key("Marvel versus Capcom"), "marvel vs capcom")
        # A whole token, never a substring: "Reversus" is not "Revs".
        self.assertEqual(match_key("Reversus"), "reversus")

    def test_normalize_search_text_is_left_alone(self) -> None:
        # It backs games.name_normalized and the FTS index: folding identity
        # variation into it would need a stored-column rebuild.
        from gamelib_mcp.data.title_normalization import normalize_search_text

        self.assertEqual(normalize_search_text("Death's Door"), "death s door")
        self.assertEqual(normalize_search_text("Hades II"), "hades ii")


class NameGateCorpusTests(unittest.TestCase):
    """The corpus through the resolver's acceptance surface, offline."""

    def test_spelling_variants_are_accepted_both_ways(self) -> None:
        for label, a, b in SPELLING_PAIRS:
            with self.subTest(label=label, query=a, candidate=b):
                self.assertTrue(_gate_accepts(a, b))
            with self.subTest(label=label, query=b, candidate=a):
                self.assertTrue(_gate_accepts(b, a))

    def test_edition_suffixes_are_stripped_off_the_query(self) -> None:
        for label, decorated, base in EDITION_PAIRS:
            with self.subTest(label=label, query=decorated, candidate=base):
                self.assertTrue(_gate_accepts(decorated, base))

    def test_distinct_games_are_refused_both_ways(self) -> None:
        for label, a, b in DISTINCT_PAIRS:
            with self.subTest(label=label, query=a, candidate=b):
                self.assertFalse(_gate_accepts(a, b))
            with self.subTest(label=label, query=b, candidate=a):
                self.assertFalse(_gate_accepts(b, a))


def _game(
    igdb_id: int,
    name: str,
    year: int | None = None,
    *,
    primary: bool = True,
    alt: list[str] | None = None,
) -> igdb.IGDBGame:
    return igdb.IGDBGame(
        igdb_id=igdb_id,
        name=name,
        category=igdb.CATEGORY_MAIN_GAME,
        first_release_date=f"{year}-06-01" if year is not None else None,
        is_primary_library_item=primary,
        alternative_names=list(alt or []),
    )


class ResolverCorpusTests(unittest.IsolatedAsyncioTestCase):
    """The same corpus end to end, with IGDB's two network calls faked.

    ``search_game`` answers every query with the whole candidate list (a
    relevance index that always returns what it holds); ``fetch_games_by_exact_name``
    answers with the candidates whose name equals the query, which is what the
    real equality filter does.
    """

    async def _resolve(
        self,
        query: str,
        candidates: list[igdb.IGDBGame],
        *,
        reference_release_date: str | None = None,
    ) -> igdb.IGDBGame | None:
        async def fake_search(name, igdb_platform_id=None, *, suppress_errors=True):
            return list(candidates)

        async def fake_exact(name, igdb_platform_id=None, *, suppress_errors=True):
            return [c for c in candidates if c.name.casefold() == name.casefold()]

        with (
            patch.dict("os.environ", {"TWITCH_CLIENT_ID": "cid"}, clear=False),
            patch("gamelib_mcp.data.igdb.search_game", AsyncMock(side_effect=fake_search)),
            patch(
                "gamelib_mcp.data.igdb.fetch_games_by_exact_name",
                AsyncMock(side_effect=fake_exact),
            ),
        ):
            outcome = await igdb._resolve_game_with_status(
                query, None, reference_release_date=reference_release_date
            )
        return outcome.game

    async def test_every_spelling_variant_resolves_to_the_other_spelling(self) -> None:
        for label, a, b in SPELLING_PAIRS:
            for query, held in ((a, b), (b, a)):
                with self.subTest(label=label, query=query, igdb_name=held):
                    match = await self._resolve(query, [_game(7, held, 2020)])
                    self.assertIsNotNone(match)
                    self.assertEqual(match.igdb_id, 7)

    async def test_every_edition_suffix_resolves_to_the_base_game(self) -> None:
        for label, decorated, base in EDITION_PAIRS:
            with self.subTest(label=label, query=decorated, igdb_name=base):
                match = await self._resolve(decorated, [_game(11, base, 2015)])
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 11)

    async def test_no_distinct_game_is_ever_accepted(self) -> None:
        for label, a, b in DISTINCT_PAIRS:
            for query, held in ((a, b), (b, a)):
                with self.subTest(label=label, query=query, igdb_name=held):
                    self.assertIsNone(await self._resolve(query, [_game(13, held, 2018)]))


class SameNameYearTiebreakTests(unittest.IsolatedAsyncioTestCase):
    """Two IGDB records, one title — the case no name key can settle."""

    async def _resolve(self, query, candidates, *, reference_release_date=None):
        return await ResolverCorpusTests._resolve(
            self, query, candidates, reference_release_date=reference_release_date
        )

    def _dead_space(self) -> tuple[igdb.IGDBGame, igdb.IGDBGame]:
        return _game(5, "Dead Space", 2008), _game(6, "Dead Space", 2023)

    async def test_the_remake_is_picked_by_reference_year_in_either_list_order(self) -> None:
        original, remake = self._dead_space()
        for order in ([original, remake], [remake, original]):
            with self.subTest(order=[g.igdb_id for g in order]):
                match = await self._resolve(
                    "Dead Space", order, reference_release_date="2023-01-27"
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 6)

    async def test_the_original_is_picked_by_reference_year_in_either_list_order(self) -> None:
        original, remake = self._dead_space()
        for order in ([original, remake], [remake, original]):
            with self.subTest(order=[g.igdb_id for g in order]):
                match = await self._resolve(
                    "Dead Space", order, reference_release_date="2008-10-13"
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 5)

    async def test_without_a_reference_year_the_same_name_pair_is_refused(self) -> None:
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve("Dead Space", list(self._dead_space()))
        self.assertIsNone(match)
        self.assertTrue(any("no reference year" in line for line in logs.output))

    async def test_inside_the_window_distance_decides_not_provider_order(self) -> None:
        # Reference 2023, candidates 2022 and 2023: the exact-year record wins
        # whichever IGDB listed first — provider order is never evidence.
        near, exact = _game(7, "Dead Space", 2022), _game(8, "Dead Space", 2023)
        for order in ([near, exact], [exact, near]):
            with self.subTest(order=[g.igdb_id for g in order]):
                match = await self._resolve(
                    "Dead Space", order, reference_release_date="2023-01-27"
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 8)

    async def test_two_records_equally_close_in_year_are_refused(self) -> None:
        # Same title, same year, two distinct records: equally plausible, and
        # picking by list position would flip the link on the next fetch.
        twins = [_game(9, "Dead Space", 2023), _game(10, "Dead Space", 2023)]
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve(
                "Dead Space", twins, reference_release_date="2023-01-27"
            )
        self.assertIsNone(match)
        self.assertTrue(any("equally close in year" in line for line in logs.output))

    async def test_a_title_carrying_its_own_year_resolves_the_ambiguity(self) -> None:
        # "Prey (2017)" — the library row's name is the only evidence there is.
        candidates = [_game(2, "Prey", 2006), _game(3, "Prey", 2017)]
        match = await self._resolve("Prey (2017)", candidates)
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 3)

    async def test_an_edition_named_remake_cannot_absorb_the_original(self) -> None:
        # "Mafia" (2002) vs "Mafia: Definitive Edition" (2020): the edition
        # strip folds the names together, so only the year refuses — and only
        # in this direction, because an edition cannot PREDATE its own game.
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve(
                "Mafia",
                [_game(21, "Mafia: Definitive Edition", 2020)],
                reference_release_date="2002-08-28",
            )
        self.assertIsNone(match)
        self.assertTrue(any("two years newer" in line for line in logs.output))

    async def test_the_reverse_direction_accepts_and_that_is_the_trade_off(self) -> None:
        # A library row NAMED "Mafia: Definitive Edition" (2020) accepts a lone
        # IGDB "Mafia" (2002): an older candidate reads as the original the
        # edition is an edition OF, which is right far more often than not
        # (every GOTY/Remastered row whose store date postdates the game). The
        # pair above is the one shape it gets wrong, and it stays wrong only
        # while IGDB holds no "Mafia: Definitive Edition" record of its own —
        # it does, and such a record wins as a tier-1 exact title.
        match = await self._resolve(
            "Mafia: Definitive Edition",
            [_game(20, "Mafia", 2002)],
            reference_release_date="2020-09-25",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 20)

    async def test_a_lone_edition_match_accepts_an_older_or_unknown_year(self) -> None:
        # "Deus Ex: Game of the Year Edition" bought in 2013; IGDB's "Deus Ex"
        # is from 2000, or carries no date at all. The row's year is the
        # store's re-listing date and says nothing about the game's release.
        for label, candidate in (
            ("unknown igdb year", _game(50, "Deus Ex")),
            ("older igdb year", _game(50, "Deus Ex", 2000)),
        ):
            with self.subTest(label=label):
                match = await self._resolve(
                    "Deus Ex: Game of the Year Edition",
                    [candidate],
                    reference_release_date="2013-02-01",
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 50)

    async def test_several_edition_matches_keep_the_symmetric_window(self) -> None:
        # Two distinct candidates is ambiguity again, so the one-sided window
        # does not apply: neither 1998 nor 2020 is within two years of 2013.
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve(
                "Thief: Definitive Edition",
                [_game(60, "Thief", 1998), _game(61, "Thief", 2020)],
                reference_release_date="2013-02-01",
            )
        self.assertIsNone(match)
        self.assertTrue(any("none within two years" in line for line in logs.output))

    async def test_the_real_original_still_resolves_with_the_same_reference_year(self) -> None:
        match = await self._resolve(
            "Mafia", [_game(20, "Mafia", 2002)], reference_release_date="2002-08-28"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 20)

    async def test_a_lone_exact_title_survives_a_far_off_year(self) -> None:
        # A store's re-release date says nothing about the game's first
        # release; one exact-title candidate is vouched for by its name.
        match = await self._resolve(
            "Grim Fandango", [_game(30, "Grim Fandango", 1998)],
            reference_release_date="2015-01-27",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 30)

    async def test_an_unknown_igdb_year_never_blocks_a_lone_exact_title(self) -> None:
        match = await self._resolve(
            "Yume Nikki", [_game(31, "Yume Nikki")], reference_release_date="2004-06-26"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 31)

    async def test_an_edition_suffix_still_resolves_within_a_year(self) -> None:
        match = await self._resolve(
            "Batman: Arkham Asylum Game of the Year Edition",
            [_game(40, "Batman: Arkham Asylum", 2009)],
            reference_release_date="2010-03-26",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 40)

    async def test_several_exact_titles_none_matching_the_year_are_refused(self) -> None:
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve(
                "Dead Space", list(self._dead_space()), reference_release_date="1995-01-01"
            )
        self.assertIsNone(match)
        self.assertTrue(any("none within a year" in line for line in logs.output))


class EditionStripFallbackTests(unittest.TestCase):
    """The broad strip's escape hatch, pinned.

    ``normalize_edition_comparison_title`` returns the RAW key when stripping
    empties a title, so a row whose whole name is an edition phrase collapses
    to itself rather than to "" — which would otherwise compare equal to every
    other fully-stripped name and match the entire catalog.
    """

    def test_a_title_that_is_only_an_edition_phrase_matches_nothing(self) -> None:
        for phrase in (
            "Definitive Edition",
            "The Complete Edition",
            ": Complete Edition",
            "Game of the Year Edition",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotEqual(normalize_edition_comparison_title(phrase), "")
                self.assertFalse(_gate_accepts(phrase, "Hollow Knight"))
                self.assertFalse(_gate_accepts("Hollow Knight", phrase))


class AlternativeNameResolverTests(unittest.IsolatedAsyncioTestCase):
    """IGDB's alternative names, through the real resolver.

    An abbreviation or a regional title is written down nowhere else: no
    normalization rule can turn "GTA V" into "Grand Theft Auto V", so the gate
    either reads IGDB's own spellings or those rows never link.
    """

    async def _resolve(self, query, candidates, *, reference_release_date=None):
        return await ResolverCorpusTests._resolve(
            self, query, candidates, reference_release_date=reference_release_date
        )

    async def test_an_abbreviation_resolves_to_the_full_title(self) -> None:
        candidate = _game(
            1020, "Grand Theft Auto V", 2013, alt=["GTA V", "GTA 5"]
        )
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve("GTA V", [candidate])

        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1020)
        # The log names the spelling that carried the match.
        self.assertTrue(
            any("via IGDB alternative name 'GTA V'" in line for line in logs.output)
        )

    async def test_the_ampersand_pair_resolves_through_an_alternative_name(self) -> None:
        for query, primary in (
            ("Rabbit and Steel", "Rabbit & Steel"),
            ("Rabbit & Steel", "Rabbit and Steel"),
        ):
            with self.subTest(query=query, igdb_name=primary):
                match = await self._resolve(
                    query, [_game(1040, primary, 2024, alt=[query])]
                )
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 1040)

    async def test_a_regional_primary_is_reached_only_by_its_alternative_name(self) -> None:
        # IGDB holds the Japanese title as the record's name; the library row
        # carries the western one. Nothing but the alternative name bridges it.
        with_alt = _game(1050, "Ryu ga Gotoku 0", 2015, alt=["Yakuza 0"])
        without_alt = _game(1050, "Ryu ga Gotoku 0", 2015)

        match = await self._resolve("Yakuza 0", [with_alt])
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1050)

        self.assertIsNone(await self._resolve("Yakuza 0", [without_alt]))

    async def test_a_conflicting_alternative_name_does_not_sink_a_matching_primary(self) -> None:
        # The names are filtered one by one: "Alan Wake II" conflicts and is
        # discarded, the primary still vouches for the record.
        match = await self._resolve(
            "Alan Wake", [_game(1060, "Alan Wake", 2010, alt=["Alan Wake II"])]
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1060)

    async def test_a_candidate_whose_every_name_conflicts_is_refused(self) -> None:
        # "Alan Wake 2" and its "Alan Wake II" spelling are the same sequel;
        # an alternative name must never smuggle in what the primary refused.
        self.assertIsNone(
            await self._resolve(
                "Alan Wake", [_game(1070, "Alan Wake 2", 2023, alt=["Alan Wake II"])]
            )
        )

    async def test_the_year_tiebreak_still_applies_to_an_alternative_name_match(self) -> None:
        candidates = [
            _game(1020, "Grand Theft Auto V", 2013, alt=["GTA V"]),
            _game(1021, "Grand Theft Auto V", 2022, alt=["GTA V"]),
        ]

        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            self.assertIsNone(await self._resolve("GTA V", candidates))
        self.assertTrue(any("no reference year" in line for line in logs.output))

        match = await self._resolve(
            "GTA V", candidates, reference_release_date="2013-09-17"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1020)


class NamedEditionYearTests(unittest.IsolatedAsyncioTestCase):
    """The broader tier-2 strip, under the year rules that make it safe."""

    async def _resolve(self, query, candidates, *, reference_release_date=None):
        return await ResolverCorpusTests._resolve(
            self, query, candidates, reference_release_date=reference_release_date
        )

    async def test_a_named_edition_reaches_its_base_game_at_the_same_year(self) -> None:
        match = await self._resolve(
            "Watch Dogs: Day One Edition",
            [_game(1080, "Watch Dogs", 2014)],
            reference_release_date="2014-05-27",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1080)

    async def test_a_named_edition_cannot_absorb_a_much_newer_record(self) -> None:
        # The tier-2 one-sided window is what keeps the broader strip honest:
        # a 2014 row may reach an older or same-age record, never a 2023 one.
        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            match = await self._resolve(
                "Watch Dogs: Day One Edition",
                [_game(1081, "Watch Dogs", 2023)],
                reference_release_date="2014-05-27",
            )
        self.assertIsNone(match)
        self.assertTrue(any("two years newer" in line for line in logs.output))


class PrimaryOverAlternativeNameTierTests(unittest.IsolatedAsyncioTestCase):
    """A record's OWN name outranks a spelling it merely also answers to.

    Alternative names carry working titles, acronyms and regional names that
    legitimately coincide with another record's primary title — "Titan" is
    Blizzard's working title for "Overwatch" and also a 2019 game of its own.
    Ranking them equally turned a clean exact-title match into a same-name
    ambiguity refusal, which generation 2 (blind to alternative names) linked
    correctly.
    """

    async def _resolve(self, query, candidates, *, reference_release_date=None):
        return await ResolverCorpusTests._resolve(
            self, query, candidates, reference_release_date=reference_release_date
        )

    def _titan_pair(self) -> tuple[igdb.IGDBGame, igdb.IGDBGame]:
        return (
            _game(1200, "Titan", 2019),
            _game(1201, "Overwatch", 2016, alt=["Titan"]),
        )

    async def test_a_primary_exact_match_wins_over_an_alt_name_coincidence(self) -> None:
        own, borrowed = self._titan_pair()
        for order in ([own, borrowed], [borrowed, own]):
            for reference in (None, "2019-03-26"):
                with self.subTest(
                    order=[g.igdb_id for g in order], reference=reference
                ):
                    match = await self._resolve(
                        "Titan", order, reference_release_date=reference
                    )
                    self.assertIsNotNone(match)
                    self.assertEqual(match.igdb_id, 1200)

    async def test_an_alt_name_still_resolves_when_no_primary_matches(self) -> None:
        # The 1b band is not decoration: with no primary-name match it is the
        # whole answer, and "GTA V" has no other way in.
        match = await self._resolve(
            "GTA V", [_game(1020, "Grand Theft Auto V", 2013, alt=["GTA V", "GTA 5"])]
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1020)

    async def test_two_alt_only_records_keep_the_ordinary_year_rules(self) -> None:
        # Nothing promotes one alt-name match over another, so the same-name
        # year rules decide — refusing without a reference year, resolving with.
        candidates = [
            _game(1201, "Overwatch", 2016, alt=["Titan"]),
            _game(1202, "Prometheus", 2021, alt=["Titan"]),
        ]

        with self.assertLogs("gamelib_mcp.data.igdb", level="INFO") as logs:
            self.assertIsNone(await self._resolve("Titan", candidates))
        self.assertTrue(any("no reference year" in line for line in logs.output))

        match = await self._resolve(
            "Titan", candidates, reference_release_date="2016-05-24"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.igdb_id, 1201)

    async def test_the_same_precedence_applies_to_edition_stripped_matches(self) -> None:
        # Tier 2 splits the same way: an edition-stripped match on the
        # candidate's own name outranks one carried by an alternative name.
        own = _game(1210, "Watch Dogs: Complete Edition", 2014)
        borrowed = _game(1211, "Some Other Game", 2021, alt=["Watch Dogs: Gold Edition"])
        for order in ([own, borrowed], [borrowed, own]):
            with self.subTest(order=[g.igdb_id for g in order]):
                match = await self._resolve("Watch Dogs", order)
                self.assertIsNotNone(match)
                self.assertEqual(match.igdb_id, 1210)


class AlternativeNameContractTests(unittest.TestCase):
    """The cap and cleanup live in the dataclass, so every record obeys them."""

    def test_a_hand_built_record_is_cleaned_and_capped(self) -> None:
        game = _game(
            1300,
            "Grand Theft Auto V",
            2013,
            alt=[
                "  GTA V  ",
                "gta v",
                "Grand Theft Auto V",
                "",
                *[f"Alt {i}" for i in range(30)],
            ],
        )
        self.assertEqual(len(game.alternative_names), igdb.ALTERNATIVE_NAME_CAP)
        self.assertEqual(game.alternative_names[0], "GTA V")
        self.assertNotIn("Grand Theft Auto V", game.alternative_names)
