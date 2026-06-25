import re


LANGUAGE_ALIASES = {
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "cn": "Chinese",
    "chinese": "Chinese",
    "中文": "Chinese",
    "汉语": "Chinese",
    "日语": "Japanese",
    "日文": "Japanese",
    "ja": "Japanese",
    "jp": "Japanese",
    "japanese": "Japanese",
    "韩语": "Korean",
    "韩文": "Korean",
    "ko": "Korean",
    "kr": "Korean",
    "korean": "Korean",
}

LANGUAGE_SLUGS = {
    "Chinese": "zh",
    "Japanese": "ja",
    "Korean": "ko",
}


def normalize_name(raw):
    raw = (raw or "Chinese").strip()
    return LANGUAGE_ALIASES.get(raw.lower(), raw)


def slug(name):
    name = normalize_name(name)
    return LANGUAGE_SLUGS.get(name, re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "target")


def is_compact(display_language):
    return normalize_name(display_language) in {"Chinese", "Japanese", "Korean"}
