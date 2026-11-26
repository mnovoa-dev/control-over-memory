#!/usr/bin/env python3
"""Revisión periódica de un trabajo con IA (reglas, tareas y herramientas): lo que un modelo haría mal a ojo.

Las instrucciones para el asistente (`CLAUDE.md`, `AGENTS.md` u otras) crecen con cada fallo. Si nadie
las repasa, se repiten, citan archivos que ya no existen y engordan hasta que el modelo deja de leerlas
enteras. Esta herramienta solo lee y da listas para revisar; no cambia nada.

Uso (desde la carpeta del proyecto, o con --raiz):
  revision.py duplicados [--umbral 0.5] [--mismo-archivo]
                                               reglas muy parecidas en archivos distintos (o también en
                                               el mismo): copias que pueden desfasarse si se cambia una
  revision.py rutas                      rutas citadas entre `comillas invertidas` que no existen
  revision.py largos [--lineas 200]      archivos que el asistente lee enteros y pasan del tope
  revision.py crecimiento --desde AAAA-MM-DD
                                               lo que más engordó y lo que solo crece (necesita Git)
  revision.py parados                    pendientes con el plazo pasado o «en curso» mucho tiempo
  revision.py vigentes [--dias 30]       hechos vigentes antiguos: ¿siguen siendo verdad?
  revision.py pruebas                    el --self-test de cada herramienta, en un solo resultado
  revision.py semanal --desde AAAA-MM-DD todo lo anterior seguido
  revision.py --self-test
Opciones comunes: --raiz RUTA, --hoy AAAA-MM-DD.

Qué archivos mira sale de `control.json` (ver `control.ejemplo.json`):
  «normas»        archivos de reglas (patrones como `*/CLAUDE.md`); por defecto, `CLAUDE.md` y `AGENTS.md`
                  en la raíz y un nivel por debajo. Se suman los `.md` que `aprendizajes.csv` da como
                  sitio de una regla incorporada (columna `donde`): si una regla nueva cae en otro
                  archivo, la revisión lo mira sin tener que apuntarlo.
  «cargados»      otros archivos que el asistente lee enteros (skills, guías); `largos` los mira junto a las
                  normas. Lo que se lee por partes (diarios, actas, historia) no se pone: no hace falta dividirlo.
  «no_cargados»   archivos largos que no se leen enteros: `largos` deja de preguntar por ellos. Los demás
                  .md de más del tope que no están en ninguna lista salen al final, para que una lista
                  vieja se note sola.
  «max_lineas»    el tope de `largos`; por defecto, 200.
  «herramientas»  scripts cuyo --self-test se pasa; por defecto, `*.py` de la raíz.
  «omitir»        comprobaciones que `semanal` se salta (por ejemplo, ["duplicados"]).
  Los nombres de `pendientes.csv` y `aprendizajes.csv`, como en las otras dos herramientas.

Código de salida: 0; 1 si falla alguna autoprueba o la configuración; 2 si la orden está mal escrita.
"""
import argparse
import contextlib
import csv
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

CONFIG = "control.json"
NORMAS = ["CLAUDE.md", "AGENTS.md", "*/CLAUDE.md", "*/AGENTS.md"]
HERRAMIENTAS = ["*.py"]
COMPROBACIONES = ["duplicados", "rutas", "largos", "crecimiento", "parados", "vigentes", "pruebas"]
MAX_LINEAS = 200
ACTIVOS = {"abierta", "en curso", "bloqueada"}
DIAS_EN_CURSO = 14
# Palabras que no distinguen una regla de otra.
VACIAS = set("""a al algo ante antes como con cual cuando de del desde donde el ella en entre es esa ese eso esta
este esto la las le lo los mas mismo muy ni no nos o otra otro para pero por que se ser si sin sobre solo su sus
tambien un una uno y ya ha hay son cada todo toda todos todas tras hace hacer va ver""".split())
SIN_GIT = "no es un repositorio Git (o Git no está instalado): no se mira"


CLAVES_PROPIAS = {"areas", "persona", "archivo_pendientes", "archivo_aprendizajes", "normas", "herramientas"}


def aviso_config_vieja(raiz):
    """Las primeras versiones leían `config.json`. Si está ese y no `control.json`, y es de estas herramientas
    (tiene alguna de sus claves), se para: seguir sin él cambiaría las áreas o los archivos sin avisar."""
    vieja = Path(raiz) / "config.json"
    if (Path(raiz) / CONFIG).exists() or not vieja.is_file():
        return
    try:
        datos = json.loads(vieja.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeDecodeError, OSError):
        return
    if isinstance(datos, dict) and CLAVES_PROPIAS & set(datos):
        raise SystemExit(f"✗ Hay un config.json de una versión anterior: renómbralo a {CONFIG} (el contenido vale tal cual).")


class Config:
    """Qué archivos se revisan. Comparte `control.json` con `pendientes.py` y `aprendizajes.py`."""

    def __init__(self, raiz):
        datos = {}
        ruta = Path(raiz) / CONFIG
        aviso_config_vieja(raiz)
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise SystemExit(f"✗ control.json no es un JSON válido: {e}")
            if not isinstance(datos, dict):
                raise SystemExit("✗ control.json debe ser un objeto")

        def lista(clave, defecto):
            valor = datos.get(clave, defecto)
            if not isinstance(valor, list) or not all(isinstance(v, str) and v.strip() for v in valor):
                raise SystemExit(f"✗ control.json: «{clave}» debe ser una lista de textos")
            if any(Path(v).is_absolute() or ".." in Path(v).parts for v in valor):
                raise SystemExit(f"✗ control.json: «{clave}» lleva rutas dentro de la carpeta, sin «..»")
            return valor

        def archivo(clave, defecto):
            valor = datos.get(clave) or defecto
            if not isinstance(valor, str) or Path(valor).name != valor or not valor.endswith(".csv"):
                raise SystemExit(f"✗ control.json: «{clave}» debe ser un nombre .csv, sin carpetas")
            return valor

        self.normas = lista("normas", NORMAS)
        self.herramientas = lista("herramientas", HERRAMIENTAS)
        self.cargados = lista("cargados", [])
        self.no_cargados = lista("no_cargados", [])
        self.max_lineas = datos.get("max_lineas", MAX_LINEAS)
        if isinstance(self.max_lineas, bool) or not isinstance(self.max_lineas, int) or self.max_lineas < 1:
            raise SystemExit("✗ control.json: «max_lineas» debe ser un número entero mayor que 0")
        self.omitir = lista("omitir", [])
        malas = [o for o in self.omitir if o not in COMPROBACIONES]
        if malas:
            raise SystemExit(f"✗ control.json: «omitir» no conoce {malas}; valen {COMPROBACIONES}")
        self.pendientes = archivo("archivo_pendientes", "pendientes.csv")
        self.aprendizajes = archivo("archivo_aprendizajes", "aprendizajes.csv")


def leer_texto(ruta):
    return Path(ruta).read_text(encoding="utf-8-sig", errors="replace")


def leer_tabla(ruta, necesarias):
    """(filas, None) o (None, motivo): un CSV que no existe, no está en UTF-8 o no trae las columnas que
    hacen falta no se lee a medias ni rompe la revisión; se dice por qué no se mira."""
    if not ruta.is_file():
        return None, f"no hay {ruta.name}"
    if ruta.stat().st_size == 0:
        return None, f"{ruta.name} está vacío"
    try:
        with open(ruta, encoding="utf-8-sig", newline="") as f:
            lector = csv.DictReader(f)
            filas = list(lector)
            columnas = lector.fieldnames or []
    except UnicodeDecodeError:
        return None, f"{ruta.name} no está en UTF-8 (¿guardado con Excel?)"
    except (OSError, csv.Error) as e:
        return None, f"{ruta.name} no se pudo leer ({e})"
    faltan = [c for c in necesarias if c not in columnas]
    if faltan:
        return None, f"a {ruta.name} le faltan las columnas {', '.join(faltan)} (¿es de otro programa?)"
    return filas, None


def expandir(raiz, patrones):
    """Rutas relativas (con «/») de los archivos que casan con los patrones, sin repetir y en orden."""
    vistos = []
    for p in patrones:
        for f in sorted(raiz.glob(p)):
            r = f.relative_to(raiz).as_posix()
            if f.is_file() and r not in vistos:
                vistos.append(r)
    return vistos


def sitios_de_reglas(raiz, cfg):
    """(los `.md` que `aprendizajes.csv` da como sitio de una regla incorporada y que existen, aviso).
    Sin `aprendizajes.csv` no hay aviso: es opcional. Si existe y no se puede leer, se dice."""
    ruta = raiz / cfg.aprendizajes
    if not ruta.is_file():
        return [], None
    filas, motivo = leer_tabla(ruta, ["estado", "donde"])
    if motivo:
        return [], f"Aviso: {motivo}; no se suman los archivos de sus reglas."
    dondes = {re.split(r" → |#", f.get("donde") or "", maxsplit=1)[0].strip().strip("`")
              for f in filas if f.get("estado") == "incorporada"}
    return sorted(d for d in dondes if d.endswith(".md") and ".." not in Path(d).parts
                  and not Path(d).is_absolute() and (raiz / d).is_file()), None


def archivos_de_normas(raiz, cfg):
    salida = expandir(raiz, cfg.normas)
    return salida + [d for d in sitios_de_reglas(raiz, cfg)[0] if d not in salida]


def sin_normas(raiz, cfg, que):
    """La línea que lo dice si no hay archivos de normas: un cero sin mirar nada parecería «todo bien»."""
    if archivos_de_normas(raiz, cfg):
        return None
    busca = f"busca {', '.join(cfg.normas)}" if cfg.normas else "«normas» está vacío"
    return f"{que}: no hay archivos de normas que mirar ({busca}; se cambia con «normas» en {CONFIG})."


# ---- duplicados ---------------------------------------------------------------------------
def plano(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def palabras(t):
    return {w for w in re.findall(r"[a-z0-9_./-]{4,}", plano(t)) if w not in VACIAS}


def unidades(texto):
    """(línea, texto) de cada viñeta, punto numerado o párrafo; sin títulos, tablas ni bloques de código."""
    actual, ini, en_codigo = [], 0, False
    for n, linea in enumerate(texto.splitlines() + [""], 1):
        s = linea.strip()
        valla = s.startswith("```")
        if valla:
            en_codigo = not en_codigo
        nueva = re.match(r"^([-*]|\d+[.)])\s", s)
        if valla or en_codigo or not s or s.startswith(("#", "|")) or nueva:
            if actual:
                yield ini, " ".join(actual)
            actual, ini = ([s], n) if (nueva and not en_codigo and not valla) else ([], 0)
            continue
        if not actual:
            ini = n
        actual.append(s)


def duplicados(raiz, cfg, umbral=0.5, minimo=5, mismo_archivo=False):
    """[(índice, (archivo, línea, texto), (archivo, línea, texto))] de pares que comparten al menos `minimo`
    palabras y una proporción `umbral` de las del más corto; en archivos distintos salvo con `mismo_archivo`."""
    todas = []
    for a in archivos_de_normas(raiz, cfg):
        for linea, texto in unidades(leer_texto(raiz / a)):
            p = palabras(texto)
            if len(p) >= minimo:
                todas.append((a, linea, texto, p))
    pares = []
    for x, y in itertools.combinations(todas, 2):
        if x[0] == y[0] and not mismo_archivo:
            continue  # dentro de un archivo, una regla y su desarrollo suelen parecerse a propósito
        comun = x[3] & y[3]
        indice = len(comun) / min(len(x[3]), len(y[3]))
        if indice >= umbral and len(comun) >= minimo:
            pares.append((round(indice, 2), x[:3], y[:3]))
    return sorted(pares, key=lambda p: -p[0])


# ---- git ----------------------------------------------------------------------------------
ERROR_GIT = {"texto": ""}


def git(raiz, *args):
    """Salida de git, o None si falla; el motivo queda en ERROR_GIT. `core.quotepath=false`: sin él, Git
    escribe entre comillas y con códigos las rutas con tildes o ñ, y no se reconocerían."""
    if not shutil.which("git"):
        ERROR_GIT["texto"] = ""
        return None
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=raiz, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                       env=dict(os.environ, LANGUAGE="C", LC_ALL="C"))  # mensajes de Git en inglés: se reconocen
    ERROR_GIT["texto"] = r.stderr.strip() if r.returncode else ""
    return r.stdout.strip() if r.returncode == 0 else None


def es_repositorio(raiz):
    return git(raiz, "rev-parse", "--is-inside-work-tree") == "true"


def sin_git(que):
    """Por qué no se mira con Git: no hay Git, no es un repositorio, u otro fallo de Git que se enseña (un
    permiso o un repositorio de otro usuario no debe parecer «no es un repositorio»)."""
    error = ERROR_GIT["texto"]
    if error and "not a git repository" not in error.lower():
        return f"{que}: Git falló ({error.splitlines()[0][:160]}); no se mira."
    return f"{que}: {SIN_GIT}."


# ---- rutas --------------------------------------------------------------------------------
RE_RUTA = re.compile(r"`([^`\s]+)`")
EXTENSIONES = (".md", ".py", ".csv", ".json", ".txt", ".html", ".pdf", ".yml", ".yaml", ".sh", ".toml")


def sin_ancla(t):
    """La ruta sin `#apartado`, sin `:línea` y sin puntuación al final: `a/b.md#x` y `a/b.md:12` citan a/b.md."""
    return re.sub(r"(#.*|:\d+(:\d+)?)$", "", t).rstrip(".,;:")


def sin_bloques(texto):
    """El texto sin bloques de código (```): una ruta dentro de un ejemplo no es una cita."""
    return re.sub(r"```.*?(```|\Z)", "", texto, flags=re.S)


def parece_ruta(t):
    # Una ruta absoluta (`/usr/lib/…`, `C:/…`) es de otro equipo: no se mira.
    if re.search(r"[<>{}*|?\[\]]|AAAA|^https?:|^~|^\$|^-|^/|^[A-Za-z]:|\.\.\.|…", t):
        return False
    # Solo rutas con carpeta: un nombre suelto suele ser un ejemplo o un archivo que aún no existe.
    return "/" in t.rstrip("/") and (t.endswith(EXTENSIONES) or t.endswith("/"))


# Enlaces de Markdown, también de imagen: [texto](ruta "título") o [texto](<ruta con espacios>).
RE_ENLACE = re.compile(r"\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+\"[^\"\n]*\")?\s*\)")
# Enlaces de nombre, al estilo de las wikis: [[nombre]], [[carpeta/nombre]], [[nombre#apartado|texto]].
RE_WIKI = re.compile(r"\[\[([^\]\|#\n]+)(?:[#|][^\]\n]*)?\]\]")
SALTAR_CARPETAS = {"node_modules", "__pycache__", "venv", "env", "site-packages"}


def sin_codigo(texto):
    """El texto sin bloques ni trozos de código: un enlace dentro de un ejemplo no es una cita."""
    return re.sub(r"`[^`\n]*`", "", sin_bloques(texto))


def destino_de_enlace(t):
    """La ruta de un enlace de Markdown, o None si es externo, un ancla o absoluto."""
    t = t.strip("<>").strip()
    if not t or t.startswith(("#", "/")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", t):
        return None
    t = urllib.parse.unquote(t.split("#")[0].split("?")[0])
    return t or None


def indice_de_nombres(raiz):
    """Cómo se puede nombrar cada archivo en [[…]]: nombre, nombre sin extensión y ruta, sin mayúsculas."""
    nombres = set()
    for carpeta, subcarpetas, archivos in os.walk(raiz):
        subcarpetas[:] = [s for s in subcarpetas if not s.startswith(".") and s not in SALTAR_CARPETAS]
        for nombre in archivos:
            rel = (Path(carpeta) / nombre).relative_to(raiz).as_posix()
            nombres |= {nombre.casefold(), Path(nombre).stem.casefold(), rel.casefold(),
                        str(Path(rel).with_suffix("")).replace("\\", "/").casefold()}
    return nombres


def rutas_rotas(raiz, cfg):
    """([(archivo, cita)], citas miradas). Se miran tres formas de citar un archivo: `ruta/con/carpeta`
    entre comillas invertidas, [texto](ruta) y [[nombre]]. Una ruta vale si existe desde la raíz o desde
    la carpeta del archivo; lo que Git ignora a propósito (datos privados o regenerables) no cuenta."""
    repo = es_repositorio(raiz)
    nombres = None
    rotas, miradas = [], 0
    for a in archivos_de_normas(raiz, cfg):
        texto = leer_texto(raiz / a)
        bases = [raiz, (raiz / a).parent]
        rutas = [(sin_ancla(t), t) for t in sorted(set(RE_RUTA.findall(sin_bloques(texto))))
                 if parece_ruta(sin_ancla(t))]
        limpio = sin_codigo(texto)
        for t in sorted(set(RE_ENLACE.findall(limpio))):
            destino = destino_de_enlace(t)
            if destino:
                rutas.append((destino, f"[…]({t.strip('<>')})"))
        miradas += len(rutas)
        for t, cita in rutas:
            if any((b / t.rstrip("/")).exists() for b in bases):
                continue
            if repo and any(git(raiz, "check-ignore", "-q", "--no-index",
                                (b / t).relative_to(raiz).as_posix()) is not None for b in bases):
                continue
            rotas.append((a, cita))
        wikis = sorted(set(w.strip() for w in RE_WIKI.findall(limpio) if w.strip()))
        miradas += len(wikis)
        if wikis and nombres is None:
            nombres = indice_de_nombres(raiz)
        rotas += [(a, f"[[{w}]]") for w in wikis if w.casefold() not in nombres]
    return rotas, miradas


# ---- crecimiento --------------------------------------------------------------------------
# ---- largos -------------------------------------------------------------------------------
def largos(raiz, cfg, maximo=None):
    """([(líneas, archivo)] de más del tope, cuántos se miraron). Mira solo lo que el asistente lee entero:
    los archivos de normas y los de «cargados». No adivina qué es historia: lo que se lee por partes
    (diarios, actas) no se pone ahí."""
    maximo = maximo or cfg.max_lineas
    archivos = archivos_de_normas(raiz, cfg)
    archivos += [a for a in expandir(raiz, cfg.cargados) if a not in archivos]
    salida = []
    for a in archivos:
        n = len(leer_texto(raiz / a).splitlines())
        if n > maximo:
            salida.append((n, a))
    return sorted(salida, key=lambda x: (-x[0], x[1])), len(archivos)


def sin_clasificar(raiz, cfg, maximo=None, cuantos=10):
    """[(líneas, archivo)] de los .md de más del tope que no están en ninguna lista (normas, «cargados» ni
    «no_cargados»): así una lista que se quedó vieja se nota sola. Lo que se marca como «no_cargados» no
    se vuelve a preguntar."""
    maximo = maximo or cfg.max_lineas
    listados = set(archivos_de_normas(raiz, cfg)) | set(expandir(raiz, cfg.cargados + cfg.no_cargados))
    salida = []
    for f in raiz.rglob("*.md"):
        r = f.relative_to(raiz)
        if (not f.is_file() or r.as_posix() in listados
                or any(p.startswith(".") or p in SALTAR_CARPETAS for p in r.parts[:-1])):
            continue
        n = len(leer_texto(f).splitlines())
        if n > maximo:
            salida.append((n, r.as_posix()))
    return sorted(salida, key=lambda x: (-x[0], x[1]))[:cuantos]


def sumar_numstat(numstat):
    """{archivo: (añadidas, quitadas)} de la salida de `git log --numstat` (los binarios no cuentan)."""
    suma = {}
    for linea in numstat.splitlines():
        partes = linea.split("\t")
        if len(partes) != 3 or partes[0] == "-":
            continue
        a, d = suma.get(partes[2], (0, 0))
        suma[partes[2]] = (a + int(partes[0]), d + int(partes[1]))
    return suma


def solo_crecen(suma, minimo=20):
    """[(archivo, añadidas, quitadas)] de los que crecieron al menos `minimo` líneas quitando menos de
    una por cada diez añadidas, de más a menos."""
    filas = [(f, a, d) for f, (a, d) in suma.items() if a >= minimo and d * 10 <= a]
    return sorted(filas, key=lambda x: -x[1])


def engordaron(suma, cuantos=5):
    """[(archivo, líneas netas)] de los que más crecieron."""
    filas = [(f, a - d) for f, (a, d) in suma.items() if a - d > 0]
    return sorted(filas, key=lambda x: -x[1])[:cuantos]


def crecimiento(raiz, cfg, desde):
    """(engordaron, solo_crecen) de normas y herramientas desde `desde`, o None si no hay Git."""
    if not es_repositorio(raiz):
        return None
    rutas = archivos_de_normas(raiz, cfg) + expandir(raiz, cfg.herramientas)
    if not rutas:
        return [], []
    numstat = git(raiz, "log", f"--since={desde}", "--no-renames", "--relative", "--numstat", "--format=",
                  "--", *rutas)
    suma = {f: v for f, v in sumar_numstat(numstat or "").items() if (raiz / f).exists()}
    return engordaron(suma), solo_crecen(suma)


# ---- pendientes ---------------------------------------------------------------------------
COLUMNAS_PENDIENTES = ["id", "para", "estado", "creada", "vence", "que"]


def fecha_valida(t):
    try:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)) and bool(date.fromisoformat(t))
    except ValueError:
        return False


def filas_legibles(filas):
    """(filas, raras): las filas con todas las columnas, código y fechas AAAA-MM-DD, con el estado sin mayúsculas
    ni espacios; y cuántas no se pueden mirar. Una fila rara no se compara a ciegas («01/02/2026» < «2026-…»
    como texto) ni rompe la revisión: se cuenta y se dice."""
    buenas, raras = [], 0
    for f in filas:
        if (None in f or any(f.get(c) is None for c in COLUMNAS_PENDIENTES) or not f["id"].strip()
                or any(f[c].strip() and not fecha_valida(f[c].strip()) for c in ("creada", "vence"))):
            raras += 1
            continue
        buenas.append({c: (v or "").strip() for c, v in f.items()} | {"estado": f["estado"].strip().lower()})
    return buenas, raras


def parados(raiz, cfg, hoy, dias=DIAS_EN_CURSO):
    """([(id, motivo, qué)], filas raras) de lo activo con el plazo pasado, o «en curso» creado hace más de
    `dias`; o un texto con el motivo si la tabla no se puede mirar."""
    filas, motivo = leer_tabla(raiz / cfg.pendientes, COLUMNAS_PENDIENTES)
    if motivo:
        return motivo
    filas, raras = filas_legibles(filas)
    corte = (date.fromisoformat(hoy) - timedelta(days=dias)).isoformat()
    salida = []
    for f in filas:
        if f.get("estado") not in ACTIVOS:
            continue
        if f.get("vence") and f["vence"] < hoy:
            salida.append((f["id"], f"plazo pasado el {f['vence']}", f.get("que", "")))
        elif f.get("estado") == "en curso" and f.get("creada") and f["creada"] < corte:
            salida.append((f["id"], f"en curso y creada el {f['creada']}", f.get("que", "")))
    return salida, raras


def vigentes_viejos(raiz, cfg, hoy, dias=30):
    """([(creada, id, para, qué)], filas raras) de los hechos vigentes apuntados hace más de `dias`, o el
    motivo."""
    filas, motivo = leer_tabla(raiz / cfg.pendientes, COLUMNAS_PENDIENTES)
    if motivo:
        return motivo
    filas, raras = filas_legibles(filas)
    corte = (date.fromisoformat(hoy) - timedelta(days=dias)).isoformat()
    return [(f["creada"], f["id"], f.get("para", ""), f.get("que", "")) for f in filas
            if f.get("estado") == "vigente" and f.get("creada") and f["creada"] < corte], raras


def aviso_raras(raras, archivo):
    return ([f"  Aviso: {cuantos(raras, 'fila')} de {archivo} con datos raros (columnas de menos, sin código o "
             "fechas que no son AAAA-MM-DD) no se miraron: revísalas en la tabla."] if raras else [])


# ---- pruebas ------------------------------------------------------------------------------
def con_prueba(raiz, cfg):
    return [r for r in expandir(raiz, cfg.herramientas)
            if r.endswith(".py") and "--self-test" in leer_texto(raiz / r)]


def pasar_pruebas(raiz, cfg):
    """[(archivo, pasa, última línea)] del --self-test de cada herramienta que lo tiene."""
    salida = []
    for r in con_prueba(raiz, cfg):
        try:
            p = subprocess.run([sys.executable, "-I", str(raiz / r), "--self-test"], cwd=raiz,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                               stdin=subprocess.DEVNULL)
            lineas = (p.stdout + p.stderr).strip().splitlines()
            salida.append((r, p.returncode == 0, lineas[-1] if lineas else "sin salida"))
        except subprocess.TimeoutExpired:
            salida.append((r, False, "no terminó en 5 minutos"))
    return salida


# ---- informes -----------------------------------------------------------------------------
def cuantos(n, palabra):
    return f"{n} {palabra}" + ("" if n == 1 else "s")


def avisos(raiz, cfg):
    aviso = sitios_de_reglas(raiz, cfg)[1]
    return [aviso] if aviso else []


def informe_duplicados(raiz, cfg, umbral, mismo_archivo=False):
    vacio = sin_normas(raiz, cfg, "Duplicados")
    if vacio:
        return avisos(raiz, cfg) + [vacio], 0
    pares = duplicados(raiz, cfg, umbral, mismo_archivo=mismo_archivo)
    donde = "también dentro de un mismo archivo" if mismo_archivo else "en archivos distintos"
    lineas = avisos(raiz, cfg) + [
        f"{len(pares)} pares de reglas parecidas {donde} (umbral {umbral}), en "
        f"{cuantos(len(archivos_de_normas(raiz, cfg)), 'archivo')} de normas. Si es la misma regla, puede desfasarse cuando "
        "se cambie solo una copia: déjala en un sitio y enlázala desde el otro o, si la copia es a propósito, "
        "cambia las dos a la vez. Es una lista para mirar, no un error."]
    for i, x, y in pares:
        lineas.append(f"\n{i}  {x[0]}:{x[1]}  ⇄  {y[0]}:{y[1]}\n   A: {x[2][:180]}\n   B: {y[2][:180]}")
    return lineas, 0


def informe_rutas(raiz, cfg):
    vacio = sin_normas(raiz, cfg, "Rutas")
    if vacio:
        return avisos(raiz, cfg) + [vacio], 0
    rr, miradas = rutas_rotas(raiz, cfg)
    n = len(archivos_de_normas(raiz, cfg))
    if not miradas:
        return avisos(raiz, cfg) + [
            f"Rutas: ninguna cita que mirar en {cuantos(n, 'archivo')} de normas (se buscan `carpeta/archivo` entre comillas "
            "invertidas, [texto](ruta) y [[nombre]])."], 0
    return (avisos(raiz, cfg) + [f"Citas que no llevan a ningún archivo ({len(rr)} de {miradas}, en {cuantos(n, 'archivo')} "
                                 "de normas): corrige la cita o recupera el archivo"]
            + [f"  {a}: {t}" for a, t in rr]), 0


def informe_largos(raiz, cfg, maximo=None):
    maximo = maximo or cfg.max_lineas
    ll, mirados = largos(raiz, cfg, maximo)
    if not mirados:
        return avisos(raiz, cfg) + [
            f"Largos: no hay archivos que mirar (busca {', '.join(cfg.normas + cfg.cargados) or 'nada'}; se cambia "
            f"con «normas» y «cargados» en {CONFIG})."] + sueltos(raiz, cfg, maximo), 0
    return (avisos(raiz, cfg) + [
        f"Archivos que el asistente lee enteros y pasan de {maximo} líneas ({len(ll)} de {mirados}): cuanto más "
        "largos, más fácil que se salte una parte. ¿Se dividen o se pasa el detalle a otro archivo que se abra "
        "solo cuando haga falta?"]
            + [f"  {n:>5}  {a}" for n, a in ll] + sueltos(raiz, cfg, maximo)), 0


def sueltos(raiz, cfg, maximo):
    ss = sin_clasificar(raiz, cfg, maximo)
    if not ss:
        return []
    return ([f"Otros .md de más de {maximo} líneas que no están en ninguna lista ({len(ss)}, los más largos): "
             "¿alguno lo lee el asistente entero? Si sí, ponlo en «cargados»; si no, en «no_cargados» y no se "
             "vuelve a preguntar."]
            + [f"  {n:>5}  {a}" for n, a in ss])


def informe_crecimiento(raiz, cfg, desde):
    res = crecimiento(raiz, cfg, desde)
    if res is None:
        return [sin_git("Crecimiento")], 0
    gordos, solo = res
    return ([f"Lo que más engordó desde {desde} (líneas netas): ¿se puede adelgazar sin cambiar lo que decide?"]
            + ([f"  +{n:<5} {f}" for f, n in gordos] or ["  nada"])
            + [f"Lo que desde {desde} solo creció (quita menos de una línea por cada diez que añade) ({len(solo)}): "
               "¿se pudo arreglar lo que ya había en vez de añadir?"]
            + [f"  +{a:<5} -{d:<5} {f}" for f, a, d in solo]), 0


def informe_parados(raiz, cfg, hoy):
    res = parados(raiz, cfg, hoy)
    if isinstance(res, str):
        return [f"Parados: {res}; no se mira."], 0
    pp, raras = res
    return ([f"Pendientes con el plazo pasado o «en curso» y creados hace más de {DIAS_EN_CURSO} días ({len(pp)}): "
             "¿se cierran, se reprograman o se retoman?"]
            + [f"  {i:<5} {motivo}: {que[:140]}" for i, motivo, que in pp] + aviso_raras(raras, cfg.pendientes)), 0


def informe_vigentes(raiz, cfg, hoy, dias):
    res = vigentes_viejos(raiz, cfg, hoy, dias)
    if isinstance(res, str):
        return [f"Vigentes: {res}; no se mira."], 0
    vv, raras = res
    return ([f"Hechos vigentes apuntados hace más de {dias} días ({len(vv)}): ¿siguen siendo verdad?"]
            + [f"  {i:<5} {fecha}  {para}: {que[:150]}" for fecha, i, para, que in vv]
            + aviso_raras(raras, cfg.pendientes)), 0


def informe_pruebas(raiz, cfg):
    res = pasar_pruebas(raiz, cfg)
    if not res:
        return [f"Autopruebas: ninguna herramienta con --self-test que pasar (busca {', '.join(cfg.herramientas)}; "
                f"se cambia con «herramientas» en {CONFIG})."], 0
    malas = [r for r in res if not r[1]]
    return ([f"Autopruebas (--self-test) de {cuantos(len(res), 'herramienta')}: {len(res) - len(malas)} pasan"]
            + [f"  FALLA {f}: {final}" for f, _, final in malas]), (1 if malas else 0)


def main(argv=None):
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="revision.py", description=__doc__.split("\n")[0])
    p.add_argument("orden", choices=COMPROBACIONES + ["semanal"])
    p.add_argument("--raiz", default=".")
    p.add_argument("--hoy")
    p.add_argument("--desde")
    p.add_argument("--umbral", type=float, default=0.5)
    p.add_argument("--mismo-archivo", action="store_true")
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--lineas", type=int)
    a = p.parse_args(argv)
    raiz = Path(a.raiz).resolve()
    if not raiz.is_dir():
        print(f"✗ La carpeta {raiz} no existe (¿--raiz mal escrito?).")
        return 1
    for nombre, valor in (("--hoy", a.hoy), ("--desde", a.desde)):
        if valor:
            try:
                date.fromisoformat(valor)
            except ValueError:
                print(f"✗ {nombre} debe ser una fecha AAAA-MM-DD")
                return 2
    if a.lineas is not None and a.lineas < 1:
        print("✗ --lineas debe ser un número mayor que 0")
        return 2
    if a.orden in ("crecimiento", "semanal") and not a.desde:
        print(f"✗ {a.orden} necesita --desde AAAA-MM-DD (el día siguiente a la última revisión)")
        return 2
    try:
        cfg = Config(raiz)
    except SystemExit as e:
        print(e)
        return 1
    hoy = a.hoy or date.today().isoformat()
    informes = {
        "duplicados": lambda: informe_duplicados(raiz, cfg, a.umbral, a.mismo_archivo),
        "rutas": lambda: informe_rutas(raiz, cfg),
        "largos": lambda: informe_largos(raiz, cfg, a.lineas),
        "crecimiento": lambda: informe_crecimiento(raiz, cfg, a.desde),
        "parados": lambda: informe_parados(raiz, cfg, hoy),
        "vigentes": lambda: informe_vigentes(raiz, cfg, hoy, a.dias),
        "pruebas": lambda: informe_pruebas(raiz, cfg),
    }
    if a.orden != "semanal":
        lineas, codigo = informes[a.orden]()
        print("\n".join(lineas))
        return codigo
    codigo = 0
    for nombre in COMPROBACIONES:
        print(f"\n=== {nombre} ===")
        if nombre in cfg.omitir:
            print("omitida en control.json")
            continue
        lineas, c = informes[nombre]()
        print("\n".join(lineas))
        codigo = max(codigo, c)
    print(f"\nPara terminar: `aprendizajes.py ver --desde {a.desde}` enseña los fallos que vuelven aunque ya tenían regla.")
    return codigo


# ---- autoprueba ---------------------------------------------------------------------------
def self_test():
    fallos, casos = [], 0

    def ok(c, n):
        nonlocal casos
        casos += 1
        if not c:
            fallos.append(n)

    with tempfile.TemporaryDirectory() as d:
        raiz = Path(d).resolve()
        # Sin repositorio por encima: que Git no suba de la carpeta temporal a otro repositorio.
        os.environ["GIT_CEILING_DIRECTORIES"] = str(raiz.parent)
        (raiz / "Casa").mkdir()
        (raiz / "notas").mkdir()
        (raiz / "CLAUDE.md").write_text(
            "# Reglas\n\n- Nada sale sin visto bueno: correos, mensajes, solicitudes y publicaciones quedan en borrador.\n"
            "- Las facturas se guardan en `notas/facturas.md` y el resumen en `notas/resumen.md`.\n\n"
            "```\n- correos mensajes solicitudes publicaciones borrador visto bueno\n```\n", encoding="utf-8")
        (raiz / "Casa" / "CLAUDE.md").write_text(
            "# Casa\n\n- Correos, mensajes, solicitudes y publicaciones quedan en borrador: nada sale sin visto bueno.\n"
            "- Regla distinta sobre plantillas tipográficas justificadas y márgenes generosos.\n"
            "- Ver `../notas/facturas.md`, `notas/x.md`, `sub/ejemplo-AAAA.md`, `https://ejemplo.org/a.md`,\n"
            "  `/usr/lib/otro/` y `C:/Datos/a.csv`.\n",
            encoding="utf-8")
        (raiz / "notas" / "facturas.md").write_text("x", encoding="utf-8")
        (raiz / "notas" / "guia.md").write_text(
            "# Guía\n\n- Las fechas del puesto van pegadas a la empresa y en gris oscuro siempre.\n", encoding="utf-8")
        (raiz / "notas" / "otra.md").write_text(
            "- Fechas del puesto: pegadas a la empresa y en gris oscuro, siempre.\n", encoding="utf-8")
        cfg = Config(raiz)

        # duplicados
        p = duplicados(raiz, cfg)
        ok(len(p) == 1 and {p[0][1][0], p[0][2][0]} == {"CLAUDE.md", "Casa/CLAUDE.md"}, "un par entre dos archivos")
        ok(all(x[1] < 6 and y[1] < 6 for _, x, y in p), "el bloque de código no cuenta")
        ok(duplicados(raiz, cfg, umbral=1.01) == [], "por encima del umbral no sale nada")
        ok(palabras("Martes y miércoles") == {"martes", "miercoles"}, "palabras sin tildes ni vacías")
        # los sitios de reglas de aprendizajes.csv se suman a las normas
        (raiz / "aprendizajes.csv").write_text(
            "id,fecha,area,patron,que,causa,regla,estado,donde,fuente\n"
            "AG1,2026-11-01,General,p,q,c,r,incorporada,notas/guia.md → fechas,\n"
            "AG2,2026-11-01,General,p,q,c,r,incorporada,notas/otra.md#fechas,\n"
            "AG3,2026-11-01,General,p,q,c,r,pendiente,notas/facturas.md,\n"
            "AG5,2026-11-01,General,p,q,c,r,incorporada,notas/nada.md,\n"
            "AG4,2026-11-01,General,p,q,c,r,incorporada,../fuera.md,\n", encoding="utf-8")
        ok(sitios_de_reglas(raiz, cfg) == (["notas/guia.md", "notas/otra.md"], None),
           "sitios de reglas: incorporadas, que existen y dentro de la carpeta")
        ok(any({x[0], y[0]} == {"notas/guia.md", "notas/otra.md"} for _, x, y in duplicados(raiz, cfg)),
           "una regla repetida en los sitios que da aprendizajes.csv")
        (raiz / "notas" / "guia.md").write_text(
            "# Guía\n\n- Las fechas del puesto van pegadas a la empresa y en gris oscuro siempre.\n\n"
            "- Fechas del puesto: siempre pegadas a la empresa, en gris oscuro.\n", encoding="utf-8")
        ok(not any(x[0] == y[0] for _, x, y in duplicados(raiz, cfg))
           and any(x[0] == y[0] == "notas/guia.md" for _, x, y in duplicados(raiz, cfg, mismo_archivo=True)),
           "dentro de un archivo, solo con --mismo-archivo")
        ok([t for _, t in unidades("- a b\n  sigue\n\npárrafo\n| t | t |\n# T\n")] == ["- a b sigue", "párrafo"],
           "unidades: viñeta con su continuación y párrafo; sin tablas ni títulos")

        # rutas (sin Git)
        ok([t for _, t in rutas_rotas(raiz, cfg)[0]] == ["notas/resumen.md", "notas/x.md"],
           "rutas rotas (desde la raíz o la carpeta del archivo; sin plantillas ni enlaces)")
        # las otras dos formas de citar: [texto](ruta) y [[nombre]]; dentro de código no cuentan
        (raiz / "notas" / "mi guía.md").write_text("x", encoding="utf-8")
        with open(raiz / "Casa" / "CLAUDE.md", "a", encoding="utf-8") as f:
            f.write("- [bien](../notas/facturas.md), [mal](docs/no.md), [espacios](<notas/mi guía.md>), "
                    "[cod](notas/mi%20gu%C3%ADa.md), [web](https://ejemplo.org/a.md), [ancla](#arriba), "
                    "![imagen](img/no.png \"título\").\n"
                    "- [[guia]], [[notas/otra]], [[Facturas|ver]], [[guia#fechas]] y [[no existe]].\n"
                    "- `[falso](no/en/codigo.md)`\n\n```\n[tampoco](no/en/bloque.md) [[tampoco]]\n```\n")
        rotas, miradas = rutas_rotas(raiz, cfg)
        ok([t for a, t in rotas if a == "Casa/CLAUDE.md"] == ["notas/x.md", "[…](docs/no.md)", "[…](img/no.png)",
                                                             "[[no existe]]"],
           "enlaces de Markdown y de nombre rotos; los buenos, externos, anclas y en código no")
        # 4 entre comillas invertidas, 5 enlaces de Markdown (sin la web ni el ancla) y 4 nombres distintos
        ok(miradas == 13, f"cuenta las citas miradas ({miradas})")
        # citas con apartado, línea o puntuación; y rutas dentro de un bloque de código, que no cuentan
        with open(raiz / "Casa" / "CLAUDE.md", "a", encoding="utf-8") as f:
            f.write("- `notas/facturas.md#apartado`, `docs/falta.md#x`, `docs/falta2.md:12` y `notas/guia.md,`\n\n"
                    "```\nver `docs/en_bloque.md`\n```\n")
        rotas, miradas = rutas_rotas(raiz, cfg)
        nuevas = [t for a, t in rotas if a == "Casa/CLAUDE.md" and t.startswith("docs/")]
        ok(nuevas == ["docs/falta.md#x", "docs/falta2.md:12"] and miradas == 17,
           f"citas con apartado o línea se miran; las de un bloque de código no ({nuevas}, {miradas})")
        # nunca un cero que parezca «todo bien»
        with tempfile.TemporaryDirectory() as d2:
            r2 = Path(d2).resolve()
            ok("no hay archivos de normas" in informe_rutas(r2, Config(r2))[0][0]
               and "no hay archivos de normas" in informe_duplicados(r2, Config(r2), 0.5)[0][0],
               "sin archivos de normas, rutas y duplicados lo dicen")
            (r2 / "CLAUDE.md").write_text("- Una regla sin citar ningún archivo.\n", encoding="utf-8")
            ok("ninguna cita que mirar" in informe_rutas(r2, Config(r2))[0][0], "sin citas, rutas lo dice")
            (r2 / "aprendizajes.csv").write_bytes("id,estado,donde\nAG1,incorporada,Revisi\xf3n.md\n".encode("cp1252"))
            ok(any("no está en UTF-8" in l for l in informe_rutas(r2, Config(r2))[0]),
               "un aprendizajes.csv ilegible se avisa, no rompe")

        # largos: normas y «cargados», con su tope; lo demás (historia) no se mira
        ok(largos(raiz, cfg) == ([], 4), "largos: nada pasa del tope en las 4 normas")
        (raiz / "notas" / "diario.md").write_text("x\n" * 500, encoding="utf-8")
        (raiz / "notas" / "skill.md").write_text("x\n" * 201, encoding="utf-8")
        ok(largos(raiz, cfg)[0] == [], "un archivo que no es norma ni «cargados» no se mira")
        ok(sin_clasificar(raiz, cfg) == [(500, "notas/diario.md"), (201, "notas/skill.md")],
           "los largos sin clasificar se enseñan para preguntar")
        (raiz / "control.json").write_text('{"no_cargados": ["notas/diario.md"]}', encoding="utf-8")
        ok(sin_clasificar(raiz, Config(raiz)) == [(201, "notas/skill.md")]
           and "no_cargados" in "\n".join(informe_largos(raiz, Config(raiz))[0]),
           "lo marcado «no_cargados» no se vuelve a preguntar")
        (raiz / "control.json").write_text('{"cargados": ["notas/skill.md"]}', encoding="utf-8")
        cfg_l = Config(raiz)
        ok(largos(raiz, cfg_l)[0] == [(201, "notas/skill.md")] and largos(raiz, cfg_l, 300)[0] == [],
           "largos mira «cargados» y respeta el tope")
        ok(sin_clasificar(raiz, cfg_l) == [(500, "notas/diario.md")], "lo de «cargados» ya no se pregunta")
        (raiz / "control.json").write_text('{"cargados": ["notas/skill.md"], "max_lineas": 100}', encoding="utf-8")
        ok(largos(raiz, Config(raiz))[0] == [(201, "notas/skill.md")] and Config(raiz).max_lineas == 100,
           "el tope sale de control.json")
        (raiz / "control.json").unlink()
        (raiz / "notas" / "diario.md").unlink()
        (raiz / "notas" / "skill.md").unlink()
        with tempfile.TemporaryDirectory() as d3:
            r3 = Path(d3).resolve()
            ok("no hay archivos que mirar" in informe_largos(r3, Config(r3))[0][0], "sin archivos, largos lo dice")
            (r3 / "carpeta.md").mkdir()
            (r3 / "venv").mkdir()
            (r3 / "venv" / "x.md").write_text("x\n" * 300, encoding="utf-8")
            ok(sin_clasificar(r3, Config(r3)) == [], "una carpeta que se llama .md o un venv no rompen largos")
            (r3 / "config.json").write_text('{"normas": ["*.md"]}', encoding="utf-8")
            try:
                Config(r3)
                pide = False
            except SystemExit as e:
                pide = "renómbralo" in str(e)
            ok(pide, "un config.json de antes pide renombrarlo")

        # sin Git, crecimiento lo dice en vez de dar cero
        ok(crecimiento(raiz, cfg, "2000-01-01") is None, "sin Git, crecimiento no da una lista vacía")
        ok(SIN_GIT in informe_crecimiento(raiz, cfg, "2000-01-01")[0][0], "sin Git, el informe lo dice")
        ok(sumar_numstat("10\t2\ta.md\n5\t1\ta.md\n-\t-\tb.png\n") == {"a.md": (15, 3)}, "sumar numstat")
        ok(solo_crecen({"a.md": (40, 1), "b.py": (50, 20), "c.md": (5, 0), "e.md": (30, 3)})
           == [("a.md", 40, 1), ("e.md", 30, 3)], "solo crecen (b quita bastante, c es pequeño; una de diez vale)")
        ok(engordaron({"a.md": (40, 1), "b.md": (5, 9), "c.md": (20, 0)}) == [("a.md", 39), ("c.md", 20)],
           "engordaron: netas positivas, de más a menos")

        # pendientes
        ok(isinstance(parados(raiz, cfg, "2026-11-19"), str) and isinstance(vigentes_viejos(raiz, cfg, "2026-11-19"), str),
           "sin pendientes.csv no se inventa nada")
        (raiz / "pendientes.csv").write_text("tarea,estado,vence\nx,abierta,2026-01-01\n", encoding="utf-8")
        ok("le faltan las columnas id" in informe_parados(raiz, cfg, "2026-11-19")[0][0],
           "un pendientes.csv de otro programa se dice, no rompe")
        (raiz / "pendientes.csv").write_bytes(
            "id,para,estado,creada,vence,que\nT1,Trabajo,abierta,2026-01-01,2026-01-02,Revisión\n".encode("cp1252"))
        ok("no está en UTF-8" in informe_vigentes(raiz, cfg, "2026-11-19", 30)[0][0],
           "un pendientes.csv guardado con Excel se dice, no rompe")
        (raiz / "pendientes.csv").write_text(
            "id,para,estado,prioridad,esfuerzo,permiso,creada,vence,cerrada,que,detalle,nota\n"
            "T1,Trabajo,abierta,,,,2026-11-01,2026-11-10,,Vencida,,\nT2,Trabajo,en curso,,,,2026-10-01,,,Parada,,\n"
            "T3,Trabajo,en curso,,,,2026-11-15,,,Reciente,,\nT4,Trabajo,hecha,,,,2026-10-01,2026-10-02,,Hecha,,\n"
            "T5,Trabajo,abierta,,,,2026-11-01,2026-11-19,,Vence hoy,,\n"
            "C1,Casa,vigente,,,,2026-09-01,,,Viejo,,\nC2,Casa,vigente,,,,2026-11-18,,,Nuevo,,\n", encoding="utf-8")
        ok([x[0] for x in parados(raiz, cfg, "2026-11-19")[0]] == ["T1", "T2"],
           "parados: plazo pasado (no el de hoy) y en curso antiguo; lo hecho no")
        ok([x[1] for x in vigentes_viejos(raiz, cfg, "2026-11-19", 30)[0]] == ["C1"], "vigentes viejos")
        # filas raras: cortas, fechas que no son AAAA-MM-DD; estado con mayúscula o espacio sí cuenta
        with open(raiz / "pendientes.csv", "a", encoding="utf-8") as f:
            f.write("T6,Trabajo,abierta\nT7,Trabajo,abierta,,,,2026-11-01,01/12/2026,,Fecha rara,,\n"
                    "T8,Trabajo, Abierta ,,,,2026-11-01,2026-11-02,,Mayúscula,,\n")
        res = parados(raiz, cfg, "2026-11-19")
        ok([x[0] for x in res[0]] == ["T1", "T2", "T8"] and res[1] == 2,
           f"parados: las filas raras se cuentan y no rompen; el estado se lee sin mayúsculas ({res})")
        ok(any("2 filas" in l for l in informe_parados(raiz, cfg, "2026-11-19")[0]), "el informe dice cuántas raras")

        # pruebas
        (raiz / "bien.py").write_text("print('SELFTEST PASS: 1/1')  # --self-test\n", encoding="utf-8")
        (raiz / "mal.py").write_text("import sys\nprint('SELFTEST FAIL')  # --self-test\nsys.exit(1)\n",
                                     encoding="utf-8")
        (raiz / "sin.py").write_text("print('no tiene autoprueba')\n", encoding="utf-8")
        ok([(f, b) for f, b, _ in pasar_pruebas(raiz, cfg)] == [("bien.py", True), ("mal.py", False)],
           "pruebas de cada herramienta que la tiene")
        ok(informe_pruebas(raiz, cfg)[1] == 1, "una autoprueba que falla da código 1")

        # configuración
        for config in ("{roto", '{"normas": "CLAUDE.md"}', '{"normas": ["../fuera/*.md"]}',
                       '{"omitir": ["nada"]}', '{"archivo_pendientes": "../p.csv"}', '{"max_lineas": 0}',
                       '{"max_lineas": true}', '{"max_lineas": "200"}', '{"cargados": ["../x.md"]}'):
            (raiz / "control.json").write_text(config, encoding="utf-8")
            try:
                Config(raiz)
                fallos.append(f"control.json no válido se acepta: {config}")
            except SystemExit:
                pass
            casos += 1
        (raiz / "control.json").write_text('{"normas": ["notas/*.md"], "herramientas": ["bien.py"], '
                                          '"omitir": ["duplicados"], "areas": {"T": "Trabajo"}}', encoding="utf-8")
        cfg2 = Config(raiz)
        ok(archivos_de_normas(raiz, cfg2) == ["notas/facturas.md", "notas/guia.md", "notas/mi guía.md", "notas/otra.md"]
           and con_prueba(raiz, cfg2) == ["bien.py"], "las normas y las herramientas salen de control.json")
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            rc_semanal = main(["semanal", "--raiz", str(raiz), "--desde", "2026-11-01", "--hoy", "2026-11-19"])
            rc_desde = main(["crecimiento", "--raiz", str(raiz)])
            rc_raiz = main(["rutas", "--raiz", str(raiz / "no-existe")])
        ok(rc_semanal == 0 and "omitida en control.json" in salida.getvalue() and "T2" in salida.getvalue(),
           "semanal pasa todo y se salta lo omitido")
        ok(rc_desde == 2, "crecimiento sin --desde se niega")
        ok(rc_raiz == 1, "--raiz inexistente: error")
        (raiz / "control.json").unlink()

        # con Git: crecimiento y lo ignorado
        if not shutil.which("git"):
            print("Aviso: Git no está instalado; no se prueban crecimiento ni lo ignorado (lo demás no lo necesita).")
        else:
            def en_git(*args):
                subprocess.run(["git", "-c", "user.name=prueba", "-c", "user.email=prueba", "-c", "user.useConfigOnly=false",
                                "-c", "commit.gpgsign=false", *args], cwd=raiz, capture_output=True, check=True)

            en_git("init", "-q")
            (raiz / ".gitignore").write_text("privado/\n", encoding="utf-8")
            (raiz / "CLAUDE.md").write_text("- Ver `privado/datos.csv` y `otro/falta.csv`.\n", encoding="utf-8")
            en_git("add", "-A")
            en_git("commit", "-q", "-m", "uno")
            ok([t for a, t in rutas_rotas(raiz, cfg)[0] if a == "CLAUDE.md"] == ["otro/falta.csv"],
               "lo que Git ignora no es ruta rota")
            (raiz / "CLAUDE.md").write_text("".join(f"- regla número {i}\n" for i in range(40)), encoding="utf-8")
            en_git("add", "-A")
            en_git("commit", "-q", "-m", "dos")
            gordos, solo = crecimiento(raiz, cfg, "2000-01-01")
            ok(gordos and gordos[0][0] == "CLAUDE.md" and any(f == "CLAUDE.md" for f, _, _ in solo),
               "con Git, crecimiento ve lo que solo crece")
            sub = raiz / "Casa"
            ok(crecimiento(sub, Config(sub), "2000-01-01") is not None, "en una subcarpeta del repositorio también")
            (raiz / "Área").mkdir()
            (raiz / "Área" / "CLAUDE.md").write_text("".join(f"- otra {i}\n" for i in range(30)), encoding="utf-8")
            en_git("add", "-A")
            en_git("commit", "-q", "-m", "tres")
            gordos, _ = crecimiento(raiz, cfg, "2000-01-01")
            ok("Área/CLAUDE.md" in [f for f, _ in gordos], f"crecimiento ve las rutas con tilde ({gordos})")
    os.environ.pop("GIT_CEILING_DIRECTORIES", None)
    print(("SELFTEST PASS" if not fallos else "SELFTEST FAIL") + f": {casos - len(fallos)}/{casos}")
    for f in fallos:
        print("  falla:", f)
    return 1 if fallos else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    sys.exit(main())
