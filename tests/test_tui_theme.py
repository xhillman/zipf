from zipf.tui.theme import ZIPF_THEME


def test_tui_uses_the_prototype_palette() -> None:
    assert ZIPF_THEME.background == "#0b0d10"
    assert ZIPF_THEME.surface == "#121519"
    assert ZIPF_THEME.panel == "#1a1e24"
    assert ZIPF_THEME.foreground == "#c3c9d3"
    assert ZIPF_THEME.variables == {
        "bright": "#eef1f5",
        "dim": "#5d6775",
        "faint": "#333a44",
        "hover": "#171b21",
        "plan": "#0f1216",
        "spend": "#d78700",
        "free": "#6aa84f",
    }
