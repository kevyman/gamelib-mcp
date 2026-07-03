def test_civilization_expansions_are_nested_under_civ_iv():
    from gamelib_mcp.data import content

    override = content.classify_title_override("Sid Meier's Civilization IV: Warlords")

    assert override is not None
    assert override.content_type == "expansion"
    assert override.parent_name == "Sid Meier's Civilization IV"
    assert override.is_primary_library_item is False


def test_civilization_colonization_remains_primary_standalone():
    from gamelib_mcp.data import content

    override = content.classify_title_override("Sid Meier's Civilization IV: Colonization")

    assert override is not None
    assert override.content_type == "standalone_expansion"
    assert override.parent_name is None
    assert override.is_primary_library_item is True


def test_fallout_new_vegas_ultimate_is_edition_alias():
    from gamelib_mcp.data import content

    override = content.classify_title_override("Fallout New Vegas Ultimate Edition")

    assert override is not None
    assert override.content_type == "edition"
    assert override.parent_name == "Fallout: New Vegas"
    assert override.alias_for_parent is True


def test_fallout_new_vegas_dead_money_is_nested_dlc():
    from gamelib_mcp.data import content

    override = content.classify_title_override("Fallout New Vegas: Dead Money")

    assert override is not None
    assert override.content_type == "dlc"
    assert override.parent_name == "Fallout: New Vegas"
    assert override.is_primary_library_item is False


def test_classify_falls_back_to_game_type_when_category_is_none():
    from gamelib_mcp.data import content

    # IGDB has effectively migrated category -> game_type for some titles
    # (e.g. "Persona 3 Reload"): category comes back None, game_type=8
    # ("remake") is populated. Without the fallback this defaults to
    # base_game via content_type_from_igdb_category(None), which happens to
    # be right here but for the wrong reason — assert the real signal (8 ->
    # remake) is what's actually used.
    result = content.classify_igdb_game(title="Persona 3 Reload", category=None, game_type=8)

    assert result.content_type == "remake"
    assert result.is_primary_library_item is True


def test_classify_game_type_pack_is_non_primary_dlc():
    from gamelib_mcp.data import content

    # game_type=13 ("pack") is IGDB's bucket for cosmetic/BGM/persona-set
    # style addon content; category is unpopulated for these too.
    result = content.classify_igdb_game(
        title="Persona 3 Reload: Persona 5 Royal Persona Set 1",
        category=None,
        game_type=13,
    )

    assert result.content_type == "dlc"
    assert result.is_primary_library_item is False


def test_classify_category_present_takes_precedence_over_game_type():
    from gamelib_mcp.data import content

    # When category IS populated, it stays authoritative even if game_type
    # disagrees — the fallback only applies when category is None.
    result = content.classify_igdb_game(
        title="Some DLC", category=1, game_type=0  # category=DLC, game_type=main game
    )

    assert result.content_type == "dlc"
    assert result.is_primary_library_item is False
