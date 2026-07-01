"""Multi-box packing algorithm using First Fit Decreasing (FFD)."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import logging

_logger = logging.getLogger(__name__)

GRAMS_PER_OUNCE = 28.3495
GRAMS_PER_CUBIC_INCH = 9.0
PRACTICAL_BOX_COUNT_SLACK = 1


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

    @property
    def line_quantities(self) -> dict:
        """Units packed in this box per line ID (items are one unit each)."""
        counts = {}
        for item in self.items:
            counts[item.line_id] = counts.get(item.line_id, 0) + item.quantity
        return counts


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
    - Combines items into a practical whole-order carton tier
    - Items exceeding all box capacities flagged as oversized
    """

    def __init__(self, items: List[PackableItem], boxes: List[BoxSpec]):
        self.items = items
        self.boxes = boxes

    @staticmethod
    def _estimate_volume(weight_grams: float) -> float:
        return weight_grams / GRAMS_PER_CUBIC_INCH if weight_grams > 0 else 0.0

    @staticmethod
    def _box_sort_key(box_spec: BoxSpec) -> Tuple[int, float, float, int]:
        volume_missing = 1 if box_spec.volume_cubic_inches <= 0 else 0
        volume_value = (
            box_spec.volume_cubic_inches
            if box_spec.volume_cubic_inches > 0
            else float("inf")
        )
        return (
            volume_missing,
            volume_value,
            box_spec.max_weight_grams,
            box_spec.priority,
        )

    @staticmethod
    def _box_can_fit(
        box_spec: BoxSpec,
        weight_grams: float,
        volume_cubic_inches: float,
    ) -> bool:
        if box_spec.max_weight_grams <= 0:
            return False
        if weight_grams > box_spec.max_weight_grams:
            return False
        if volume_cubic_inches and box_spec.volume_cubic_inches > 0:
            return volume_cubic_inches <= box_spec.volume_cubic_inches
        return True

    def _smallest_fitting_box(
        self,
        weight_grams: float,
        volume_cubic_inches: float,
        boxes: List[BoxSpec],
    ) -> Optional[BoxSpec]:
        candidates = [
            box_spec
            for box_spec in boxes
            if self._box_can_fit(box_spec, weight_grams, volume_cubic_inches)
        ]
        if not candidates:
            return None
        return sorted(candidates, key=self._box_sort_key)[0]

    def _pack_items_for_target_box(
        self,
        items: List[PackableItem],
        target_box: BoxSpec,
        boxes: List[BoxSpec],
    ) -> Optional[List[PackedBox]]:
        bins: List[Tuple[float, float, List[PackableItem]]] = []

        for item in items:
            item_volume = self._estimate_volume(item.weight_grams)
            if not self._box_can_fit(target_box, item.weight_grams, item_volume):
                return None

            placed = False
            for i, (current_weight, current_volume, bin_items) in enumerate(bins):
                new_weight = current_weight + item.weight_grams
                new_volume = current_volume + item_volume
                if self._box_can_fit(target_box, new_weight, new_volume):
                    bins[i] = (new_weight, new_volume, bin_items + [item])
                    placed = True
                    break

            if not placed:
                bins.append((item.weight_grams, item_volume, [item]))

        packed_boxes = []
        for total_weight, total_volume, bin_items in bins:
            box_spec = self._smallest_fitting_box(total_weight, total_volume, boxes)
            if not box_spec:
                return None
            packed_boxes.append(
                PackedBox(
                    box_spec=box_spec,
                    items=bin_items,
                    total_item_weight=total_weight,
                    is_oversized=False,
                )
            )

        return packed_boxes

    def _choose_practical_packing_plan(
        self,
        items: List[PackableItem],
        boxes: List[BoxSpec],
    ) -> Optional[List[PackedBox]]:
        solutions = []
        for target_box in boxes:
            packed_boxes = self._pack_items_for_target_box(items, target_box, boxes)
            if not packed_boxes:
                continue
            solutions.append((target_box, packed_boxes))

        if not solutions:
            return None

        fewest_boxes = min(len(packed_boxes) for _, packed_boxes in solutions)
        practical_solutions = [
            solution
            for solution in solutions
            if len(solution[1]) <= fewest_boxes + PRACTICAL_BOX_COUNT_SLACK
        ]

        def _solution_key(solution):
            target_box, packed_boxes = solution
            volume_missing = sum(
                1
                for packed_box in packed_boxes
                if packed_box.box_spec.volume_cubic_inches <= 0
            )
            total_box_volume = sum(
                packed_box.box_spec.volume_cubic_inches
                if packed_box.box_spec.volume_cubic_inches > 0
                else float("inf")
                for packed_box in packed_boxes
            )
            return (
                volume_missing,
                total_box_volume,
                target_box.max_weight_grams,
                len(packed_boxes),
                target_box.priority,
            )

        return sorted(practical_solutions, key=_solution_key)[0][1]

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

        # Step 3: Sort usable boxes by max_weight ASCENDING, then priority.
        # Boxes with no positive max weight are ignored for automatic packing.
        usable_boxes = [box for box in self.boxes if box.max_weight_grams > 0]
        if not usable_boxes:
            return PackingResult(
                success=False,
                error_message="No boxes with positive max weight configured",
            )

        sorted_boxes = sorted(
            usable_boxes, key=lambda b: (b.max_weight_grams, b.priority)
        )

        # Step 4: Find largest box capacity
        max_box_capacity = max(b.max_weight_grams for b in usable_boxes)
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
            total_volume = self._estimate_volume(total_weight)
            candidate_boxes = [
                b
                for b in sorted_boxes
                if self._box_can_fit(b, total_weight, total_volume)
            ]
            if candidate_boxes:
                best_box = sorted(candidate_boxes, key=self._box_sort_key)[0]
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
                return PackingResult(
                    packed_boxes=packed_boxes,
                    unpacked_items=[],
                    success=True,
                )

        # Step 8: Choose a practical whole-order carton tier, then pack with FFD.
        # This avoids opening one tiny carton per light item when larger cartons are
        # available and only costs at most one label versus the absolute minimum.
        practical_boxes = self._choose_practical_packing_plan(packable_items, sorted_boxes)
        if practical_boxes is None:
            first_item = packable_items[0] if packable_items else None
            if first_item:
                _logger.error(
                    "Failed to place item: SKU=%s weight=%.0fg",
                    first_item.sku,
                    first_item.weight_grams,
                )
                return PackingResult(
                    unpacked_items=[first_item],
                    success=False,
                    error_message=(
                        f"Item {first_item.sku} ({first_item.weight_grams:.0f}g) "
                        "exceeds all box capacities"
                    ),
                )
            return PackingResult(success=False, error_message="No packable items")

        packed_boxes.extend(practical_boxes)
        if practical_boxes:
            _logger.info(
                "Packing tier selected: %d packable items -> %d boxes",
                len(packable_items),
                len(practical_boxes),
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
