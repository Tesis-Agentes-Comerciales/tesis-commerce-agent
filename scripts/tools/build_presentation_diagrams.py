"""Build the editable architecture and purchase diagrams."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs" / "diagrams"
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
        curved: bool = False,
        dashed: bool = False,
        arrow: bool = True,
        width: float = 2,
        color: str = LINE,
    ) -> None:
        curve_style = "curved=1;" if curved else ""
        style = (
            f"edgeStyle=none;{curve_style}rounded=0;html=1;"
            f"strokeColor={color};strokeWidth={width};"
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
        show_type: bool = True,
    ) -> None:
        shapes = {
            "person": "shape=mxgraph.c4.person2;align=center;",
            "system": "rounded=1;arcSize=10;align=center;",
            "container": "rounded=1;arcSize=10;align=center;",
            "component": "rounded=1;arcSize=10;align=center;",
            "browser": "shape=mxgraph.c4.webBrowserContainer2;boundedLbl=1;align=center;",
            "data": "shape=cylinder3;size=15;boundedLbl=1;align=center;",
        }
        type_name = {
            "person": "Persona",
            "system": "Sistema de software",
            "container": "Contenedor",
            "component": "Componente",
            "browser": "Contenedor",
            "data": "Contenedor",
        }[kind]
        type_line = "[%c4Type%: %c4Technology%]" if technology else "[%c4Type%]"
        type_markup = (
            f'<div style="font-size:15px;color:#686868">{type_line}</div>' if show_type else ""
        )
        label = (
            '<div style="font-family:Arial;color:#333333">'
            '<div style="font-size:22px"><b>%c4Name%</b></div>'
            f"{type_markup}"
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

    def boundary(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        name: str,
        border: str,
        *,
        scope: str = "Sistema de software",
    ) -> None:
        node = ET.SubElement(
            self.root,
            "object",
            {
                "id": self._id(),
                "placeholders": "1",
                "c4Name": name,
                "c4Type": "ContainerScopeBoundary"
                if scope == "Contenedor"
                else "SystemScopeBoundary",
                "c4Application": scope,
                "label": (
                    '<div style="text-align:left;font-size:21px;font-family:Arial">'
                    "<b>%c4Name%</b></div>"
                ),
            },
        )
        style = (
            "rounded=0;whiteSpace=wrap;html=1;dashed=1;dashPattern=8 4;"
            f"fillColor=none;strokeColor={border};strokeWidth=2.5;fontColor={INK};"
            "align=left;verticalAlign=top;spacingLeft=24;spacingTop=18;metaEdit=1;rotatable=0;"
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
        "Proveedor de pago",
        "Procesa pagos de prueba",
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
    p = C4Page("C4 · Ecosistema del agente", 2860, 1450)
    p.boundary(430, 265, 1570, 1060, "Agente comprador", AGENT_BLUE)
    elements = (
        ("person", 50, 610, 300, 220, "Comprador", "Chatea y aprueba", BUYER_GREEN),
        ("browser", 530, 560, 350, 190, "Interfaz del agente", "Chat y aprobación", AGENT_BLUE),
        ("container", 1150, 560, 350, 190, "API del agente", "Orquesta la compra", AGENT_BLUE),
        ("container", 530, 980, 350, 175, "Identidad", "Autentica compradores", AGENT_BLUE),
        ("data", 1050, 980, 330, 175, "Compras", "Términos y pedidos", AGENT_BLUE),
        (
            "data",
            1550,
            980,
            330,
            175,
            "Memoria del grafo",
            "Historial y puntos de control",
            AGENT_BLUE,
        ),
        ("system", 1150, 35, 350, 175, "OpenAI", "Interpreta solicitudes", EXTERNAL_WINE),
        ("system", 2300, 560, 390, 190, "Comercio", "Catálogo y compra", MERCHANT_AMBER),
        ("system", 2300, 990, 390, 190, "Proveedor de pago", "Pago de prueba", EXTERNAL_WINE),
    )
    for kind, x, y, width, height, name, description, border in elements:
        p.element(kind, x, y, width, height, name, description, border, show_type=False)

    p.edge((350, 670), (530, 670), dashed=True)
    p.text("HTTPS", 370, 605, 140, 38, size=18)
    p.edge((880, 665), (1150, 665), dashed=True)
    p.text("HTTPS · SSE", 895, 605, 235, 38, size=18)
    p.edge((705, 750), (705, 980), dashed=True)
    p.text("HTTPS", 555, 840, 130, 38, size=18)
    p.edge((1150, 720), (880, 1060), points=((960, 720), (960, 1060)), dashed=True)
    p.text("JWT · JWKS", 765, 845, 175, 38, size=18)
    p.edge((1215, 750), (1215, 980), dashed=True)
    p.text("SQLite", 1060, 850, 125, 38, size=18)
    p.edge((1430, 750), (1715, 980), points=((1430, 925), (1715, 925)), dashed=True)
    p.text("SQLite", 1535, 850, 125, 38, size=18)
    p.edge((1325, 560), (1325, 210), dashed=True)
    p.text("HTTPS · Responses API", 1360, 345, 290, 38, size=18, align="left")
    p.edge((1500, 665), (2300, 665), dashed=True)
    p.text("HTTPS · catálogo + ACP REST", 1600, 600, 340, 38, size=18)
    p.edge((1500, 725), (2300, 1085), points=((2145, 725), (2145, 1085)), dashed=True)
    p.text("HTTPS · AP2", 1760, 850, 170, 38, size=18)
    p.edge((2495, 750), (2495, 990), dashed=True)
    p.text("HTTPS", 2515, 845, 130, 38, size=18)
    return p


def agent_components() -> C4Page:
    p = C4Page("C4 · Componentes de la API", 2440, 1450)
    p.boundary(95, 90, 2250, 1270, "API del agente", AGENT_BLUE, scope="Contenedor")

    for title, y in (
        ("Entrada", 225),
        ("Orquestación", 490),
        ("Compra", 755),
        ("Conectores y datos", 1020),
    ):
        p.text(title, 145, y, 360, 42, size=21, bold=True, align="left")

    components = (
        ("API HTTP", "Turnos y compras", 365, 285, 400, 155),
        ("Sesión JWT", "Verifica al comprador", 970, 285, 400, 155),
        ("Eventos SSE", "Muestra el progreso", 1575, 285, 400, 155),
        ("LangGraph", "Conduce cada turno", 365, 550, 400, 155),
        ("Enrutador LLM", "Interpreta y elige", 970, 550, 400, 155),
        ("lookup_catalog", "Busca en el catálogo vigente", 1575, 550, 400, 155),
        ("Selección", "Filtra y propone variante", 245, 815, 400, 155),
        ("Sesión ACP", "Valida total y prepara sesión", 785, 815, 400, 155),
        ("Aprobación AP2", "Firma tras consentimiento", 1325, 815, 400, 155),
        ("Finalización", "Delega token y crea pedido", 1865, 815, 400, 155),
    )
    for name, description, x, y, width, height in components:
        p.element("component", x, y, width, height, name, description, AGENT_BLUE, show_type=False)

    adapters = (
        ("Cliente de catálogo", "Consulta al comercio", 175),
        ("Cliente ACP", "Sesión y pedido", 605),
        ("Cliente de pago", "Token de prueba", 1035),
        ("Compras", "Términos y mandatos", 1465),
        ("Puntos de control", "Memoria por hilo", 1895),
    )
    for name, description, x in adapters:
        p.element("component", x, 1080, 340, 145, name, description, AGENT_BLUE, show_type=False)

    p.edge((565, 440), (565, 550), dashed=True)
    p.edge((765, 625), (970, 625), dashed=True)
    p.edge((1370, 625), (1575, 625), dashed=True)
    p.edge((565, 705), (565, 815), dashed=True)
    p.edge((645, 890), (785, 890), dashed=True)
    p.edge((1185, 890), (1325, 890), dashed=True)
    p.edge((1725, 890), (1865, 890), dashed=True)
    return p


LANES = (
    ("Comprador", 180),
    ("Interfaz del agente", 555),
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


PURCHASE_SECTIONS = (
    (
        140,
        "CONVERSACIÓN Y CATÁLOGO",
        250,
        (
            (0, 1, "Hola"),
            (1, 2, "POST /assistant"),
            (2, 1, "Saludo · respuesta sin compra", True),
            (0, 1, "Quiero audífonos negros, máx. COP 700.000"),
            (1, 2, "POST /assistant · nuevo turno"),
            (2, 3, "GET /feeds/current · por turno"),
            (3, 2, "Catálogo vigente", True),
            (2, 1, "SSE · candidatos del catálogo", True),
            (1, 0, "Muestra candidatos informativos", True),
            (2, 1, "SSE · variante elegida por el agente", True),
        ),
    ),
    (
        1210,
        "SESIÓN ACP · VARIANTE ELEGIDA",
        1320,
        (
            (2, 3, "POST /checkout_sessions · variante elegida"),
            (3, 2, "Sesión, precio y opciones", True),
            (2, 3, "POST /checkout_sessions/{id} · entrega"),
            (3, 2, "Total final vigente", True),
            (2, 3, "GET /checkout_sessions/{id}/ap2/checkout-jwt"),
            (3, 2, "JWT de compra firmado", True),
            (2, 1, "SSE · producto y términos", True),
            (1, 0, "Producto, entrega, total y botón", True),
        ),
    ),
    (
        2080,
        "CONSENTIMIENTO Y PAGO DE PRUEBA",
        2190,
        (
            (0, 1, "Aprueba los términos"),
            (1, 2, "POST /purchases/{id}/approval"),
            (2, 3, "GET /checkout_sessions/{id} · verificar"),
            (3, 2, "Términos vigentes", True),
            (2, 1, "Firma mandatos y registra aprobación", True),
            (1, 2, "POST /purchases/{id}/complete"),
            (2, 4, "POST /agentic_commerce/delegate_payment · mandato de pago AP2"),
            (4, 2, "Token acotado", True),
            (2, 3, "Completar compra AP2 · mandato de compra + token"),
            (3, 4, "POST /payments/confirm · token"),
            (4, 3, "Pago sintético aprobado", True),
            (3, 2, "Pedido + comprobante de compra AP2", True),
            (2, 4, "POST /agentic_commerce/payment_receipt"),
            (4, 2, "Comprobante de pago AP2", True),
            (2, 1, "JSON · pedido confirmado", True),
            (1, 0, "Muestra número de pedido", True),
        ),
    ),
)


def add_purchase_sections(
    p: Page,
    sections: tuple[
        tuple[int, str, int, tuple[tuple[int, int, str] | tuple[int, int, str, bool], ...]], ...
    ],
) -> None:
    for phase_y, heading, first_y, rows in sections:
        phase(p, heading, phase_y)
        for index, row in enumerate(rows):
            message(
                p,
                row[0],
                row[1],
                first_y + index * 93,
                row[2],
                reply=row[3] if len(row) > 3 else False,
            )


def purchase_sequence() -> Page:
    p = sequence_page("Interacción de compra · comprador presente", 3760)
    add_purchase_sections(p, PURCHASE_SECTIONS)
    return p


def purchase_sequence_discovery() -> Page:
    p = sequence_page("Conversación y sesión ACP", 2150)
    add_purchase_sections(p, PURCHASE_SECTIONS[:2])
    return p


def purchase_sequence_payment() -> Page:
    p = sequence_page("Aprobación y pago de prueba", 1830)
    _, heading, _, rows = PURCHASE_SECTIONS[2]
    add_purchase_sections(p, ((140, heading, 250, rows),))
    return p


def state_machine() -> Page:
    p = Page("Estados de compra del agente", 3900, 1490)
    main_states = (
        ("sin compra", 100, 250, AGENT_BLUE),
        ("recibido", 540, 250, AGENT_BLUE),
        ("seleccionado", 980, 250, AGENT_BLUE),
        ("sesión preparada", 1420, 310, AGENT_BLUE),
        ("esperando aprobación", 1910, 350, MERCHANT_AMBER),
        ("aprobado", 2450, 250, MERCHANT_AMBER),
        ("completando", 2890, 250, AGENT_BLUE),
        ("compra completada", 3330, 270, BUYER_GREEN),
    )
    for name, x, width, border in main_states:
        p.box(x, 520, width, 110, border=border, stroke=2.5, double=name == "compra completada")
        p.text(name, x + 8, 552, width - 16, 46, size=25, bold=True)
    for name, x, width, border in (
        ("fallido", 1430, 250, FAILURE_RED),
        ("cancelado", 1955, 260, FAILURE_RED),
        ("resultado incierto", 2875, 265, UNCERTAIN_PURPLE),
    ):
        p.box(x, 1010, width, 110, border=border, stroke=2.5, double=name != "resultado incierto")
        p.text(name, x + 8, 1042, width - 16, 46, size=25, bold=True)

    p.initial(50, 564)
    p.edge((72, 575), (100, 575))
    labels = (
        "compra",
        "variante",
        "crear sesión",
        "total válido",
        "aprueba + AP2",
        "completar",
        "pedido",
    )
    for (_, x, width, _), (_, next_x, _, _), label in zip(
        main_states[:-1], main_states[1:], labels, strict=True
    ):
        start = x + width
        gap = next_x - start
        p.edge((start, 575), (next_x, 575))
        p.text(label, start + 5, 530, gap - 10, 34, size=22)

    p.edge((170, 520), (300, 520), points=((65, 330), (405, 330)), curved=True)
    p.text("responde o pide aclaración", 80, 260, 330, 40, size=19)

    p.edge((1120, 630), (1480, 1010), points=((1250, 810),), curved=True)
    p.text("fallo al crear sesión", 930, 745, 240, 42, size=19)
    p.edge((1510, 630), (1580, 1010), points=((1500, 825),), curved=True)
    p.text("fallo de preparación", 1570, 865, 235, 42, size=19)
    p.edge((1670, 630), (2010, 1010), points=((1830, 810),), curved=True)
    p.text("supera presupuesto", 1845, 725, 230, 42, size=19)
    p.edge((2140, 630), (2140, 1010))
    p.text("usuario cancela", 2160, 790, 180, 42, size=19)

    p.edge((2940, 520), (2610, 520), points=((2780, 335),), curved=True)
    p.text("error recuperable", 2670, 380, 245, 42, size=19)

    p.edge((3015, 630), (3015, 1010))
    p.text("tiempo agotado / 5xx", 2790, 805, 205, 42, size=19)
    p.edge((3090, 1010), (3060, 630), points=((3250, 820),), curved=True)
    p.text("reintenta", 3145, 760, 130, 42, size=19)
    p.edge((3140, 1065), (3510, 630), points=((3710, 965), (3740, 730)), curved=True)
    p.text("reconcilia pedido", 3500, 795, 250, 42, size=19)

    p.edge(
        (2900, 630),
        (1555, 1120),
        points=((2590, 720), (2500, 1280), (1555, 1280)),
        curved=True,
    )
    p.text("pago rechazado / sin inventario", 1940, 1210, 320, 42, size=19)
    return p


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write("agent-system-context-c4.drawio", [system_context()])
    write("agent-architecture-c4.drawio", [architecture()])
    write("agent-components-c4.drawio", [agent_components()])
    write("purchase-sequence.drawio", [purchase_sequence()])
    write("purchase-sequence-1-discovery.drawio", [purchase_sequence_discovery()])
    write("purchase-sequence-2-payment.drawio", [purchase_sequence_payment()])
    write("purchase-state-machine.drawio", [state_machine()])


if __name__ == "__main__":
    main()
