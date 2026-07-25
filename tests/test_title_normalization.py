import unittest

from gamelib_mcp.data.title_normalization import (
    is_edition_variant_of,
    normalize_edition_comparison_title,
    normalize_purchase_title,
    normalize_search_text,
    prepare_catalog_title,
)


class NormalizeSearchTextTests(unittest.TestCase):
    def test_strips_punctuation_to_token_boundaries(self) -> None:
        self.assertEqual(
            normalize_search_text("Sekiro: Shadows Die Twice"),
            "sekiro shadows die twice",
        )
        self.assertEqual(normalize_search_text("Don't Starve"), "don t starve")
        self.assertEqual(normalize_search_text("Half-Life 2"), "half life 2")

    def test_folds_trademark_glyphs_and_accents(self) -> None:
        self.assertEqual(normalize_search_text("NieR™ Replicant"), "nier replicant")
        self.assertEqual(normalize_search_text("Pokémon"), "pokemon")

    def test_collapses_whitespace_and_casefolds(self) -> None:
        self.assertEqual(normalize_search_text("  HADES   II  "), "hades ii")

    def test_punctuation_only_input_normalizes_to_empty(self) -> None:
        self.assertEqual(normalize_search_text("%"), "")
        self.assertEqual(normalize_search_text("™:!_"), "")


class TitleNormalizationTests(unittest.TestCase):
    def test_prepare_catalog_title_skips_obvious_non_game_rows(self) -> None:
        self.assertIsNone(prepare_catalog_title("H1Z1: Test Server"))
        self.assertIsNone(prepare_catalog_title("Death Stranding Content"))
        self.assertIsNone(prepare_catalog_title("Q.U.B.E. 2 Soundtrack"))
        self.assertIsNone(prepare_catalog_title("Chivalry 2 - Public Testing"))
        self.assertIsNone(prepare_catalog_title("Conan Exiles - Public Beta Client"))
        self.assertIsNone(prepare_catalog_title("Hello Neighbor Demo"))
        self.assertIsNone(prepare_catalog_title("Civilization VI : Australia Civilization & Scenario Pack"))
        self.assertIsNone(prepare_catalog_title("Europa Universalis IV: Catholic Majors Unit Pack"))
        self.assertIsNone(prepare_catalog_title("Cyberpunk 2077 Goodies Collection"))
        self.assertIsNone(prepare_catalog_title("Blink and Dash VFX"))
        self.assertIsNone(prepare_catalog_title("FM21 Editor"))
        self.assertIsNone(prepare_catalog_title("Football Manager 2022 Editor"))
        self.assertIsNone(prepare_catalog_title("Model Builder: Expansion Pack no.1"))

    def test_prepare_catalog_title_keeps_real_titles_with_overlap_words(self) -> None:
        self.assertEqual(prepare_catalog_title("Content Warning"), "Content Warning")
        self.assertEqual(prepare_catalog_title("DLC Quest"), "DLC Quest")
        self.assertEqual(prepare_catalog_title("The Stanley Parable Demo"), "The Stanley Parable Demo")
        self.assertEqual(prepare_catalog_title("Beta Max"), "Beta Max")
        self.assertEqual(prepare_catalog_title("Hogwarts Legacy"), "Hogwarts Legacy")
        self.assertEqual(prepare_catalog_title("Squid Game"), "Squid Game")
        self.assertEqual(prepare_catalog_title("STREET FIGHTER 6"), "STREET FIGHTER 6")
        self.assertEqual(
            prepare_catalog_title("METAL GEAR SOLID - Master Collection Version"),
            "METAL GEAR SOLID - Master Collection Version",
        )

    def test_prepare_catalog_title_normalizes_storefront_variants(self) -> None:
        self.assertEqual(
            prepare_catalog_title("Batman: Arkham Asylum GOTY Edition"),
            "Batman: Arkham Asylum",
        )
        self.assertEqual(
            prepare_catalog_title("Grand Theft Auto V (PlayStation®5)"),
            "Grand Theft Auto V",
        )
        self.assertEqual(
            prepare_catalog_title("Hollow Knight – Nintendo Switch 2 Edition"),
            "Hollow Knight",
        )
        self.assertEqual(
            prepare_catalog_title("LEGO® Star Wars™: The Skywalker Saga"),
            "LEGO Star Wars: The Skywalker Saga",
        )

    def test_prepare_catalog_title_strips_dangling_separator_after_suffix(self) -> None:
        # A stripped subtitle/edition must not leave a trailing ":" or "-".
        self.assertEqual(
            prepare_catalog_title("Deus Ex: Game of the Year Edition"), "Deus Ex"
        )
        self.assertEqual(
            prepare_catalog_title("Sleeping Dogs: Definitive Edition"), "Sleeping Dogs"
        )
        self.assertEqual(
            prepare_catalog_title("Mafia III: Definitive Edition"), "Mafia III"
        )
        self.assertEqual(
            prepare_catalog_title("Ori and the Blind Forest: Definitive Edition"),
            "Ori and the Blind Forest",
        )
        # Internal colons in a retained title are untouched.
        self.assertEqual(
            prepare_catalog_title("Batman: Arkham Asylum"), "Batman: Arkham Asylum"
        )


class NormalizePurchaseTitleTests(unittest.TestCase):
    def test_strips_switch2_edition_and_trademark_glyphs(self) -> None:
        self.assertEqual(
            normalize_purchase_title("DAVE THE DIVER Nintendo Switch™ 2 Edition"),
            "DAVE THE DIVER",
        )
        self.assertEqual(
            normalize_purchase_title("No Man's Sky – Nintendo Switch™ 2 Edition"),
            "No Man's Sky",
        )

    def test_strips_switch2_upgrade_pack_marker(self) -> None:
        # The upgrade-pack marker sits below the edition suffix; both peel off.
        self.assertEqual(
            normalize_purchase_title(
                "Hollow Knight – Nintendo Switch 2 Edition-upgradepack"
            ),
            "Hollow Knight",
        )
        self.assertEqual(
            normalize_purchase_title(
                "Red Dead Redemption: Nintendo Switch™ 2 Edition-upgradepack"
            ),
            "Red Dead Redemption",
        )

    def test_strips_marketing_editions(self) -> None:
        self.assertEqual(
            normalize_purchase_title("Danganronpa: Trigger Happy Havoc Anniversary Edition"),
            "Danganronpa: Trigger Happy Havoc",
        )
        self.assertEqual(normalize_purchase_title("LUMINES REMASTERED"), "LUMINES")

    def test_leaves_bundle_and_sequel_titles_intact(self) -> None:
        # A bundle name is NOT an edition suffix — it must survive so it can't
        # false-match a single constituent game. Sequel numbers stay too.
        self.assertEqual(
            normalize_purchase_title("Blasphemous + Blasphemous 2 Bundle"),
            "Blasphemous + Blasphemous 2 Bundle",
        )
        self.assertEqual(normalize_purchase_title("Portal 2"), "Portal 2")


class PurchaseSkuSuffixTests(unittest.TestCase):
    """Purchase-history SKU decorations (region/package/edition markers).

    Real misses from a prod Steam purchase import — every one of these SKU
    names failed to match its library row until stripped.
    """

    def test_strips_region_markers(self) -> None:
        self.assertEqual(
            normalize_purchase_title("Fallout New Vegas Ultimate ROW"),
            "Fallout New Vegas",
        )
        self.assertEqual(
            normalize_purchase_title("Sekiro: Shadows Die Twice (Rest of World)"),
            "Sekiro: Shadows Die Twice",
        )
        self.assertEqual(
            normalize_purchase_title("Deus Ex: Human Revolution - Director's Cut (ROW)"),
            "Deus Ex: Human Revolution",
        )

    def test_strips_package_kind_markers(self) -> None:
        self.assertEqual(normalize_purchase_title("Nidhogg Store"), "Nidhogg")
        self.assertEqual(normalize_purchase_title("Teleglitch: Base Game"), "Teleglitch")

    def test_strips_bare_edition_words(self) -> None:
        self.assertEqual(
            normalize_purchase_title("Oblivion Game of the Year Deluxe"), "Oblivion"
        )
        self.assertEqual(
            normalize_purchase_title("Saints Row IV Game of the Century Edition"),
            "Saints Row IV",
        )
        self.assertEqual(
            normalize_purchase_title("Morrowind Game of the Year"), "Morrowind"
        )

    def test_strips_standard_edition_tail(self) -> None:
        self.assertEqual(
            normalize_purchase_title("Nioh: Complete Edition Standard"),
            "Nioh: Complete Edition",
        )
        self.assertEqual(
            normalize_purchase_title("Monster Hunter World Standard Edition"),
            "Monster Hunter World",
        )

    def test_strips_capcom_biohazard_alternate_name(self) -> None:
        # Capcom store titles carry the JP-market name as a "GAME / BIOHAZARD …"
        # tail that no library row uses; the edition tail rides along after it.
        self.assertEqual(
            normalize_purchase_title(
                "RESIDENT EVIL 2 / BIOHAZARD RE:2 Standard Edition"
            ),
            "RESIDENT EVIL 2",
        )
        self.assertEqual(
            normalize_purchase_title(
                "RESIDENT EVIL VILLAGE / BIOHAZARD VILLAGE"
            ),
            "RESIDENT EVIL VILLAGE",
        )

    def test_complete_is_never_stripped(self) -> None:
        # "X Complete" routinely names a multi-game compilation (Hexcells
        # Complete = three games) — stripping it would book the compilation's
        # price onto the base game.
        self.assertEqual(normalize_purchase_title("Hexcells Complete"), "Hexcells Complete")

    def test_strips_early_access_marker_and_ultra_tail(self) -> None:
        # Old Humble bundle keys carry store-state and SKU tails the library
        # row never does ("GRAV (Early Access)", "Beat Hazard Ultra").
        self.assertEqual(normalize_purchase_title("GRAV (Early Access)"), "GRAV")
        self.assertEqual(normalize_purchase_title("Streamline Early Access"), "Streamline")
        self.assertEqual(normalize_purchase_title("Beat Hazard Ultra"), "Beat Hazard")


class EditionComparisonTitleTests(unittest.TestCase):
    """The comparison-only normalizer behind the edition/drift/dedup checks."""

    def test_edition_skus_collapse_onto_the_base_title(self) -> None:
        for library_name, base_name in [
            ("Mass Effect (2007)", "Mass Effect"),
            ("SimCity 4 Deluxe", "SimCity 4"),
            ("Nioh 2 - The Complete Edition", "Nioh 2"),
            ("Burnout Paradise: The Ultimate Box", "Burnout Paradise"),
            ("Grand Theft Auto IV: The Complete Edition", "Grand Theft Auto IV"),
            (
                "STAR WARS: The Force Unleashed Ultimate Sith Edition",
                "Star Wars: The Force Unleashed",
            ),
            ("Cities XL Platinum", "Cities XL"),
            (
                "Sid Meier's Civilization III: Complete",
                "Sid Meier's Civilization III: Game of the Year Edition",
            ),
            ("STRAFE: Millennium Edition", "STRAFE: Gold Edition"),
            ("The Witcher: Enhanced Edition", "The Witcher"),
            ("Pinball FX Classic", "Pinball FX"),
            # Verification-report 3d: multi-word edition tails with no known
            # qualifier word, and "Uncut and Uncensored" SKU decoration.
            ("DARK SOULS: Prepare To Die Edition", "Dark Souls"),
            (
                "Leisure Suit Larry - Magna Cum Laude Uncut and Uncensored",
                "Leisure Suit Larry: Magna Cum Laude",
            ),
        ]:
            with self.subTest(library_name=library_name):
                self.assertTrue(is_edition_variant_of(library_name, base_name))

    def test_distinct_games_never_collapse(self) -> None:
        for a, b in [
            ("Half-Life 2: Episode One", "Half-Life 2"),
            ("BioShock 2", "BioShock"),
            ("The Forest", "Forest"),
            ("FTL: Faster Than Light", "Faster than light?"),
            ("Deus Ex: Human Revolution", "Deus Ex"),
            ("Halo: The Master Chief Collection", "Halo"),
            ("Sand Patch Grade", "Train Sim World 3"),
            # A leading article is an identity difference, never an edition
            # suffix — including when the casing also differs.
            ("The Masterplan", "MasterPlan"),
            # Verification-report 3d: the pinned entity is a genuinely
            # different SKU, so stripping the suffix must not hide the mispin.
            ("Divinity II: Developer's Cut", "Divinity II: Ego Draconis"),
            (
                "Galactic Civilizations II: Ultimate Edition",
                "Galactic Civilizations II: Dread Lords",
            ),
            ("Sacred 2 Gold", "Sacred 2: Fallen Angel"),
        ]:
            with self.subTest(a=a, b=b):
                self.assertFalse(is_edition_variant_of(a, b))

    def test_identical_names_are_not_an_edition_relationship(self) -> None:
        self.assertFalse(is_edition_variant_of("Hades", "Hades"))

    def test_fully_stripped_title_falls_back_to_the_original(self) -> None:
        # Returning "" would make every fully-stripped title compare equal.
        self.assertEqual(normalize_edition_comparison_title("(2007)"), "2007")
