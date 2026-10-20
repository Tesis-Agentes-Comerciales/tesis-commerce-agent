"""Build the B0/B2/HNP diagrams of docs/diagrams/ and export them with the draw.io CLI.

Reuses the primitives and palette of build_presentation_diagrams.py. Each diagram marks what
is done in org/main, what is in progress for the B2 pilot and what is future work.

    uv run python scripts/tools/build_governance_diagrams.py            # .drawio only
    uv run python scripts/tools/build_governance_diagrams.py --export   # + .svg and .png
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from build_presentation_diagrams import (
    AGENT_BLUE,
    BUYER_GREEN,
    EXTERNAL_WINE,
    FAILURE_RED,
    INK,
    LINE,
    MERCHANT_AMBER,
    MUTED,
    OUT,
    PALE,
    C4Page,
    write,
)

DESCRIPTION = "#555555"
FUTURE_GRAY = "#8C8C8C"
OUTSIDE_GRAY = "#686868"
PNG_WIDTH = 1920
TIMES = "\u00d7"  # multiplication sign, spelled out for ruff RUF001

DONE, ONGOING, FUTURE = "hecho", "en curso", "futuro"
DASH = {
    DONE: "dashed=0;",
    ONGOING: "dashed=1;dashPattern=10 6;",
    FUTURE: "dashed=1;dashPattern=2 5;",
}
STATUS_NOTE = {
    DONE: "en org/main",
    ONGOING: "piloto B2, semana del 23-oct",
    FUTURE: "después del 23-oct",
}


class Sheet(C4Page):
    """C4 page with status-aware cards, labelled arrows and legends."""

    def vertex(self, value: str, style: str, x: int, y: int, width: int, height: int) -> None:
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

    def card(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        name: str,
        description: str = "",
        *,
        kind: str | None = None,
        border: str = AGENT_BLUE,
        status: str = DONE,
        shape: str = "rounded=1;arcSize=8;",
        name_size: int = 20,
        description_size: int = 17,
        kind_size: int = 15,
        stroke: float = 2.5,
        valign: str = "middle",
        align: str = "center",
        fill: str = "#FFFFFF",
        extra: str = "",
    ) -> None:
        future = status == FUTURE
        name_color = MUTED if future else INK
        parts = [
            f'<div style="font-size:{name_size}px;color:{name_color};line-height:1.15">'
            f"<b>{name}</b></div>"
        ]
        if kind:
            parts.append(
                f'<div style="font-size:{kind_size}px;color:{MUTED};margin-top:3px">[{kind}]</div>'
            )
        if description:
            parts.append(
                f'<div style="font-size:{description_size}px;color:{DESCRIPTION};'
                f'margin-top:6px;line-height:1.25">{description}</div>'
            )
        style = (
            f"{shape}whiteSpace=wrap;html=1;fillColor={fill};"
            f"strokeColor={FUTURE_GRAY if future else border};strokeWidth={stroke};"
            f"{DASH[status]}fontFamily=Arial;fontColor={INK};align={align};"
            f"verticalAlign={valign};spacing=10;{extra}"
        )
        self.vertex(
            f'<div style="font-family:Arial">{"".join(parts)}</div>', style, x, y, width, height
        )

    def label(
        self,
        x: int,
        y: int,
        width: int,
        verb: str,
        technology: str | None = None,
        *,
        height: int = 62,
        align: str = "center",
        size: int = 18,
        color: str = INK,
    ) -> None:
        html = f'<b style="color:{color}">{verb}</b>'
        if technology:
            html += f'<br><span style="font-size:{size - 2}px;color:{MUTED}">[{technology}]</span>'
        style = (
            "text;html=1;whiteSpace=wrap;fillColor=none;strokeColor=none;"
            f"fontFamily=Arial;fontSize={size};fontColor={INK};align={align};"
            "verticalAlign=middle;spacing=0;"
        )
        self.vertex(html, style, x, y, width, height)

    def link(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        points: tuple[tuple[int, int], ...] = (),
        color: str = LINE,
        dashed: bool = True,
        width: float = 2.1,
    ) -> None:
        self.edge(start, end, points=points, dashed=dashed, width=width, color=color)

    def heading(self, title: str, subtitle: str, *, width: int) -> None:
        self.text(title, 50, 26, width - 100, 46, size=31, bold=True, align="left")
        self.text(subtitle, 50, 76, width - 100, 34, size=19, color=MUTED, align="left")

    def status_legend(self, x: int, y: int, *, card_width: int = 300) -> None:
        self.text("Estado", x, y, 90, 64, size=19, bold=True, align="left")
        for index, status in enumerate((DONE, ONGOING, FUTURE)):
            self.card(
                x + 100 + index * (card_width + 22),
                y,
                card_width,
                64,
                status.capitalize(),
                STATUS_NOTE[status],
                status=status,
                name_size=18,
                description_size=15,
                stroke=2.2,
            )

    def color_legend(self, text: str, x: int, y: int, width: int) -> None:
        self.text(text, x, y, width, 34, size=17, color=MUTED, align="left")


def write_one(name: str, page: Sheet) -> None:
    write(f"{name}.drawio", [page])


# ---------------------------------------------------------------------------
# 1-2. Container views: B0 and B2 Human Present
# ---------------------------------------------------------------------------

STEPS = (
    (
        "conversation",
        "Conversación",
        "Componente: LangGraph + LLM",
        "Interpreta y propone variante. El LLM no es confiable.",
    ),
    (
        "purchase",
        "PurchaseService",
        "Componente B0",
        "Guardas duras, POST del checkout ACP y presupuesto",
    ),
    (
        "terms",
        "Presentación de términos",
        "Componente B0",
        "Términos cerrados con huella HMAC",
    ),
    (
        "approval",
        "ApprovalService",
        "Componente B0",
        "Compara la huella y firma mandatos AP2",
    ),
    (
        "completion",
        "CompletionService",
        "Componente B0",
        "PSP: pago delegado. Comercio: complete",
    ),
)

# (row, column, span, name, description, status); columns 0..2 inside the control area.
B2_CONTROLS: dict[str, tuple[tuple[int, int, int, str, str, str], ...]] = {
    "conversation": (
        (
            0,
            0,
            2,
            "Recuperación gobernada",
            "Explica la causa con texto canónico: 200, nunca 5xx (S02)",
            ONGOING,
        ),
    ),
    "purchase": (
        (0, 0, 1, "Captura hechos canónicos", "Feed, intención, ranking y variante", DONE),
        (
            0,
            1,
            1,
            "Evalúa POL-01 y POL-02",
            "Producto elegible; comercio en la lista de Settings",
            DONE,
        ),
        (
            0,
            2,
            1,
            "Confirma una transacción",
            "Evidencia, reserva y sobre HMAC",
            DONE,
        ),
        (1, 2, 1, "Envía un solo POST", "Clave idempotente: frontera exactly-once", ONGOING),
        (
            1,
            1,
            1,
            "Verifica la creación",
            "GET = POST → awaiting_confirmation",
            ONGOING,
        ),
        (1, 0, 1, "Recuperación D17", "Lookup de un despacho uncertain", FUTURE),
    ),
    "terms": (
        (0, 0, 2, "Frescura al presentar", "Relee la oferta vigente (/lab/offers/get)", ONGOING),
    ),
    "approval": (
        (
            0,
            0,
            1,
            "Continuación gobernada",
            "Exige allow comprometido y checkout confirmed",
            ONGOING,
        ),
        (0, 1, 1, "Frescura al aprobar", "Si cambió: 409 y no se firma (S04)", ONGOING),
        (0, 2, 1, "D2-HP completo", "Challenge, identidad, POL-03..06", FUTURE),
    ),
    "completion": (
        (
            0,
            0,
            2,
            "Evidencia hasta los recibos",
            "Encadena D1, aprobación, mandatos y recibos",
            FUTURE,
        ),
    ),
}


def container_view(governed: bool) -> Sheet:
    ui_x, ui_w = 340, 250
    api_x = 800
    frame_x = api_x + 36
    b0_w = 330 if governed else 430
    ctrl_w, ctrl_gap, ctrl_h, ctrl_row_gap = 250, 24, 128, 30
    frame_w = b0_w + (16 + 3 * ctrl_w + 2 * ctrl_gap + 18 if governed else 0)
    frame_r = frame_x + frame_w
    api_r = frame_r + 36
    agent_r = api_r + 36
    ext_x, ext_w = agent_r + 230, 330
    width = ext_x + ext_w + 50

    top = 210 if governed else 190
    row_h = {
        "conversation": 168,
        "purchase": 382 if governed else 168,
        "terms": 168,
        "approval": 172,
        "completion": 168,
    }
    row_gap = 76
    api_y = top
    y = api_y + (150 if governed else 80)
    rows: dict[str, tuple[int, int]] = {}
    for key, *_ in STEPS:
        rows[key] = (y, row_h[key])
        y += row_h[key] + row_gap
    api_bottom = y - row_gap + 40
    data_y = api_bottom + 110
    agent_bottom = data_y + 150 + 40
    eval_y = agent_bottom + 50
    legend_y = eval_y + 190
    height = legend_y + 110

    title = (
        "B2 · Contenedores del agente con gobernanza Human Present"
        if governed
        else "B0 · Contenedores del agente comprador"
    )
    subtitle = (
        "Mismos contenedores que B0. Los controles B2 aparecen dentro del componente "
        "donde actúan, con su estado al 20-oct-2026."
        if governed
        else "Línea base: el LLM propone, código determinista prepara el checkout "
        "y el humano aprueba términos exactos. Sin gobernanza de decisión."
    )
    p = Sheet("B2-HP · contenedores" if governed else "B0 · contenedores", width, height)
    p.heading(title, subtitle, width=width)

    center = (rows["terms"][0] + rows["approval"][0] + rows["approval"][1]) // 2
    p.element("person", 40, center - 110, 230, 200, "Comprador", "Persona que compra", BUYER_GREEN)
    p.boundary(
        300,
        top - 70,
        agent_r - 300,
        agent_bottom - top + 70,
        "Agente comprador · producto (src/, ui/, auth/)",
        AGENT_BLUE,
    )
    ui_top = rows["conversation"][0]
    ui_bottom = rows["completion"][0] + rows["completion"][1]
    p.card(
        ui_x,
        ui_top,
        ui_w,
        ui_bottom - ui_top,
        "Interfaz web",
        "Chat, tarjetas de producto y botón «Aprobar»",
        kind="Contenedor: React · assistant-ui",
        shape="shape=mxgraph.c4.webBrowserContainer2;",
        valign="top",
        name_size=24,
        description_size=18,
        extra="spacingTop=48;",
    )
    p.boundary(
        api_x,
        api_y,
        api_r - api_x,
        api_bottom - api_y,
        "API del agente  [Contenedor: FastAPI · Python]",
        AGENT_BLUE,
        scope="Contenedor",
    )

    # Steps of the purchase, top to bottom, with B2 controls nested where they act.
    for key, name, kind, description in STEPS:
        row_y, row_height = rows[key]
        if governed:
            p.box(frame_x, row_y, frame_w, row_height, border=AGENT_BLUE, stroke=2.5)
            p.card(
                frame_x + 6,
                row_y + 6,
                b0_w - 12,
                row_height - 12,
                name,
                description,
                kind=kind,
                stroke=0,
                name_size=23,
                description_size=18,
                extra="strokeColor=none;",
            )
            area_x = frame_x + b0_w + 16
            area_y = row_y + 22
            if key == "purchase":
                p.text(
                    "Gate D1 · antes del checkout",
                    area_x,
                    row_y + 8,
                    500,
                    34,
                    size=19,
                    bold=True,
                    align="left",
                )
                area_y = row_y + 50
            p.box(
                frame_x + b0_w,
                row_y + 16,
                2,
                row_height - 32,
                fill="#D6DEE6",
                border="#D6DEE6",
                rounded=False,
                stroke=0,
            )
            for row, column, span, c_name, c_desc, status in B2_CONTROLS[key]:
                p.card(
                    area_x + column * (ctrl_w + ctrl_gap),
                    area_y + row * (ctrl_h + ctrl_row_gap),
                    span * ctrl_w + (span - 1) * ctrl_gap,
                    ctrl_h,
                    c_name,
                    c_desc,
                    kind=f"B2 · {status}",
                    status=status,
                    name_size=19,
                    description_size=17,
                    kind_size=14,
                )
            if key == "purchase":
                chain_y = area_y + ctrl_h // 2
                for column in (0, 1):
                    start = area_x + column * (ctrl_w + ctrl_gap) + ctrl_w
                    p.link((start, chain_y), (start + ctrl_gap, chain_y), color=AGENT_BLUE)
                drop_x = area_x + 2 * (ctrl_w + ctrl_gap) + ctrl_w // 2
                p.link(
                    (drop_x, area_y + ctrl_h),
                    (drop_x, area_y + ctrl_h + ctrl_row_gap),
                    color=AGENT_BLUE,
                )
                back_y = area_y + ctrl_h + ctrl_row_gap + ctrl_h // 2
                back_x = area_x + 2 * (ctrl_w + ctrl_gap)
                p.link((back_x, back_y), (back_x - ctrl_gap, back_y), color=AGENT_BLUE)
        else:
            p.card(
                frame_x,
                row_y,
                frame_w,
                row_height,
                name,
                description,
                kind=kind,
                name_size=23,
                description_size=18,
            )

    if governed:
        p.card(
            frame_r - 2 * ctrl_w - ctrl_gap,
            api_y + 22,
            2 * ctrl_w + ctrl_gap,
            100,
            "GET /health",
            "Reporta governance_mode y hp_flow",
            kind="B2 · hecho",
            name_size=19,
            description_size=17,
            kind_size=14,
        )

    # Flow between steps inside the API.
    step_x = frame_x + b0_w // 2
    for upper, lower, verb in (
        ("conversation", "purchase", "Elige variante"),
        ("purchase", "terms", "Entrega checkout listo"),
        ("approval", "completion", "Entrega aprobación firmada"),
    ):
        start = rows[upper][0] + rows[upper][1]
        p.link((step_x, start), (step_x, rows[lower][0]))
        p.label(
            step_x + 14, start + 6, frame_r - step_x - 24, verb, height=row_gap - 12, align="left"
        )
    wait_y = rows["terms"][0] + rows["terms"][1]
    p.text(
        "Espera el clic humano",
        step_x - 160,
        wait_y + 18,
        320,
        40,
        size=17,
        color=MUTED,
    )

    # Buyer and web interface.
    def row_mid(key: str, shift: int = 0) -> int:
        return rows[key][0] + rows[key][1] // 2 + shift

    p.link((270, center), (ui_x, center))
    p.label(40, center + 100, 230, "Conversa y aprueba", "HTTPS")
    for key, verb, technology, inbound in (
        ("conversation", "Envía turnos", "SSE /assistant", True),
        ("terms", "Muestra términos y huella", "SSE", False),
        ("approval", "Envía el clic humano", "POST /approval", True),
        ("completion", "Pide completar", "POST /complete", True),
    ):
        y_line = row_mid(key, 34)
        if inbound:
            p.link((ui_x + ui_w, y_line), (frame_x, y_line))
        else:
            p.link((frame_x, y_line), (ui_x + ui_w, y_line))
        p.label(ui_x + ui_w + 12, y_line - 84, api_x - ui_x - ui_w - 24, verb, technology)

    # Identity, local stores and evidence ledgers.
    auth_y = data_y
    p.card(
        ui_x,
        auth_y,
        ui_w,
        150,
        "Auth",
        "Emite el JWT del comprador",
        kind="Contenedor: Better Auth",
        name_size=22,
        description_size=17,
    )
    p.link((ui_x + ui_w // 2, ui_bottom), (ui_x + ui_w // 2, auth_y))
    p.label(ui_x + ui_w // 2 + 14, ui_bottom + 14, 200, "Inicia sesión", "HTTPS", align="left")
    jwt_x = api_x - 50
    p.link(
        (api_x, api_bottom - 20),
        (ui_x + ui_w, auth_y + 95),
        points=((jwt_x, api_bottom - 20), (jwt_x, auth_y + 95)),
    )
    p.label(ui_x + ui_w + 10, auth_y + 105, jwt_x - ui_x - ui_w - 20, "Valida JWT", "JWKS")

    stores: list[tuple[str, str, str, str]] = [
        ("SQLite del agente", "Compras, aprobaciones, claves idempotentes", "SQLite", DONE),
        ("SQLite del grafo", "Checkpoints por hilo", "SQLite", DONE),
    ]
    if governed:
        stores += [
            ("governance_evidence", "Ledger D1: decisión y aplicación", "solo inserción", DONE),
            ("hp_evidence", "Frescura y recuperación, por digest", "solo inserción", ONGOING),
        ]
    store_w = 250 if governed else 220
    store_gap = 34
    store_x = api_x + 24
    for index, (name, description, technology, status) in enumerate(stores):
        x = store_x + index * (store_w + store_gap)
        p.card(
            x,
            data_y,
            store_w,
            150,
            name,
            description,
            kind=f"{technology}" + ("" if index < 2 else f" · {status}"),
            status=status,
            shape="shape=cylinder3;size=14;boundedLbl=1;",
            name_size=19,
            description_size=16,
            kind_size=14,
        )
        p.link((x + store_w // 2, api_bottom), (x + store_w // 2, data_y))
    p.label(
        store_x + store_w // 2 + 12,
        api_bottom + 10,
        store_w + store_gap - 24,
        "Lee y escribe",
        "SQLite",
        height=88,
        align="left",
    )
    if governed:
        ledger_x = store_x + 2 * (store_w + store_gap)
        p.label(
            ledger_x + store_w // 2 + 12,
            api_bottom + 10,
            store_w + store_gap - 24,
            "Inserta evidencia",
            "sin UPDATE ni DELETE",
            height=88,
            align="left",
        )

    # External systems.
    conv_y, conv_h = rows["conversation"]
    p.card(
        ext_x,
        conv_y - 10,
        ext_w,
        conv_h - 20,
        "OpenAI",
        "Modelo conversacional",
        kind="Sistema externo: Responses API",
        border=EXTERNAL_WINE,
        name_size=22,
    )
    merchant_y = rows["purchase"][0]
    merchant_bottom = rows["approval"][0] + rows["approval"][1]
    p.card(
        ext_x,
        merchant_y,
        ext_w,
        merchant_bottom - merchant_y,
        "Comercio",
        "Fuente de verdad: catálogo, precio, stock, checkout y pedido",
        kind="Sistema externo: ACP REST · PostgreSQL",
        border=MERCHANT_AMBER,
        name_size=24,
        description_size=18,
    )
    psp_y = rows["completion"][0] + 40
    p.card(
        ext_x,
        psp_y,
        ext_w,
        150,
        "PSP sandbox",
        "Proveedor de pago de prueba",
        kind="Sistema externo: AP2",
        border=EXTERNAL_WINE,
        name_size=22,
    )
    gap_x = agent_r + 12
    gap_w = ext_x - agent_r - 24

    p.link((frame_r, conv_y + 50), (ext_x, conv_y + 50))
    p.label(gap_x, conv_y - 22, gap_w, "Interpreta y propone", "Responses API")
    feed_y = conv_y + conv_h - 22
    feed_x = ext_x + 70
    p.link((frame_r, feed_y), (feed_x, merchant_y), points=((feed_x, feed_y),))
    p.label(gap_x, feed_y + 6, gap_w, "Lee el catálogo", "GET /feeds/current")

    merchant_calls = [
        ("purchase", "Crea y actualiza el checkout", "ACP REST"),
        ("terms", "Lee checkout y JWT firmado", "ACP"),
        ("approval", "Relee el checkout vigente", "ACP GET"),
    ]
    if governed:
        merchant_calls = [
            ("purchase", "POST idempotente; GET de verificación", "ACP REST"),
            ("terms", "Relee la oferta vigente", "POST /lab/offers/get"),
            ("approval", "Relee checkout y oferta", "ACP GET · /lab/offers/get"),
        ]
    for key, verb, technology in merchant_calls:
        y_line = row_mid(key, 30)
        if key == "purchase" and governed:
            y_line = rows["purchase"][0] + 50 + ctrl_h + ctrl_row_gap + ctrl_h // 2
            area_end = frame_x + b0_w + 16 + 3 * ctrl_w + 2 * ctrl_gap
            p.link((area_end, y_line), (frame_r, y_line), color=AGENT_BLUE)
        p.link((frame_r, y_line), (ext_x, y_line))
        p.label(gap_x, y_line - 74, gap_w, verb, technology, height=68)

    comp_y = rows["completion"][0]
    up_x = ext_x + 70
    complete_y = comp_y + 22
    p.link((frame_r, complete_y), (up_x, merchant_bottom), points=((up_x, complete_y),))
    p.label(gap_x, complete_y - 70, gap_w, "Completa con mandato + token", "ACP + AP2", height=64)
    pay_y = psp_y + 95
    p.link((frame_r, pay_y), (ext_x, pay_y))
    p.label(gap_x, pay_y - 66, gap_w, "Delega el pago", "mandato de pago AP2", height=60)
    confirm_x = ext_x + ext_w - 70
    p.link((confirm_x, merchant_bottom), (confirm_x, psp_y))
    p.label(confirm_x - 230, merchant_bottom + 4, 216, "Confirma el pago", height=36, align="right")

    # The evaluation package lives outside the product.
    eval_w = 520
    p.card(
        ui_x,
        eval_y,
        eval_w,
        130,
        "evaluation/",
        "Runners del piloto B0/B2 y de HNP; el producto nunca lo importa",
        kind="Paquete fuera del producto",
        border=OUTSIDE_GRAY,
        name_size=21,
        description_size=17,
    )
    eval_link_x = agent_r + 60
    p.link(
        (ui_x + eval_w, eval_y + 65),
        (api_r, api_bottom - 20),
        points=((eval_link_x, eval_y + 65), (eval_link_x, api_bottom - 20)),
    )
    p.label(
        ui_x + eval_w + 30,
        eval_y - 2,
        min(480, eval_link_x - ui_x - eval_w - 40),
        "Ejecuta escenarios" if not governed else "Verifica la condición y ejecuta",
        "HTTP" if not governed else "GET /health · HTTP",
        align="left",
    )

    colors = (
        "Verde: persona · Azul: agente · Ocre: comercio · Vino: sistemas externos · "
        "Gris: fuera del producto · Flechas punteadas: relaciones"
    )
    if governed:
        p.status_legend(ui_x, legend_y)
        p.color_legend(colors, ui_x + 1100, legend_y + 15, 1200)
    else:
        p.color_legend(
            "Todo lo que muestra B0 está en org/main. Con AGENT_GOVERNANCE_MODE=off es la "
            "condición B0' del piloto; el monitor de gobernanza se retiró (PR #26).",
            ui_x,
            legend_y,
            1700,
        )
        p.color_legend(colors, ui_x, legend_y + 40, 1700)
    return p


# ---------------------------------------------------------------------------
# 3. B0 vs B2 delta, one slide
# ---------------------------------------------------------------------------


def delta_view() -> Sheet:
    width, height = 1920, 1080
    p = Sheet("B0 vs B2 · qué agrega la gobernanza", width, height)
    p.heading(
        "Qué agrega B2 al flujo Human Present, y en qué punto",
        "Mismo commit, modelo, prompt, comercio y PSP. B2 = B0 + controles deterministas "
        "fuera del LLM.",
        width=width,
    )
    x0, col_w, gap = 170, 320, 26
    cols = [x0 + index * (col_w + gap) for index in range(5)]
    for index, title in enumerate(
        (
            "1 · Conversación",
            "2 · Checkout ACP",
            "3 · Términos",
            "4 · Aprobación humana",
            "5 · Pago y pedido",
        )
    ):
        p.text(title, cols[index], 128, col_w, 34, size=21, bold=True)

    p.text("B0", 40, 180, 110, 40, size=26, bold=True, align="left")
    p.text("línea base", 40, 218, 120, 30, size=16, color=MUTED, align="left")
    b0 = (
        ("Conversa y propone", "LangGraph + LLM no confiable; filtros duros"),
        ("Crea el checkout", "PurchaseService: guardas, POST y presupuesto"),
        ("Presenta términos", "Términos cerrados con huella HMAC"),
        ("Aprueba y firma", "Clic humano; ApprovalService firma AP2"),
        ("Paga y completa", "PSP delega el pago; el comercio completa"),
    )
    for index, (name, description) in enumerate(b0):
        border = MERCHANT_AMBER if index == 3 else AGENT_BLUE
        p.card(cols[index], 172, col_w, 118, name, description, border=border, name_size=20)
        if index < 4:
            p.link((cols[index] + col_w, 231), (cols[index + 1], 231), dashed=False, width=2)

    p.box(40, 318, width - 80, 2, fill="#DDDDDD", border="#DDDDDD", rounded=False, stroke=0)
    p.text("B2", 40, 346, 110, 40, size=26, bold=True, align="left")
    p.text("añade", 40, 384, 120, 30, size=16, color=MUTED, align="left")

    added = {
        0: (
            (
                "Recuperación gobernada",
                "Explica sin 5xx si no hay candidato válido (S02)",
                ONGOING,
            ),
        ),
        1: (
            (
                "Gate D1 antes del POST",
                "Hechos canónicos → POL-01 · POL-02 → evidencia, reserva y sobre HMAC "
                "en una transacción",
                DONE,
            ),
            ("Un solo POST idempotente", "Frontera exactly-once; timeout → uncertain", ONGOING),
            ("Verificación post-creación", "GET = POST → awaiting_confirmation", ONGOING),
        ),
        2: (("Frescura al presentar", "Relee la oferta vigente", ONGOING),),
        3: (
            (
                "Continuación gobernada",
                "Solo con allow comprometido y checkout confirmado",
                ONGOING,
            ),
            ("Frescura al aprobar", "Si la oferta cambió: 409 y no se firma (cierra S04)", ONGOING),
        ),
    }
    heights = {(1, 0): 150}
    for column, controls in added.items():
        y = 346
        for row, (name, description, status) in enumerate(controls):
            h = heights.get((column, row), 112)
            p.card(
                cols[column],
                y,
                col_w,
                h,
                name,
                description,
                kind=status,
                status=status,
                name_size=19,
                description_size=16,
                kind_size=14,
            )
            y += h + 16
    p.text(
        "Sin cambios en el piloto: firma AP2, PSP y complete",
        cols[4] + 10,
        346,
        col_w - 20,
        90,
        size=17,
        color=MUTED,
    )

    p.text("Verificable", 40, 806, 130, 40, size=20, bold=True, align="left")
    for index, (name, description, status, span_from, span) in enumerate(
        (
            ("GET /health", "Reporta governance_mode y hp_flow", DONE, 0, 1),
            (
                "governance_evidence",
                "Ledger D1 de solo inserción: decisión, insumos y aplicación",
                DONE,
                1,
                2,
            ),
            (
                "hp_evidence",
                "Solo inserción, por digest: frescura y recuperación",
                ONGOING,
                3,
                2,
            ),
        )
    ):
        del index
        p.card(
            cols[span_from],
            786,
            span * col_w + (span - 1) * gap,
            88,
            name,
            description,
            kind=status if span_from == 0 else f"SQLite · {status}",
            status=status,
            name_size=18,
            description_size=15,
            kind_size=13,
        )

    p.text("Futuro", 40, 902, 120, 40, size=20, bold=True, align="left")
    for column, name, description in (
        (1, "Recuperación D17", "Lookup de un despacho uncertain"),
        (3, "D2-HP completo", "Challenge, identidad, POL-03..06"),
        (4, "Evidencia hasta recibos", "Cadena D1 → aprobación → recibos"),
    ):
        p.card(
            cols[column],
            892,
            col_w,
            74,
            name,
            description,
            status=FUTURE,
            name_size=18,
            description_size=15,
        )
    p.status_legend(170, 990, card_width=280)
    p.color_legend("Ocre: punto de aprobación humana", 1290, 1005, 600)
    return p


# ---------------------------------------------------------------------------
# 4. Governed purchase sequence
# ---------------------------------------------------------------------------

SEQ_LANES = (
    ("Comprador", 150, BUYER_GREEN),
    ("Interfaz web", 480, AGENT_BLUE),
    ("Agente (API)", 840, AGENT_BLUE),
    ("Gate D1 y evidencia", 1220, AGENT_BLUE),
    ("Comercio", 1600, MERCHANT_AMBER),
    ("PSP sandbox", 1960, EXTERNAL_WINE),
)
STEP = 76


class Sequence:
    def __init__(self, page: Sheet, y: int) -> None:
        self.p = page
        self.y = y

    def phase(self, text: str) -> None:
        self.y += 36
        self.p.text(text, 40, self.y, 1200, 36, size=21, bold=True, align="left")
        self.p.edge((40, self.y + 40), (2090, self.y + 40), arrow=False, width=1, color="#BBBBBB")
        self.y += 60

    def msg(
        self,
        sender: int,
        receiver: int,
        label: str,
        *,
        reply: bool = False,
        color: str = LINE,
        status: str | None = None,
    ) -> None:
        self.y += STEP
        x1, x2 = SEQ_LANES[sender][1], SEQ_LANES[receiver][1]
        self.p.edge(
            (x1, self.y),
            (x2, self.y),
            dashed=reply,
            width=2.2 if color != LINE else 1.9,
            color=color,
        )
        if status:
            label += f' <span style="color:{MUTED};font-size:15px">[{status}]</span>'
        left, span = min(x1, x2) + 10, abs(x2 - x1) - 20
        text_color = FAILURE_RED if color == FAILURE_RED else INK
        style = (
            "text;html=1;whiteSpace=wrap;fillColor=none;strokeColor=none;fontFamily=Arial;"
            f"fontSize=18;fontColor={text_color};align=center;verticalAlign=bottom;spacing=0;"
        )
        self.p.vertex(label, style, left, self.y - 66, span, 60)

    def note(
        self,
        first: int,
        last: int,
        name: str,
        description: str,
        status: str,
        *,
        border: str = AGENT_BLUE,
        height: int = 92,
    ) -> None:
        self.y += 30
        left = SEQ_LANES[first][1] - 170
        right = SEQ_LANES[last][1] + 170
        self.p.card(
            left,
            self.y,
            right - left,
            height,
            name,
            description,
            kind=status if status != DONE or border == AGENT_BLUE else None,
            status=status,
            border=border,
            name_size=18,
            description_size=16,
            kind_size=14,
        )
        self.y += height


def governed_sequence() -> Sheet:
    width = 2130
    p = Sheet("Secuencia HP gobernada", width, 3800)
    p.heading(
        "Compra Human Present gobernada: puntos de control",
        "Azul: mensajes que agrega B2. Gris: flujo B0 que se conserva. "
        "Rojo: rechazo sin efecto comercial.",
        width=width,
    )
    for title, x, border in SEQ_LANES:
        p.box(x - 150, 130, 300, 70, fill=PALE, border=border, stroke=2)
        p.text(title, x - 140, 145, 280, 40, size=22, bold=True)
    s = Sequence(p, 210)
    blue = AGENT_BLUE

    s.note(
        1,
        3,
        "Condición verificable · GET /health",
        "El runner confirma governance_mode=enforce y hp_flow=governed antes de cada repetición",
        DONE,
        height=74,
    )

    s.phase("1 · CONVERSACIÓN · el LLM propone, no decide")
    s.msg(0, 1, "Quiero audífonos negros, máx. COP 700.000")
    s.msg(1, 2, "POST /assistant · turno (SSE)")
    s.msg(2, 4, "GET /feeds/current · catálogo vigente")
    s.msg(4, 2, "Feed validado", reply=True)
    s.note(2, 2, "LLM + filtros duros", "Interpreta y propone una variante elegible", DONE)
    s.note(
        1,
        2,
        "Recuperación gobernada",
        "Sin candidato válido o variante fuera de la lista: explica la causa, 200 y sin 5xx (S02)",
        ONGOING,
    )

    s.phase("2 · GATE D1 · antes del checkout")
    s.msg(2, 3, "Hechos canónicos: feed, intención, ranking y variante", color=blue)
    s.note(
        3,
        3,
        "Evalúa POL-01 y POL-02",
        "Producto elegible; comercio en la lista de Settings",
        DONE,
    )
    s.note(
        3,
        3,
        "Una transacción SQLite",
        "governance_evidence + reserva + sobre HMAC",
        DONE,
    )
    s.msg(3, 2, "deny → d1_denied · sin POST", reply=True, color=FAILURE_RED, status=DONE)
    s.msg(3, 4, "allow → POST /checkout_sessions · clave idempotente", color=blue, status=ONGOING)
    s.msg(4, 3, "Checkout creado", reply=True)
    s.msg(3, 4, "GET /checkout_sessions/{id} · igual al POST", color=blue, status=ONGOING)
    s.msg(3, 2, "confirmed → awaiting_confirmation", reply=True, color=blue)
    s.note(
        3,
        4,
        "Frontera exactly-once",
        "Timeout o respuesta inválida → uncertain; nunca se reenvía el POST",
        ONGOING,
        height=74,
    )

    s.phase("3 · PRESENTACIÓN DE TÉRMINOS")
    s.msg(2, 4, "POST /checkout_sessions/{id} · entrega; aplica presupuesto")
    s.msg(4, 2, "Total final y ready_for_payment", reply=True)
    s.msg(2, 4, "POST /lab/offers/get · frescura al presentar", color=blue, status=ONGOING)
    s.msg(4, 2, "Oferta vigente", reply=True)
    s.msg(2, 1, "SSE · producto, total, huella y checkout JWT", reply=True)
    s.msg(1, 0, "Muestra términos y botón «Aprobar»", reply=True)

    s.phase("4 · APROBACIÓN · frescura antes de firmar")
    s.msg(0, 1, "Aprueba los términos mostrados")
    s.msg(1, 2, "POST /purchases/{id}/approval · huella")
    s.msg(2, 3, "¿Allow comprometido y checkout confirmed?", color=blue, status=ONGOING)
    s.msg(3, 2, "Continuación gobernada", reply=True, color=blue)
    s.msg(2, 4, "POST /lab/offers/get · relee la oferta vigente", color=blue, status=ONGOING)
    s.msg(4, 2, "Oferta actual", reply=True)
    s.msg(2, 3, "freshness_check → hp_evidence", color=blue, status=ONGOING)
    s.msg(
        2,
        1,
        "Si cambió: 409 checkout_terms_changed, sin firma (S04)",
        reply=True,
        color=FAILURE_RED,
    )
    s.note(
        2,
        2,
        "Si sigue vigente: firma AP2",
        "Mandato de checkout y de pago",
        DONE,
        border=MERCHANT_AMBER,
        height=74,
    )
    s.msg(2, 1, "Aprobación registrada", reply=True)

    s.phase("5 · PAGO Y PEDIDO · sin cambios frente a B0")
    s.msg(1, 2, "POST /purchases/{id}/complete")
    s.msg(2, 5, "POST /agentic_commerce/delegate_payment · mandato de pago")
    s.msg(5, 2, "Token acotado", reply=True)
    s.msg(2, 4, "Completar · mandato de compra + token")
    s.msg(4, 5, "POST /payments/confirm")
    s.msg(5, 4, "Pago aprobado", reply=True)
    s.msg(4, 2, "Pedido + recibo AP2", reply=True)
    s.msg(2, 1, "Pedido confirmado", reply=True)
    s.note(
        2,
        5,
        "Futuro",
        "D2-HP completo (challenge, identidad, POL-03..06) · cadena de evidencia hasta los "
        "recibos · recuperación D17 con lookup",
        FUTURE,
        height=74,
    )
    end = s.y + 40
    for _, x, _ in SEQ_LANES:
        # Lifelines go first in the model so notes and labels stay on top of them.
        p.edge((x, 200), (x, end), dashed=True, arrow=False, width=1.5, color="#AAAAAA")
        p.root.insert(2, p.root[-1])
    p.status_legend(40, end + 40)
    model = p.diagram.find("mxGraphModel")
    assert model is not None
    model.set("pageHeight", str(end + 150))
    return p


# ---------------------------------------------------------------------------
# 5. HNP experiment
# ---------------------------------------------------------------------------


def hnp_view() -> Sheet:
    width, height = 2400, 1180
    p = Sheet("HNP · experimento", width, height)
    p.heading(
        "HNP · experimento offline: comprador, validador y backend",
        "Brazo A sin validador; brazo B con validador. El backend siempre decide, así que "
        "B admite un subconjunto de lo que admite A (B ⊆ A).",
        width=width,
    )
    p.boundary(
        40,
        130,
        2320,
        900,
        "evaluation/ · fuera del producto: sin checkout, pago ni comercio",
        OUTSIDE_GRAY,
    )
    top, box_h = 260, 200
    mid = top + box_h // 2
    p.card(
        80,
        top,
        280,
        box_h,
        "Corpus HNP",
        "27 casos; HNP-02 usa los 19 de negocio. Oráculo separado",
        kind="evaluation/data/hnp",
        border=OUTSIDE_GRAY,
        name_size=21,
    )
    p.card(
        440,
        top,
        300,
        box_h,
        "Comprador",
        "Arma la propuesta de compra del caso",
        kind="guionado · HNP-01 y HNP-02 · hecho",
        name_size=22,
    )
    p.card(
        440,
        top + 400,
        300,
        130,
        "Comprador LLM",
        "Fase 2: preferencias y texto inyectado",
        kind="futuro",
        status=FUTURE,
        name_size=20,
    )
    p.card(
        820,
        top,
        300,
        box_h,
        "Propuesta",
        "BuyerDraft + sobre con digest; sin campos de autoridad",
        kind="ProposalEnvelope",
        name_size=22,
    )
    val_x, val_y, val_w, val_h = 1250, top + 330, 360, 200
    p.card(
        val_x,
        val_y,
        val_w,
        val_h,
        "Validador",
        "Solo ve hechos públicos. Puede bloquear, nunca admitir",
        kind="HNP-01 guionado · HNP-02 gpt-5-mini · hecho",
        name_size=22,
    )
    p.card(
        val_x,
        val_y + val_h + 70,
        val_w,
        110,
        "OpenAI",
        "Responses API con tope de costo",
        kind="Sistema externo · solo HNP-02",
        border=EXTERNAL_WINE,
        name_size=20,
    )
    ctl_x, ctl_y, ctl_w, ctl_h = 1680, top - 40, 300, 280
    co_x, co_w = 2080, 250
    p.card(
        ctl_x,
        ctl_y,
        ctl_w,
        ctl_h,
        "Controles del backend",
        "11 checks deterministas: selección, comercio, presupuesto, oferta, consentimiento, "
        "vigencia, revocación, uso, restricciones, alcance y vínculo",
        kind="siempre deciden · hecho",
        name_size=22,
        description_size=16,
    )
    p.card(
        co_x, ctl_y, co_w, 120, "Coordinador", "Escribe evidencia y emite un permiso", name_size=20
    )
    p.card(
        co_x,
        ctl_y + 170,
        co_w,
        150,
        "Sumidero ficticio",
        "En memoria; registra el efecto simulado y rechaza duplicados",
        border=OUTSIDE_GRAY,
        name_size=20,
        description_size=16,
    )
    p.card(
        ctl_x,
        val_y + 90,
        co_x + co_w - ctl_x,
        110,
        "Scoring",
        "Escapes, bloqueos falsos, capturas únicas, McNemar A/B, costo y latencia",
        border=OUTSIDE_GRAY,
        name_size=20,
    )

    p.link((360, mid), (440, mid))
    p.label(352, mid - 70, 96, "Entrega", "caso", height=60)
    p.link((740, mid), (820, mid))
    p.label(726, mid - 46, 108, "Propone", height=36)
    p.link((590, top + 400), (590, top + box_h), color=FUTURE_GRAY)
    p.label(604, top + 260, 240, "Reemplazará al guionado", height=40, align="left", color=MUTED)

    # Arm A goes straight to the backend; arm B goes through the validator.
    arm_a_y = mid - 20
    p.link((1120, arm_a_y), (ctl_x, arm_a_y), dashed=False, width=2.4)
    p.label(
        1150, arm_a_y - 70, 520, "Brazo A · sin validador", "payload directo al backend", height=62
    )
    p.link(
        (970, top + box_h),
        (val_x, val_y + 100),
        points=((970, val_y + 100),),
        dashed=False,
        width=2.4,
    )
    p.label(990, val_y + 34, 250, "Brazo B · pide revisión", height=60, align="left")
    back_x = ctl_x + ctl_w // 2
    p.link(
        (val_x + val_w, val_y + 60),
        (back_x, ctl_y + ctl_h),
        points=((back_x, val_y + 60),),
        dashed=False,
        width=2.4,
    )
    p.label(
        val_x + val_w + 10,
        val_y - 6,
        back_x - val_x - val_w - 20,
        "accept / reject / abstain",
        height=60,
        align="left",
    )
    p.link((val_x + val_w // 2, val_y + val_h), (val_x + val_w // 2, val_y + val_h + 70))
    p.label(
        val_x + val_w // 2 + 14,
        val_y + val_h + 4,
        220,
        "Consulta",
        "HNP-02",
        height=60,
        align="left",
    )

    co_mid = co_x + co_w // 2
    p.link((ctl_x + ctl_w, ctl_y + 60), (co_x, ctl_y + 60))
    p.label(ctl_x + ctl_w + 6, ctl_y + 8, co_x - ctl_x - ctl_w - 12, "Admite", height=44)
    p.link((co_mid, ctl_y + 120), (co_mid, ctl_y + 170))
    p.label(co_mid + 14, ctl_y + 124, 110, "Emite", height=40, align="left")
    p.link((co_mid, ctl_y + 320), (co_mid, val_y + 90))
    p.label(co_mid + 14, ctl_y + 350, 120, "Resultados", height=40, align="left")

    p.text(
        "Un rechazo del validador bloquea el episodio; su accept no salta ningún control del "
        "backend.",
        1250,
        170,
        1080,
        36,
        size=18,
        color=MUTED,
        align="left",
    )
    p.status_legend(40, 1070)
    p.color_legend(
        "Azul: roles del agente simulados · Gris: infraestructura del experimento · Vino: "
        f"sistema externo. HNP-01: 27 casos {TIMES} A/B. HNP-02 fase 1: 19 {TIMES} 5 {TIMES} A/B.",
        1130,
        1072,
        1250,
    )
    return p


# ---------------------------------------------------------------------------
# 6. Pilot design
# ---------------------------------------------------------------------------

SCENARIOS = (
    ("S01", "Compra válida", "Oráculo 5/5 · D1 + continuación"),
    ("S02", "Presupuesto incompatible", "200 sin 5xx; 0 checkouts · recuperación"),
    ("S03", "Solicitud insuficiente", "Oráculo 5/5 · sin mecanismo B2"),
    ("S04", "Precio cambia tras mostrar", "409 checkout_terms_changed 5/5 · frescura"),
    ("S05", "Instrucción maliciosa en catálogo", "Oráculo 5/5 · D1 + continuación"),
)


def pilot_view() -> Sheet:
    width, height = 2400, 1300
    p = Sheet("Diseño del piloto", width, height)
    p.heading(
        "Diseño experimental: piloto HP pareado y HNP-02",
        "Resultados descriptivos (5 repeticiones), sin inferencia estadística en el piloto HP.",
        width=width,
    )
    # Left panel: HP pilot.
    p.boundary(40, 140, 1440, 1000, "Piloto HP · B0' vs B2 · en curso", AGENT_BLUE)
    p.text(
        "Mismo commit y mismo día · gpt-5-mini · mismo prompt, catálogo, comercio y PSP. "
        "GET /health verifica la condición antes de cada repetición; si no coincide, aborta.",
        80,
        200,
        1360,
        60,
        size=18,
        color=MUTED,
        align="left",
    )
    p.card(
        80,
        280,
        640,
        96,
        "B0'",
        "AGENT_GOVERNANCE_MODE=off",
        kind="comparación primaria",
        name_size=22,
    )
    p.card(
        760,
        280,
        680,
        96,
        "B2",
        "enforce · hp_flow=governed · claves D1 por corrida",
        kind="tratamiento",
        name_size=22,
    )

    grid_x, grid_y = 80, 420
    name_w, cell_w, cell_h, cell_gap = 420, 184, 80, 14
    p.text("Escenario", grid_x, grid_y, name_w, 34, size=18, bold=True, align="left")
    for rep in range(5):
        p.text(
            f"Rep. {rep + 1}",
            grid_x + name_w + rep * cell_w,
            grid_y,
            cell_w,
            34,
            size=18,
            bold=True,
        )
    for row, (code, name, criterion) in enumerate(SCENARIOS):
        y = grid_y + 44 + row * (cell_h + cell_gap)
        p.card(
            grid_x,
            y,
            name_w - 20,
            cell_h,
            f"{code} · {name}",
            criterion,
            name_size=18,
            description_size=15,
            align="left",
            extra="spacingLeft=14;",
        )
        for rep in range(5):
            first, second = ("B0'", "B2") if rep % 2 == 0 else ("B2", "B0'")
            p.card(
                grid_x + name_w + rep * cell_w + 8,
                y,
                cell_w - 16,
                cell_h,
                f"{first} → {second}",
                stroke=1.6,
                name_size=17,
            )
    p.text(
        f"S01-S05 {TIMES} 5 repeticiones {TIMES} 2 condiciones = 50 corridas, pareadas por "
        "(escenario, repetición). Orden ilustrativo: la condición que va primero alterna.",
        80,
        grid_y + 44 + 5 * (cell_h + cell_gap) + 6,
        1360,
        56,
        size=17,
        color=MUTED,
        align="left",
    )
    p.text(
        "Evaluado como paquete, reportado por mecanismo: S02 → recuperación; S04 → frescura; "
        "nada se atribuye al PDP si lo produjo otro control.",
        80,
        grid_y + 44 + 5 * (cell_h + cell_gap) + 56,
        1360,
        56,
        size=17,
        color=MUTED,
        align="left",
    )

    p.text(
        "Plan B si el despacho en vivo falla: B2-lite (D1 en shadow, frescura y recuperación "
        "activas), declarado aparte y nunca reportado como enforce.",
        80,
        grid_y + 44 + 5 * (cell_h + cell_gap) + 106,
        1360,
        56,
        size=17,
        color=MUTED,
        align="left",
    )

    # Right panel: HNP-02.
    p.boundary(1520, 140, 840, 1000, "HNP-02 · validador LLM · fase 1 hecha", OUTSIDE_GRAY)
    p.text(
        "Offline, sin comercio. Pares barajados con HNP_SEED; tope de costo HNP_MAX_COST_USD.",
        1560,
        200,
        760,
        60,
        size=18,
        color=MUTED,
        align="left",
    )
    p.card(1560, 280, 360, 96, "Brazo A", "Payload + controles del backend", name_size=22)
    p.card(1960, 280, 360, 96, "Brazo B", "A + validador gpt-5-mini", name_size=22)
    factors = (
        ("19", "casos de negocio"),
        (f"{TIMES} 5", "repeticiones"),
        (f"{TIMES} 2", "brazos A/B"),
        ("= 190", "episodios"),
    )
    for index, (number, text) in enumerate(factors):
        x = 1560 + index * 192
        p.card(
            x,
            430,
            176,
            130,
            number,
            text,
            border=OUTSIDE_GRAY,
            name_size=30,
            description_size=16,
            stroke=2,
        )
    p.text(
        "Métricas por brazo: efectos, escapes y bloqueos falsos (Wilson 95 %). Validador: "
        "capturas, capturas únicas, rechazos falsos, abstenciones, salidas inválidas. "
        "McNemar exacto A vs B; costo y latencia.",
        1560,
        600,
        760,
        100,
        size=17,
        color=INK,
        align="left",
    )
    p.text(
        "Amenaza a la validez: el prompt replica las reglas del backend, así que se esperan "
        "0 capturas únicas con payloads guionados. La fase 2 (comprador LLM) es futura.",
        1560,
        710,
        760,
        90,
        size=17,
        color=MUTED,
        align="left",
    )
    p.card(
        1560,
        830,
        760,
        110,
        "Fase 2 · comprador LLM",
        "Preferencias en lenguaje natural e instrucciones inyectadas en el catálogo",
        kind="futuro",
        status=FUTURE,
        name_size=20,
    )
    p.status_legend(40, 1180)
    return p


# ---------------------------------------------------------------------------

DIAGRAMS = {
    "architecture-b0": lambda: container_view(governed=False),
    "architecture-b2-hp": lambda: container_view(governed=True),
    "b0-vs-b2-delta": delta_view,
    "purchase-sequence-governed": governed_sequence,
    "architecture-hnp-experiment": hnp_view,
    "pilot-design": pilot_view,
}


def drawio_binary() -> str:
    candidates = [
        os.environ.get("DRAWIO"),
        shutil.which("drawio"),
        shutil.which("draw.io"),
        "/Applications/draw.io.app/Contents/MacOS/draw.io",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("draw.io CLI not found; set DRAWIO to its path")


def export(names: list[str]) -> None:
    binary = drawio_binary()
    for name in names:
        source = OUT / f"{name}.drawio"
        for fmt, options in (
            ("svg", ["--theme", "light"]),
            ("png", ["--width", str(PNG_WIDTH)]),
        ):
            subprocess.run(
                [
                    binary,
                    "-x",
                    "-f",
                    fmt,
                    *options,
                    "-b",
                    "24",
                    "-o",
                    str(OUT / f"{name}.{fmt}"),
                    str(source),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", action="store_true", help="also export .svg and .png")
    parser.add_argument("names", nargs="*", help=f"subset to build: {', '.join(DIAGRAMS)}")
    args = parser.parse_args()
    names = args.names or list(DIAGRAMS)
    unknown = sorted(set(names) - set(DIAGRAMS))
    if unknown:
        parser.error(f"unknown diagrams: {', '.join(unknown)}")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names:
        write_one(name, DIAGRAMS[name]())
    if args.export:
        export(names)


if __name__ == "__main__":
    main()
