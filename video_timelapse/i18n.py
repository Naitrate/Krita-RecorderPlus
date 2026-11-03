"""Translations for Recorder+, following the language Krita's UI uses.

Catalogs live in locale/<code>.json and map the English text (exactly as
written in tr() calls) to its translation. Strings a catalog doesn't have fall
back to Krita's own translations (covers common words like "Export"), then to
English. tools/check_strings.py lists strings missing from each catalog.
"""

import configparser
import json
import os

from .qt import QLocale, QStandardPaths

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
_catalog = {}
_language = None          # None = English


def _normalize(code):
    """"de_DE.UTF-8" -> "de_DE", "ca_ES@valencia" -> "ca_ES@valencia", "C" -> "en"."""
    code = code.split(".")[0].replace("-", "_")
    code, _, variant = code.partition("@")
    if code in ("C", "POSIX", ""):
        return "en"
    parts = code.split("_")
    code = parts[0].lower() + ("_" + parts[1].upper() if len(parts) > 1 else "")
    return code + ("@" + variant.lower() if variant else "")


def _requested_languages():
    """Languages in priority order: Krita's own override (Settings → Switch
    Application Language), then what Krita inherits from the environment."""
    langs = []
    cfg_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
    override = os.path.join(cfg_dir, "klanguageoverridesrc")
    if os.path.isfile(override):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        try:
            parser.read(override, encoding="utf-8")
            value = parser.get("Language", "krita", fallback="")
            langs += [l for l in value.split(":") if l]
        except configparser.Error:
            pass
    langs += [l for l in os.environ.get("LANGUAGE", "").split(":") if l]
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            langs.append(os.environ[var])
            break
    langs += QLocale.system().uiLanguages()
    return [_normalize(l) for l in langs]


def _catalog_for(code):
    """Best catalog file for a language code, or None."""
    available = {f[:-5] for f in os.listdir(_DIR) if f.endswith(".json")} if os.path.isdir(_DIR) else set()
    available.discard("template")
    region, _, variant = code.partition("@")
    base = region.split("_")[0]
    if base == "no":
        base = region = "nb"   # legacy code for Norwegian Bokmål
    candidates = [code]
    if variant:
        candidates += [f"{base}@{variant}"]      # ca_ES@valencia -> ca@valencia
    candidates += [region]
    if base == "zh":
        # Traditional script for Taiwan/Hong Kong/Macau, simplified otherwise.
        candidates += ["zh_TW" if region in ("zh_TW", "zh_HK", "zh_MO") or variant == "hant" else "zh_CN"]
    candidates += [base]
    for c in candidates:
        if c in available:
            return c
    # e.g. "pt_PT" with only pt_BR available: any catalog of the same language.
    return next((a for a in sorted(available) if a.split("_")[0].split("@")[0] == base), None)


def load():
    global _catalog, _language
    _catalog, _language = {}, None
    for code in _requested_languages():
        if code.startswith("en"):
            return
        found = _catalog_for(code)
        if found:
            try:
                with open(os.path.join(_DIR, found + ".json"), encoding="utf-8") as f:
                    _catalog = json.load(f)
            except (OSError, ValueError):
                _catalog = {}
            _language = found
            return
        if _language is None:
            _language = code   # no catalog, but Krita may still translate some


def language():
    return _language or "en"


def tr(text):
    """Translate a UI string. Use str.format() placeholders for values:
    tr("Saved {path}").format(path=p)."""
    if _language is None:
        return text
    translated = _catalog.get(text)
    if translated:
        return translated
    try:
        from krita import Krita
        return Krita.krita_i18n(text)
    except Exception:
        return text


load()
