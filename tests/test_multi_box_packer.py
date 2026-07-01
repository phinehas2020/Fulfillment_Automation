import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKER_PATH = ROOT / "shopify_fulfillment" / "services" / "multi_box_packer.py"

spec = importlib.util.spec_from_file_location("multi_box_packer", PACKER_PATH)
multi_box_packer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = multi_box_packer
spec.loader.exec_module(multi_box_packer)

BoxSpec = multi_box_packer.BoxSpec
MultiBoxPacker = multi_box_packer.MultiBoxPacker
PackableItem = multi_box_packer.PackableItem
GRAMS_PER_OUNCE = multi_box_packer.GRAMS_PER_OUNCE


def box(
    box_id,
    name,
    max_weight_ounces,
    box_weight_ounces,
    volume,
    priority,
    length=12,
    width=12,
    height=12,
):
    return BoxSpec(
        box_id=box_id,
        name=name,
        max_weight_grams=max_weight_ounces * GRAMS_PER_OUNCE,
        box_weight_grams=box_weight_ounces * GRAMS_PER_OUNCE,
        volume_cubic_inches=volume,
        priority=priority,
        length=length,
        width=width,
        height=height,
    )


class MultiBoxPackerTest(unittest.TestCase):
    def test_large_mixed_order_uses_practical_carton_tier(self):
        boxes = [
            box(4, "11x8x5 Box (5lb)", 80, 3, 440, 10, 11, 8, 5),
            box(5, "12x12x6 Box (10lb)", 160, 5, 864, 20, 12, 12, 6),
            box(6, "12x12x8 Box (20lb)", 336, 7, 1152, 30, 12, 12, 8),
            box(7, "12x12x12 Dense (40lb)", 640, 10, 1728, 40, 12, 12, 12),
            box(8, "12x12x14 Flour (40lb)", 640, 12, 2016, 50, 12, 12, 14),
            box(9, "12x12x18 Dense (60lb)", 960, 16, 2592, 60, 12, 12, 18),
            box(10, "12x12x20 Flour (60lb)", 960, 18, 2880, 70, 12, 12, 20),
            box(11, "New box", 32, 7, 210, 10, 10, 7, 3),
            box(12, "8x8x8", 0, 1, 512, 100, 8, 8, 8),
        ]
        lines = [
            ("1314", 2, 907),
            ("651", 1, 142),
            ("654G", 1, 5443),
            ("276", 1, 5443),
            ("165", 2, 113),
            ("6607", 1, 454),
            ("249", 1, 907),
            ("621", 2, 113),
            ("1320B", 1, 4536),
            ("247", 1, 907),
            ("1350", 4, 227),
            ("97-10", 1, 4536),
            ("248-10", 1, 4536),
            ("110-10", 1, 4536),
            ("100-10", 1, 4536),
            ("1333", 3, 454),
            ("113-5", 1, 2268),
            ("98", 1, 907),
            ("617", 3, 907),
            ("611", 3, 907),
            ("126", 1, 907),
            ("108-10", 1, 4536),
            ("211", 1, 907),
            ("1380", 1, 454),
        ]
        items = [
            PackableItem(line_id=index, sku=sku, weight_grams=weight, quantity=quantity)
            for index, (sku, quantity, weight) in enumerate(lines, start=1)
        ]

        result = MultiBoxPacker(items, boxes).pack()

        self.assertTrue(result.success)
        self.assertEqual(result.box_count, 4)
        self.assertEqual([packed.box_spec.box_id for packed in result.packed_boxes], [7, 7, 7, 6])
        self.assertNotIn(11, [packed.box_spec.box_id for packed in result.packed_boxes])
        self.assertNotIn(12, [packed.box_spec.box_id for packed in result.packed_boxes])
        for packed_box in result.packed_boxes:
            self.assertLessEqual(
                packed_box.total_item_weight,
                packed_box.box_spec.max_weight_grams,
            )

    def test_line_quantities_track_units_per_box_when_lines_split(self):
        boxes = [box(1, "Small (5lb)", 80, 3, 440, 10)]
        # Six 1lb units on one line: too heavy for a single 5lb box, so the
        # line must split across boxes and per-box unit counts must add up.
        items = [PackableItem(line_id=42, sku="FLOUR", weight_grams=454, quantity=6)]

        result = MultiBoxPacker(items, boxes).pack()

        self.assertTrue(result.success)
        self.assertGreater(result.box_count, 1)
        total_units = 0
        for packed_box in result.packed_boxes:
            quantities = packed_box.line_quantities
            self.assertEqual(list(quantities.keys()), [42])
            self.assertEqual(quantities[42], len(packed_box.items))
            total_units += quantities[42]
        self.assertEqual(total_units, 6)


if __name__ == "__main__":
    unittest.main()
