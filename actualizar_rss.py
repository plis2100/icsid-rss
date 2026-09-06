from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


BASE = "https://icsid.worldbank.org"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"

PAGINAS_CASOS = {
    "Caso registrado": f"{BASE}/cases/recent?type=registered",
    "Documento publicado": f"{BASE}/cases/recent?type=published",
    "Caso concluido": f"{BASE}/cases/recent?type=concluded",
    "Tribunal constituido": f"{BASE}/cases/recent?type=constituted",
}

SALIDA = Path("rss.xml")
ESTADO = Path("estado.json")

MAXIMO_ENTRADAS_RSS = 2000
MAXIMO_PAGINAS_ESTADO = 30000
MAXIMO_EVENTOS_ESTADO = 30000
MAXIMO_PROCESADOS_POR_EJECUCION = 200

PAGINAS_INICIALES = 100
CASOS_INICIALES_POR_TIPO = 15

TRABAJADORES = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
)

CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml,"
        "application/rss+xml,text/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8,fr;q=0.7",
    "Cache-Control": "no-cache",
}

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
}


def descargar(url: str, timeout: int = 90) -> bytes:
    peticion = urllib.request.Request(
        url,
        headers=CABECERAS,
    )

    with urllib.request.urlopen(
        peticion,
        timeout=timeout,
    ) as respuesta:
        contenido = respuesta.read()

    if not contenido:
        raise RuntimeError(f"Respuesta vacía: {url}")

    return contenido


def limpiar_url(url: str) -> str:
    url = urljoin(BASE, url.strip())

    if url.startswith("http://icsid.worldbank.org"):
        url = url.replace(
            "http://icsid.worldbank.org",
            BASE,
            1,
        )

    return url.split("#", 1)[0]


def convertir_fecha(fecha: str) -> datetime:
    if not fecha:
        return datetime.now(timezone.utc)

    fecha = fecha.strip().replace("Z", "+00:00")

    try:
        resultado = datetime.fromisoformat(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except ValueError:
        pass

    formatos = (
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%d",
    )

    for formato in formatos:
        try:
            return datetime.strptime(
                fecha,
                formato,
            ).replace(tzinfo=timezone.utc)

        except ValueError:
            continue

    try:
        resultado = email.utils.parsedate_to_datetime(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def fecha_rss(fecha: datetime) -> str:
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)

    return email.utils.format_datetime(fecha)


def es_contenido_editorial(url: str) -> bool:
    ruta = urlparse(url).path.lower()

    rutas_admitidas = (
        "/news-and-events/",
        "/es/noticias-y-eventos/",
        "/fr/actualites-et-evenements/",
        "/resources/",
        "/es/recursos/",
        "/fr/ressources/",
        "/cases/content/",
        "/es/cases/content/",
        "/fr/cases/content/",
    )

    if not any(
        ruta.startswith(inicio)
        for inicio in rutas_admitidas
    ):
        return False

    exclusiones = (
        "/search",
        "/node/",
        "/user/",
        "/contact",
        "/contacts",
    )

    return not any(
        exclusion in ruta
        for exclusion in exclusiones
    )


def localizar_sitemaps() -> list[str]:
    contenido = descargar(SITEMAP_INDEX)
    raiz = ET.fromstring(contenido)

    sitemaps: list[str] = []

    for nodo in raiz.findall("sm:sitemap", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        ).strip()

        if url:
            sitemaps.append(limpiar_url(url))

    if not sitemaps:
        raise RuntimeError(
            "ICSID no devolvió sus sitemaps"
        )

    return sitemaps


def leer_sitemap(
    url_sitemap: str,
) -> dict[str, dict]:
    resultados: dict[str, dict] = {}

    try:
        contenido = descargar(url_sitemap)
        raiz = ET.fromstring(contenido)

    except Exception as error:
        print(
            f"No se pudo leer {url_sitemap}: {error}"
        )
        return resultados

    for nodo in raiz.findall("sm:url", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        )

        url = limpiar_url(url)

        if not es_contenido_editorial(url):
            continue

        ultima_modificacion = nodo.findtext(
            "sm:lastmod",
            default="",
            namespaces=NS,
        ).strip()

        resultados[url] = {
            "url": url,
            "ultima_modificacion": ultima_modificacion,
        }

    return resultados


def localizar_paginas() -> dict[str, dict]:
    sitemaps = localizar_sitemaps()
    paginas: dict[str, dict] = {}

    with ThreadPoolExecutor(
        max_workers=min(
            TRABAJADORES,
            len(sitemaps),
        )
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                leer_sitemap,
                sitemap,
            ): sitemap
            for sitemap in sitemaps
        }

        for trabajo in as_completed(trabajos):
            paginas.update(trabajo.result())

    print(
        f"Páginas editoriales localizadas: "
        f"{len(paginas)}"
    )

    return paginas


def generar_guid_evento(
    tipo: str,
    fecha: str,
    titulo: str,
    url: str,
) -> str:
    contenido = "|".join(
        [
            tipo.strip(),
            fecha.strip(),
            titulo.strip(),
            url.strip(),
        ]
    )

    codigo = hashlib.sha256(
        contenido.encode("utf-8")
    ).hexdigest()

    return f"icsid-event-{codigo}"


def leer_eventos_casos(
    tipo: str,
    url: str,
) -> list[dict]:
    eventos: list[dict] = []

    try:
        contenido = descargar(url)
        sopa = BeautifulSoup(contenido, "html.parser")

        for bloque in sopa.select(
            ".recent-listing .listing"
        ):
            titulo_nodo = bloque.select_one(
                ".case-title"
            )

            if not titulo_nodo:
                continue

            titulo = " ".join(
                titulo_nodo.get_text(
                    " ",
                    strip=True,
                ).split()
            )

            enlace = titulo_nodo.find(
                "a",
                href=True,
            )

            if not titulo or not enlace:
                continue

            direccion = limpiar_url(enlace["href"])

            fecha_nodo = bloque.select_one(".meta")
            fecha_texto = ""

            if fecha_nodo:
                fecha_texto = fecha_nodo.get_text(
                    " ",
                    strip=True,
                )

            guid = generar_guid_evento(
                tipo,
                fecha_texto,
                titulo,
                direccion,
            )

            eventos.append(
                {
                    "guid": guid,
                    "url": direccion,
                    "titulo": f"{tipo}: {titulo}",
                    "descripcion": (
                        f"ICSID ha incorporado una actualización "
                        f"clasificada como «{tipo}». "
                        f"Fecha publicada: "
                        f"{fecha_texto or 'no indicada'}."
                    ),
                    "fecha": convertir_fecha(fecha_texto),
                    "categoria": tipo,
                    "autor": "ICSID",
                    "imagen": "",
                }
            )

    except Exception as error:
        print(
            f"No se pudo leer {tipo}: {error}"
        )

    return eventos


def localizar_eventos_casos() -> dict[str, list[dict]]:
    resultados: dict[str, list[dict]] = {}

    with ThreadPoolExecutor(
        max_workers=4
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                leer_eventos_casos,
                tipo,
                url,
            ): tipo
            for tipo, url in PAGINAS_CASOS.items()
        }

        for trabajo in as_completed(trabajos):
            tipo = trabajos[trabajo]
            resultados[tipo] = trabajo.result()

    return resultados


def cargar_estado() -> tuple[dict[str, str], set[str], bool]:
    if not ESTADO.exists():
        return {}, set(), True

    try:
        datos = json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

        paginas = datos.get("paginas", {})
        eventos = set(datos.get("eventos", []))

        if not isinstance(paginas, dict):
            paginas = {}

        return paginas, eventos, False

    except (json.JSONDecodeError, OSError):
        return {}, set(), True


def guardar_texto_atomico(
    ruta: Path,
    contenido: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=".",
        prefix=f"{ruta.stem}_",
        suffix=".tmp",
    ) as temporal:
        temporal.write(contenido)
        ruta_temporal = Path(temporal.name)

    os.replace(ruta_temporal, ruta)


def guardar_estado(
    paginas: dict[str, str],
    eventos: set[str],
) -> None:
    paginas_limitadas = dict(
        list(
            sorted(paginas.items())
        )[-MAXIMO_PAGINAS_ESTADO:]
    )

    eventos_limitados = sorted(eventos)[
        -MAXIMO_EVENTOS_ESTADO:
    ]

    datos = {
        "ultima_actualizacion": datetime.now(
            timezone.utc
        ).isoformat(),
        "paginas": paginas_limitadas,
        "eventos": eventos_limitados,
    }

    guardar_texto_atomico(
        ESTADO,
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )


def obtener_meta(
    sopa: BeautifulSoup,
    nombre: str,
    atributo: str = "property",
) -> str:
    etiqueta = sopa.find(
        "meta",
        attrs={atributo: nombre},
    )

    if not etiqueta:
        return ""

    return etiqueta.get("content", "").strip()


def obtener_idioma(url: str) -> str:
    ruta = urlparse(url).path.lower()

    if ruta.startswith("/es/"):
        return "es"

    if ruta.startswith("/fr/"):
        return "fr"

    return "en"


def categoria_desde_url(url: str) -> str:
    ruta = urlparse(url).path.lower()

    categorias = [
        (
            "/news-and-events/news-releases/",
            "News releases",
        ),
        (
            "/news-and-events/announcements/",
            "Announcements",
        ),
        (
            "/news-and-events/events/",
            "Events",
        ),
        (
            "/news-and-events/speeches-articles/",
            "Speeches and articles",
        ),
        (
            "/news-and-events/blogs/",
            "Blogs",
        ),
        (
            "/news-and-events/comunicados/",
            "Comunicados",
        ),
        (
            "/news-and-events/communiques/",
            "Communiqués",
        ),
        (
            "/es/noticias-y-eventos/",
            "Noticias y eventos",
        ),
        (
            "/fr/actualites-et-evenements/",
            "Actualités et événements",
        ),
        (
            "/resources/publications/",
            "Publications",
        ),
        (
            "/resources/multimedia/",
            "Multimedia",
        ),
        (
            "/es/recursos/",
            "Recursos",
        ),
        (
            "/fr/ressources/",
            "Ressources",
        ),
        (
            "/cases/content/",
            "Cases and decisions",
        ),
        (
            "/es/cases/content/",
            "Casos y decisiones",
        ),
        (
            "/fr/cases/content/",
            "Affaires et décisions",
        ),
    ]

    for patron, categoria in categorias:
        if ruta.startswith(patron):
            return categoria

    return "ICSID"


def extraer_pagina(
    url: str,
    datos_sitemap: dict,
) -> dict | None:
    try:
        contenido = descargar(url, timeout=60)
        sopa = BeautifulSoup(contenido, "html.parser")

        titulo = obtener_meta(sopa, "og:title")

        if not titulo:
            titulo_nodo = sopa.find("h1")

            if titulo_nodo:
                titulo = titulo_nodo.get_text(
                    " ",
                    strip=True,
                )

        descripcion = (
            obtener_meta(sopa, "description", "name")
            or obtener_meta(sopa, "og:description")
        )

        fecha = (
            obtener_meta(
                sopa,
                "article:published_time",
            )
            or obtener_meta(
                sopa,
                "og:published_time",
            )
            or datos_sitemap.get(
                "ultima_modificacion",
                "",
            )
        )

        imagen = obtener_meta(sopa, "og:image")

        titulo = html.unescape(
            " ".join(titulo.split())
        )

        descripcion = html.unescape(
            " ".join(descripcion.split())
        )

        if not titulo:
            return None

        modificacion = datos_sitemap.get(
            "ultima_modificacion",
            "",
        )

        # El GUID cambia cuando cambia lastmod.
        # De esta manera Feedly avisa si una página antigua
        # recibe una actualización relevante.
        guid = generar_guid_evento(
            "page",
            modificacion,
            titulo,
            url,
        )

        return {
            "guid": guid,
            "url": url,
            "titulo": titulo,
            "descripcion": descripcion,
            "fecha": convertir_fecha(fecha),
            "categoria": categoria_desde_url(url),
            "autor": "ICSID",
            "imagen": imagen,
            "idioma": obtener_idioma(url),
        }

    except Exception as error:
        print(
            f"No se pudo procesar {url}: {error}"
        )
        return None


def texto_elemento(
    elemento: ET.Element,
    nombre: str,
) -> str:
    nodo = elemento.find(nombre)

    if nodo is None or nodo.text is None:
        return ""

    return nodo.text.strip()


def cargar_rss_anterior() -> dict[str, ET.Element]:
    elementos: dict[str, ET.Element] = {}

    if not SALIDA.exists():
        return elementos

    try:
        raiz = ET.parse(SALIDA).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return elementos

        for item in canal.findall("item"):
            guid = (
                texto_elemento(item, "guid")
                or texto_elemento(item, "link")
            )

            if guid:
                elementos[guid] = item

    except ET.ParseError:
        print("El rss.xml anterior no era válido")

    return elementos


def fecha_item(item: ET.Element) -> datetime:
    return convertir_fecha(
        texto_elemento(item, "pubDate")
    )


def crear_item(datos: dict) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = datos["titulo"]
    ET.SubElement(item, "link").text = datos["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "false"},
    )
    guid.text = datos["guid"]

    ET.SubElement(item, "pubDate").text = fecha_rss(
        datos["fecha"]
    )

    ET.SubElement(item, "category").text = (
        datos["categoria"]
    )

    if datos.get("descripcion"):
        ET.SubElement(item, "description").text = (
            datos["descripcion"]
        )

    if datos.get("autor"):
        ET.SubElement(item, "author").text = (
            datos["autor"]
        )

    if datos.get("imagen"):
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": datos["imagen"],
                "type": "image/jpeg",
            },
        )

    return item


def crear_rss(
    elementos: dict[str, ET.Element],
) -> ET.ElementTree:
    ordenados = sorted(
        elementos.values(),
        key=fecha_item,
        reverse=True,
    )[:MAXIMO_ENTRADAS_RSS]

    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "ICSID — Todas las novedades"
    )

    ET.SubElement(canal, "link").text = BASE

    ET.SubElement(canal, "description").text = (
        "Casos registrados, documentos publicados, "
        "casos concluidos, tribunales constituidos, "
        "noticias, eventos, comunicados y publicaciones "
        "del ICSID."
    )

    ET.SubElement(canal, "language").text = "multilingual"

    ET.SubElement(canal, "lastBuildDate").text = fecha_rss(
        datetime.now(timezone.utc)
    )

    for item in ordenados:
        canal.append(item)

    return ET.ElementTree(rss)


def guardar_xml_atomico(
    arbol: ET.ElementTree,
) -> None:
    ET.indent(arbol, space="  ")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=".",
        prefix="rss_",
        suffix=".xml",
    ) as temporal:
        ruta_temporal = Path(temporal.name)

        arbol.write(
            temporal,
            encoding="utf-8",
            xml_declaration=True,
        )

    os.replace(ruta_temporal, SALIDA)


def main() -> None:
    paginas_actuales = localizar_paginas()
    casos_actuales = localizar_eventos_casos()

    paginas_guardadas, eventos_guardados, primera = (
        cargar_estado()
    )

    rss = cargar_rss_anterior()

    paginas_pendientes: list[tuple[str, dict]] = []
    eventos_pendientes: list[dict] = []

    if primera:
        ordenadas = sorted(
            paginas_actuales.items(),
            key=lambda elemento: convertir_fecha(
                elemento[1].get(
                    "ultima_modificacion",
                    "",
                )
            ),
            reverse=True,
        )

        paginas_pendientes = ordenadas[
            :PAGINAS_INICIALES
        ]

        for tipo, casos in casos_actuales.items():
            eventos_pendientes.extend(
                casos[:CASOS_INICIALES_POR_TIPO]
            )

        # Guarda el estado histórico completo para
        # no inundar Feedly en la primera ejecución.
        paginas_guardadas = {
            url: datos.get(
                "ultima_modificacion",
                "",
            )
            for url, datos in paginas_actuales.items()
        }

        for casos in casos_actuales.values():
            eventos_guardados.update(
                caso["guid"]
                for caso in casos
            )

        print(
            "Primera ejecución: se incorporará "
            "únicamente el contenido reciente"
        )

    else:
        for url, datos in paginas_actuales.items():
            modificacion_actual = datos.get(
                "ultima_modificacion",
                "",
            )

            modificacion_anterior = paginas_guardadas.get(
                url
            )

            if modificacion_anterior is None:
                paginas_pendientes.append(
                    (url, datos)
                )

            elif (
                modificacion_actual
                and modificacion_actual
                != modificacion_anterior
            ):
                paginas_pendientes.append(
                    (url, datos)
                )

        for casos in casos_actuales.values():
            for caso in casos:
                if caso["guid"] not in eventos_guardados:
                    eventos_pendientes.append(caso)

        print(
            f"Páginas nuevas o modificadas: "
            f"{len(paginas_pendientes)}"
        )

        print(
            f"Nuevas actuaciones de casos: "
            f"{len(eventos_pendientes)}"
        )

    paginas_pendientes.sort(
        key=lambda elemento: convertir_fecha(
            elemento[1].get(
                "ultima_modificacion",
                "",
            )
        ),
        reverse=True,
    )

    paginas_pendientes = paginas_pendientes[
        :MAXIMO_PROCESADOS_POR_EJECUCION
    ]

    paginas_procesadas: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_pagina,
                url,
                datos,
            ): (url, datos)
            for url, datos in paginas_pendientes
        }

        for trabajo in as_completed(trabajos):
            url, datos = trabajos[trabajo]
            resultado = trabajo.result()

            if resultado:
                paginas_procesadas.append(resultado)

                paginas_guardadas[url] = datos.get(
                    "ultima_modificacion",
                    "",
                )

    for pagina in paginas_procesadas:
        rss[pagina["guid"]] = crear_item(pagina)

    for evento in eventos_pendientes:
        if evento["guid"] not in rss:
            rss[evento["guid"]] = crear_item(evento)

        eventos_guardados.add(evento["guid"])

    guardar_xml_atomico(
        crear_rss(rss)
    )

    guardar_estado(
        paginas_guardadas,
        eventos_guardados,
    )

    print(
        f"Entradas conservadas en la RSS: {len(rss)}"
    )

    print(
        f"Páginas nuevas o modificadas añadidas: "
        f"{len(paginas_procesadas)}"
    )

    print(
        f"Actuaciones de casos añadidas: "
        f"{len(eventos_pendientes)}"
    )


if __name__ == "__main__":
    main()
