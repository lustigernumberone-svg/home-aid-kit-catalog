#!/usr/bin/env python3
"""Build a small GTIN-sharded medicine catalogue from the public Chestny ZNAK dataset.
No credentials, paid APIs or private MDLP endpoints are used.
"""
from __future__ import annotations
import csv, datetime as dt, io, json, re, sys, time
from pathlib import Path
from urllib.parse import urljoin
import requests

DATASET_PAGE = "https://датамаркет.честныйзнак.рф/bi/opendata/7731376812-MDLPGtins"
OUT = Path("public/catalog")
HEADERS = {"User-Agent": "Home-AID-Kit-open-data-catalog/1.0 (+GitHub Actions)"}

ALIASES = {
    "gtin": ["gtin", "код gtin", "гтин", "global trade item number"],
    "name": ["prod_sell_name", "торговое наименование лекарственного препарата", "торговое наименование", "prod_desc_label", "наименование на упаковке", "наименование товара", "название препарата", "product name"],
    "inn": ["prod_name", "мнн", "международное непатентованное наименование", "наименование действующего вещества"],
    "form": ["type_form", "лекарственная форма", "форма выпуска", "форма препарата", "dosage form"],
    "pack": ["prod_pack1_ed_name", "количество (мера", "количество в первичной упаковке", "описание упаковки", "первичная упаковка", "packaging"],
}

def norm(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(s or "").lower().replace("ё", "е"))

def get_field(row: dict, key: str) -> str:
    aliases = [norm(x) for x in ALIASES[key]]
    # Prefer the explicitly registered trade name over the generic label/INN.
    for alias in aliases:
        for header, value in row.items():
            h = norm(header)
            if (h == alias or (len(alias) >= 7 and alias in h)) and value and str(value).strip():
                return str(value).strip()
    return ""

def find_dataset_url(html: str) -> str | None:
    # The open-data landing page exposes the latest CSV link. The fallback below
    # handles short delays between publishing a new page and a new CSV file.
    matches = re.findall(r"(?:href=[\"'])([^\"']*data-\d{8}-structure-\d+\.csv[^\"']*)", html, re.I)
    if matches:
        return urljoin(DATASET_PAGE, matches[0].replace("&amp;", "&"))
    matches = re.findall(r"(?:https?://[^\s\"']+|/[^\s\"']*)data-\d{8}-structure-\d+\.csv", html, re.I)
    if matches:
        return urljoin(DATASET_PAGE, matches[0])
    # The currently published structure version is stable; try the latest dates.
    base = "https://датамаркет.честныйзнак.рф/bi/api/opendata/7731376812-MDLPGtins/data/"
    today = dt.date.today()
    for offset in range(0, 15):
        day = (today - dt.timedelta(days=offset)).strftime("%Y%m%d")
        candidate = f"{base}data-{day}-structure-20240611.csv"
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=30)
            if r.ok and len(r.content) > 100:
                return candidate
        except requests.RequestException:
            pass
    return None

def category_for(form: str, name: str, pack: str) -> str:
    text = " ".join([form, name, pack]).lower().replace("ё", "е")
    if re.search(r"ампул", text): return "ампулы"
    if re.search(r"спре[йя]|аэрозол", text): return "спреи"
    if re.search(r"маз[ьи]|крем|гел[ьи]|паст", text): return "мази"
    if re.search(r"таблет|капсул|драже", text): return "таблетки"
    if re.search(r"порош|гранул|саше|пакетик", text): return "пакетики"
    if re.search(r"раствор|сироп|капл|суспенз|флакон|бутыл|капельниц", text): return "емкости"
    return ""

def package_count(category: str, pack: str, form: str) -> int | None:
    if not pack: return None
    patterns = {
        "таблетки": r"(?<!\d)(\d{1,4})\s*(?:таблет(?:ок|ки|ка)?|капсул(?:а|ы)?|драже)\b",
        "пакетики": r"(?<!\d)(\d{1,4})\s*(?:пакетик(?:ов|а)?|пакет(?:ов|а)?|саше)\b",
        "ампулы": r"(?<!\d)(\d{1,4})\s*ампул(?:а|ы)?\b",
    }
    pattern = patterns.get(category)
    if not pattern: return None
    m = re.search(pattern, (pack + " " + form).lower().replace("ё", "е"))
    if not m: return None
    value = int(m.group(1))
    return value if 0 < value <= 10000 else None

def parse_csv(content: bytes):
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try: dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
    except csv.Error: dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise RuntimeError("В CSV не найдена строка заголовков.")
    return reader.fieldnames, reader

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session(); session.headers.update(HEADERS)
    page = session.get(DATASET_PAGE, timeout=45); page.raise_for_status()
    url = find_dataset_url(page.text)
    if not url:
        raise RuntimeError("Не удалось определить ссылку на актуальный CSV Честного знака.")
    print(f"Источник: {url}")
    response = session.get(url, timeout=180); response.raise_for_status()
    headers, rows = parse_csv(response.content)
    print("Колонки CSV:", " | ".join(headers))
    shards: dict[str, dict] = {}
    total = 0
    for row in rows:
        gtin = re.sub(r"\D", "", get_field(row, "gtin"))
        if len(gtin) not in (8, 12, 13, 14): continue
        gtin = gtin.zfill(14)
        name = get_field(row, "name") or get_field(row, "inn")
        if not name: continue
        form = get_field(row, "form")
        pack = get_field(row, "pack")
        category = category_for(form, name, pack)
        item = {"name": name, "form": form, "category": category, "packageInfo": pack}
        count = package_count(category, pack, form)
        if count is not None: item["packageCount"] = count
        # Deliberately no dose/expiry fields: this open catalogue cannot guarantee
        # dosage for every GTIN or the expiry date of a specific physical pack.
        shard = gtin[:3]
        shards.setdefault(shard, {})[gtin] = item
        total += 1
    if not total:
        raise RuntimeError("Не удалось извлечь записи GTIN. Проверьте формат CSV/названия колонок; каталог не опубликован.")
    for old in OUT.glob("*.json"): old.unlink()
    for shard, data in shards.items():
        (OUT / f"{shard}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    meta = {
        "source": DATASET_PAGE,
        "sourceFile": url,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "gtinCount": total,
        "shardCount": len(shards),
        "dosageIncluded": False,
        "expiryIncluded": False,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово: {total} GTIN, {len(shards)} частей")

if __name__ == "__main__":
    main()
