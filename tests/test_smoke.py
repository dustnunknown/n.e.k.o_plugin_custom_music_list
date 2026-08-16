from pathlib import Path


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "custom_music_list"' in text
    assert 'entry = "plugin.plugins.custom_music_list:CustomMusicListPlugin"' in text
