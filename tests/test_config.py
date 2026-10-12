import os
import json
import pytest
from langsync.config import load_config, get_default_config, save_config

def test_get_default_config():
    config = get_default_config()
    assert config['source'] == 'messages/en-GB.json'
    assert 'whitelist' in config
    assert isinstance(config['whitelist'], list)

def test_save_and_load_config(tmp_path, isolated, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "langsync.json"
    custom_config = {
        "source": "src/locales/en.json",
        "dir": "src/locales",
        "max_parallel_locales": 10,
        "delay_between_requests": 0.5,
        "whitelist": ["MyCompany"]
    }
    save_config(str(config_file), custom_config)
    
    # Need to change CWD or pass path
    loaded, loaded_path = load_config(str(config_file))
    
    assert loaded_path == str(config_file)
    assert loaded['source'] == "src/locales/en.json"
    assert loaded['max_parallel_locales'] == 10
    assert loaded['delay_between_requests'] == 0.5
    # Whitelist is merged with the (now empty) default
    assert loaded['whitelist'] == ["MyCompany"]

def test_load_config_invalid_json(tmp_path, isolated, monkeypatch):
    monkeypatch.chdir(isolated)
    config_file = tmp_path / "langsync.json"
    with open(config_file, 'w') as f:
        f.write("invalid json")
    
    loaded, loaded_path = load_config(str(config_file))
    # Should return default config if invalid
    assert loaded == get_default_config()


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A project dir with a .git marker, and no global config in play."""
    import langsync.config as cfg
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG_PATH", str(tmp_path / "no-global.json"))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    return project


def test_default_whitelist_empty():
    from langsync.config import WHITELIST
    assert WHITELIST == []
    assert get_default_config()['whitelist'] == []


def test_starter_config_is_short():
    from langsync.config import get_starter_config
    starter = get_starter_config()
    assert set(starter) == {"source", "dir", "whitelist", "exclude", "glossary", "key_order"}
    assert starter["key_order"] == "keep"


def test_default_config_new_keys():
    config = get_default_config()
    assert config["provider"] == "google"
    assert config["key_order"] == "keep"
    assert config["glossary"] == {} and config["language_map"] == {} and config["exclude"] == []
    for key in ("api_key", "api_key_env", "provider_url", "source_lang", "locales",
                "pattern", "source_locale"):
        assert config[key] is None


def test_whitelist_merge_dedupes_preserving_order(isolated, monkeypatch):
    monkeypatch.chdir(isolated)
    save_config(str(isolated / "langsync.json"), {"whitelist": ["B", "A", "B"]})
    loaded, _ = load_config()
    assert loaded["whitelist"] == ["B", "A"]


def test_relative_paths_resolve_against_config_dir(isolated, monkeypatch):
    sub = isolated / "app" / "src"
    sub.mkdir(parents=True)
    save_config(str(isolated / "langsync.json"), {
        "source": "messages/en.json", "dir": "./messages",
        "state_file": "state/s.json", "pattern": "messages/{locale}.json",
    })
    monkeypatch.chdir(sub)
    loaded, loaded_path = load_config()
    assert loaded_path == str(isolated / "langsync.json")
    assert loaded["source"] == os.path.join("..", "..", "messages", "en.json")
    assert loaded["dir"] == os.path.join("..", "..", "messages")
    assert loaded["state_file"] == os.path.join("..", "..", "state", "s.json")
    assert loaded["pattern"] == os.path.join("..", "..", "messages", "{locale}.json")


def test_relative_paths_unchanged_when_run_from_config_dir(isolated, monkeypatch):
    save_config(str(isolated / "langsync.json"), {"source": "messages/en.json", "dir": "messages"})
    monkeypatch.chdir(isolated)
    loaded, _ = load_config()
    assert loaded["source"] == os.path.join("messages", "en.json")
    assert loaded["dir"] == "messages"


def test_explicit_config_paths_relative_to_file(isolated, monkeypatch, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    save_config(str(other / "custom.json"), {"dir": "locales", "state_file": "/abs/state.json"})
    monkeypatch.chdir(isolated)
    loaded, _ = load_config(str(other / "custom.json"))
    assert loaded["dir"] == os.path.join("..", "elsewhere", "locales")
    assert loaded["state_file"] == "/abs/state.json"


def test_global_config_paths_not_rewritten(tmp_path, monkeypatch):
    import langsync.config as cfg
    global_path = tmp_path / "home" / ".langsync.json"
    global_path.parent.mkdir()
    save_config(str(global_path), {"dir": "messages"})
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG_PATH", str(global_path))
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    monkeypatch.chdir(project)
    loaded, loaded_path = load_config()
    assert loaded_path == str(global_path)
    assert loaded["dir"] == "messages"


def test_find_local_config_searches_parents(isolated, monkeypatch):
    from langsync.config import find_local_config
    deep = isolated / "a" / "b"
    deep.mkdir(parents=True)
    assert find_local_config(str(deep)) is None
    (isolated / ".langsync.json").write_text("{}")
    assert find_local_config(str(deep)) == str(isolated / ".langsync.json")
    (isolated / "a" / "langsync.json").write_text("{}")
    assert find_local_config(str(deep)) == str(isolated / "a" / "langsync.json")
    monkeypatch.chdir(deep)
    assert find_local_config() == str(isolated / "a" / "langsync.json")


def test_find_local_config_stops_at_git_root(tmp_path, monkeypatch):
    import langsync.config as cfg
    from langsync.config import find_local_config
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG_PATH", str(tmp_path / "none.json"))
    (tmp_path / "langsync.json").write_text("{}")  # outside the repo
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    assert find_local_config(str(repo / "sub")) is None


def test_find_local_config_skips_global(tmp_path, monkeypatch):
    import langsync.config as cfg
    from langsync.config import find_local_config
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (home / ".langsync.json").write_text("{}")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG_PATH", str(home / ".langsync.json"))
    (tmp_path / ".git").mkdir()
    assert find_local_config(str(home / "work")) is None


def test_new_keys_validated(isolated, monkeypatch):
    monkeypatch.chdir(isolated)
    good = {
        "provider": "DeepL", "api_key": "k:fx", "api_key_env": "DEEPL_KEY",
        "provider_url": "http://localhost:5000", "source_lang": "en",
        "language_map": {"nb-NO": "no"}, "glossary": {"de": {"Seat map": "Sitzplan"}},
        "exclude": ["meta.*"], "key_order": "alpha", "locales": ["de", "fr"],
        "pattern": "{locale}.json", "source_locale": "en-GB",
        "batch_size": 10, "retry_count": 5,
    }
    save_config(str(isolated / "langsync.json"), good)
    loaded, _ = load_config()
    for key, value in good.items():
        if key == "provider":
            assert loaded[key] == "deepl"
        elif key == "pattern":
            assert loaded[key] == "{locale}.json"
        else:
            assert loaded[key] == value


@pytest.mark.parametrize("key,bad", [
    ("provider", "bing"), ("api_key", 5), ("api_key_env", ["X"]), ("provider_url", 1),
    ("source_lang", 1), ("language_map", {"de": 1}), ("language_map", ["de"]),
    ("glossary", {"de": "x"}), ("glossary", {"de": {"a": 1}}), ("exclude", "a.*"),
    ("exclude", [1]), ("key_order", "random"), ("locales", "de"), ("pattern", 3),
    ("source_locale", 1), ("batch_size", 0), ("batch_size", True), ("retry_count", -1),
    ("retry_count", 1.5), ("whitelist", "PRO"),
])
def test_bad_values_warn_and_ignored(isolated, monkeypatch, key, bad):
    monkeypatch.chdir(isolated)
    save_config(str(isolated / "langsync.json"), {key: bad})
    loaded, _ = load_config()
    assert loaded[key] == get_default_config()[key]


def test_resolve_api_key(monkeypatch):
    from langsync.config import resolve_api_key
    monkeypatch.setenv("MY_KEY", "secret")
    assert resolve_api_key({"api_key": "direct", "api_key_env": "MY_KEY"}) == "direct"
    assert resolve_api_key({"api_key": None, "api_key_env": "MY_KEY"}) == "secret"
    monkeypatch.delenv("MY_KEY")
    assert resolve_api_key({"api_key_env": "MY_KEY"}) is None
    assert resolve_api_key({}) is None
