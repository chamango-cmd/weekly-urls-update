# sitemap-scraper

Recorre semanalmente el sitemap de https://www.muchoneumatico.com/blog/,
visita cada URL con un navegador headless (Playwright, necesario para
sortear el WAF de Cloudflare del sitio) y escribe Title / H1 / Meta
Description de cada página en una pestaña de Google Sheets. Se ejecuta
automáticamente vía GitHub Actions (cron semanal) o a mano desde la
pestaña Actions del repo ("Run workflow").

## Estructura

- `scripts/scrape_and_update_sheet.py` — script principal.
- `.github/workflows/weekly-sitemap-sync.yml` — workflow de GitHub Actions.
- `requirements.txt` — dependencias Python.

## Configuración (una sola vez)

1. Crea una cuenta de servicio de Google Cloud con la API de Sheets y
   Drive habilitadas, y descarga su clave JSON.
2. Comparte tu Google Sheet (permiso Editor) con el email de la cuenta
   de servicio (campo `client_email` del JSON).
3. En el repo de GitHub: Settings → Secrets and variables → Actions →
   añade dos secretos:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: el contenido completo del JSON.
   - `GOOGLE_SHEET_ID`: el ID de tu hoja (la parte de la URL entre
     `/d/` y `/edit`).
4. Ve a la pestaña **Actions** del repo → selecciona el workflow →
   **Run workflow** para probarlo manualmente.

## Ejecución local (opcional, para depurar)

```bash
pip install -r requirements.txt
playwright install --with-deps chromium

export SITEMAP_INDEX_URL="https://www.muchoneumatico.com/blog/sitemap_index.xml"
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat ruta/a/tu-clave.json)"
export GOOGLE_SHEET_ID="tu_sheet_id"

python scripts/scrape_and_update_sheet.py
```
