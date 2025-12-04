"""Box selection algorithm."""

from typing import List, Optional


def select_box(boxes: List[dict], total_weight: float, estimated_volume: float) -> Optional[int]:
    """
    Select smallest fitting box by volume then priority.
    """
    if not boxes:
        return None

    candidates = []
    for box in boxes:
        max_w = (box.get("max_weight") or 0) * 28.3495  # ounces -> grams
        if max_w and total_weight > max_w:
            continue
        if box.get("volume", 0) and box["volume"] < estimated_volume:
            continue
        candidates.append(box)

    if not candidates:
        return None

    candidates.sort(key=lambda b: (b.get("volume") or 0, b.get("priority") or 9999))
    return candidates[0]["id"]



