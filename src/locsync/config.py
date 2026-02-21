import re
import os
import json

# Whitelist of terms that should not be translated
WHITELIST = [
    "MilesOrCash",
    "FlightPoints",
    "Lascade",
    "SwayWM",
    "Arch Linux",
    "Google",
    "Facebook",
    "Twitter",
    "Instagram",
    "Virgin Atlantic",
    "Virgin Points",
    "JFK",
    "LON",
    "NYC",
    "Heathrow",
    "Skyscanner",
    "Kayak",
    "Booking.com",
    "Agoda",
    "Kiwi",
    "Cheapflights",
    "Momondo",
    "Priceline",
    "AirAsia",
    "Air India",
    "Emirates",
    "IndiGo",
    "Qatar Airways",
    "Singapore Airlines",
    "SpiceJet",
    "PRO",
    "WP-Total",
    "WP-TotalPages",
    "MilesOrCash.",
]

# Regex for placeholders like {name} or <tag>...</tag>
PLACEHOLDER_REGEX = re.compile(r'(\{[^}]+\}|<[^>]+>[^<]*</[^>]+>|<[^>]+/>)')

# Locale mapping for deep-translator
LANG_MAP = {
    'zh': lambda locale: 'zh-CN' if 'TW' not in locale else 'zh-TW',
    'nb': lambda _: 'no',
    'he': lambda _: 'iw',
}

# Concurrency settings
MAX_WORKERS_PER_LOCALE = 5
MAX_PARALLEL_LOCALES = 3
DELAY_BETWEEN_REQUESTS = 0.2

# File settings
DEFAULT_SOURCE = 'messages/en-GB.json'
DEFAULT_DIR = 'messages'

def load_config(config_path=None):
    """
    Load configuration from a file.
    Order of preference:
    1. config_path (if provided)
    2. trsync.json or .trsync.json in CWD
    3. ~/.trsync.json
    """
    config = {
        'source': DEFAULT_SOURCE,
        'dir': DEFAULT_DIR,
        'max_workers_per_locale': MAX_WORKERS_PER_LOCALE,
        'max_parallel_locales': MAX_PARALLEL_LOCALES,
        'delay_between_requests': DELAY_BETWEEN_REQUESTS,
        'whitelist': WHITELIST
    }

    search_paths = []
    if config_path:
        search_paths.append(config_path)
    
    # Check for both locsync.json and .locsync.json in CWD
    search_paths.append(os.path.join(os.getcwd(), 'locsync.json'))
    search_paths.append(os.path.join(os.getcwd(), '.locsync.json'))
    # Global fallback
    search_paths.append(os.path.expanduser('~/.locsync.json'))

    loaded_path = None
    for path in search_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    file_config = json.load(f)
                    
                    # Validate and update config
                    for key, value in file_config.items():
                        if key in config:
                            if key == 'whitelist':
                                if isinstance(value, list):
                                    # Merge whitelists and remove duplicates
                                    config['whitelist'] = list(set(WHITELIST + value))
                                else:
                                    print(f"Warning: 'whitelist' in {path} must be a list. Ignoring.")
                            elif key in ['max_workers_per_locale', 'max_parallel_locales']:
                                if isinstance(value, int) and value > 0:
                                    config[key] = value
                                else:
                                    print(f"Warning: '{key}' in {path} must be a positive integer. Ignoring.")
                            elif key == 'delay_between_requests':
                                if isinstance(value, (int, float)) and value >= 0:
                                    config[key] = float(value)
                                else:
                                    print(f"Warning: 'delay_between_requests' in {path} must be a non-negative number. Ignoring.")
                            else:
                                config[key] = value
                    
                    loaded_path = path
                    break 
            except json.JSONDecodeError:
                print(f"Error: {path} is not a valid JSON file.")
            except Exception as e:
                print(f"Error loading config from {path}: {e}")

    return config, loaded_path
