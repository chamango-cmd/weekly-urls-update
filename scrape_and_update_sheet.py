#!/usr/bin/env python3
"""
Recorre un sitemap_index.xml (con Playwright, para sortear el WAF de
Cloudflare), extrae Title / H1 / Meta Description de cada URL, y escribe
el resultado en una hoja de Google Sheets.

Variables de entorno requeridas:
    SITEMAP_INDEX_URL              URL del sitemap_index.xml
    GOOGLE_SERVICE_ACCOUNT_JSON    Contenido completo del JSON de la cuenta
                                    de servicio (como texto)
    GOOGLE_SHEET_ID                ID de la hoja de cálculo (de la URL)
    SHEET_TAB_NAME                 (opcional) nombre de la pestaña, por
                                    defecto "Sitemap URLs"
    CONCURRENCY                    (opcional) páginas en paralelo, por
                                    defecto 5
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from xml.etree import ElementTree

import gspread
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SITEMAP_INDEX_URL = os.environ["SITEMAP_INDEX_URL"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_TAB_NAME = os.environ.get("SHEET_TAB_NAME", "Sitemap URLs")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "5"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def get_sheet():
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB_NAME, rows=10, cols=6)
    return ws


# ---------------------------------------------------------------------------
# Extracción de datos HTML
# ---------------------------------------------------------------------------
def extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return decode_entities(m.group(1).strip()) if m else ""


def extract_h1(html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return decode_entities(text)


def extract_meta_description(html: str) -> str:
    m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']\s*/?>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        m = re.search(
            r'<meta\s+content=["\'](.*?)["\']\s+name=["\']description["\']\s*/?>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
    return decode_entities(m.group(1).strip()) if m else ""


def decode_entities(text: str) -> str:
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
        .replace("&nbsp;", " ")
    )


# ---------------------------------------------------------------------------
# Sitemap (vía Playwright, cuerpo crudo de la respuesta)
# ---------------------------------------------------------------------------
async def fetch_xml(page, url: str) -> ElementTree.Element:
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if resp is None or resp.status >= 400:
        status = resp.status if resp else "sin respuesta"
        raise RuntimeError(f"HTTP {status} al descargar {url}")
    raw = await resp.body()
    return ElementTree.fromstring(raw)


def get_urls_from_urlset(root) -> list[str]:
    return [el.text.strip() for el in root.findall(".//sm:url/sm:loc", NAMESPACE) if el.text]


def get_sub_sitemaps(root) -> list[str]:
    return [el.text.strip() for el in root.findall(".//sm:sitemap/sm:loc", NAMESPACE) if el.text]


async def collect_all_urls(page, sitemap_index_url: str) -> list[str]:
    root = await fetch_xml(page, sitemap_index_url)
    tag = root.tag.split("}")[-1]

    if tag == "sitemapindex":
        sub_sitemaps = get_sub_sitemaps(root)
        print(f"[i] {len(sub_sitemaps)} sub-sitemaps encontrados", file=sys.stderr)
        all_urls: list[str] = []
        for sm_url in sub_sitemaps:
            try:
                sub_root = await fetch_xml(page, sm_url)
                urls = get_urls_from_urlset(sub_root)
                print(f"[i] {sm_url} -> {len(urls)} URLs", file=sys.stderr)
                all_urls.extend(urls)
            except Exception as e:
                print(f"[!] Error con {sm_url}: {e}", file=sys.stderr)
        return all_urls
    elif tag == "urlset":
        return get_urls_from_urlset(root)
    else:
        raise RuntimeError(f"Tag raíz de sitemap no reconocido: {tag}")


# ---------------------------------------------------------------------------
# Scraping de cada página (con concurrencia limitada)
# ---------------------------------------------------------------------------
async def scrape_page(context, url: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is None:
                return {"url": url, "error": "sin respuesta"}
            if resp.status >= 400:
                return {"url": url, "error": f"HTTP {resp.status}"}
            html = await page.content()
            return {
                "url": url,
                "title": extract_title(html),
                "h1": extract_h1(html),
                "description": extract_meta_description(html),
            }
        except Exception as e:
            return {"url": url, "error": str(e)}
        finally:
            await page.close()


async def scrape_all_pages(context, urls: list[str]) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    tasks = [scrape_page(context, u, semaphore) for u in urls]
    results = []
    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        result = await coro
        results.append(result)
        if i % 25 == 0 or i == len(urls):
            print(f"[i] Procesadas {i}/{len(urls)} páginas", file=sys.stderr)
    # as_completed no preserva el orden; lo reordenamos por URL de entrada
    order = {u: idx for idx, u in enumerate(urls)}
    results.sort(key=lambda r: order.get(r["url"], 0))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, locale="es-ES")

        index_page = await context.new_page()
        urls = await collect_all_urls(index_page, SITEMAP_INDEX_URL)
        await index_page.close()
        print(f"[i] Total de URLs a procesar: {len(urls)}", file=sys.stderr)

        results = await scrape_all_pages(context, urls)
        await browser.close()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = [["URL", "Title", "H1", "Meta Description", "Estado", "Última comprobación"]]
    for r in results:
        if "error" in r:
            rows.append([r["url"], "", "", "", f"Error: {r['error']}", now])
        else:
            rows.append([r["url"], r["title"], r["h1"], r["description"], "OK", now])

    print("[i] Escribiendo en Google Sheets...", file=sys.stderr)
    ws = get_sheet()
    ws.clear()
    ws.update(values=rows, range_name="A1")
    print(f"[✓] {len(rows) - 1} filas escritas en la pestaña '{SHEET_TAB_NAME}'", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
