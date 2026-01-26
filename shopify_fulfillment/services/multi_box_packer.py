"""Multi-box packing algorithm using First Fit Decreasing (FFD)."""

from dataclasses import dataclass, field
from typing import List, Optional
import logging

_logger = logging.getLogger(__name__)

GRAMS_PER_OUNCE = 28.3495
GRAMS_PER_CUBIC_INCH = 9.0


@dataclass
class PackableItem:
    """Represents a single unit to be packed."""

    line_id: int
    sku: str
    weight_grams: float
    quantity: int = 1  # Always 1 after expansion


@dataclass
class BoxSpec:
    """Box specification from fulfillment.box."""

    box_id: int
    name: str
    max_weight_grams: float
    box_weight_grams: float
    volume_cubic_inches: float
    priority: int
    length: float
    width: float
    height: float


@dataclass
class PackedBox:
    """Result of packing - a box with its assigned items."""

    box_spec: BoxSpec
    items: List[PackableItem] = field(default_factory=list)
    total_item_weight: float = 0.0
    is_oversized: bool = False

    @property
    def total_weight_with_box(self) -> float:
        """Total weight including box weight (for shipping API)."""
        return self.total_item_weight + self.box_spec.box_weight_grams

    @property
    def line_ids(self) -> List[int]:
        """Get unique line IDs in this box."""
        return list(set(item.line_id for item in self.items))


@dataclass
class PackingResult:
    """Complete packing solution."""

    packed_boxes: List[PackedBox] = field(default_factory=list)
    unpacked_items: List[PackableItem] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None

    @property
    def box_count(self) -> int:
        return len(self.packed_boxes)

    @property
    def has_oversized(self) -> bool:
        return any(pb.is_oversized for pb in self.packed_boxes)


class MultiBoxPacker:
    """FFD bin-packing algorithm for order fulfillment.

    Hybrid approach:
    - Combines items into fewest boxes possible
    - Items exceeding all box capacities flagged as oversized
    """

    def __init__(self, items: List[PackableItem], boxes: List[BoxSpec]):
        self.items = items
        self.boxes = boxes

    @classmethod
    def from_order(cls, order, boxes_data: List[dict]) -> "MultiBoxPacker":
        """Factory method to create packer from Odoo order record."""
        items = []
        for line in order.line_ids:
            if not line.requires_shipping:
                continue
            if not line.weight or line.weight <= 0:
                continue
            items.append(
                PackableItem(
                    line_id=line.id,
                    sku=line.sku or "",
                    weight_grams=line.weight or 0.0,
                    quantity=line.quantity or 1,
                )
            )

        boxes = []
        for b in boxes_data:
            boxes.append(
                BoxSpec(
                    box_id=b["id"],
                    name=b.get("name", ""),
                    max_weight_grams=(b.get("max_weight") or 0) * GRAMS_PER_OUNCE,
                    box_weight_grams=(b.get("box_weight") or 0) * GRAMS_PER_OUNCE,
                    volume_cubic_inches=b.get("volume") or 0,
                    priority=b.get("priority") or 9999,
                    length=b.get("length") or 0,
                    width=b.get("width") or 0,
                    height=b.get("height") or 0,
                )
            )

        return cls(items, boxes)

    def pack(self) -> PackingResult:
        """Execute FFD bin-packing algorithm.

        Returns PackingResult with packed boxes or error info.
        """
        if not self.items:
            return PackingResult(success=False, error_message="No items to pack")

        if not self.boxes:
            return PackingResult(success=False, error_message="No active boxes configured")

        # Step 1: Expand items by quantity (each unit is separate)
        expanded_items = []
        for item in self.items:
            for _ in range(item.quantity):
                expanded_items.append(
                    PackableItem(
                        line_id=item.line_id,
                        sku=item.sku,
                        weight_grams=item.weight_grams,
                        quantity=1,
                    )
                )

        # Step 2: Sort items by weight DESCENDING (First Fit Decreasing)
        expanded_items.sort(key=lambda x: x.weight_grams, reverse=True)

        # Step 3: Sort boxes by max_weight ASCENDING, then priority
        sorted_boxes = sorted(
            self.boxes, key=lambda b: (b.max_weight_grams, b.priority)
        )

        # Step 4: Find largest box capacity
        max_box_capacity = max(b.max_weight_grams for b in self.boxes)
        largest_box = sorted_boxes[-1]  # Last after sorting is largest

        # Step 5: Separate oversized items
        oversized_items = []
        packable_items = []
        for item in expanded_items:
            if item.weight_grams > max_box_capacity:
                oversized_items.append(item)
            else:
                packable_items.append(item)

        # Step 6: Initialize result
        packed_boxes: List[PackedBox] = []

        # Step 7: Handle oversized items (each gets largest box, flagged)
        for item in oversized_items:
            _logger.warning(
                "Oversized item: SKU=%s weight=%.0fg exceeds max box capacity %.0fg",
                item.sku,
                item.weight_grams,
                max_box_capacity,
            )
            packed_boxes.append(
                PackedBox(
                    box_spec=largest_box,
                    items=[item],
                    total_item_weight=item.weight_grams,
                    is_oversized=True,
                )
            )

        # Step 7b: If everything fits in a single box (by weight/volume), prefer 1 box.
        if not oversized_items:
            total_weight = sum(item.weight_grams for item in packable_items)
            total_volume = (
                total_weight / GRAMS_PER_CUBIC_INCH if total_weight > 0 else 0.0
            )

            def _box_can_fit_all(box_spec: BoxSpec) -> bool:
                if total_weight > box_spec.max_weight_grams:
                    return False
                if total_volume and box_spec.volume_cubic_inches > 0:
                    return total_volume <= box_spec.volume_cubic_inches
                return True

            candidate_boxes = [b for b in sorted_boxes if _box_can_fit_all(b)]
            if candidate_boxes:
                def _sort_key(b: BoxSpec):
                    volume_missing = 1 if b.volume_cubic_inches <= 0 else 0
                    volume_value = (
                        b.volume_cubic_inches if b.volume_cubic_inches > 0 else float("inf")
                    )
                    return (volume_missing, volume_value, b.max_weight_grams, b.priority)

                best_box = sorted(candidate_boxes, key=_sort_key)[0]
                _logger.info(
                    "Packing shortcut: all items fit in one box (%s) - %.0fg, %.0fin³",
                    best_box.name,
                    total_weight,
                    total_volume,
                )
                packed_boxes.append(
                    PackedBox(
                        box_spec=best_box,
                        items=packable_items,
                        total_item_weight=total_weight,
                        is_oversized=False,
                    )
                )
                return PackingResult(packed_boxes=packed_boxes, unpacked_items=[], success=True)

        # Step 8: FFD Bin Packing for remaining items
        # open_bins: list of [box_spec, current_weight, items_list]
        open_bins: List[List] = []

        for item in packable_items:
            placed = False

            # Try to fit in existing open bin (First Fit)
            for i, (box_spec, current_weight, bin_items) in enumerate(open_bins):
                new_weight = current_weight + item.weight_grams
                if new_weight <= box_spec.max_weight_grams:
                    open_bins[i] = [box_spec, new_weight, bin_items + [item]]
                    placed = True
                    break

            if not placed:
                # Open new bin - find smallest box that fits this item
                for box_spec in sorted_boxes:
                    if item.weight_grams <= box_spec.max_weight_grams:
                        open_bins.append([box_spec, item.weight_grams, [item]])
                        placed = True
                        break

            if not placed:
                # Should not happen if we filtered oversized items correctly
                _logger.error(
                    "Failed to place item: SKU=%s weight=%.0fg",
                    item.sku,
                    item.weight_grams,
                )
                return PackingResult(
                    unpacked_items=[item],
                    success=False,
                    error_message=f"Item {item.sku} ({item.weight_grams:.0f}g) exceeds all box capacities",
                )

        # Step 9: Convert open bins to PackedBox objects
        for box_spec, total_weight, items in open_bins:
            packed_boxes.append(
                PackedBox(
                    box_spec=box_spec,
                    items=items,
                    total_item_weight=total_weight,
                    is_oversized=False,
                )
            )

        # Step 10: Log packing summary
        _logger.info(
            "Packing complete: %d items -> %d boxes (oversized: %d)",
            len(expanded_items),
            len(packed_boxes),
            len(oversized_items),
        )
        for i, pb in enumerate(packed_boxes, 1):
            _logger.debug(
                "  Box %d: %s (%.0fg/%.0fg) items=%d oversized=%s",
                i,
                pb.box_spec.name,
                pb.total_item_weight,
                pb.box_spec.max_weight_grams,
                len(pb.items),
                pb.is_oversized,
            )

        # Return result
        return PackingResult(
            packed_boxes=packed_boxes,
            unpacked_items=[],
            success=True,
            error_message="Contains oversized items requiring manual review"
            if oversized_items
            else None,
        )
