from __future__ import annotations

import json
from typing import Any


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['event']}\nid: {event['id']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
