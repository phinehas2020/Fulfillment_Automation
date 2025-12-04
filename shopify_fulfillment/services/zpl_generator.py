"""ZPL generation utilities placeholder."""


def pdf_to_zpl(pdf_data: bytes) -> str:
    """
    Placeholder: in production, convert PDF/PNG label to ZPL.
    """
    _ = pdf_data
    return "^XA^FO50,50^ADN,36,20^FDLabel placeholder^FS^XZ"


def generate_packing_slip_zpl(order) -> str:
    """
    Render a simple packing slip as ZPL.
    """
    lines = [
        "^XA",
        "^FO50,40^A0N,30,30^FDPacking Slip^FS",
        f"^FO50,90^A0N,25,25^FDOrder: {order.order_name or order.order_number}^FS",
    ]
    y = 130
    for line in order.line_ids:
        lines.append(f"^FO50,{y}^A0N,22,22^FD{line.quantity} x {line.title or line.sku}^FS")
        y += 30
    lines.append("^XZ")
    return "".join(lines)



