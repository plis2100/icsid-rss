"""Genera un RSS con cambios públicos en los expedientes del CIADI."""

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from lxml import html


BASE = "https://icsid.worldbank.org"
DATABASE = f"{BASE}/cases/case-database"
STATE = Path("estado-expedientes.json")
FEED = Path("rss-expedientes.xml")
CLOSED_PER_RUN = 120
FEED_LIMIT = 400


def download(url):
    request = Request(url, headers={
        "User-Agent": "ICSID-RSS-Expedientes/1.0",
        "Accept": "application/json,text/html",
    })
    with urlopen(request, timeout=18) as response:
        return response.read()


def clean(text):
    return " ".join((text or "").split())


def case_url(number):
    return f"{DATABASE}/case-detail?{urlencode({'CaseNo': number})}"


def listing(payload):
    cases = {}
    for row in json.loads(payload)["data"]["GetAllCasesResult"]:
        number = clean(row["caseno"])
        status = clean(row["status"])
        if not number or not status or number in cases:
            raise ValueError(f"Fila de expediente incompleta o duplicada: {number}")
        registration = next(
            (p["dateregistered"] for p in row.get("caseproceedings", [])
             if p.get("dateregistered")), ""
        )
        cases[number] = {
            "claimant": clean(row["claimant"]),
            "respondent": clean(row["respondent"]),
            "status": status,
            "registered": registration.split(" ", 1)[0],
        }
    if not cases:
        raise ValueError("El catálogo del CIADI está vacío")
    return cases


def case_events(payload, number):
    root = html.fromstring(payload)
    heading = root.xpath('//*[@id="block-icsidcasedetailblock"]//h1')
    chronology = root.xpath('//*[@id="procedural_details"]')
    materials = root.xpath('//*[@id="materials"]')
    if not (heading and number in heading[0].text_content() and chronology and materials):
        raise ValueError(f"Ficha incompleta de {number}")

    for row in chronology[0].xpath(".//table//tr[td]"):
        cells = row.xpath("./td")
        if len(cells) < 2:
            continue
        date, description = (clean(cell.text_content()) for cell in cells[:2])
        if not description:
            continue
        section = row.xpath("preceding::h3[ancestor::*[@id='procedural_details']][1]")
        procedure = clean(section[0].text_content()) if section else ""
        yield (
            ["actuación", procedure, date, description],
            "Actuación procesal", description, date, case_url(number) + "#procedural_details",
        )

    for link in materials[0].xpath(".//p[a[@href]]/a[@href]"):
        url = urljoin(BASE, link.get("href"))
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        paragraph = link.getparent()
        title_parts = paragraph.xpath("text() | .//text()[not(ancestor::a) and not(parent::p)]")
        title = clean(" ".join(title_parts))
        label = clean(link.text_content())
        date_match = re.search(r"\(([A-Za-z]+ \d{1,2}, \d{4})\)", title)
        date = date_match.group(1) if date_match else ""
        yield (
            ["documento", url], "Documento publicado",
            " — ".join(part for part in (title, label) if part) or url,
            date, url,
        )


def event_id(number, identity):
    raw = json.dumps([number, identity], ensure_ascii=False).encode()
    return "icsid-expediente-" + hashlib.sha256(raw).hexdigest()


def headline(kind, description, number, case):
    parties = " v. ".join(filter(None, (case["claimant"], case["respondent"])))
    short = description if len(description) <= 150 else description[:147].rstrip() + "…"
    return f"CIADI | {number} | {parties or number} | {kind} | {short}"


def add_item(items, number, case, identity, kind, description, date, url, published):
    guid = event_id(number, identity)
    items[guid] = {
        "title": headline(kind, description, number, case),
        "link": url,
        "category": kind,
        "description": f"{description}\nFecha indicada por el CIADI: {date}." if date else description,
        "published": published.isoformat(),
    }


def update_catalog(state, current, now):
    previous = state["catalog"]
    for number, case in current.items():
        old = previous.get(number)
        if old == case:
            continue
        if old is None:
            if previous:
                add_item(
                    state["items"], number, case, ["registro"], "Caso registrado",
                    f"Estado: {case['status']}", case["registered"], case_url(number), now,
                )
            continue
        fields = {"status": "Estado", "claimant": "Demandante", "respondent": "Demandado"}
        changes = [
            f"{label}: {old[key] or '—'} → {case[key] or '—'}"
            for key, label in fields.items() if old[key] != case[key]
        ]
        if changes:
            kind = "Cambio de estado" if old["status"] != case["status"] else "Cambio de partes"
            add_item(
                state["items"], number, case, [kind, now.isoformat()], kind,
                " · ".join(changes), "", case_url(number), now,
            )
    previous.update(current)


def read_case(number):
    return list(case_events(download(case_url(number)), number))


def update_details(state, current, now):
    details = state["details"]
    pending = [n for n, case in current.items() if case["status"] != "Concluded"]
    closed = [n for n, case in current.items() if case["status"] == "Concluded"]
    closed.sort(key=lambda n: state["attempts"].get(n, ""))
    selected = pending + closed[:CLOSED_PER_RUN]
    errors = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(read_case, number): number for number in selected}
        for job in as_completed(jobs):
            number = jobs[job]
            state["attempts"][number] = now.isoformat()
            try:
                events = job.result()
            except (OSError, ValueError) as error:
                errors += 1
                print(f"Ficha pendiente de reintento {number}: {error}")
                if isinstance(error, HTTPError) and error.code in {403, 429}:
                    for future in jobs:
                        future.cancel()
                    raise RuntimeError(f"El CIADI rechazó las consultas: HTTP {error.code}") from error
                continue
            old = details.get(number)
            seen = set(old["seen"]) if old else set()
            for identity, kind, description, date, url in events:
                guid = event_id(number, identity)
                if old is not None and guid not in seen:
                    add_item(
                        state["items"], number, current[number], identity, kind,
                        description, date, url, now,
                    )
                seen.add(guid)
            details[number] = {"seen": sorted(seen)}
    print(f"Fichas leídas: {len(selected) - errors}/{len(selected)}")
    if selected and errors == len(selected):
        raise RuntimeError("No se pudo leer ninguna ficha")


def rss_bytes(items):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "CIADI — Cambios en expedientes"
    ET.SubElement(channel, "link").text = DATABASE
    ET.SubElement(channel, "description").text = "Altas, actuaciones, documentos y cambios de estado en expedientes del CIADI."
    ET.SubElement(channel, "language").text = "es"
    for guid, item in items:
        node = ET.SubElement(channel, "item")
        for field in ("title", "link", "category", "description"):
            ET.SubElement(node, field).text = item[field]
        ET.SubElement(node, "guid", isPermaLink="false").text = guid
        ET.SubElement(node, "pubDate").text = format_datetime(datetime.fromisoformat(item["published"]))
    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True) + b"\n"


def atomic_write(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    current = listing(download(f"{BASE}/api/all/cases"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {
        "catalog": {}, "details": {}, "items": {}, "attempts": {},
    }
    if len(current) < len(state["catalog"]) * 0.90:
        raise RuntimeError("El catálogo ha caído más del 10 %. Se conserva la última versión.")
    update_catalog(state, current, now)
    update_details(state, current, now)
    items = sorted(
        state["items"].items(), key=lambda pair: (pair[1]["published"], pair[0]), reverse=True
    )[:FEED_LIMIT]
    state["items"] = dict(items)
    atomic_write(FEED, rss_bytes(items))
    atomic_write(STATE, (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode())
    print(f"Expedientes del catálogo: {len(current)}. Entradas RSS: {len(items)}")


if __name__ == "__main__":
    main()
