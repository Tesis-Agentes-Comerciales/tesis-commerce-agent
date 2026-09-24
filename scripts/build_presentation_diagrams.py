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


class C4Page(Page):
    """Use the element styles and metadata from draw.io's C4 shape palette."""

    def element(
        self,
        kind: str,
        x: int,
        y: int,
        width: int,
        height: int,
        name: str,
        description: str,
        border: str,
        *,
        technology: str | None = None,
    ) -> None:
        shapes = {
            "person": "shape=mxgraph.c4.person2;align=center;",
            "system": "rounded=1;arcSize=10;align=center;",
            "container": "rounded=1;arcSize=10;align=center;",
            "browser": "shape=mxgraph.c4.webBrowserContainer2;boundedLbl=1;align=center;",
            "data": "shape=cylinder3;size=15;boundedLbl=1;align=center;",
        }
        type_name = {
            "person": "Persona",
            "system": "Sistema de software",
            "container": "Contenedor",
            "browser": "Contenedor",
            "data": "Contenedor",
        }[kind]
        type_line = "[%c4Type%: %c4Technology%]" if technology else "[%c4Type%]"
        label = (
            '<div style="font-family:Arial;color:#333333">'
            '<div style="font-size:22px"><b>%c4Name%</b></div>'
            f'<div style="font-size:15px;color:#686868">{type_line}</div>'
            '<div style="font-size:17px;color:#555555;margin-top:7px">%c4Description%</div>'
            "</div>"
        )
        attributes = {
            "id": self._id(),
            "placeholders": "1",
            "c4Name": name,
            "c4Type": type_name,
            "c4Description": description,
            "label": label,
        }
        if technology:
            attributes["c4Technology"] = technology
        node = ET.SubElement(self.root, "object", attributes)
        style = (
            f"{shapes[kind]}whiteSpace=wrap;html=1;labelBackgroundColor=none;"
            f"fillColor=#FFFFFF;strokeColor={border};strokeColor2={border};"
            "strokeWidth=2.5;fontColor=#333333;metaEdit=1;resizable=0;"
        )
        cell = ET.SubElement(node, "mxCell", {"style": style, "vertex": "1", "parent": "1"})
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

    def boundary(self, x: int, y: int, width: int, height: int, name: str, border: str) -> None:
        node = ET.SubElement(
            self.root,
            "object",
            {
                "id": self._id(),
                "placeholders": "1",
                "c4Name": name,
                "c4Type": "SystemScopeBoundary",
                "c4Application": "Sistema de software",
                "label": (
                    '<div style="text-align:left;font-size:21px;font-family:Arial">'
                    "<b>%c4Name%</b><br/>[%c4Application%]</div>"
                ),
            },
        )
        style = (
            "rounded=1;arcSize=20;whiteSpace=wrap;html=1;dashed=1;dashPattern=8 4;"
            f"fillColor=none;strokeColor={border};strokeWidth=2.5;fontColor={INK};"
            "align=left;verticalAlign=bottom;spacing=20;metaEdit=1;rotatable=0;"
            "perimeter=rectanglePerimeter;connectable=0;pointerEvents=0;"
        )
        cell = ET.SubElement(node, "mxCell", {"style": style, "vertex": "1", "parent": "1"})
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

    def relationship(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        description: str,
        label_x: int,
        label_y: int,
        label_width: int,
        *,
        technology: str | None = None,
        points: tuple[tuple[int, int], ...] = (),
    ) -> None:
        self.edge(start, end, points=points, dashed=True, width=2.1)
        self.text(description, label_x, label_y, label_width, 38, size=18, bold=True)
        if technology:
            self.text(
                f"[{technology}]",
                label_x,
                label_y + 34,
                label_width,
                30,
                size=16,
                color=MUTED,
            )


def write(name: str, pages: list[Page]) -> None:
    root = ET.Element(
        "mxfile",
        {"host": "app.diagrams.net", "agent": "Codex", "version": "31.4.5", "type": "device"},
    )
    for page in pages:
        root.append(page.diagram)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(OUT / name, encoding="utf-8", xml_declaration=True)


def system_context() -> C4Page:
    p = C4Page("C4 · Contexto del agente", 2280, 1250)
    p.element("person", 80, 510, 300, 230, "Comprador", "Solicita y aprueba", BUYER_GREEN)
    p.element(
        "system", 750, 505, 420, 190, "Agente comprador", "Coordina compras asistidas", AGENT_BLUE
    )
    p.element(
        "system", 1600, 330, 390, 190, "Comercio", "Publica productos y vende", MERCHANT_AMBER
    )
    p.element(
        "system",
        1600,
        800,
        390,
        190,
        "Proveedor sandbox",
        "Procesa pagos sintéticos",
        EXTERNAL_WINE,
    )
    p.element(
        "system", 775, 85, 370, 175, "Modelo de lenguaje", "Interpreta solicitudes", EXTERNAL_WINE
    )

    p.relationship((380, 620), (750, 620), "Conversa y aprueba", 420, 555, 290)
    p.relationship((960, 505), (960, 260), "Solicita interpretación a", 665, 335, 280)
    p.relationship(
        (1170, 565),
        (1600, 430),
        "Consulta catálogo y compra en",
        1230,
        315,
        335,
        points=((1350, 565), (1350, 430)),
    )
    p.relationship(
        (1170, 640),
        (1600, 895),
        "Delega pago de prueba a",
        1255,
        775,
        315,
        points=((1350, 640), (1350, 895)),
    )
    p.relationship((1795, 520), (1795, 800), "Confirma el pago en", 1810, 635, 280)
    p.text(
        "Verde: persona  |  Azul: sistema en foco  |  Ocre y vino: sistemas externos",
        420,
        1125,
        1500,
        45,
        size=17,
        color=MUTED,
    )
    return p


def architecture() -> C4Page:
    p = C4Page("C4 · Contenedores del agente", 2650, 1530)
    p.boundary(410, 260, 1250, 1010, "Agente comprador", AGENT_BLUE)
    p.element("person", 65, 565, 300, 230, "Comprador", "Chatea y aprueba", BUYER_GREEN)
    p.element(
        "browser",
        495,
        575,
        350,
        210,
        "Interfaz del agente",
        "Chat y aprobación",
        AGENT_BLUE,
        technology="React · assistant-ui",
    )
    p.element(
        "container",
        1060,
        585,
        350,
        190,
        "API del agente",
        "Conversa y coordina",
        AGENT_BLUE,
        technology="FastAPI · LangGraph",
    )
    p.element(
        "container",
        495,
        910,
        350,
        190,
        "Identidad",
        "Autentica compradores",
        AGENT_BLUE,
        technology="Better Auth",
    )
    p.element(
        "data",
        1060,
        910,
        350,
        190,
        "Estado persistente",
        "Compras y conversaciones",
        AGENT_BLUE,
        technology="SQLite",
    )
    p.element(
        "system",
        1060,
        50,
        350,
        180,
        "Modelo de lenguaje",
        "Interpreta solicitudes",
        EXTERNAL_WINE,
    )
    p.element(
        "system",
        1960,
        575,
        390,
        190,
        "Comercio",
        "Catálogo y checkout",
        MERCHANT_AMBER,
    )
    p.element(
        "system",
        1960,
        1030,
        390,
        190,
        "Proveedor sandbox",
        "Token de pago sintético",
        EXTERNAL_WINE,
    )

    p.relationship((365, 685), (495, 685), "Usa el chat", 425, 495, 180, technology="HTTPS")
    p.relationship(
        (845, 680),
        (1060, 680),
        "Envía turnos",
        845,
        495,
        215,
        technology="HTTPS · SSE",
    )
    p.relationship((670, 785), (670, 910), "Inicia sesión", 690, 805, 220, technology="HTTPS")
    p.relationship(
        (1060, 745),
        (845, 1010),
        "Verifica identidad",
        970,
        810,
        190,
        technology="JWT · JWKS",
        points=((965, 745), (965, 1010)),
    )
    p.relationship(
        (1235, 775),
        (1235, 910),
        "Guarda estado",
        1240,
        790,
        190,
        technology="SQLite",
    )
    p.relationship(
        (1235, 585),
        (1235, 230),
        "Solicita inferencia",
        1250,
        365,
        240,
        technology="HTTPS API",
    )
    p.relationship(
        (1410, 680),
        (1960, 680),
        "Catálogo y checkout",
        1365,
        495,
        285,
        technology="HTTPS · Feed / ACP REST / AP2",
    )
    p.relationship(
        (1410, 745),
        (1960, 1125),
        "Delega pago",
        1790,
        865,
        170,
        technology="HTTPS · AP2",
        points=((1775, 745), (1775, 1125)),
    )
    p.relationship(
        (2155, 765),
        (2155, 1030),
        "Confirma pago",
        2170,
        865,
        220,
        technology="HTTPS",
    )
    p.text(
        "Verde: persona  |  Azul: contenedores del agente  |  Ocre y vino: sistemas externos",
        510,
        1390,
        1700,
        45,
        size=17,
        color=MUTED,
    )
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
    write("agent-system-context-c4.drawio", [system_context()])
    write("agent-architecture-c4.drawio", [architecture()])
    write("purchase-sequence-compact.drawio", [compact_sequence()])
    write("purchase-sequence.drawio", [detailed_discovery(), detailed_payment()])
    write("purchase-state-machine.drawio", [state_machine()])


if __name__ == "__main__":
    main()
