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
