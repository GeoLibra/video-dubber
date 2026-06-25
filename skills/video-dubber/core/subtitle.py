import hashlib
import itertools
import re

import pysubs2


def normalize_spaces(text):
    return re.sub(r"\s+", " ", text or "").strip()


def strip_display_punctuation(text):
    text = normalize_spaces(text)
    text = re.sub(r"[。！？!?，,；;：:、\"“”'‘’（）()《》<>【】\[\]{}]", "", text or "")
    text = re.sub(r"(?<![A-Za-z0-9])\.(?![A-Za-z0-9])", "", text)
    return normalize_spaces(text)


def is_cjk_char(char):
    return "\u3400" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff" or "\uac00" <= char <= "\ud7af"


def display_width(text):
    return sum(2 if is_cjk_char(ch) else 1 for ch in text)


def tokenize_display_text(text):
    raw_tokens = re.findall(
        r"[A-Za-z0-9]+(?:[._+#:/-][A-Za-z0-9]+)*|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+|[^\s]",
        text,
    )
    tokens = []
    for token in raw_tokens:
        if all(is_cjk_char(ch) for ch in token):
            tokens.extend(token)
        else:
            tokens.append(token)
    return tokens


def needs_space(left, right):
    if not left or not right:
        return False
    return bool(re.search(r"[A-Za-z0-9]$", left) and re.search(r"^[A-Za-z0-9]", right))


def normalize_subtitle_text(text):
    return normalize_spaces(text).replace("{", "（").replace("}", "）")


def semantic_units(text):
    text = normalize_subtitle_text(text)
    if not text:
        return []
    units = []
    current = ""
    for char in text:
        current += char
        if char in "，,、；;：:。！？!?":
            units.append(current.strip())
            current = ""
    if current.strip():
        units.append(current.strip())
    return units or [text]


def join_units(units):
    line = ""
    for unit in units:
        if not line:
            line = unit
        elif needs_space(line, unit):
            line += " " + unit
        else:
            line += unit
    return line


def auto_subtitle_max_width(target_language, subtitle_mode="target", playres_x=1920, font_size=40, margin_l=120):
    usable_width = max(320, playres_x - margin_l * 2)
    max_width = int(usable_width / max(1, font_size * 0.8))

    from .lang import is_compact
    if subtitle_mode == "bilingual":
        max_width = min(max_width, 44)
    else:
        max_width = min(max_width, 58)
    if not is_compact(target_language):
        max_width = min(max_width, 64)
    return max(34, max_width)


def balanced_semantic_wrap(text, max_width, max_lines):
    units = semantic_units(text)
    if len(units) <= 1 or max_lines < 2:
        return None

    best = None
    for line_count in range(2, min(max_lines, len(units)) + 1):
        for splits in itertools.combinations(range(1, len(units)), line_count - 1):
            bounds = (0, *splits, len(units))
            lines = [join_units(units[bounds[i] : bounds[i + 1]]) for i in range(line_count)]
            widths = [display_width(line) for line in lines]
            overflow = sum(max(0, width - max_width) for width in widths)
            balance = max(widths) - min(widths)
            score = (overflow, line_count, max(widths), balance)
            if best is None or score < best[0]:
                best = (score, lines)
    if best and best[0][0] == 0:
        return best[1]
    if best and display_width(normalize_subtitle_text(text)) > max_width:
        return best[1]
    return None


def token_wrap(text, max_width, max_lines):
    text = normalize_subtitle_text(text)
    tokens = tokenize_display_text(text)
    if not tokens:
        return ""

    lines = []
    current = ""
    for token in tokens:
        sep = " " if needs_space(current, token) else ""
        candidate = f"{current}{sep}{token}" if current else token
        if current and display_width(candidate) > max_width and len(lines) < max_lines:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["".join(lines[max_lines - 1 :])]
    return "\\N".join(lines)


def wrap_mixed_text(text, max_width, max_lines):
    text = normalize_subtitle_text(text)
    if display_width(text) <= max_width or max_lines <= 1:
        return text

    semantic = balanced_semantic_wrap(text, max_width, max_lines)
    if semantic:
        fixed = []
        for line in semantic[:max_lines]:
            if display_width(line) > int(max_width * 1.25):
                fixed.extend(token_wrap(line, max_width, max_lines).split("\\N"))
            else:
                fixed.append(line)
        if len(fixed) <= max_lines:
            return "\\N".join(fixed)
    return token_wrap(text, max_width, max_lines)


def wrap_display_text(text, target_language, subtitle_mode="target"):
    max_lines = 2 if subtitle_mode == "bilingual" else 3
    max_width = auto_subtitle_max_width(target_language, subtitle_mode)
    return wrap_mixed_text(text, max_width=max_width, max_lines=max_lines)


def get_sub_source_text(sub):
    return normalize_spaces(re.sub(r"\{[^}]*\}", "", sub.text.replace("\\N", " ")))


def source_hash(subs):
    digest = hashlib.sha256()
    for sub in subs:
        digest.update(f"{sub.start}|{sub.end}|{get_sub_source_text(sub)}\n".encode("utf-8"))
    return digest.hexdigest()


def is_non_speech(text):
    compact = normalize_spaces(text)
    return not compact or bool(re.search(r"音乐|music|applause|掌声|欢快", compact, re.IGNORECASE))


FONT_PROFILE = {
    "fontname": "Hiragino Sans GB",
    "fontsize": 40,
    "secondary_fontsize": 24,
    "outline": 4,
    "shadow": 0,
    "marginv": 74,
    "marginl": 120,
    "alignment": 2,
}


def apply_ass_style(subs):
    subs.info["PlayResX"] = "1920"
    subs.info["PlayResY"] = "1080"
    style = subs.styles.get("Default", pysubs2.SSAStyle())
    style.fontname = FONT_PROFILE["fontname"]
    style.fontsize = FONT_PROFILE["fontsize"]
    style.primarycolor = pysubs2.Color(255, 255, 255, 0)
    style.outlinecolor = pysubs2.Color(0, 0, 0, 0)
    style.backcolor = pysubs2.Color(0, 0, 0, 0)
    style.borderstyle = 1
    style.outline = FONT_PROFILE["outline"]
    style.shadow = FONT_PROFILE["shadow"]
    style.alignment = FONT_PROFILE["alignment"]
    style.marginv = FONT_PROFILE["marginv"]
    style.marginl = FONT_PROFILE["marginl"]
    style.marginr = FONT_PROFILE["marginl"]
    subs.styles["Default"] = style


def apply_translations_to_subs(subs, translations, subtitle_mode, target_language):
    for idx, sub in enumerate(subs):
        item = translations.get(idx, {})
        original = get_sub_source_text(sub)
        display_raw = item.get("display_text") or original
        tts_raw = item.get("tts_text") or display_raw
        if is_non_speech(display_raw) or is_non_speech(original):
            sub.display_text = ""
            sub.tts_text = normalize_spaces(tts_raw)
            sub.text = ""
            continue

        display = wrap_display_text(display_raw, target_language, subtitle_mode)
        tts_text = normalize_spaces(tts_raw or display.replace("\\N", " "))
        sub.display_text = display
        sub.tts_text = tts_text
        if subtitle_mode == "target":
            sub.text = r"{\fs40}" + display
        elif subtitle_mode == "source":
            sub.text = r"{\fs40}" + original
        else:
            sub.text = r"{\fs40}" + display + r"\N{\fs24}" + original
