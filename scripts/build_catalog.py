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
    "label": ["prod_desc_label", "наименование на упаковке", "описание на упаковке"],
    "inn": ["prod_name", "мнн", "международное непатентованное наименование", "наименование действующего вещества"],
    "form": ["type_form", "лекарственная форма", "форма выпуска", "форма препарата", "dosage form"],
    "pack": ["prod_pack1_ed_name", "количество (мера", "количество в первичной упаковке", "описание упаковки", "первичная упаковка", "packaging"],
    "content": ["prod_content", "содержимое лекарственного препарата", "состав/содержимое", "содержимое"],
    "description": ["prod_desc", "описание лекарственного препарата", "описание препарата", "описание"],
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

def category_for(form: str, name: str, pack: str, label: str = "", content: str = "", description: str = "") -> str:
    """Infer the app's inventory unit from the registered dosage form first."""
    def clean(value: str) -> str:
        return str(value or "").lower().replace("ё", "е")

    form_text = clean(form)
    package_text = clean(" ".join([pack, label, content, description]))
    all_text = " ".join([form_text, clean(name), package_text])

    # Explicit packaging/form clues take priority over words in a trade name.
    if re.search(r"ампул", form_text + " " + package_text): return "ампулы"
    if re.search(r"спре[йя]|аэрозол", form_text): return "спреи"
    if re.search(r"маз[ьи]|крем|гел[ьи]|паст|линимент", form_text): return "мази"
    if re.search(r"порош|гранул|саше|пакетик", form_text): return "пакетики"
    if re.search(r"капл|раствор|сироп|суспенз|эмульс|микстур|концентрат|настойк", form_text): return "емкости"
    if re.search(r"таблет|капсул|драже", form_text): return "таблетки"

    if re.search(r"спре[йя]|аэрозол", all_text): return "спреи"
    if re.search(r"маз[ьи]|крем|гел[ьи]|паст|линимент", all_text): return "мази"
    if re.search(r"порош|гранул|саше|пакетик", all_text): return "пакетики"
    if re.search(r"капл|раствор|сироп|суспенз|эмульс|микстур|концентрат|настойк|флакон|бутыл", all_text): return "емкости"
    if re.search(r"таблет|капсул|драже", all_text): return "таблетки"
    return ""


def package_count(category: str, package_text: str) -> int | None:
    """Extract only explicit units-per-pack counts; never treat mg/ml as a count."""
    text = str(package_text or "").lower().replace("ё", "е").replace("×", "x")
    if not text:
        return None

    unit = {
        "таблетки": r"таблет(?:ка|ки|ок)?|капсул(?:а|ы)?|драже",
        "пакетики": r"пакетик(?:а|и|ов)?|пакет(?:а|ы|ов)?|саше",
        "ампулы": r"ампул(?:а|ы)?",
    }.get(category)
    if not unit:
        return None

    # e.g. 3 блистера по 10 таблеток, 2x10 таблеток, 2 х 10 капсул.
    m = re.search(rf"(?<!\d)(\d{{1,3}})\s*(?:блистер\w*|упаковк\w*)\s*(?:по\s*)?(\d{{1,3}})\s*(?:{unit})\b", text)
    if m:
        value = int(m.group(1)) * int(m.group(2))
        return value if 0 < value <= 10000 else None
    m = re.search(rf"(?<!\d)(\d{{1,3}})\s*x\s*(\d{{1,3}})\s*(?:{unit})\b", text)
    if m:
        value = int(m.group(1)) * int(m.group(2))
        return value if 0 < value <= 10000 else None

    # Explicit count next to the dosage form, in either word order.
    patterns = [
        rf"(?<!\d)(\d{{1,4}})\s*(?:{unit})\b",
        rf"(?:{unit})\s*(?:№|n\.?\s*)\s*(\d{{1,4}})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            value = int(m.group(1))
            return value if 0 < value <= 10000 else None

    # A package number such as "таблетки 500 мг № 20" is safe only when
    # the same source text explicitly identifies tablets/capsules/dragees.
    if category == "таблетки" and re.search(r"таблет|капсул|драже", text):
        m = re.search(r"(?:№\s*|n\.?\s*)(\d{1,4})\b", text)
        if m:
            value = int(m.group(1))
            return value if 0 < value <= 10000 else None
    return None


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
        label = get_field(row, "label")
        content = get_field(row, "content")
        description = get_field(row, "description")
        category = category_for(form, name, pack, label, content, description)
        package_text = " ".join(x for x in [pack, label, content, description] if x)
        item = {"name": name, "form": form, "category": category, "packageInfo": pack}
        count = package_count(category, package_text)
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
