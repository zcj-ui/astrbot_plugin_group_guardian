"""Read AstrBot's default/global administrator IDs without consulting session config."""

from collections.abc import Mapping


def _normalize(value):
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            return []
        text = str(item).strip()
        if not text:
            continue
        if text.casefold() in {"none", "null", "nan"}:
            return []
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def read_astrbot_admin_ids(context):
    """Return normalized global admin IDs, failing closed on malformed official data.

    Legacy ``astrbot_config`` is consulted only for hosts with no callable
    ``get_config`` accessor. The official accessor is always invoked without UMO.
    """
    try:
        accessor = getattr(context, "get_config", None)
    except Exception:
        return []
    if callable(accessor):
        try:
            config = accessor()
        except Exception:
            return []
        if not isinstance(config, Mapping):
            return []
    else:
        try:
            config = getattr(context, "astrbot_config", None)
        except Exception:
            return []
        if not isinstance(config, Mapping):
            return []
    try:
        return _normalize(config.get("admin_id", []))
    except Exception:
        return []
