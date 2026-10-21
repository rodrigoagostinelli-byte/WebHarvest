# -*- coding: utf-8 -*-
"""Xplorador ecommerce: scraping en memoria, exploración y descarga con Streamlit."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
from io import BytesIO
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import pandas as pd
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

st.set_page_config(page_title="Xplorador Ecommerce", page_icon="🛍️", layout="wide")

# ------------------------------ Extracción ------------------------------
ALIASES = {
    "sku": ["sku", "itemId", "item_id", "productId", "product_id", "id", "code", "productCode", "partNumber", "mpn", "gtin", "ean"],
    "nombre": ["name", "title", "productName", "product_name", "displayName", "description", "nombre", "titulo"],
    "marca": ["brand", "brandName", "manufacturer", "marca", "Brand"],
    "precio": ["price", "sellingPrice", "salePrice", "currentPrice", "bestPrice", "lowPrice", "amount", "precio", "Price"],
    "precio_lista": ["listPrice", "originalPrice", "regularPrice", "priceWithoutDiscount", "oldPrice", "precioAnterior"],
    "url": ["url", "link", "productUrl", "product_url", "canonicalUrl", "href"],
    "imagen": ["image", "imageUrl", "image_url", "thumbnail", "src", "imageLink", "images"],
    "disponibilidad": ["availability", "availabilityStatus", "stock", "inStock", "available", "disponibilidad"],
    "categoria": ["category", "categoryName", "categories", "department", "categoria"],
}


def first_value(obj, keys):
    if not isinstance(obj, dict):
        return None
    low = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        v = obj.get(key)
        if v not in (None, "", [], {}):
            return v
    for key in keys:
        v = low.get(key.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def text_value(value):
    if isinstance(value, dict):
        value = first_value(value, ["name", "value", "label", "url", "content", "title"])
    if isinstance(value, list):
        value = value[0] if value else None
        if isinstance(value, dict):
            value = first_value(value, ["name", "value", "label", "url", "content", "title"])
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def price_value(value):
    if isinstance(value, dict):
        value = first_value(value, ["value", "amount", "price", "sellingPrice", "lowPrice"])
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    s = re.sub(r"[^\d,.\-]", "", str(value).strip())
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        tail = s.rsplit(",", 1)[-1]
        s = s.replace(".", "").replace(",", ".") if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        n = float(s)
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def norm_product(obj, source, base_url):
    if not isinstance(obj, dict):
        return None
    # Soporta objetos comunes, JSON-LD y respuestas VTEX.
    nested = first_value(obj, ["product", "Product", "item", "Item"])
    if isinstance(nested, dict) and not first_value(obj, ALIASES["nombre"]):
        obj = nested
    name = text_value(first_value(obj, ALIASES["nombre"]))
    if not name or len(name) < 3:
        return None
    sku = text_value(first_value(obj, ALIASES["sku"]))
    brand = text_value(first_value(obj, ALIASES["marca"]))
    price = price_value(first_value(obj, ALIASES["precio"]))
    list_price = price_value(first_value(obj, ALIASES["precio_lista"]))
    raw_url = first_value(obj, ALIASES["url"])
    if isinstance(raw_url, dict):
        raw_url = first_value(raw_url, ["url", "href"])
    product_url = urljoin(base_url, text_value(raw_url)) if raw_url else None
    image = first_value(obj, ALIASES["imagen"])
    if isinstance(image, dict):
        image = first_value(image, ["url", "src"])
    if isinstance(image, list):
        image = image[0] if image else None
        if isinstance(image, dict):
            image = first_value(image, ["url", "src"])
    image = urljoin(base_url, text_value(image)) if image else None
    availability = text_value(first_value(obj, ALIASES["disponibilidad"]))
    category = text_value(first_value(obj, ALIASES["categoria"]))
    return {
        "sitio": urlparse(base_url).netloc.lower().removeprefix("www."),
        "sku": sku,
        "producto": name,
        "marca": brand,
        "precio": price,
        "precio_lista": list_price,
        "moneda": text_value(first_value(obj, ["priceCurrency", "currency", "moneda"])),
        "disponibilidad": availability,
        "categoria": category,
        "url_producto": product_url,
        "imagen": image,
        "fuente_dato": source,
    }


def walk_json(obj, source, base_url, out, depth=0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        row = norm_product(obj, source, base_url)
        if row:
            out.append(row)
        for value in obj.values():
            if isinstance(value, (dict, list)):
                walk_json(value, source, base_url, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:20000]:
            if isinstance(value, (dict, list)):
                walk_json(value, source, base_url, out, depth + 1)


def dedupe(rows):
    seen, result = set(), []
    for r in rows:
        url = (r.get("url_producto") or "").split("?")[0].rstrip("/").lower()
        sku = (r.get("sku") or "").strip().lower()
        name = (r.get("producto") or "").strip().lower()
        key = (url or sku or name, r.get("sitio"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(r)
    return result


def page_url(base, number):
    """Prueba el parámetro de página habitual sin destruir los demás parámetros."""
    p = urlparse(base)
    q = parse_qs(p.query, keep_blank_values=True)
    for key in ("page", "pagina", "PageNumber", "pageNumber", "p"):
        if key in q:
            q[key] = [str(number)]
            return urlunparse(p._replace(query=urlencode(q, doseq=True)))
    q["page"] = [str(number)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


async def scrape_one(url, max_scrolls, timeout_ms):
    rows, errors, json_payloads = [], [], []
    async with async_playwright() as p:
        # En Streamlit Community Cloud el paquete Playwright no siempre incluye Chromium.
        if not os.path.exists(p.chromium.executable_path):
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True, text=True)
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            viewport={"width": 1440, "height": 1000}, locale="es-AR",
        )
        page = await context.new_page()
        async def capture_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype.lower() and response.status < 400:
                    payload = await response.json()
                    json_payloads.append(payload)
            except Exception:
                pass
        page.on("response", lambda response: asyncio.create_task(capture_response(response)))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 12000))
            except PlaywrightTimeoutError:
                pass
            await page.wait_for_timeout(1800)
            # JSON-LD, DataLayer y estado JS expuesto por el sitio.
            try:
                payloads = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('script[type="application/ld+json"]').forEach(e => { try { out.push(JSON.parse(e.textContent)); } catch (_) {} });
                    const dl = window.dataLayer || []; out.push(dl);
                    for (const k of ['__NEXT_DATA__','__INITIAL_STATE__','__PRELOADED_STATE__','__NUXT__','initialState','productData']) {
                      try { if (window[k]) out.push(window[k]); } catch (_) {}
                    }
                    return out;
                }""")
                for payload in payloads:
                    walk_json(payload, "JSON-LD/DataLayer/estado", url, rows)
            except Exception as e:
                errors.append(f"Lectura de datos embebidos: {e}")
            # Captura de tarjetas DOM y enlaces de producto.
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(450)
            try:
                dom_rows = await page.evaluate("""() => {
                  const selectors = ['[data-product-id]','[data-sku]','[itemtype*="Product"]','article','li[class*="product"]','div[class*="product-card"]','div[class*="ProductCard"]','[class*="product-item"]'];
                  const nodes = [...new Set(selectors.flatMap(s => [...document.querySelectorAll(s)]))];
                  const out = [];
                  for (const el of nodes) {
                    const txt = (el.innerText || '').trim(); if (!txt || txt.length < 8 || txt.length > 3000) continue;
                    const a = el.matches('a[href]') ? el : el.querySelector('a[href]');
                    const img = el.querySelector('img');
                    const meta = (sel, attr) => el.querySelector(sel)?.getAttribute(attr);
                    const priceNode = el.querySelector('[itemprop="price"], [class*="price"], [data-price]');
                    const sku = el.getAttribute('data-sku') || el.getAttribute('data-product-id') || meta('[itemprop="sku"]','content');
                    const name = meta('[itemprop="name"]','content') || el.querySelector('h2,h3,h4,[class*="name"],[class*="title"]')?.innerText || a?.innerText;
                    const brand = meta('[itemprop="brand"]','content') || el.querySelector('[class*="brand"]')?.innerText;
                    const price = priceNode?.getAttribute('content') || priceNode?.getAttribute('data-price') || priceNode?.innerText;
                    const image = img?.getAttribute('src') || img?.getAttribute('data-src') || img?.getAttribute('data-lazy-src');
                    if (name) out.push({sku, name, brand, price, url:a?.href, image, description:txt.slice(0,700)});
                  }
                  return out;
                }""")
                walk_json(dom_rows, "tarjetas HTML", url, rows)
            except Exception as e:
                errors.append(f"Lectura HTML: {e}")
            for payload in json_payloads:
                walk_json(payload, "respuesta API JSON", url, rows)
        except Exception as e:
            errors.append(f"Navegación: {type(e).__name__}: {e}")
        finally:
            await context.close()
            await browser.close()
    return dedupe(rows), errors


async def scrape_urls(urls, pages, max_scrolls, timeout_ms, progress):
    all_rows, all_errors = [], []
    for i, base in enumerate(urls):
        site_rows = []
        for pg in range(1, pages + 1):
            target = base if pg == 1 else page_url(base, pg)
            progress((i * pages + pg - 1) / max(1, len(urls) * pages), f"Analizando {urlparse(base).netloc} · página {pg}/{pages}")
            try:
                rows, errors = await scrape_one(target, max_scrolls, timeout_ms)
                site_rows.extend(rows)
                all_errors.extend([f"{target} — {e}" for e in errors])
                # Si no se detectan productos en páginas posteriores, evita insistir en páginas vacías.
                if pg > 1 and not rows:
                    break
            except Exception as e:
                all_errors.append(f"{target} — {type(e).__name__}: {e}")
        all_rows.extend(site_rows)
    progress(1.0, "Extracción finalizada")
    return dedupe(all_rows), all_errors


def run_scrape(urls, pages, max_scrolls, timeout_ms, progress):
    return asyncio.run(scrape_urls(urls, pages, max_scrolls, timeout_ms, progress))


def csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8-sig")


def excel_bytes(df):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Productos")
    return buf.getvalue()

# ------------------------------ Interfaz ------------------------------
st.markdown("""
<style>
:root { --accent: #6C5CE7; }
.block-container {padding-top: 1.7rem; padding-bottom: 3rem; max-width: 1500px;}
.hero {padding: 1.5rem 1.7rem; border-radius: 22px; background: linear-gradient(120deg,#17172b 0%,#312e81 55%,#6c5ce7 100%); color:white; margin-bottom:1.2rem;}
.hero h1 {font-size:2.2rem; margin:0 0 .4rem 0; letter-spacing:-.04em;}
.hero p {color:#e5e7eb; margin:0; font-size:1rem;}
div[data-testid="stMetric"] {border:1px solid rgba(128,128,128,.2); padding:1rem; border-radius:16px; background:rgba(128,128,128,.04);}
.stButton button, .stDownloadButton button {border-radius:10px; font-weight:600;}
</style>
<div class="hero"><h1>🛍️ Xplorador Ecommerce</h1><p>Extraé, explorá y descargá datos de productos en una sola sesión. Sin base de datos ni persistencia propia.</p></div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Configuración")
    urls_text = st.text_area("URLs de listados (una por línea)", value="https://www.cetrogar.com.ar/\nhttps://www.fravega.com/", height=120, help="Pegá URLs de categorías, búsquedas u ofertas; mejor que la home del sitio.")
    pages = st.slider("Páginas por URL", 1, 10, 2)
    max_scrolls = st.slider("Scrolls para cargar productos", 0, 12, 5)
    timeout_seconds = st.slider("Timeout por página (segundos)", 20, 120, 60, step=10)
    start = st.button("🚀 Ejecutar extracción", type="primary", use_container_width=True)
    st.caption("La extracción usa memoria temporal. No se escribe un dataset en disco ni en una base de datos.")

if "products_df" not in st.session_state:
    st.session_state.products_df = pd.DataFrame()
if "scrape_errors" not in st.session_state:
    st.session_state.scrape_errors = []

if start:
    urls = []
    for raw in urls_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw
        if urlparse(raw).netloc:
            urls.append(raw)
    if not urls:
        st.error("Ingresá al menos una URL válida.")
    else:
        progress = st.progress(0, text="Preparando navegador…")
        status = st.empty()
        def update_progress(value, message):
            progress.progress(min(1.0, max(0.0, value)), text=message)
        with st.spinner("Visitando los sitios y recopilando productos…"):
            try:
                rows, errors = run_scrape(urls, pages, max_scrolls, timeout_seconds * 1000, update_progress)
                st.session_state.products_df = pd.DataFrame(rows)
                st.session_state.scrape_errors = errors
            except Exception as exc:
                st.error(f"No se pudo iniciar la extracción: {type(exc).__name__}: {exc}")
        progress.empty()
        status.empty()
        if not st.session_state.products_df.empty:
            st.success(f"Extracción terminada: {len(st.session_state.products_df):,} productos únicos en memoria.")
        else:
            st.warning("No se encontraron productos. Probá con una URL de categoría/listado, menos páginas o revisá si el sitio bloquea automatizaciones.")

df = st.session_state.products_df.copy()
if df.empty:
    st.info("Pegá una o más URLs de listados en la barra lateral y ejecutá la extracción. Los resultados aparecerán acá y podrás filtrarlos y descargarlos.")
    st.stop()

# Normalización para filtros, métricas y exportación
for col in ["sitio", "sku", "producto", "marca", "categoria", "disponibilidad", "url_producto", "imagen", "fuente_dato", "moneda"]:
    if col not in df.columns:
        df[col] = None
for col in ["precio", "precio_lista"]:
    if col not in df.columns:
        df[col] = None
    df[col] = pd.to_numeric(df[col], errors="coerce")

st.subheader("Resumen de resultados")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Productos únicos", f"{len(df):,}")
c2.metric("Sitios", f"{df['sitio'].nunique():,}")
c3.metric("Con precio", f"{df['precio'].notna().sum():,}")
c4.metric("Con SKU", f"{df['sku'].notna().sum():,}")

with st.expander("🔎 Filtros de exploración", expanded=True):
    a, b, c, d = st.columns(4)
    sites = sorted(df["sitio"].dropna().astype(str).unique().tolist())
    brands = sorted(df["marca"].dropna().astype(str).unique().tolist())
    selected_sites = a.multiselect("Sitio", sites, default=sites)
    selected_brands = b.multiselect("Marca", brands)
    query = c.text_input("Buscar producto / SKU", placeholder="Ej. heladera, Samsung, 12345")
    price_col = d.selectbox("Ordenar por", ["precio ascendente", "precio descendente", "nombre A–Z", "sitio", "sin ordenar"])
    p1, p2 = st.columns(2)
    available_prices = df["precio"].dropna()
    if not available_prices.empty:
        min_price, max_price = float(available_prices.min()), float(available_prices.max())
        if min_price < max_price:
            price_range = p1.slider("Rango de precio", min_value=min_price, max_value=max_price, value=(min_price, max_price))
        else:
            price_range = (min_price, max_price)
            p1.caption(f"Precio detectado: {min_price:,.2f}")
    else:
        price_range = None
        p1.caption("No hay precios numéricos para filtrar.")
    only_with_price = p2.checkbox("Solo productos con precio", value=False)

filtered = df.copy()
if selected_sites:
    filtered = filtered[filtered["sitio"].astype(str).isin(selected_sites)]
if selected_brands:
    filtered = filtered[filtered["marca"].astype(str).isin(selected_brands)]
if query.strip():
    needle = query.strip().lower()
    searchable = filtered[["producto", "sku", "marca", "categoria"]].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
    filtered = filtered[searchable.str.contains(re.escape(needle), na=False)]
if price_range:
    filtered = filtered[filtered["precio"].isna() | filtered["precio"].between(price_range[0], price_range[1])]
if only_with_price:
    filtered = filtered[filtered["precio"].notna()]
if price_col == "precio ascendente":
    filtered = filtered.sort_values("precio", na_position="last")
elif price_col == "precio descendente":
    filtered = filtered.sort_values("precio", ascending=False, na_position="last")
elif price_col == "nombre A–Z":
    filtered = filtered.sort_values("producto", na_position="last")
elif price_col == "sitio":
    filtered = filtered.sort_values("sitio", na_position="last")

st.subheader(f"Productos · {len(filtered):,} resultados")
view = st.radio("Vista", ["Tabla", "Tarjetas"], horizontal=True, label_visibility="collapsed")
if view == "Tabla":
    show_cols = [c for c in ["sitio", "sku", "producto", "marca", "precio", "precio_lista", "moneda", "disponibilidad", "categoria", "url_producto", "fuente_dato"] if c in filtered.columns]
    st.dataframe(filtered[show_cols], use_container_width=True, hide_index=True, column_config={
        "precio": st.column_config.NumberColumn("Precio", format="$ %.2f"),
        "precio_lista": st.column_config.NumberColumn("Precio de lista", format="$ %.2f"),
        "url_producto": st.column_config.LinkColumn("Producto", display_text="Abrir producto"),
    })
else:
    for start_idx in range(0, min(len(filtered), 60), 3):
        cols = st.columns(3)
        for col, (_, row) in zip(cols, filtered.iloc[start_idx:start_idx+3].iterrows()):
            with col:
                with st.container(border=True):
                    if isinstance(row.get("imagen"), str) and row["imagen"].startswith("http"):
                        st.image(row["imagen"], use_container_width=True)
                    st.markdown(f"**{row.get('producto') or 'Producto sin nombre'}**")
                    st.caption(f"{row.get('marca') or 'Marca sin identificar'} · {row.get('sitio') or ''}")
                    if pd.notna(row.get("precio")):
                        st.markdown(f"### ${row['precio']:,.2f}")
                    else:
                        st.markdown("### Precio no detectado")
                    if row.get("sku"):
                        st.caption(f"SKU: {row['sku']}")
                    if isinstance(row.get("url_producto"), str) and row["url_producto"].startswith("http"):
                        st.link_button("Ver producto ↗", row["url_producto"], use_container_width=True)
    if len(filtered) > 60:
        st.caption("La vista de tarjetas muestra los primeros 60 productos; la descarga incluye todos los resultados filtrados.")

st.divider()
st.subheader("Descargar resultados")
left, right = st.columns(2)
with left:
    st.download_button("⬇️ Descargar CSV", data=csv_bytes(filtered), file_name="productos_ecommerce.csv", mime="text/csv", use_container_width=True)
with right:
    st.download_button("⬇️ Descargar Excel", data=excel_bytes(filtered), file_name="productos_ecommerce.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
st.caption("Los archivos se generan en memoria al preparar la descarga; la app no persiste los resultados en una base de datos ni en archivos propios.")

if st.session_state.scrape_errors:
    with st.expander(f"⚠️ Detalles técnicos ({len(st.session_state.scrape_errors)})"):
        for err in st.session_state.scrape_errors[:40]:
            st.code(err)
        if len(st.session_state.scrape_errors) > 40:
            st.caption(f"Se muestran 40 de {len(st.session_state.scrape_errors)} avisos.")
