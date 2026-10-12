import re
import os
import json

from rich.console import Console

console = Console()

# Terms that should never be translated, for every project. Intentionally
# empty: brand names are project-specific and belong in that project's
# langsync.json "whitelist".
WHITELIST = []

# Tokens that must reach the translated text byte-identical. Order matters:
# at any given position the first alternative that matches wins, so the more
# specific forms ("{{name}}", "$t(...)", "%{name}") come before the generic
# ones ("{name}", "%s").
PLACEHOLDER_REGEX = re.compile(
    r'('
    # i18next interpolation: {{name}}, {{- name}}, {{count, number}}
    r'\{\{[^{}]*\}\}'
    # i18next nesting: $t(key), $t(key, {"count": 2})
    r'|\$t\((?:[^()]|\([^()]*\))*\)'
    # JS template literal: ${var}
    r'|\$\{[^{}]*\}'
    # Ruby/Rails: %{name}
    r'|%\{[^{}]*\}'
    # ICU / simple: {name}, {0}, {count, number}
    r'|\{[^{}]+\}'
    # printf: %s %d %1$s %.2f %@ and the literal %%. No space flag, so prose
    # like "100% sure" is left alone.
    r'|%%'
    r'|%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?(?:hh|h|ll|l|L|z|j|t)?[sdifuxXeEgG@]'
    # Laravel: :name, only at the start or after whitespace/punctuation, so
    # "10:30" and "https://" never match.
    r'|(?<![^\s(\[{,;"\'])\:[A-Za-z_]\w*'
    # HTML entities: &amp; &#39; &#x27; &nbsp;
    r'|&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9A-Fa-f]{1,6});'
    # URLs (trailing sentence punctuation stays outside the token)
    r'|https?://[^\s<>"\']*[^\s<>"\'.,;:!?)]'
    # Email addresses
    r'|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
    # HTML/XML tags, each one on its own (<b>, </b>, <br/>, <a href="...">)
    # plus react-i18next numbered tags (<0>, </0>). The text between them
    # stays visible to the translator. "a < b" has no tag name, so no match.
    r'|</?(?:[A-Za-z][\w:.-]*|\d+)'
    r'(?:\s+[\w:.@-]+(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s"\'<>]+))?)*\s*/?>'
    r')'
)

# Concurrency settings
MAX_WORKERS_PER_LOCALE = 2
MAX_PARALLEL_LOCALES = 3
DELAY_BETWEEN_REQUESTS = 0.2
BATCH_SIZE = 25
RETRY_COUNT = 3

# Translation providers (see translator.TranslationService)
PROVIDERS = ('google', 'deepl', 'libre', 'microsoft', 'mymemory')
KEY_ORDERS = ('keep', 'source', 'alpha')

# File settings
DEFAULT_SOURCE = 'messages/en-GB.json'
DEFAULT_DIR = 'messages'
GLOBAL_CONFIG_PATH = os.path.expanduser('~/.langsync.json')
LOCAL_CONFIG_NAMES = ['langsync.json', '.langsync.json']

# Keys holding paths. In a local config file they are relative to the file's
# own directory, not to wherever langsync happens to be run from.
PATH_KEYS = ('source', 'dir', 'state_file', 'pattern')


def get_default_config():
    """Returns the default configuration dictionary (every known key)."""
    return {
        'source': DEFAULT_SOURCE,
        'dir': DEFAULT_DIR,
        'max_workers_per_locale': MAX_WORKERS_PER_LOCALE,
        'max_parallel_locales': MAX_PARALLEL_LOCALES,
        'delay_between_requests': DELAY_BETWEEN_REQUESTS,
        'batch_size': BATCH_SIZE,
        'retry_count': RETRY_COUNT,
        'whitelist': list(WHITELIST),
        # Optional: override snapshot location. Defaults to <dir>/.langsync-state.json.
        'state_file': None,
        # Translation provider and its credentials.
        'provider': 'google',
        'api_key': None,
        'api_key_env': None,   # name of an env var holding the key
        'provider_url': None,  # LibreTranslate base URL
        'source_lang': None,   # None: derive from the source file / auto-detect
        'language_map': {},    # locale -> provider language code overrides
        'glossary': {},        # language or locale -> {term: forced translation}
        'exclude': [],         # glob patterns of keys never sent to the translator
        'key_order': 'keep',   # keep | source | alpha
        'locales': None,       # allowlist of target locales
        'pattern': None,       # locale file pattern
        'source_locale': None,
    }


def get_starter_config():
    """The short config written to a new project's langsync.json: only the
    keys people commonly edit. Everything else keeps its default."""
    return {
        'source': DEFAULT_SOURCE,
        'dir': DEFAULT_DIR,
        'whitelist': [],
        'exclude': [],
        'glossary': {},
        'key_order': 'keep',
    }


def save_config(path, config_dict):
    """Saves a configuration dictionary to a JSON file."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config_dict, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _is_global_config(path):
    try:
        return os.path.realpath(path) == os.path.realpath(GLOBAL_CONFIG_PATH)
    except (OSError, ValueError):
        return False


def find_local_config(start=None):
    """Find langsync.json / .langsync.json in `start` (default: CWD) or the
    nearest parent directory. The search stops after the first directory that
    contains a .git entry (the repository root) and never returns the global
    ~/.langsync.json. Returns the path, or None."""
    current = os.path.abspath(start or os.getcwd())
    while True:
        for name in LOCAL_CONFIG_NAMES:
            candidate = os.path.join(current, name)
            if os.path.isfile(candidate) and not _is_global_config(candidate):
                return candidate
        if os.path.exists(os.path.join(current, '.git')):
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def resolve_api_key(config):
    """The provider API key: 'api_key' when set, else the value of the env var
    named by 'api_key_env'. Returns None when neither yields a key."""
    key = config.get('api_key')
    if key:
        return key
    env_name = config.get('api_key_env')
    if env_name:
        return os.environ.get(env_name) or None
    return None


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_str_dict(value):
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _is_glossary(value):
    return isinstance(value, dict) and all(
        isinstance(k, str) and _is_str_dict(v) for k, v in value.items()
    )


# key -> (validator, human description). Keys whose default is None also
# accept an explicit null.
_VALIDATORS = {
    'max_workers_per_locale': (_is_positive_int, "a positive integer"),
    'max_parallel_locales': (_is_positive_int, "a positive integer"),
    'batch_size': (_is_positive_int, "a positive integer"),
    'retry_count': (_is_positive_int, "a positive integer"),
    'delay_between_requests': (
        lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
        "a non-negative number",
    ),
    'source': (lambda v: isinstance(v, str), "a string"),
    'dir': (lambda v: isinstance(v, str), "a string"),
    'state_file': (lambda v: isinstance(v, str), "a string"),
    'provider': (
        lambda v: isinstance(v, str) and v.lower() in PROVIDERS,
        "one of " + ", ".join(PROVIDERS),
    ),
    'api_key': (lambda v: isinstance(v, str), "a string"),
    'api_key_env': (lambda v: isinstance(v, str), "a string (an environment variable name)"),
    'provider_url': (lambda v: isinstance(v, str), "a string (a URL)"),
    'source_lang': (lambda v: isinstance(v, str), "a string"),
    'language_map': (_is_str_dict, 'an object of {"locale": "code"} strings'),
    'glossary': (_is_glossary, 'an object of {"language": {"term": "translation"}}'),
    'exclude': (_is_str_list, "a list of strings"),
    'key_order': (
        lambda v: isinstance(v, str) and v in KEY_ORDERS,
        "one of " + ", ".join(KEY_ORDERS),
    ),
    'locales': (_is_str_list, "a list of strings"),
    'pattern': (lambda v: isinstance(v, str), "a string"),
    'source_locale': (lambda v: isinstance(v, str), "a string"),
}


def _apply_file_config(config, file_config, path):
    """Validate file_config and merge it into config. Values of the wrong type
    are reported and ignored; unknown keys are ignored."""
    defaults = get_default_config()
    for key, value in file_config.items():
        if key not in config:
            continue
        if key == 'whitelist':
            if _is_str_list(value):
                # Merge with the defaults, dropping duplicates but keeping order.
                merged = []
                for term in list(WHITELIST) + value:
                    if term not in merged:
                        merged.append(term)
                config['whitelist'] = merged
            else:
                console.print(f"[yellow]Warning: 'whitelist' in {path} must be a list of strings. Ignoring.[/yellow]")
            continue
        if value is None and defaults.get(key) is None:
            config[key] = None
            continue
        check = _VALIDATORS.get(key)
        if check is None:
            config[key] = value
        elif check[0](value):
            if key == 'delay_between_requests':
                value = float(value)
            elif key == 'provider':
                value = value.lower()
            config[key] = value
        else:
            console.print(f"[yellow]Warning: '{key}' in {path} must be {check[1]}. Ignoring.[/yellow]")


def _resolve_paths(config, config_dir):
    """Rewrite relative path keys so they are relative to the CWD while still
    pointing at the location the config file meant (its own directory)."""
    for key in PATH_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value or os.path.isabs(value):
            continue
        absolute = os.path.normpath(os.path.join(config_dir, value))
        try:
            config[key] = os.path.relpath(absolute)
        except ValueError:
            # Different drive on Windows: no relative form exists.
            config[key] = absolute


def load_config(config_path=None):
    """
    Load configuration from a file.
    Order of preference:
    1. config_path (if provided)
    2. langsync.json or .langsync.json in the CWD or its nearest parent
       (up to the repository root)
    3. ~/.langsync.json

    Relative paths in a local config file (1 or 2) are resolved against the
    directory that file lives in. Returns (config, loaded_path).
    """
    config = get_default_config()

    search_paths = []
    if config_path:
        search_paths.append(config_path)

    local = find_local_config()
    if local:
        search_paths.append(local)

    # Global fallback
    search_paths.append(GLOBAL_CONFIG_PATH)

    loaded_path = None
    for path in search_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            if not isinstance(file_config, dict):
                console.print(f"[red]Error: {path} must contain a JSON object.[/red]")
                continue
            _apply_file_config(config, file_config, path)
            if not _is_global_config(path):
                _resolve_paths(config, os.path.dirname(os.path.abspath(path)))
            loaded_path = path
            break
        except json.JSONDecodeError:
            console.print(f"[red]Error: {path} is not a valid JSON file.[/red]")
        except Exception as e:
            console.print(f"[red]Error loading config from {path}: {e}[/red]")

    return config, loaded_path
