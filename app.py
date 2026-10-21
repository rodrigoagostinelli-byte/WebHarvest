# -*- coding: utf-8 -*-
"""Xplorador Ecommerce v2: scraping multi-fuente en memoria con Streamlit."""
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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Xplorador Ecommerce", page_icon="🛍️", layout="wide")

OUTPUT_COLUMNS = [
    "sitio", "sku", "producto", "marca", "variante", "precio", "precio_lista",
    "moneda", "disponibilidad", "pagina", "fuente_dato", "url_producto", "imagen", "url_pagina",
]
DATALAYER_EVENTS = {"view_item_list_custom", "view_item_list", "view_item"}

# ============================================================
# NORMALIZACION
# ============================================================
def first_value(obj, keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    lower = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        value = lower.get(str(key).lower())
        if value not in (None, "", [], {}):
            return value
    return None


def clean_text(value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = first_value(value, ["name", "Name", "value", "label", "title", "url", "content"])
    if isinstance(value, list):
        value = value[0] if value else None
        if isinstance(value, dict):
            value = first_value(value, ["name", "Name", "value", "label", "title", "url", "content"])
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def clean_sku(value):
    value = clean_text(value)
    if not value or len(value) > 100:
        return None
    return value


def normalize_name(value):
    value = clean_text(value)
    if not value:
        return None
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip() or None


def clean_price(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        value = first_value(value, [
            "Price", "price", "SellingPrice", "sellingPrice", "salePrice", "spotPrice",
            "lowPrice", "highPrice", "value", "amount", "PriceWithoutDiscount", "ListPrice",
        ])
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    text = str(value).strip()
    matches = re.findall(r"\$\s*[\d.]+(?:,\d{1,2})?", text)
    if matches:
        text = matches[-1]
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(".", "").replace(",", ".") if len(tail) in (1, 2) else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    elif text.count(".") == 1 and len(text.rsplit(".", 1)[-1]) == 3:
        text = text.replace(".", "")
    try:
        number = float(text)
        return number if math.isfinite(number) and number > 0 else None
    except ValueError:
        return None


def absolute_url(base_url, value):
    value = clean_text(value)
    return urljoin(base_url, value) if value else None


def get_sku_from_url(url):
    if not url:
        return None
    for pattern in [r"/p/(\d+)(?:[/?#]|$)", r"/producto/[^?#]*/(\d+)(?:[/?#]|$)", r"[?&](?:sku|productId|product_id)=(\d+)"]:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1)
    return None

# ============================================================
# PRECIOS VTEX Y EXTRACCION CONTEXTUAL
# ============================================================
def extract_vtex_offers(obj):
    """Devuelve precio actual, lista y disponibilidad desde sellers[].commertialOffer."""
    if not isinstance(obj, dict):
        return None, None, None
    sellers = first_value(obj, ["sellers", "Sellers", "seller"])
    if isinstance(sellers, dict):
        sellers = [sellers]
    if not isinstance(sellers, list):
        return None, None, None

    candidates = []
    for seller in sellers:
        if not isinstance(seller, dict):
            continue
        offer = first_value(seller, ["commertialOffer", "commercialOffer", "offer"])
        if not isinstance(offer, dict):
            continue
        price = clean_price(first_value(offer, ["Price", "price", "SellingPrice", "sellingPrice", "spotPrice", "PriceWithoutDiscount"]))
        list_price = clean_price(first_value(offer, ["ListPrice", "listPrice", "PriceWithoutDiscount", "priceWithoutDiscount"]))
        quantity = clean_price(first_value(offer, ["AvailableQuantity", "availableQuantity", "quantity"]))
        available = quantity is None or quantity > 0
        if price:
            candidates.append((0 if available else 1, price, list_price, "Disponible" if available else "Sin stock"))
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda x: (x[0], x[1]))
    _, price, list_price, availability = candidates[0]
    return price, list_price, availability


def extract_price_range(obj):
    if not isinstance(obj, dict):
        return None, None
    price_range = first_value(obj, ["priceRange", "PriceRange", "price_range"])
    if not isinstance(price_range, dict):
        return None, None
    selling = first_value(price_range, ["sellingPrice", "SellingPrice", "salePrice"])
    listing = first_value(price_range, ["listPrice", "ListPrice"])
    return clean_price(selling), clean_price(listing)


def generic_product_from_object(obj, source, base_url, inherited=None):
    inherited = inherited or {}
    if not isinstance(obj, dict):
        return None
    name = clean_text(first_value(obj, ["item_name", "productName", "product_name", "nameComplete", "productNameComplete", "title", "name"])) or inherited.get("producto")
    brand = clean_text(first_value(obj, ["item_brand", "brand", "brandName", "manufacturer", "vendor"])) or inherited.get("marca")
    sku = clean_sku(first_value(obj, ["item_id", "itemId", "sku", "skuId", "sku_id", "variantId", "variant_id", "productReference", "productReferenceCode", "referenceId", "id"]))
    price = clean_price(first_value(obj, ["price", "sellingPrice", "spotPrice", "salePrice", "sale_price", "bestPrice", "currentPrice", "lowPrice"]))
    list_price = clean_price(first_value(obj, ["listPrice", "originalPrice", "regularPrice", "priceWithoutDiscount", "oldPrice"]))
    offer_price, offer_list, availability = extract_vtex_offers(obj)
    range_price, range_list = extract_price_range(obj)
    price = price or offer_price or range_price
    list_price = list_price or offer_list or range_list
    raw_url = first_value(obj, ["link", "linkText", "productUrl", "product_url", "canonicalUrl", "url", "href"])
    image = first_value(obj, ["image", "imageUrl", "image_url", "thumbnail", "src", "imageLink"])
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = first_value(image, ["imageUrl", "url", "src", "contentUrl"])
    if not name or (sku is None and price is None):
        return None
    if not 3 <= len(name) <= 500:
        return None
    return {
        "sku": sku,
        "producto": name,
        "marca": brand,
        "variante": clean_text(first_value(obj, ["item_variant", "variant", "variantName", "variation"])),
        "precio": price,
        "precio_lista": list_price,
        "moneda": clean_text(first_value(obj, ["priceCurrency", "currency"])) or "ARS",
        "disponibilidad": availability or clean_text(first_value(obj, ["availability", "availabilityStatus", "stock", "inStock"])),
        "url_producto": absolute_url(base_url, raw_url),
        "imagen": absolute_url(base_url, image),
        "fuente_dato": source,
    }


def extract_vtex_products(obj, source, base_url, results, inherited=None, visited=None, depth=0):
    """Propaga nombre/marca del padre hacia items y sellers para no perder precios."""
    inherited = inherited or {}
    visited = visited or set()
    if depth > 25:
        return
    if isinstance(obj, (dict, list)):
        oid = id(obj)
        if oid in visited:
            return
        visited.add(oid)
    if isinstance(obj, list):
        for value in obj:
            extract_vtex_products(value, source, base_url, results, inherited, visited, depth + 1)
        return
    if not isinstance(obj, dict):
        return

    context = {
        "producto": clean_text(first_value(obj, ["productName", "product_name", "nameComplete", "productNameComplete", "item_name", "name", "title"])) or inherited.get("producto"),
        "marca": clean_text(first_value(obj, ["brand", "brandName", "item_brand", "manufacturer", "vendor"])) or inherited.get("marca"),
        "url_producto": absolute_url(base_url, first_value(obj, ["link", "linkText", "productUrl", "product_url", "url"])) or inherited.get("url_producto"),
    }

    direct = generic_product_from_object(obj, source, base_url, context)
    if direct:
        if not direct.get("url_producto"):
            direct["url_producto"] = context.get("url_producto")
        results.append(direct)

    items = first_value(obj, ["items", "Items", "skus", "SKUs", "variants"])
    if isinstance(items, dict):
        items = [items]
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            price, list_price, availability = extract_vtex_offers(item)
            range_price, range_list = extract_price_range(item)
            name = clean_text(first_value(item, ["nameComplete", "item_name", "name", "productName"])) or context.get("producto")
            sku = clean_sku(first_value(item, ["itemId", "item_id", "skuId", "sku_id", "sku", "id", "referenceId"]))
            if name and (sku or price or range_price):
                results.append({
                    "sku": sku,
                    "producto": name,
                    "marca": clean_text(first_value(item, ["brand", "brandName", "manufacturer"])) or context.get("marca"),
                    "variante": clean_text(first_value(item, ["item_variant", "variant", "nameComplete", "variation"])),
                    "precio": price or range_price,
                    "precio_lista": list_price or range_list,
                    "moneda": "ARS",
                    "disponibilidad": availability,
                    "url_producto": absolute_url(base_url, first_value(item, ["link", "url"])) or context.get("url_producto"),
                    "imagen": absolute_url(base_url, first_value(item, ["imageUrl", "image", "thumbnail"])),
                    "fuente_dato": source + ":vtex-item",
                })

    for key, value in obj.items():
        if key in {"items", "Items", "skus", "SKUs", "variants"}:
            continue
        if isinstance(value, (dict, list)):
            extract_vtex_products(value, source, base_url, results, context, visited, depth + 1)


def extract_datalayer_products(datalayer, base_url):
    rows = []
    if not isinstance(datalayer, list):
        return rows
    available = {e.get("event") for e in datalayer if isinstance(e, dict)}
    selected = {"view_item_list_custom"} if "view_item_list_custom" in available else ({"view_item_list"} if "view_item_list" in available else {"view_item"})
    for event in datalayer:
        if not isinstance(event, dict) or event.get("event") not in selected:
            continue
        items = (event.get("ecommerce") or {}).get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            row = generic_product_from_object(item, f"datalayer:{event.get('event')}", base_url)
            if row:
                rows.append(row)
    return rows

# ============================================================
# VALIDACION COMERCIAL Y FILTRO DE RUIDO
# ============================================================
NOISE_PRODUCT_NAMES = {
    "breadcrumb", "children", "contentaccordion", "customcartsidebar",
    "customfooter", "customincentives", "customnavigation", "customplptitle",
    "customproductshelf", "imagebanner", "maintenancepage", "preheaderpoints",
    "productgallery", "productshelf", "regionbar", "resetpoints", "richtext",
    "salesforceform", "global sections", "product list page", "default",
    "predeterminada", "predeterminada marketplace", "search page",
    "technical information", "compare products", "filters", "filtros",
    "name", "availability", "true", "false", "undefined", "null",
}
NOISE_PREFIXES = (
    "cp ", "ge ", "banco ", "tarjeta ", "vitrina ", "seleccionados ",
    "categorias selec", "product list page", "seller ", "base ",
    "transferencia ", "pago marketplace", "payment matrix", "cuotas ",
)
BLOCKED_RESPONSE_PATTERNS = (
    "paymentmatrix", "protectionsbundle", "cucardasactive", "sucursales",
    "contactate", "centro-ayuda", "terminos", "quienes-somos",
    "marketing-proveedores", "venta-empresas", "libro-de-quejas",
    "arrepentimiento", "salesforce", "footer", "navigation",
)
PRODUCT_RESPONSE_PATTERNS = (
    "clientmanyproductsquery", "productsearch", "search-result", "searchresult",
    "/api/catalog", "/api/io", "/_next/data/", "graphql", "products",
)


def is_product_response(response_url):
    value = str(response_url or "").lower()
    if any(pattern in value for pattern in BLOCKED_RESPONSE_PATTERNS):
        return False
    return any(pattern in value for pattern in PRODUCT_RESPONSE_PATTERNS)


def product_url_looks_valid(value):
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    path = urlsplit(value).path.lower()
    if any(x in path for x in ("/p/", "/producto/", "/product/")):
        return True
    return path not in ("", "/") and not any(x in path for x in ("/search", "/s", "/cart", "/checkout", "/ayuda"))


def product_image_looks_valid(value):
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return False
    low = value.lower()
    return any(x in low for x in ("/arquivos/ids/", ".jpg", ".jpeg", ".png", ".webp", "image"))


def is_valid_product_candidate(row):
    name = normalize_name(row.get("producto"))
    if not name or name in NOISE_PRODUCT_NAMES or name.startswith(NOISE_PREFIXES):
        return False
    if len(name) < 4 or len(name) > 420:
        return False
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", name):
        return False
    source = str(row.get("fuente_dato") or "")
    has_price = row.get("precio") is not None
    has_url = product_url_looks_valid(row.get("url_producto"))
    has_image = product_image_looks_valid(row.get("imagen"))
    is_datalayer = source.startswith("datalayer:")
    is_vtex_item = ":vtex-item" in source
    has_sku_brand = bool(clean_sku(row.get("sku")) and clean_text(row.get("marca")))
    # SKU solo no alcanza. SKU + marca sí es una señal comercial razonable
    # para respuestas de catálogo que publican el precio en otro objeto.
    return has_price or has_url or has_image or is_datalayer or is_vtex_item or has_sku_brand


def merge_rows_by_name(rows):
    """Une la representación comercial, el SKU y la oferta aunque vengan separados."""
    merged = {}
    for original in rows:
        row = dict(original)
        name_key = normalize_name(row.get("producto"))
        if not name_key:
            continue
        key = (row.get("sitio") or "", name_key)
        if key not in merged:
            merged[key] = row
            continue
        current = merged[key]
        current_source = str(current.get("fuente_dato") or "")
        new_source = str(row.get("fuente_dato") or "")

        # DataLayer suele aportar el SKU comercial; la red, el ID interno.
        if new_source.startswith("datalayer:") and row.get("sku"):
            old_sku = clean_sku(current.get("sku"))
            new_sku = clean_sku(row.get("sku"))
            if old_sku and old_sku != new_sku and not current.get("variante"):
                current["variante"] = old_sku
            current["sku"] = new_sku
        elif not current.get("sku") and row.get("sku"):
            current["sku"] = row.get("sku")

        for field in OUTPUT_COLUMNS:
            if field == "sku":
                continue
            if current.get(field) in (None, "", [], {}) and row.get(field) not in (None, "", [], {}):
                current[field] = row[field]

        # Preferir precio comercial de VTEX, pero nunca borrar uno existente.
        if row.get("precio") is not None:
            if current.get("precio") is None or source_priority(new_source) > source_priority(current_source):
                current["precio"] = row["precio"]
                if row.get("precio_lista") is not None:
                    current["precio_lista"] = row["precio_lista"]

        sources = [x for x in current_source.split(" | ") if x]
        if new_source and new_source not in sources:
            sources.append(new_source)
        current["fuente_dato"] = " | ".join(sources[:5])
    return list(merged.values())

# ============================================================
# CONSOLIDACION
# ============================================================
def candidate_key(row):
    site = row.get("sitio") or ""
    sku = clean_sku(row.get("sku"))
    if sku:
        return (site, "sku", sku.lower())
    url = clean_text(row.get("url_producto"))
    if url:
        path = urlsplit(url).path.rstrip("/").lower()
        if path:
            return (site, "url", path)
    name = normalize_name(row.get("producto"))
    return (site, "name", name) if name else None


def source_priority(value):
    value = str(value or "")
    if ":vtex-item" in value or "commertialOffer" in value:
        return 5
    if value.startswith("datalayer"):
        return 4
    if value.startswith("html"):
        return 3
    if value.startswith("network"):
        return 2
    return 1


def merge_rows(rows):
    """Une candidatos del mismo SKU y rellena precio desde otras respuestas."""
    merged = {}
    for original in rows:
        row = dict(original)
        key = candidate_key(row)
        if not key:
            continue
        if key not in merged:
            merged[key] = row
            continue
        current = merged[key]
        for field in OUTPUT_COLUMNS:
            if current.get(field) in (None, "", [], {}) and row.get(field) not in (None, "", [], {}):
                current[field] = row[field]
        if row.get("precio") is not None and (current.get("precio") is None or source_priority(row.get("fuente_dato")) > source_priority(current.get("fuente_dato"))):
            current["precio"] = row["precio"]
            if row.get("precio_lista") is not None:
                current["precio_lista"] = row["precio_lista"]
        sources = [x for x in str(current.get("fuente_dato") or "").split(" | ") if x]
        if row.get("fuente_dato") and row["fuente_dato"] not in sources:
            sources.append(row["fuente_dato"])
        current["fuente_dato"] = " | ".join(sources[:4])
    return list(merged.values())


def prepare_dataframe(rows):
    if not rows:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    valid_rows = [row for row in rows if is_valid_product_candidate(row)]
    valid_rows = merge_rows_by_name(valid_rows)
    valid_rows = merge_rows(valid_rows)
    if not valid_rows:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    df = pd.DataFrame(valid_rows)
    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    for col in ["precio", "precio_lista"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["sku", "producto", "marca", "variante"]:
        df[col] = df[col].replace(["", "None", "null", "undefined"], pd.NA)
    df = df[df["producto"].notna()].copy()
    df["_quality"] = df[["sku", "marca", "precio", "url_producto", "imagen"]].notna().sum(axis=1)
    df = df.sort_values(["_quality", "producto"], ascending=[False, True])
    df = df.drop_duplicates(subset=["sitio", "producto"], keep="first")
    df = df.drop(columns=["_quality"])
    return df[OUTPUT_COLUMNS].sort_values(["sitio", "producto"], na_position="last").reset_index(drop=True)


# ============================================================
# HTML Y PAGINACION
# ============================================================
def build_page_url(base_url, page_number):
    parts = urlsplit(base_url)
    items = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in {"page", "pagina", "pagenumber"}]
    items.append(("page", str(page_number)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(items, doseq=True), parts.fragment))


def strategy_for(url, selected):
    if selected != "Automática":
        return {"Red dinámica": "dynamic", "Paginación por URL": "paginated", "Una sola URL": "single"}[selected]
    domain = urlsplit(url).netloc.lower()
    if "cetrogar.com.ar" in domain:
        return "dynamic"
    if "fravega.com" in domain:
        return "paginated"
    return "single"


async def extract_dom_cards(page, base_url):
    raw = await page.evaluate(r"""() => {
      const out=[],seen=new Set(), clean=v=>v?String(v).replace(/\s+/g,' ').trim():null, txt=n=>n?clean(n.innerText||n.textContent||''):null;
      const links=[...document.querySelectorAll('a[href]')].filter(a=>/\/p\/|\/producto\/|\/product\//i.test(a.href||''));
      for(const a of links){
        let card=a.closest('article,li,[itemtype*="Product"],[data-product-id],[data-sku],[data-testid*="product"],[data-test*="product"],[class*="ProductCard"],[class*="product-card"],[class*="productCard"],[class*="product-item"]');
        if(!card){let n=a;for(let i=0;i<8&&n;i++,n=n.parentElement){const t=txt(n)||'';if(t.length<2200&&(/\$\s*[\d.]+/.test(t))&&n.querySelector('h2,h3,h4,[itemprop="name"]')){card=n;break;}}}
        if(!card)continue;const text=txt(card)||'', key=a.href.split('?')[0];if(seen.has(key))continue;
        const name=txt(card.querySelector('[itemprop="name"],[data-testid*="title"],[data-testid*="name"],[class*="product-name"],[class*="productName"],[class*="ProductName"],h2,h3,h4'))||a.title||a.getAttribute('aria-label')||txt(a);
        if(!name||name.length<4||name.length>500)continue;
        const prices=text.match(/\$\s*[\d.]+(?:,\d{1,2})?/g)||[];
        const attr=names=>{for(const n of [card,...card.querySelectorAll('*')])for(const k of names){const v=n.getAttribute(k);if(v)return v;}return null};
        const img=card.querySelector('img');seen.add(key);out.push({sku:attr(['data-sku','data-product-id','data-item-id']),producto:name,marca:txt(card.querySelector('[class*="brand" i]')),precio:prices.at(-1)||null,url_producto:a.href,imagen:img?.src||img?.getAttribute('data-src')||null});
      }return out;
    }""")
    rows = []
    for item in raw:
        item["precio"] = clean_price(item.get("precio"))
        item["sku"] = clean_sku(item.get("sku")) or get_sku_from_url(item.get("url_producto"))
        item["url_producto"] = absolute_url(base_url, item.get("url_producto"))
        item["imagen"] = absolute_url(base_url, item.get("imagen"))
        item["fuente_dato"] = "html:listing-card"
        rows.append(item)
    return rows

# ============================================================
# MOTORES
# ============================================================
async def scrape_dynamic(context, url, max_actions, timeout_ms, progress):
    rows, errors, tasks = [], [], set()
    stats = {"respuestas": 0, "json": 0, "json_con_productos": 0, "scrolls": 0}
    page = await context.new_page()

    async def process_response(response):
        stats["respuestas"] += 1
        try:
            ctype = response.headers.get("content-type", "").lower()
            if response.status >= 400 or not ("json" in ctype or response.request.resource_type in {"xhr", "fetch"}):
                return
            # No filtrar por nombre de endpoint antes de inspeccionar el JSON.
            # Cetrogar puede servir catálogo desde endpoints cuyos nombres cambian.
            # El ruido se elimina después mediante is_valid_product_candidate().
            payload = await response.json()
            stats["json"] += 1
            before = len(rows)
            extract_vtex_products(
                payload,
                f"network:{response.url[:180]}",
                page.url or url,
                rows,
            )
            if len(rows) > before:
                stats["json_con_productos"] += 1
        except Exception:
            pass

    def schedule(response):
        task = asyncio.create_task(process_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", schedule)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            errors.append("Timeout parcial de navegación")
        await page.wait_for_timeout(8000)
        previous_height, stagnant = 0, 0
        for idx in range(max_actions):
            progress(idx / max(max_actions, 1), f"Cargando catálogo dinámico · acción {idx + 1}/{max_actions}")
            current = await page.evaluate("() => document.body.scrollHeight")
            await page.evaluate("() => window.scrollTo({top:document.body.scrollHeight,behavior:'instant'})")
            await page.wait_for_timeout(1500)
            new_height = await page.evaluate("() => document.body.scrollHeight")
            stats["scrolls"] += 1
            stagnant = stagnant + 1 if new_height == current == previous_height else 0
            previous_height = new_height
            if stagnant >= 2:
                break
        await page.wait_for_timeout(2000)
        try:
            datalayer = await page.evaluate("() => JSON.parse(JSON.stringify(window.dataLayer||[],(k,v)=>typeof v==='function'?undefined:v))")
            rows.extend(extract_datalayer_products(datalayer, page.url))
        except Exception as exc:
            errors.append(f"DataLayer: {type(exc).__name__}: {exc}")
        try:
            rows.extend(await extract_dom_cards(page, page.url))
        except Exception as exc:
            errors.append(f"DOM: {type(exc).__name__}: {exc}")
        for block in await page.locator('script[type="application/ld+json"]').all_text_contents():
            try:
                extract_vtex_products(json.loads(block), "html:json-ld", page.url, rows)
            except Exception:
                pass
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        site = urlsplit(url).netloc.lower().removeprefix("www.")
        for row in rows:
            row.update({"sitio": site, "pagina": 1, "url_pagina": page.url})
        diagnostic = {"sitio": site, "estrategia": "Red dinámica", "pagina": 1, "respuestas": stats["respuestas"], "respuestas_json": stats["json"], "json_con_productos": stats["json_con_productos"], "acciones": stats["scrolls"], "candidatos": len(rows), "url_real": page.url}
        return rows, [diagnostic], errors
    finally:
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await page.close()


async def scrape_page(context, url, number, timeout_ms, actions):
    rows, errors = [], []
    page = await context.new_page()
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            errors.append("Timeout parcial")
        await page.wait_for_timeout(3500)
        for _ in range(max(1, min(actions, 6))):
            await page.evaluate("() => window.scrollTo(0,document.body.scrollHeight)")
            await page.wait_for_timeout(700)
        dom = await extract_dom_cards(page, page.url)
        rows.extend(dom)
        try:
            dl = await page.evaluate("() => JSON.parse(JSON.stringify(window.dataLayer||[],(k,v)=>typeof v==='function'?undefined:v))")
            rows.extend(extract_datalayer_products(dl, page.url))
        except Exception:
            pass
        for block in await page.locator('script[type="application/ld+json"]').all_text_contents():
            try:
                extract_vtex_products(json.loads(block), "html:json-ld", page.url, rows)
            except Exception:
                pass
        site = urlsplit(url).netloc.lower().removeprefix("www.")
        for row in rows:
            row.update({"sitio": site, "pagina": number, "url_pagina": page.url})
        return rows, {"sitio": site, "estrategia": "Paginación por URL", "pagina": number, "DOM": len(dom), "candidatos": len(rows), "url_real": page.url}, errors
    finally:
        await page.close()


async def scrape_paginated(context, base_url, max_pages, timeout_ms, actions, progress):
    all_rows, diagnostics, errors, seen = [], [], [], set()
    empty = repeated = 0
    for number in range(1, max_pages + 1):
        progress((number - 1) / max_pages, f"Analizando página {number}/{max_pages}")
        rows, diag, page_errors = await scrape_page(context, build_page_url(base_url, number), number, timeout_ms, actions)
        new_rows = []
        for row in merge_rows(rows):
            key = candidate_key(row)
            if key and key not in seen:
                seen.add(key)
                new_rows.append(row)
        all_rows.extend(new_rows)
        diag.update({"nuevos": len(new_rows), "acumulados": len(all_rows)})
        diagnostics.append(diag)
        errors.extend(page_errors)
        empty = empty + 1 if not rows else 0
        repeated = repeated + 1 if rows and not new_rows else 0
        if empty >= 2 or repeated >= 2:
            break
    return all_rows, diagnostics, errors


async def scrape_all(urls, strategy_choice, max_pages, max_actions, timeout_ms, progress):
    all_rows, diagnostics, errors = [], [], []
    async with async_playwright() as p:
        if not os.path.exists(p.chromium.executable_path):
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True, text=True)
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="es-AR", user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
        try:
            for idx, url in enumerate(urls):
                strategy = strategy_for(url, strategy_choice)
                progress(idx / max(len(urls), 1), f"Preparando {urlsplit(url).netloc} · {strategy}")
                if strategy == "dynamic":
                    rows, diag, err = await scrape_dynamic(context, url, max_actions, timeout_ms, progress)
                elif strategy == "paginated":
                    rows, diag, err = await scrape_paginated(context, url, max_pages, timeout_ms, max_actions, progress)
                else:
                    rows, one_diag, err = await scrape_page(context, url, 1, timeout_ms, max_actions)
                    diag = [one_diag]
                all_rows.extend(rows); diagnostics.extend(diag); errors.extend(err)
        finally:
            await context.close(); await browser.close()
    progress(1.0, "Extracción finalizada")
    return prepare_dataframe(all_rows), pd.DataFrame(diagnostics), errors


def run_scrape(*args):
    return asyncio.run(scrape_all(*args))


def excel_bytes(df, diagnostics):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
        diagnostics.to_excel(writer, index=False, sheet_name="Diagnostico")
    return buffer.getvalue()

# ============================================================
# INTERFAZ
# ============================================================
st.markdown("""
<style>
.block-container{padding-top:1.25rem;padding-bottom:3rem;max-width:1520px}.hero{padding:1.8rem;border-radius:24px;background:radial-gradient(circle at 85% 15%,rgba(167,139,250,.45),transparent 34%),linear-gradient(125deg,#111827,#312e81 58%,#6d28d9);color:#fff;margin-bottom:1.25rem;box-shadow:0 18px 45px rgba(49,46,129,.22)}.hero h1{font-size:2.35rem;line-height:1.05;margin:0 0 .55rem;letter-spacing:-.045em}.hero p{max-width:850px;color:#e5e7eb;margin:0;font-size:1.02rem}.pill{display:inline-block;padding:.3rem .65rem;border:1px solid rgba(255,255,255,.24);border-radius:999px;background:rgba(255,255,255,.1);font-size:.78rem;margin-bottom:.8rem}div[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.18);padding:1rem;border-radius:18px;background:linear-gradient(145deg,rgba(128,128,128,.06),rgba(128,128,128,.015))}.stButton button,.stDownloadButton button{border-radius:12px;font-weight:650}.section-note{padding:.8rem 1rem;border-radius:14px;background:rgba(108,92,231,.08);border:1px solid rgba(108,92,231,.18);margin:.25rem 0 1rem}
</style>
<div class="hero"><span class="pill">Exploración multi-fuente</span><h1>🛍️ Xplorador Ecommerce</h1><p>Captura productos desde APIs, estructuras VTEX, DataLayer, JSON-LD y tarjetas HTML. Une automáticamente nombre, SKU y precio aunque lleguen en respuestas separadas.</p></div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Configuración")
    urls_text = st.text_area("URLs de listados", value="https://www.cetrogar.com.ar/s?fuzzy=0&operator=and&tipo-de-oferta=oferta&facets=fuzzy%2Coperator%2Ctipo-de-oferta&sort=score_desc&page=1", height=180, help="Una URL por línea. Usá búsquedas, categorías u ofertas.")
    strategy_choice = st.selectbox("Estrategia", ["Automática", "Red dinámica", "Paginación por URL", "Una sola URL"], help="Automática usa red dinámica para Cetrogar y paginación para Frávega.")
    max_pages = st.slider("Máximo de páginas", 1, 50, 10)
    max_actions = st.slider("Cargas dinámicas / scrolls", 1, 30, 10)
    timeout_seconds = st.slider("Timeout por URL", 20, 120, 90, step=10)
    start = st.button("🚀 Ejecutar extracción", type="primary", use_container_width=True)
    clear = st.button("Limpiar resultados", use_container_width=True)
    st.caption("Los resultados viven solamente durante la sesión y se exportan en memoria.")

for key, default in [("products_df", pd.DataFrame()), ("diagnostics_df", pd.DataFrame()), ("scrape_errors", [])]:
    if key not in st.session_state:
        st.session_state[key] = default
if clear:
    st.session_state.products_df = pd.DataFrame(); st.session_state.diagnostics_df = pd.DataFrame(); st.session_state.scrape_errors = []

if start:
    urls = []
    for raw in urls_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        if urlsplit(raw).netloc:
            urls.append(raw)
    if not urls:
        st.error("Ingresá al menos una URL válida.")
    else:
        bar = st.progress(0, text="Preparando Chromium…")
        try:
            with st.spinner("Recopilando productos y reconstruyendo ofertas…"):
                df, diagnostics, errors = run_scrape(urls, strategy_choice, max_pages, max_actions, timeout_seconds * 1000, lambda v, m: bar.progress(min(1.0, max(0.0, v)), text=m))
                st.session_state.products_df, st.session_state.diagnostics_df, st.session_state.scrape_errors = df, diagnostics, errors
            if df.empty:
                st.warning(
                    "No se encontraron productos válidos. Abrí Diagnóstico de extracción: "
                    "si respuestas_json es mayor que 0 pero candidatos es 0, el payload usa una estructura nueva; "
                    "si candidatos es mayor que 0, el filtro comercial los descartó."
                )
            else:
                st.success(f"Extracción finalizada: {len(df):,} productos únicos.")
        except Exception as exc:
            st.error(f"Error: {type(exc).__name__}: {exc}")
        finally:
            bar.empty()

df = st.session_state.products_df.copy()
if df.empty:
    st.markdown('<div class="section-note">Pegá una URL de búsqueda o categoría en la barra lateral. Para Cetrogar, dejá la estrategia en <b>Automática</b>.</div>', unsafe_allow_html=True)
    diagnostics = st.session_state.diagnostics_df
    if not diagnostics.empty:
        with st.expander("🧪 Diagnóstico de extracción", expanded=True):
            st.dataframe(
                diagnostics,
                use_container_width=True,
                hide_index=True,
                column_config={"url_real": st.column_config.LinkColumn("URL real", display_text="Abrir")},
            )
    if st.session_state.scrape_errors:
        with st.expander(f"⚠️ Avisos técnicos ({len(st.session_state.scrape_errors)})", expanded=True):
            for error in st.session_state.scrape_errors[:80]:
                st.code(error)
    st.stop()

coverage_price = df["precio"].notna().mean() * 100
coverage_sku = df["sku"].notna().mean() * 100
coverage_brand = df["marca"].notna().mean() * 100
st.subheader("Resumen de cobertura")
a,b,c,d,e = st.columns(5)
a.metric("Productos únicos", f"{len(df):,}")
b.metric("Sitios", f"{df['sitio'].nunique():,}")
c.metric("Cobertura precio", f"{coverage_price:.1f}%")
d.metric("Cobertura SKU", f"{coverage_sku:.1f}%")
e.metric("Cobertura marca", f"{coverage_brand:.1f}%")

missing_price = df[df["precio"].isna()]
st.caption("El resultado muestra únicamente candidatos con evidencia comercial: precio, URL, imagen, DataLayer o estructura VTEX.")
if not missing_price.empty:
    st.warning(f"Quedaron {len(missing_price):,} productos sin precio ({100-coverage_price:.1f}%). Podés identificarlos con el filtro de cobertura.")

with st.expander("🔎 Explorar y filtrar", expanded=True):
    c1,c2,c3,c4 = st.columns(4)
    sites = sorted(df["sitio"].dropna().astype(str).unique())
    brands = sorted(df["marca"].dropna().astype(str).unique())
    selected_sites = c1.multiselect("Sitio", sites, default=sites)
    selected_brands = c2.multiselect("Marca", brands)
    query = c3.text_input("Buscar", placeholder="Producto, SKU o marca")
    coverage_filter = c4.selectbox("Cobertura", ["Todos", "Solo con precio", "Solo sin precio", "Solo con SKU", "Solo sin SKU"])
    order = st.radio("Orden", ["Precio ascendente", "Precio descendente", "Producto A-Z", "Página", "Sin ordenar"], horizontal=True)

filtered = df.copy()
if selected_sites:
    filtered = filtered[filtered["sitio"].isin(selected_sites)]
if selected_brands:
    filtered = filtered[filtered["marca"].isin(selected_brands)]
if query.strip():
    search = filtered[["producto", "sku", "marca", "variante"]].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
    filtered = filtered[search.str.contains(re.escape(query.strip().lower()), na=False)]
if coverage_filter == "Solo con precio": filtered = filtered[filtered["precio"].notna()]
elif coverage_filter == "Solo sin precio": filtered = filtered[filtered["precio"].isna()]
elif coverage_filter == "Solo con SKU": filtered = filtered[filtered["sku"].notna()]
elif coverage_filter == "Solo sin SKU": filtered = filtered[filtered["sku"].isna()]
if order == "Precio ascendente": filtered = filtered.sort_values("precio", na_position="last")
elif order == "Precio descendente": filtered = filtered.sort_values("precio", ascending=False, na_position="last")
elif order == "Producto A-Z": filtered = filtered.sort_values("producto", na_position="last")
elif order == "Página": filtered = filtered.sort_values(["sitio", "pagina"], na_position="last")

st.subheader(f"Productos · {len(filtered):,}")
view = st.radio("Vista", ["Tabla", "Tarjetas"], horizontal=True, label_visibility="collapsed")
if view == "Tabla":
    visible = [c for c in OUTPUT_COLUMNS if c in filtered.columns]
    st.dataframe(filtered[visible], use_container_width=True, hide_index=True, height=620, column_config={
        "precio": st.column_config.NumberColumn("Precio", format="$ %.0f"),
        "precio_lista": st.column_config.NumberColumn("Precio lista", format="$ %.0f"),
        "url_producto": st.column_config.LinkColumn("Producto", display_text="Abrir"),
        "url_pagina": st.column_config.LinkColumn("Página", display_text="Abrir"),
        "imagen": st.column_config.ImageColumn("Imagen"),
    })
else:
    for start_idx in range(0, min(len(filtered), 48), 4):
        cols = st.columns(4)
        for col, (_, row) in zip(cols, filtered.iloc[start_idx:start_idx+4].iterrows()):
            with col:
                with st.container(border=True):
                    if isinstance(row.get("imagen"), str) and row["imagen"].startswith("http"):
                        st.image(row["imagen"], use_container_width=True)
                    st.markdown(f"**{row.get('producto') or 'Producto'}**")
                    st.caption(f"{row.get('marca') or 'Marca no informada'} · {row.get('sitio')}")
                    st.markdown(f"### ${row['precio']:,.0f}" if pd.notna(row.get("precio")) else "### Precio no detectado")
                    if row.get("sku"): st.caption(f"SKU: {row['sku']}")
                    if isinstance(row.get("url_producto"), str) and row["url_producto"].startswith("http"):
                        st.link_button("Ver producto ↗", row["url_producto"], use_container_width=True)

with st.expander("🧪 Diagnóstico de extracción"):
    diagnostics = st.session_state.diagnostics_df
    if diagnostics.empty:
        st.caption("Sin diagnóstico disponible.")
    else:
        st.dataframe(diagnostics, use_container_width=True, hide_index=True, column_config={"url_real": st.column_config.LinkColumn("URL real", display_text="Abrir")})

st.divider(); st.subheader("Descargar resultados")
left,right = st.columns(2)
left.download_button("⬇️ Descargar CSV filtrado", filtered.to_csv(index=False).encode("utf-8-sig"), "productos_ecommerce.csv", "text/csv", use_container_width=True)
right.download_button("⬇️ Descargar Excel + diagnóstico", excel_bytes(filtered, st.session_state.diagnostics_df), "productos_ecommerce.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
if st.session_state.scrape_errors:
    with st.expander(f"⚠️ Avisos técnicos ({len(st.session_state.scrape_errors)})"):
        for error in st.session_state.scrape_errors[:80]:
            st.code(error)
