# -*- coding: utf-8 -*-


def _change_duration_to_seconds(duration: str | None = None) -> int:
    if duration[-1] == "m":
        return int(duration[:-1]) * 60
    elif duration[-1] == "h":
        return int(duration[:-1]) * 60 * 60
    elif duration[-1] == "d":
        return int(duration[:-1]) * 24 * 60 * 60
    else:
        return 3600 #1h
    
def _change_duration_to_milliseconds(duration: str | None = None) -> int:
    if duration[-1] == "m":
        return int(duration[:-1]) * 60 * 1000
    elif duration[-1] == "h":
        return int(duration[:-1]) * 60 * 60 * 1000
    elif duration[-1] == "d":
        return int(duration[:-1]) * 24 * 60 * 60 * 1000
    else:
        return 3600 #1h
