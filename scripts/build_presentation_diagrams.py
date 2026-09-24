"""Build the editable architecture and purchase diagrams."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "diagrams"
INK = "#333333"
MUTED = "#686868"
LINE = "#767676"
PALE = "#F7F7F7"
AGENT_BLUE = "#336C96"
MERCHANT_AMBER = "#A96628"
BUYER_GREEN = "#3C7549"
EXTERNAL_WINE = "#A14E5C"
FAILURE_RED = "#A84E4E"
UNCERTAIN_PURPLE = "#756392"


class Page:
    def __init__(self, name: str, width: int, height: int) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.diagram = ET.Element("diagram", {"id": name.lower().replace(" ", "-"), "name": name})
        model = ET.SubElement(
            self.diagram,
            "mxGraphModel",
            {
                "dx": str(width),
                "dy": str(height),
                "grid": "0",
                "guides": "1",
                "tooltips": "1",
                "connect": "1",
                "arrows": "1",
                "fold": "1",
                "page": "1",
                "pageScale": "1",
                "pageWidth": str(width),
                "pageHeight": str(height),
                "math": "0",
                "shadow": "0",
                "background": "#FFFFFF",
            },
        )
        self.root = ET.SubElement(model, "root")
        ET.SubElement(self.root, "mxCell", {"id": "0"})
        ET.SubElement(self.root, "mxCell", {"id": "1", "parent": "0"})
        self.next_id = 2

    def _id(self) -> str:
        value = str(self.next_id)
        self.next_id += 1
        return value

    def box(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        fill: str = "#FFFFFF",
        border: str = INK,
        rounded: bool = True,
        stroke: float = 2,
        double: bool = False,
    ) -> None:
        style = (
            f"rounded={int(rounded)};arcSize=9;whiteSpace=wrap;html=1;"
            f"fillColor={fill};strokeColor={border};strokeWidth={stroke};"
        )
        if double:
            style += "double=1;"
        cell = ET.SubElement(
            self.root,
            "mxCell",
            {"id": self._id(), "value": "", "style": style, "vertex": "1", "parent": "1"},
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {
                "x": str(x),
                "y": str(y),
                "width": str(width),
                "height": str(height),
                "as": "geometry",
            },
        )

    def initial(self, x: int, y: int) -> None:
        cell = ET.SubElement(
            self.root,
            "mxCell",
            {
                "id": self._id(),
                "value": "",
                "style": f"ellipse;fillColor={INK};strokeColor={INK};strokeWidth=1;",
                "vertex": "1",
                "parent": "1",
            },
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {"x": str(x), "y": str(y), "width": "22", "height": "22", "as": "geometry"},
        )

    def text(
        self,
        value: str,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        size: int = 23,
        bold: bool = False,
        color: str = INK,
        align: str = "center",
    ) -> None:
        style = (
            "text;html=1;whiteSpace=wrap;fillColor=none;strokeColor=none;"
            f"fontFamily=Arial;fontSize={size};fontStyle={int(bold)};fontColor={color};"
            f"align={align};verticalAlign=middle;spacing=0;"
        )
        cell = ET.SubElement(
            self.root,
            "mxCell",
            {"id": self._id(), "value": value, "style": style, "vertex": "1", "parent": "1"},
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            {
                "x": str(x),
                "y": str(y),
                "width": str(width),
                "height": str(height),
                "as": "geometry",
            },
        )

    def edge(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        points: tuple[tuple[int, int], ...] = (),
        dashed: bool = False,
        arrow: bool = True,
        width: float = 2,
        color: str = LINE,
    ) -> None:
        style = (
            f"edgeStyle=none;rounded=0;html=1;strokeColor={color};strokeWidth={width};"
            f"dashed={int(dashed)};dashPattern=7 6;"
            f"endArrow={'block' if arrow else 'none'};endFill=1;endSize=11;"
        )
        cell = ET.SubElement(
            self.root,
            "mxCell",
            {"id": self._id(), "value": "", "style": style, "edge": "1", "parent": "1"},
        )
        geometry = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        ET.SubElement(
            geometry,
            "mxPoint",
            {"x": str(start[0]), "y": str(start[1]), "as": "sourcePoint"},
        )
        ET.SubElement(
            geometry,
            "mxPoint",
            {"x": str(end[0]), "y": str(end[1]), "as": "targetPoint"},
        )
        if points:
            array = ET.SubElement(geometry, "Array", {"as": "points"})
            for x, y in points:
                ET.SubElement(array, "mxPoint", {"x": str(x), "y": str(y)})

    def component(self, x: int, y: int, width: int, name: str, role: str, *, border: str) -> None:
        self.box(x, y, width, 130, border=border, stroke=2.5)
        self.text(name, x + 12, y + 23, width - 24, 40, size=25, bold=True)
        self.text(role, x + 12, y + 73, width - 24, 28, size=19, color=MUTED)


def write(name: str, pages: list[Page]) -> None:
    root = ET.Element(
        "mxfile",
        {"host": "app.diagrams.net", "agent": "Codex", "version": "31.4.5", "type": "device"},
    )
    for page in pages:
        root.append(page.diagram)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(OUT / name, encoding="utf-8", xml_declaration=True)


def architecture() -> Page:
    p = Page("C4 · Contenedores", 2460, 1510)
    p.box(350, 240, 900, 880, border=AGENT_BLUE, stroke=2.5)
    p.text("AGENTE COMPRADOR", 390, 1070, 400, 36, size=22, bold=True, align="left")
    p.box(1400, 240, 680, 880, border=MERCHANT_AMBER, stroke=2.5)
    p.text("COMERCIO", 1440, 1070, 300, 36, size=22, bold=True, align="left")

    p.component(40, 580, 245, "Comprador", "Pide y aprueba", border=BUYER_GREEN)
    p.component(440, 545, 275, "Interfaz del agente", "Chat y aprobación", border=AGENT_BLUE)
    p.component(860, 545, 275, "Agente", "Decide y coordina", border=AGENT_BLUE)
    p.component(440, 850, 275, "Identidad", "Autentica compradores", border=AGENT_BLUE)
    p.component(860, 850, 275, "Estado del agente", "Guarda compras", border=AGENT_BLUE)
    p.component(1470, 545, 280, "API del comercio", "Catálogo y checkout", border=MERCHANT_AMBER)
    p.component(1760, 300, 260, "Tienda web", "Muestra productos", border=MERCHANT_AMBER)
    p.component(1760, 850, 260, "Datos del comercio", "Catálogo y pedidos", border=MERCHANT_AMBER)
    p.component(860, 55, 275, "Modelo de lenguaje", "Interpreta solicitudes", border=EXTERNAL_WINE)
    p.component(1470, 1270, 280, "Proveedor de pago", "Token de prueba", border=EXTERNAL_WINE)

    p.edge((285, 645), (440, 645))
    p.text("HTTPS", 295, 597, 130, 34, size=20)
    p.edge((715, 610), (860, 610))
    p.text("HTTPS · SSE", 712, 562, 150, 34, size=19)
    p.edge((575, 675), (575, 850))
    p.text("HTTPS", 585, 741, 120, 34, size=19, align="left")
    p.edge((860, 660), (715, 915), points=((785, 660), (785, 915)))
    p.text("JWT · JWKS", 718, 750, 145, 34, size=18)
    p.edge((997, 675), (997, 850))
    p.text("SQLite", 1005, 749, 125, 34, size=19)
    p.edge((997, 545), (997, 185))
    p.text("HTTPS · inferencia", 1007, 349, 250, 34, size=19, align="left")
    p.edge((1135, 610), (1470, 610))
    p.text("HTTPS · ACP REST + Product Feed", 1145, 552, 320, 50, size=19)
    p.edge((1890, 430), (1610, 545), points=((1610, 430),))
    p.text("HTTPS · JSON", 1630, 438, 195, 34, size=19)
    p.edge((1610, 675), (1890, 850), points=((1610, 780), (1890, 780)))
    p.text("SQL", 1675, 738, 150, 34, size=19)
    p.edge((1135, 675), (1470, 1335), points=((1290, 675), (1290, 1335)))
    p.text("HTTPS · AP2 / delegación", 1270, 1170, 270, 34, size=19)
    p.edge((1610, 675), (1610, 1270))
    p.text("HTTPS · confirmación", 1624, 1170, 280, 34, size=19, align="left")
    p.edge((160, 580), (1890, 300), points=((160, 205), (1890, 205)))
    p.text("HTTPS · navegación", 1550, 157, 260, 34, size=19)
    return p


LANES = (
    ("Comprador", 180),
    ("Agent UI", 555),
    ("Agente", 945),
    ("Comercio", 1370),
    ("Proveedor de pago", 1820),
)


def sequence_page(name: str, height: int) -> Page:
    p = Page(name, 2020, height)
    for title, x in LANES:
        p.box(x - 140, 40, 280, 72, fill=PALE)
        p.text(title, x - 130, 55, 260, 40, size=24, bold=True)
        p.edge((x, 112), (x, height - 50), dashed=True, arrow=False, width=1.5, color="#AAAAAA")
    return p


def phase(p: Page, text: str, y: int) -> None:
    p.text(text, 42, y, 570, 38, size=22, bold=True, align="left")
    p.edge((42, y + 42), (1978, y + 42), arrow=False, width=1, color="#BBBBBB")


def message(
    p: Page, sender: int, receiver: int, y: int, label: str, *, reply: bool = False
) -> None:
    x1 = LANES[sender][1]
    x2 = LANES[receiver][1]
    p.edge((x1, y), (x2, y), dashed=reply, width=1.9)
    left = min(x1, x2) + 12
    width = abs(x2 - x1) - 24
    p.text(label, left, y - 54, width, 46, size=19, color=INK)


def compact_sequence() -> Page:
    p = sequence_page("Compra · resumen", 1340)
    phase(p, "SOLICITUD Y CHECKOUT", 135)
    rows = [
        (0, 1, "iPhone 12 mini 256 GB, máx. COP 800.000"),
        (1, 2, "POST /assistant · HTTPS/SSE"),
        (2, 3, "GET /feeds/current · Product Feed"),
        (3, 2, "Variantes disponibles", True),
        (2, 3, "ACP REST · crear y actualizar checkout"),
        (3, 2, "Términos vigentes", True),
        (2, 1, "Producto y términos · SSE", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 235 + index * 62, row[2], reply=row[3] if len(row) > 3 else False
        )
    phase(p, "APROBACIÓN Y PEDIDO", 675)
    rows = [
        (0, 1, "Aprueba los términos"),
        (1, 2, "POST /purchases/{id}/approval"),
        (1, 2, "POST /purchases/{id}/complete"),
        (2, 4, "Delegar · Payment Mandate AP2"),
        (4, 2, "Token de prueba", True),
        (2, 3, "Completar · Checkout Mandate AP2 + token"),
        (3, 4, "Confirmar token"),
        (3, 2, "Pedido + recibo AP2", True),
        (2, 1, "Pedido confirmado · SSE", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 775 + index * 62, row[2], reply=row[3] if len(row) > 3 else False
        )
    return p


def detailed_discovery() -> Page:
    p = sequence_page("Conversación y selección", 2630)
    phase(p, "CONVERSACIÓN", 150)
    rows = [
        (0, 1, "Hola"),
        (1, 2, "POST /assistant · HTTPS/SSE"),
        (2, 1, "Hola, ¿qué buscas? · respond", True),
        (1, 0, "Muestra la respuesta", True),
        (0, 1, "Quiero un celular"),
        (1, 2, "POST /assistant · nuevo turno"),
        (2, 3, "GET /feeds/current · Product Feed"),
        (3, 2, "Catálogo vigente", True),
        (2, 1, "¿Qué modelo o presupuesto? · clarify", True),
        (1, 0, "Pide una preferencia", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 270 + index * 100, row[2], reply=row[3] if len(row) > 3 else False
        )
    phase(p, "SELECCIÓN Y CHECKOUT", 1320)
    rows = [
        (0, 1, "iPhone 12 mini 256 GB, máx. COP 800.000"),
        (1, 2, "POST /assistant · nuevo turno"),
        (2, 3, "GET /feeds/current · catálogo vigente"),
        (3, 2, "Variantes disponibles", True),
        (2, 3, "POST /checkout_sessions · ACP REST"),
        (3, 2, "Sesión, precio y opciones", True),
        (2, 3, "POST /checkout_sessions/{id} · entrega"),
        (3, 2, "Total final vigente", True),
        (2, 1, "Selección y términos · SSE", True),
        (1, 0, "Revisa producto y total", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 1430 + index * 100, row[2], reply=row[3] if len(row) > 3 else False
        )
    return p


def detailed_payment() -> Page:
    p = sequence_page("Aprobación y pago", 2430)
    phase(p, "CONSENTIMIENTO EXPLÍCITO", 150)
    rows = [
        (0, 1, "Pulsa Aprobar"),
        (1, 2, "POST /purchases/{id}/approval"),
        (2, 3, "GET /checkout_sessions/{id}"),
        (3, 2, "Términos actuales", True),
        (2, 1, "Aprobación registrada", True),
        (1, 0, "Muestra aprobación", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 270 + index * 100, row[2], reply=row[3] if len(row) > 3 else False
        )
    phase(p, "PAGO DE PRUEBA Y PEDIDO", 980)
    rows = [
        (1, 2, "POST /purchases/{id}/complete"),
        (2, 3, "GET /checkout_sessions/{id}"),
        (3, 2, "Checkout vigente", True),
        (2, 4, "POST /agentic_commerce/delegate_payment · AP2"),
        (4, 2, "Token acotado", True),
        (2, 3, "POST /checkout_sessions/{id}/ap2/complete"),
        (3, 4, "POST /payments/confirm · token"),
        (4, 3, "Pago aprobado", True),
        (3, 2, "Pedido + Checkout Receipt", True),
        (2, 4, "POST /agentic_commerce/payment_receipt"),
        (4, 2, "Payment Receipt", True),
        (2, 1, "Pedido confirmado · SSE", True),
        (1, 0, "Número de pedido", True),
    ]
    for index, row in enumerate(rows):
        message(
            p, row[0], row[1], 1100 + index * 95, row[2], reply=row[3] if len(row) > 3 else False
        )
    return p


def state_machine() -> Page:
    p = Page("Estados de compra", 2460, 1050)
    p.box(45, 360, 205, 110, border=AGENT_BLUE, stroke=2.5)
    p.text("received", 55, 390, 185, 42, size=25, bold=True)
    p.box(345, 360, 205, 110, border=AGENT_BLUE, stroke=2.5)
    p.text("selected", 355, 390, 185, 42, size=25, bold=True)
    p.box(645, 360, 270, 110, border=AGENT_BLUE, stroke=2.5)
    p.text("checkout_prepared", 655, 390, 250, 42, size=23, bold=True)
    p.box(1010, 360, 290, 110, border=MERCHANT_AMBER, stroke=2.5)
    p.text("awaiting_confirmation", 1020, 390, 270, 42, size=22, bold=True)
    p.box(1395, 360, 205, 110, border=MERCHANT_AMBER, stroke=2.5)
    p.text("approved", 1405, 390, 185, 42, size=25, bold=True)
    p.box(1695, 360, 225, 110, border=AGENT_BLUE, stroke=2.5)
    p.text("completing", 1705, 390, 205, 42, size=25, bold=True)
    p.box(2210, 360, 205, 110, border=BUYER_GREEN, stroke=2.5, double=True)
    p.text("completed", 2220, 390, 185, 42, size=25, bold=True)
    p.box(345, 710, 205, 110, border=FAILURE_RED, stroke=2.5, double=True)
    p.text("failed", 355, 740, 185, 42, size=25, bold=True)
    p.box(1050, 710, 205, 110, border=FAILURE_RED, stroke=2.5, double=True)
    p.text("canceled", 1060, 740, 185, 42, size=25, bold=True)
    p.box(1695, 710, 225, 110, border=UNCERTAIN_PURPLE, stroke=2.5)
    p.text("uncertain", 1705, 740, 205, 42, size=25, bold=True)
    p.initial(10, 399)

    transitions = [
        ((32, 410), (45, 410), (), "", (0, 0, 0)),
        ((250, 415), (345, 415), (), "variante válida", (247, 321, 105)),
        ((550, 415), (645, 415), (), "crear sesión", (550, 321, 95)),
        ((915, 415), (1010, 415), (), "total válido", (915, 321, 95)),
        ((1300, 415), (1395, 415), (), "clic + AP2", (1300, 321, 95)),
        ((1600, 415), (1695, 415), (), "completar", (1600, 321, 95)),
        ((1920, 415), (2210, 415), (), "pedido", (1970, 321, 190)),
        ((448, 470), (448, 710), (), "error de preparación", (455, 565, 230)),
        ((780, 470), (1050, 765), ((780, 640), (1050, 640)), "sobre presupuesto", (792, 593, 225)),
        ((1155, 470), (1155, 710), (), "cancelar", (1160, 565, 145)),
        ((1807, 470), (1807, 710), (), "timeout / 5xx", (1815, 565, 170)),
        (
            (1920, 765),
            (2312, 470),
            ((2070, 765), (2070, 560), (2312, 560)),
            "reconciliar",
            (2095, 592, 180),
        ),
        ((1695, 765), (1695, 470), ((1645, 765), (1645, 470)), "reintentar", (1530, 575, 155)),
        (
            (1807, 360),
            (1498, 360),
            ((1807, 210), (1498, 210)),
            "4xx recuperable",
            (1548, 164, 210),
        ),
        (
            (1695, 425),
            (550, 765),
            ((1650, 425), (1650, 900), (550, 900)),
            "pago rechazado",
            (1170, 856, 210),
        ),
    ]
    for start, end, points, label, (x, y, width) in transitions:
        p.edge(start, end, points=points)
        if label:
            p.text(label, x, y, width, 38, size=20)
    return p


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write("agent-architecture-c4.drawio", [architecture()])
    write("purchase-sequence-compact.drawio", [compact_sequence()])
    write("purchase-sequence.drawio", [detailed_discovery(), detailed_payment()])
    write("purchase-state-machine.drawio", [state_machine()])


if __name__ == "__main__":
    main()
