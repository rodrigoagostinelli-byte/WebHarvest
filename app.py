# -*- coding: utf-8 -*-
"""Xplorador Ecommerce: extractor multi-ecommerce en memoria con Streamlit."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from io import BytesIO
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import pandas as pd
import streamlit as st
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURACIÓN GENERAL
# ============================================================

st.set_page_config(
    page_title="Xplorador Ecommerce",
    page_icon="🛍️",
    layout="wide",
)

DATALAYER_EVENTS = {
    "view_item_list_custom",
    "view_item_list",
    "view_item",
}

OUTPUT_COLUMNS = [
    "sitio",
    "sku",
    "producto",
    "marca",
    "variante",
    "precio",
    "pagina",
    "fuente_dato",
    "url_producto",
    "url_pagina",
]


# ============================================================
# LIMPIEZA Y NORMALIZACIÓN
# ============================================================

def first_value(obj, keys):
    """Devuelve el primer valor no vacío encontrado."""

    if not isinstance(obj, dict):
        return None

    for key in keys:
        value = obj.get(key)

        if value not in (None, "", [], {}):
            return value

    lower_map = {
        str(key).lower(): value
        for key, value in obj.items()
    }

    for key in keys:
        value = lower_map.get(key.lower())

        if value not in (None, "", [], {}):
            return value

    return None


def clean_text(value):
    """Convierte un valor en texto limpio."""

    if value is None:
        return None

    if isinstance(value, dict):
        value = first_value(
            value,
            [
                "name",
                "value",
                "label",
                "title",
                "url",
                "content",
            ],
        )

    if isinstance(value, list):
        value = value[0] if value else None

        if isinstance(value, dict):
            value = first_value(
                value,
                [
                    "name",
                    "value",
                    "label",
                    "title",
                    "url",
                    "content",
                ],
            )

    if value is None:
        return None

    text = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()

    return text or None


def normalize_name(value):
    """Normaliza un nombre para comparar y deduplicar."""

    value = clean_text(value)

    if not value:
        return None

    value = unicodedata.normalize(
        "NFKD",
        value,
    )

    value = "".join(
        character
        for character in value
        if not unicodedata.combining(character)
    )

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value or None


def clean_price(value):
    """
    Convierte precios como:

    459999
    459999.0
    $459.999
    $459.999,50
    ARS 459.999
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, dict):
        value = first_value(
            value,
            [
                "price",
                "sellingPrice",
                "salePrice",
                "spotPrice",
                "lowPrice",
                "value",
                "amount",
                "Price",
            ],
        )

    if isinstance(value, list):
        value = value[0] if value else None

    if value is None:
        return None

    if isinstance(value, (int, float)):
        number = float(value)

        if math.isfinite(number):
            return number

        return None

    text = str(value).strip()

    # Si el texto tiene varios precios, toma el último.
    matches = re.findall(
        r"\$\s*[\d.]+(?:,\d{1,2})?",
        text,
    )

    if matches:
        text = matches[-1]

    text = re.sub(
        r"[^\d,.\-]",
        "",
        text,
    )

    if not text:
        return None

    # Ejemplo: 1.299.999,50
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = (
                text
                .replace(".", "")
                .replace(",", ".")
            )
        else:
            text = text.replace(",", "")

    # Ejemplo: 1299999,50
    elif "," in text:
        decimal_part = text.rsplit(",", 1)[-1]

        if len(decimal_part) in (1, 2):
            text = (
                text
                .replace(".", "")
                .replace(",", ".")
            )
        else:
            text = text.replace(",", "")

    # Ejemplo: 1.299.999
    elif text.count(".") > 1:
        text = text.replace(".", "")

    # Ejemplo argentino: 459.999
    elif (
        text.count(".") == 1
        and len(text.rsplit(".", 1)[-1]) == 3
    ):
        text = text.replace(".", "")

    try:
        number = float(text)

        if math.isfinite(number):
            return number

        return None

    except ValueError:
        return None


def get_sku_from_url(url):
    """Intenta recuperar un SKU desde la URL del producto."""

    if not url:
        return None

    patterns = [
        r"/p/(\d+)(?:[/?#]|$)",
        r"/producto/[^?#]*/(\d+)(?:[/?#]|$)",
        r"[?&](?:sku|productId|product_id)=(\d+)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            url,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(1)

    return None


# ============================================================
# PAGINACIÓN
# ============================================================

def build_page_url(base_url, page_number):
    """
    Añade o reemplaza page=N sin eliminar filtros.

    Conserva parámetros repetidos, por ejemplo:

    promo-cuotas=18-20-cuotas
    promo-cuotas=14-15-18-cuotas
    """

    parts = urlsplit(base_url)

    query_items = parse_qsl(
        parts.query,
        keep_blank_values=True,
    )

    pagination_names = {
        "page",
        "pagina",
  "pagenumber",
    }

    query_items = [
        (key, value)
        for key, value in query_items
        if key.lower() not in pagination_names
    ]

    query_items.append(
        ("page", str(page_number))
    )

    new_query = urlencode(
        query_items,
        doseq=True,
    )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            new_query,
            parts.fragment,
        )
    )


def should_paginate(url, mode):
    """Decide si un sitio debe paginarse."""

    if mode == "Forzar paginación":
        return True

    if mode == "Solo URL exacta":
        return False

    domain = urlsplit(url).netloc.lower()

    # Estos sitios funcionan habitualmente con page=N.
    automatic_domains = (
        "fravega.com",
        "oncity.com",
    )

    return any(
        domain_name in domain
        for domain_name in automatic_domains
    )


# ============================================================
# CLAVE DE DEDUPLICACIÓN
# ============================================================

def product_key(row):
    """
    Prioridad para deduplicar:

    1. URL del producto.
    2. Nombre normalizado.
    3. SKU.
    """

    site = row.get("sitio")

    product_url = clean_text(
        row.get("url_producto")
    )

    if product_url:
        path = (
            urlsplit(product_url)
            .path
            .rstrip("/")
            .lower()
        )

        if path:
            return (
                "url",
                site,
                path,
            )

    product_name = normalize_name(
        row.get("producto")
    )

    if product_name:
        return (
            "name",
            site,
            product_name,
        )

    sku = clean_text(
        row.get("sku")
    )

    if sku:
        return (
            "sku",
            site,
            sku.lower(),
        )

    return None


# ============================================================
# EXTRACCIÓN DEL DATALAYER
# ============================================================

def extract_datalayer(datalayer, base_url):
    """
    Extrae productos exclusivamente desde eventos ecommerce.

    Prioridad:

    1. view_item_list_custom
    2. view_item_list
    3. view_item

    Esto evita recorrer objetos de interfaz como breadcrumbs,
    filtros, galerías o configuraciones internas.
    """

    if not isinstance(datalayer, list):
        return []

    available_events = {
        event.get("event")
        for event in datalayer
        if isinstance(event, dict)
    }

    if "view_item_list_custom" in available_events:
        selected_events = {
            "view_item_list_custom"
        }

    elif "view_item_list" in available_events:
        selected_events = {
            "view_item_list"
        }

    else:
        selected_events = {
            "view_item"
        }

    rows = []

    for event in datalayer:
        if not isinstance(event, dict):
            continue

        event_name = event.get("event")

        if event_name not in selected_events:
            continue

        ecommerce = event.get("ecommerce")

        if not isinstance(ecommerce, dict):
            continue

        items = ecommerce.get("items", [])

        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            name = clean_text(
                first_value(
                    item,
                    [
                        "item_name",
                        "productName",
                        "product_name",
                        "name",
                        "title",
                    ],
                )
            )

            if not name:
                continue

            raw_url = clean_text(
                first_value(
                    item,
                    [
                        "item_url",
                        "itemUrl",
                        "product_url",
                        "productUrl",
                        "url",
                        "link",
                        "href",
                    ],
                )
            )

            row = {
                "sku": clean_text(
                    first_value(
                        item,
                        [
                            "item_id",
                            "itemId",
                            "sku",
                            "skuId",
                            "productId",
                            "product_id",
                            "id",
                        ],
                    )
                ),
                "producto": name,
                "marca": clean_text(
                    first_value(
                        item,
                        [
                            "item_brand",
                            "brand",
                            "brandName",
                            "manufacturer",
                            "vendor",
                        ],
                    )
                ),
                "variante": clean_text(
                    first_value(
                        item,
                        [
                            "item_variant",
                            "variant",
                            "variantId",
                            "variant_id",
                        ],
                    )
                ),
                "precio": clean_price(
                    first_value(
                        item,
                        [
                            "price",
                            "item_price",
                            "sellingPrice",
                            "salePrice",
                            "spotPrice",
                        ],
                    )
                ),
                "url_producto": (
                    urljoin(base_url, raw_url)
                    if raw_url
                    else None
                ),
                "imagen": clean_text(
                    first_value(
                        item,
                        [
                            "image",
                            "imageUrl",
                            "image_url",
                            "item_image",
                            "thumbnail",
                        ],
                    )
                ),
                "fuente_dato": (
                    f"datalayer:{event_name}"
                ),
            }

            rows.append(row)

    # Deduplicación dentro del DataLayer.
    unique_rows = []
    seen = set()

    for row in rows:
        key = (
            normalize_name(row.get("producto"))
            or row.get("sku")
        )

        if not key:
            continue

        if key in seen:
            continue

        seen.add(key)
        unique_rows.append(row)

    return unique_rows


# ============================================================
# EXTRACCIÓN JSON-LD
# ============================================================

def walk_jsonld(obj, rows, base_url):
    """Recorre JSON-LD buscando objetos de tipo Product."""

    if isinstance(obj, list):
        for item in obj:
            walk_jsonld(
                item,
                rows,
                base_url,
            )

        return

    if not isinstance(obj, dict):
        return

    object_type = obj.get("@type", [])

    if isinstance(object_type, list):
        object_types = object_type
    else:
        object_types = [object_type]

    if "Product" in object_types:
        offers = obj.get(
            "offers",
            {},
        )

        if isinstance(offers, list):
            offers = (
                offers[0]
                if offers
                else {}
            )

        brand = obj.get("brand")

        if isinstance(brand, dict):
            brand = brand.get("name")

        raw_url = clean_text(
            obj.get("url")
            or obj.get("@id")
        )

        name = clean_text(
            obj.get("name")
        )

        image = obj.get("image")

        if isinstance(image, list):
            image = image[0] if image else None

        if isinstance(image, dict):
            image = (
                image.get("url")
                or image.get("contentUrl")
            )

        if name:
            rows.append(
                {
                    "sku": clean_text(
                        obj.get("sku")
                        or obj.get("mpn")
                        or obj.get("productID")
                        or get_sku_from_url(raw_url)
                    ),
                    "producto": name,
                    "marca": clean_text(brand),
                    "variante": None,
                    "precio": clean_price(
                        offers.get("price")
                        if isinstance(offers, dict)
                        else None
                    ),
                    "url_producto": (
                        urljoin(base_url, raw_url)
                        if raw_url
                        else None
                    ),
                    "imagen": (
                        urljoin(
                            base_url,
                            clean_text(image),
                        )
                        if image
                        else None
                    ),
                    "fuente_dato": "json-ld",
                }
            )

    for value in obj.values():
        if isinstance(
            value,
            (dict, list),
        ):
            walk_jsonld(
                value,
                rows,
                base_url,
            )


# ============================================================
# EXTRACCIÓN DE TARJETAS HTML
# ============================================================

async def extract_cards(page):
    """Extrae tarjetas del catálogo visibles en el DOM."""

    raw_rows = await page.evaluate(
        r"""
        () => {
            const output = [];
            const seen = new Set();

            const clean = value => {
                if (!value) return null;

                const result = String(value)
                    .replace(/\s+/g, " ")
                    .trim();

                return result || null;
            };

            const textOf = node => {
                if (!node) return null;

                return clean(
                    node.innerText ||
                    node.textContent ||
                    ""
                );
            };

            const links = [
                ...document.querySelectorAll(
                    "a[href]"
                )
            ].filter(link => {
                const href = link.href || "";

                return (
                    /\/p\//i.test(href) ||
                    /\/producto\//i.test(href) ||
                    /\/product\//i.test(href)
                );
            });

            const attributeFromTree = (
                root,
                names
            ) => {
                const nodes = [
                    root,
                    ...root.querySelectorAll("*")
                ];

                for (const node of nodes) {
                    for (const name of names) {
                        const value =
                            node.getAttribute(name);

                        if (value) {
                            return value;
                        }
                    }
                }

                return null;
            };

            for (const link of links) {
                let card = link.closest(
                    [
                        "article",
                        "li",
                        '[data-testid*="product"]',
                        '[data-test*="product"]',
                        '[class*="ProductCard"]',
                        '[class*="product-card"]',
                        '[class*="productCard"]',
                        '[class*="product-item"]'
                    ].join(",")
                );

                if (!card) {
                    let current = link;

                    for (
                        let level = 0;
                        level < 7 && current;
                        level += 1
                    ) {
                        const value =
                            textOf(current) || "";

                        if (
                            value.includes("$") &&
                            value.length < 2200
                        ) {
                            card = current;
                            break;
                        }

                        current =
                            current.parentElement;
                    }
                }

                if (!card) {
                    continue;
                }

                const cardText = textOf(card);

                if (
                    !cardText ||
                    !cardText.includes("$")
                ) {
                    continue;
                }

                const uniqueKey =
                    link.href || cardText;

                if (seen.has(uniqueKey)) {
                    continue;
                }

                seen.add(uniqueKey);

                const nameNode =
                    card.querySelector(
                        [
                            '[data-testid*="title"]',
                            '[data-testid*="name"]',
                            '[data-test*="title"]',
                            '[class*="ProductName"]',
                            '[class*="productName"]',
                            '[class*="product-name"]',
                            "h2",
                            "h3",
                            "h4"
                        ].join(",")
                    );

                const name =
                    textOf(nameNode) ||
                    link.getAttribute("title") ||
                    link.getAttribute("aria-label") ||
                    textOf(link);

                const prices =
                    cardText.match(
                        /\$\s*[\d.]+(?:,\d{1,2})?/g
                    ) || [];

                const imageNode =
                    card.querySelector("img");

                output.push({
                    sku: attributeFromTree(
                        card,
                        [
                            "data-sku",
                            "data-product-id",
                            "data-item-id",
                            "data-id"
                        ]
                    ),
                    producto: name,
                    marca: attributeFromTree(
                        card,
                        [
                            "data-brand",
                            "data-product-brand"
                        ]
                    ),
                    variante: attributeFromTree(
                        card,
                        [
                            "data-variant",
                            "data-variant-id"
                        ]
                    ),
                    precio:
                        prices.length
                            ? prices[
                                prices.length - 1
                            ]
                            : null,
                    url_producto: link.href,
                    imagen:
                        imageNode?.src ||
                        imageNode?.getAttribute(
                            "data-src"
                        ) ||
                        imageNode?.getAttribute(
                            "data-lazy-src"
                        ) ||
                        null
                });
            }

            return output;
        }
        """
    )

    rows = []

    for item in raw_rows:
        name = clean_text(
            item.get("producto")
        )

        if not name:
            continue

        if not 3 < len(name) < 500:
            continue

        product_url = clean_text(
            item.get("url_producto")
        )

        rows.append(
            {
                "sku": (
                    clean_text(
                        item.get("sku")
                    )
                    or get_sku_from_url(
                        product_url
                    )
                ),
                "producto": name,
                "marca": clean_text(
                    item.get("marca")
                ),
                "variante": clean_text(
                    item.get("variante")
                ),
                "precio": clean_price(
                    item.get("precio")
                ),
                "url_producto": product_url,
                "imagen": clean_text(
                    item.get("imagen")
                ),
                "fuente_dato":
                    "html:listing-card",
            }
        )

    return rows


# ============================================================
# COMBINACIÓN DE FUENTES
# ============================================================

def combine_sources(
    dom_rows,
    datalayer_rows,
    jsonld_rows,
):
    """
    Jerarquía:

    1. Si existe DOM, determina qué productos forman parte
       del catálogo.
    2. DataLayer y JSON-LD completan campos.
    3. Sin DOM, DataLayer es el fallback principal.
    4. JSON-LD es el último fallback.
    """

    supplemental = {}

    for row in (
        datalayer_rows + jsonld_rows
    ):
        key = normalize_name(
            row.get("producto")
        )

        if not key:
            continue

        quality = sum(
            row.get(column) is not None
            for column in [
                "sku",
                "marca",
                "variante",
                "precio",
                "url_producto",
            ]
        )

        current = supplemental.get(key)

        if (
            current is None
            or quality > current["_quality"]
        ):
            supplemental[key] = {
                **row,
                "_quality": quality,
            }

    if dom_rows:
        final_rows = []

        for row in dom_rows:
            key = normalize_name(
                row.get("producto")
            )

            additional = supplemental.get(
                key,
                {},
            )

            combined = {}

            for column in [
                "sku",
                "producto",
                "marca",
                "variante",
                "precio",
                "url_producto",
                "imagen",
                "fuente_dato",
            ]:
                if row.get(column) is not None:
                    combined[column] = row.get(
                        column
                    )
                else:
                    combined[column] = (
                        additional.get(column)
                    )

            final_rows.append(combined)

        return final_rows

    if datalayer_rows:
        return datalayer_rows

    return jsonld_rows


# ============================================================
# EXTRACCIÓN DE UNA PÁGINA
# ============================================================

async def extract_page(
    context,
    base_url,
    page_number,
    paginate,
    max_scrolls,
    timeout_ms,
):
    """Extrae una página y devuelve productos y diagnóstico."""

    if paginate:
        requested_url = build_page_url(
            base_url,
            page_number,
        )
    else:
        requested_url = base_url

    page = await context.new_page()

    errors = []

    try:
        try:
            await page.goto(
                requested_url,
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )

        except PlaywrightTimeoutError:
            errors.append(
                "Timeout parcial de navegación"
            )

        # Tiempo inicial para GTM, React, VTEX y APIs.
        await page.wait_for_timeout(2500)

        # Scroll progresivo para lazy loading.
        for _ in range(max_scrolls):
            await page.evaluate(
                """
                window.scrollTo(
                    0,
                    document.body.scrollHeight
                )
                """
            )

            await page.wait_for_timeout(700)

        # HTML
        try:
            dom_rows = await extract_cards(page)

        except Exception as error:
            dom_rows = []

            errors.append(
                "DOM: "
                f"{type(error).__name__}: "
                f"{error}"
            )

        # JSON-LD
        jsonld_rows = []

        try:
            blocks = await page.locator(
                'script[type="application/ld+json"]'
            ).all_text_contents()

            for block in blocks:
                try:
                    payload = json.loads(block)

                    walk_jsonld(
                        payload,
                        jsonld_rows,
                        page.url,
                    )

                except Exception:
                    pass

        except Exception as error:
            errors.append(
                "JSON-LD: "
                f"{type(error).__name__}: "
                f"{error}"
            )

        # DataLayer
        try:
            datalayer = await page.evaluate(
                """
                () => {
                    const source =
                        window.dataLayer || [];

                    return source
                        .map(value => {
                            try {
                                return JSON.parse(
                                    JSON.stringify(
                                        value,
                                        (
                                            key,
                                            item
                                        ) => {
                                            if (
                                                typeof item ===
                                                "function"
                                            ) {
                                                return undefined;
                                            }

                                            return item;
                                        }
                                    )
                                );
                            } catch (_) {
                                return null;
                            }
                        })
                        .filter(Boolean);
                }
                """
            )

            datalayer_rows = extract_datalayer(
                datalayer,
                page.url,
            )

        except Exception as error:
            datalayer_rows = []

            errors.append(
                "DataLayer: "
                f"{type(error).__name__}: "
                f"{error}"
            )

        final_rows = combine_sources(
            dom_rows=dom_rows,
            datalayer_rows=datalayer_rows,
            jsonld_rows=jsonld_rows,
        )

        summary = {
            "pagina": page_number,
            "DOM": len(dom_rows),
            "JSON-LD": len(jsonld_rows),
            "DataLayer": len(
                datalayer_rows
            ),
            "finales": len(final_rows),
            "url_real": page.url,
        }

        return (
            final_rows,
            summary,
            errors,
        )

    finally:
        await page.close()


# ============================================================
# EXTRACCIÓN COMPLETA
# ============================================================

async def scrape_all(
    urls,
    max_pages,
    pagination_mode,
    max_scrolls,
    timeout_ms,
    progress,
):
    """Procesa todas las URLs utilizando un único navegador."""

    all_rows = []
    summaries = []
    errors = []

    total_steps = sum(
        (
            max_pages
            if should_paginate(
                url,
                pagination_mode,
            )
            else 1
        )
        for url in urls
    )

    current_step = 0

    async with async_playwright() as playwright:
        if not os.path.exists(
            playwright.chromium.executable_path
        ):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "playwright",
                    "install",
                    "chromium",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="es-AR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 "
                "Safari/537.36"
            ),
        )

        try:
            for base_url in urls:
                paginate = should_paginate(
                    base_url,
                    pagination_mode,
                )

                if paginate:
                    page_numbers = range(
                        1,
                        max_pages + 1,
                    )
                else:
                    page_numbers = [1]

                empty_pages = 0
                repeated_pages = 0
                site_seen = set()

                for page_number in page_numbers:
                    progress(
                        (
                            current_step
                            / max(total_steps, 1)
                        ),
                        (
                            "Analizando "
                            f"{urlsplit(base_url).netloc}"
                            f" · página {page_number}"
                        ),
                    )

                    (
                        rows,
                        summary,
                        page_errors,
                    ) = await extract_page(
                        context=context,
                        base_url=base_url,
                        page_number=page_number,
                        paginate=paginate,
                        max_scrolls=max_scrolls,
                        timeout_ms=timeout_ms,
                    )

                    site = (
                        urlsplit(base_url)
                        .netloc
                        .lower()
                        .removeprefix("www.")
                    )

                    new_rows = []

                    for row in rows:
                        row.update(
                            {
                                "sitio": site,
                                "pagina": page_number,
                                "url_pagina":
                                    summary["url_real"],
                            }
                        )

                        key = product_key(row)

                        if key is None:
                            continue

                        if key in site_seen:
                            continue

                        site_seen.add(key)
                        new_rows.append(row)

                    all_rows.extend(new_rows)

                    summary.update(
                        {
                            "sitio": site,
                            "nuevos": len(new_rows),
                            "acumulados_sitio":
                                len(site_seen),
                        }
                    )

                    summaries.append(summary)

                    errors.extend(
                        [
                            (
                                f"{summary['url_real']}"
                                f" — {error}"
                            )
                            for error in page_errors
                        ]
                    )

                    current_step += 1

                    if not rows:
                        empty_pages += 1
                    else:
                        empty_pages = 0

                    if rows and not new_rows:
                        repeated_pages += 1
                    else:
                        repeated_pages = 0

                    if paginate and (
                        empty_pages >= 2
                        or repeated_pages >= 2
                    ):
                        break

        finally:
            await context.close()
            await browser.close()

    progress(
        1.0,
        "Extracción finalizada",
    )

    return (
        prepare_dataframe(all_rows),
        pd.DataFrame(summaries),
        errors,
    )


# ============================================================
# PREPARACIÓN DEL DATAFRAME
# ============================================================

def prepare_dataframe(rows):
    """Normaliza y deduplica el resultado final."""

    columns = OUTPUT_COLUMNS + [
        "imagen"
    ]

    if not rows:
        return pd.DataFrame(
            columns=columns
        )

    df = pd.DataFrame(rows)

    for column in columns:
        if column not in df.columns:
            df[column] = None

    df["precio"] = pd.to_numeric(
        df["precio"],
        errors="coerce",
    )

    df = df[
        df["producto"].notna()
    ].copy()

    df["_key"] = df.apply(
        product_key,
        axis=1,
    )

    df["_quality"] = (
        df[
            [
                "sku",
                "marca",
                "variante",
                "precio",
                "url_producto",
            ]
        ]
        .notna()
        .sum(axis=1)
    )

    df["_priority"] = (
        df["fuente_dato"]
        .astype(str)
        .str.startswith("datalayer")
        .astype(int)
    )

    df = df.sort_values(
        [
            "_quality",
            "_priority",
        ],
        ascending=[
            False,
            False,
        ],
    )

    df = df.drop_duplicates(
        subset=["_key"],
        keep="first",
    )

    df = df.drop(
        columns=[
            "_key",
            "_quality",
            "_priority",
        ]
    )

    return df.reset_index(drop=True)


def run_scrape(
    urls,
    max_pages,
    pagination_mode,
    max_scrolls,
    timeout_ms,
    progress,
):
    """Ejecuta el proceso async desde Streamlit."""

    return asyncio.run(
        scrape_all(
            urls=urls,
            max_pages=max_pages,
            pagination_mode=pagination_mode,
            max_scrolls=max_scrolls,
            timeout_ms=timeout_ms,
            progress=progress,
        )
    )


# ============================================================
# EXPORTACIÓN
# ============================================================

def csv_bytes(df):
    return df.to_csv(
        index=False
    ).encode("utf-8-sig")


def excel_bytes(
    df,
    diagnostics,
):
    buffer = BytesIO()

    with pd.ExcelWriter(
        buffer,
        engine="openpyxl",
    ) as writer:
        df.to_excel(
            writer,
            index=False,
            sheet_name="Productos",
        )

        diagnostics.to_excel(
            writer,
            index=False,
            sheet_name="Diagnostico",
        )

    return buffer.getvalue()


# ============================================================
# INTERFAZ
# ============================================================

st.markdown(
    """
    <style>
    :root {
        --accent: #6C5CE7;
    }

    .block-container {
        padding-top: 1.7rem;
        padding-bottom: 3rem;
        max-width: 1500px;
    }

    .hero {
        padding: 1.5rem 1.7rem;
        border-radius: 22px;
        background:
            linear-gradient(
                120deg,
                #17172b 0%,
                #312e81 55%,
                #6c5ce7 100%
            );
        color: white;
        margin-bottom: 1.2rem;
    }

    .hero h1 {
        font-size: 2.2rem;
        margin: 0 0 .4rem 0;
        letter-spacing: -.04em;
    }

    .hero p {
        color: #e5e7eb;
        margin: 0;
        font-size: 1rem;
    }

    div[data-testid="stMetric"] {
        border:
            1px solid
            rgba(128,128,128,.2);
        padding: 1rem;
        border-radius: 16px;
        background:
            rgba(128,128,128,.04);
    }

    .stButton button,
    .stDownloadButton button {
        border-radius: 10px;
        font-weight: 600;
    }
    </style>

    <div class="hero">
        <h1>🛍️ Xplorador Ecommerce</h1>

        <p>
            Extraé SKU, producto, marca, variante y precio
            desde HTML, DataLayer y JSON-LD.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)


with st.sidebar:
    st.header("⚙️ Configuración")

    urls_text = st.text_area(
        "URLs de listados (una por línea)",
        value=(
            "https://www.cetrogar.com.ar/s"
            "?fuzzy=0"
            "&operator=and"
            "&promo-cuotas=18-20-cuotas"
            "&promo-cuotas=14-15-18-cuotas"
            "&facets=fuzzy%2Coperator%2Cpromo-cuotas"
            "&sort=score_desc"
            "&page=1"
            "\n"
            "https://www.fravega.com/l/"
            "?promociones=ofertas-semanales"
        ),
        height=180,
        help=(
            "Usá URLs de categorías, búsquedas u ofertas. "
            "Evitá usar solamente la página principal."
        ),
    )

    pagination_mode = st.selectbox(
        "Paginación",
        [
            "Automática",
            "Forzar paginación",
            "Solo URL exacta",
        ],
        help=(
            "Automática pagina Frávega y On City. "
            "Para Cetrogar se recomienda URL exacta."
        ),
    )

    max_pages = st.slider(
        "Máximo de páginas",
        min_value=1,
        max_value=50,
        value=5,
    )

    max_scrolls = st.slider(
        "Scrolls por página",
        min_value=0,
        max_value=12,
        value=5,
    )

    timeout_seconds = st.slider(
        "Timeout por página",
        min_value=20,
        max_value=120,
        value=60,
        step=10,
    )

    start = st.button(
        "🚀 Ejecutar extracción",
        type="primary",
        use_container_width=True,
    )

    st.caption(
        "Los resultados permanecen solamente durante "
        "la sesión de Streamlit."
    )


# ============================================================
# ESTADO DE SESIÓN
# ============================================================

if "products_df" not in st.session_state:
    st.session_state.products_df = (
        pd.DataFrame()
    )

if "diagnostics_df" not in st.session_state:
    st.session_state.diagnostics_df = (
        pd.DataFrame()
    )

if "scrape_errors" not in st.session_state:
    st.session_state.scrape_errors = []


# ============================================================
# EJECUTAR EXTRACCIÓN
# ============================================================

if start:
    urls = []

    for raw_url in urls_text.splitlines():
        raw_url = raw_url.strip()

        if not raw_url:
            continue

        if not raw_url.startswith(
            (
                "http://",
                "https://",
            )
        ):
            raw_url = (
                "https://"
                + raw_url
            )

        parsed = urlsplit(raw_url)

        if parsed.netloc:
            urls.append(raw_url)

    if not urls:
        st.error(
            "Ingresá al menos una URL válida."
        )

    else:
        progress = st.progress(
            0,
            text="Preparando Chromium…",
        )

        def update_progress(
            value,
            message,
        ):
            progress.progress(
                min(
                    1.0,
                    max(0.0, value),
                ),
                text=message,
            )

        with st.spinner(
            "Visitando los sitios y recopilando productos…"
        ):
            try:
                (
                    products_df,
                    diagnostics_df,
                    errors,
                ) = run_scrape(
                    urls=urls,
                    max_pages=max_pages,
                    pagination_mode=
                        pagination_mode,
                    max_scrolls=max_scrolls,
                    timeout_ms=
                        timeout_seconds * 1000,
                    progress=update_progress,
                )

                st.session_state.products_df = (
                    products_df
                )

                st.session_state.diagnostics_df = (
                    diagnostics_df
                )

                st.session_state.scrape_errors = (
                    errors
                )

            except Exception as error:
                st.error(
                    "No se pudo completar la extracción: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

        progress.empty()

        if not (
            st.session_state
            .products_df
            .empty
        ):
            st.success(
                "Extracción terminada: "
                f"{len(st.session_state.products_df):,} "
                "productos únicos."
            )

        else:
            st.warning(
                "No se encontraron productos. "
                "Revisá el diagnóstico y probá una URL "
                "de categoría, búsqueda u ofertas."
            )


# ============================================================
# RESULTADOS
# ============================================================

df = (
    st.session_state
    .products_df
    .copy()
)

if df.empty:
    st.info(
        "Pegá una o más URLs en la barra lateral "
        "y ejecutá la extracción."
    )

    st.stop()


st.subheader("Resumen de resultados")

metric_1, metric_2, metric_3, metric_4, metric_5 = (
    st.columns(5)
)

metric_1.metric(
    "Productos",
    f"{len(df):,}",
)

metric_2.metric(
    "Sitios",
    f"{df['sitio'].nunique():,}",
)

metric_3.metric(
    "Con SKU",
    f"{df['sku'].notna().sum():,}",
)

metric_4.metric(
    "Con marca",
    f"{df['marca'].notna().sum():,}",
)

metric_5.metric(
    "Con precio",
    f"{df['precio'].notna().sum():,}",
)


# ============================================================
# FILTROS
# ============================================================

with st.expander(
    "🔎 Filtros de exploración",
    expanded=True,
):
    filter_1, filter_2, filter_3, filter_4 = (
        st.columns(4)
    )

    sites = sorted(
        df["sitio"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    brands = sorted(
        df["marca"]
        .dropna()
        .astype(str)
        .unique()
        .tolist()
    )

    selected_sites = filter_1.multiselect(
        "Sitio",
        sites,
        default=sites,
    )

    selected_brands = filter_2.multiselect(
        "Marca",
        brands,
    )

    query = filter_3.text_input(
        "Buscar producto / SKU",
        placeholder=(
            "Ej. heladera, Samsung, 12345"
        ),
    )

    order = filter_4.selectbox(
        "Ordenar por",
        [
            "Precio ascendente",
            "Precio descendente",
            "Producto A-Z",
            "Página",
            "Sin ordenar",
        ],
    )

    only_with_price = st.checkbox(
        "Solo productos con precio",
        value=False,
    )


filtered = df.copy()

if selected_sites:
    filtered = filtered[
        filtered["sitio"].isin(
            selected_sites
        )
    ]

if selected_brands:
    filtered = filtered[
        filtered["marca"].isin(
            selected_brands
        )
    ]

if query.strip():
    needle = query.strip().lower()

    searchable = (
        filtered[
            [
                "producto",
                "sku",
                "marca",
                "variante",
            ]
        ]
        .fillna("")
        .astype(str)
        .agg(" ".join, axis=1)
        .str.lower()
    )

    filtered = filtered[
        searchable.str.contains(
            re.escape(needle),
            na=False,
        )
    ]

if only_with_price:
    filtered = filtered[
        filtered["precio"].notna()
    ]

if order == "Precio ascendente":
    filtered = filtered.sort_values(
        "precio",
        na_position="last",
    )

elif order == "Precio descendente":
    filtered = filtered.sort_values(
        "precio",
        ascending=False,
        na_position="last",
    )

elif order == "Producto A-Z":
    filtered = filtered.sort_values(
        "producto",
        na_position="last",
    )

elif order == "Página":
    filtered = filtered.sort_values(
        [
            "sitio",
            "pagina",
        ],
        na_position="last",
    )


# ============================================================
# TABLA
# ============================================================

st.subheader(
    f"Productos · {len(filtered):,}"
)

visible_columns = [
    column
    for column in OUTPUT_COLUMNS
    if column in filtered.columns
]

st.dataframe(
    filtered[visible_columns],
    use_container_width=True,
    hide_index=True,
    column_config={
        "precio": st.column_config.NumberColumn(
            "Precio",
            format="$ %.2f",
        ),
        "url_producto":
            st.column_config.LinkColumn(
                "Producto",
                display_text="Abrir",
            ),
        "url_pagina":
            st.column_config.LinkColumn(
                "Página",
                display_text="Abrir",
            ),
    },
)


# ============================================================
# DIAGNÓSTICO
# ============================================================

with st.expander(
    "🧪 Diagnóstico por página"
):
    diagnostics = (
        st.session_state
        .diagnostics_df
    )

    if diagnostics.empty:
        st.caption(
            "No hay diagnóstico disponible."
        )

    else:
        st.dataframe(
            diagnostics,
            use_container_width=True,
            hide_index=True,
            column_config={
                "url_real":
                    st.column_config.LinkColumn(
                        "URL real",
                        display_text="Abrir",
                    )
            },
        )


# ============================================================
# DESCARGAS
# ============================================================

st.divider()

st.subheader("Descargar resultados")

download_1, download_2 = st.columns(2)

with download_1:
    st.download_button(
        "⬇️ Descargar CSV",
        data=csv_bytes(filtered),
        file_name="productos_ecommerce.csv",
        mime="text/csv",
        use_container_width=True,
    )

with download_2:
    st.download_button(
        "⬇️ Descargar Excel",
        data=excel_bytes(
            filtered,
            st.session_state.diagnostics_df,
        ),
        file_name="productos_ecommerce.xlsx",
        mime=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
        use_container_width=True,
    )


# ============================================================
# ERRORES TÉCNICOS
# ============================================================

if st.session_state.scrape_errors:
    with st.expander(
        "⚠️ Avisos técnicos "
        f"({len(st.session_state.scrape_errors)})"
    ):
        for error in (
            st.session_state
            .scrape_errors[:60]
        ):
            st.code(error)

        if (
            len(
                st.session_state
                .scrape_errors
            )
            > 60
        ):
            st.caption(
                "Se muestran los primeros 60 avisos."
            )
