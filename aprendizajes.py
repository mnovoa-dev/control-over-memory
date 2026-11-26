#!/usr/bin/env python3
"""Registro único de fallos y aprendizajes de un trabajo con IA: `aprendizajes.csv`.

Cada fila es un caso: qué pasó, por qué (el mecanismo), qué regla lo evita y dónde quedó esa regla.
La columna `patron` junta los casos del mismo tipo para ver lo que se repite. En la revisión periódica
basta con `ver --desde <fecha de la última revisión>`, sin releer todas las conversaciones.

    py aprendizajes.py ver [--area X] [--desde AAAA-MM-DD]
    py aprendizajes.py nuevo --area X --patron P --que "…" --causa "…"
           [--regla "…"] [--donde ruta] [--estado E] [--fecha AAAA-MM-DD] [--fuente "…"] [--patron-nuevo]
    py aprendizajes.py cambiar <id> [--estado E] [--regla …] [--donde …] [--patron …]
    py aprendizajes.py patrones
    py aprendizajes.py comprobar
    py aprendizajes.py resolver      (filas repetidas tras juntar ramas de Git)
    py aprendizajes.py reparar       (CSV roto: deja una copia y lo repara)
    py aprendizajes.py apartadas     (lo que «reparar» no entendió)
    py aprendizajes.py devolver <n> [--fila "…" | --descartar]
    py aprendizajes.py --self-test
Opción común, antes de la orden: --raiz RUTA (por defecto, la carpeta actual).

Un tipo (`--patron`) que no exista se rechaza y se proponen los parecidos: un sinónimo esconde la
repetición. Para crearlo hay que pedirlo expresamente con `--patron-nuevo`.

Estados: `pendiente` (falta una regla), `incorporada` (la regla está en `donde`), `sin regla` (caso
suelto: `regla` dice por qué no hace falta) y `descartada`. Una fila incorporada exige `donde` con una
ruta que exista; lo que vaya tras « → » o «#» en `donde` es el apartado.

Las áreas salen de `control.json` en la raíz (ver `control.ejemplo.json`); sin él, hay una sola: General.
Una orden cada vez: si otra está escribiendo, la segunda espera unos segundos (archivo `.lock`).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import difflib
import io
import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

COLUMNAS = ["id", "fecha", "area", "patron", "que", "causa", "regla", "estado", "donde", "fuente"]
ESTADOS = ["pendiente", "incorporada", "sin regla", "descartada"]
AVANCE = {"pendiente": 0, "descartada": 1, "sin regla": 2, "incorporada": 3}
RE_PATRON = re.compile(r"^[a-z0-9ñ]+(-[a-z0-9ñ]+)*\Z")


def aviso_config_vieja(raiz):
    """Las primeras versiones leían `config.json`. Si está ese y no `control.json`, y es de estas herramientas
    (tiene alguna de sus claves), se para: seguir sin él cambiaría las áreas o los archivos sin avisar."""
    vieja = Path(raiz) / "config.json"
    if (Path(raiz) / "control.json").exists() or not vieja.is_file():
        return
    try:
        datos = json.loads(vieja.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeDecodeError, OSError):
        return
    if isinstance(datos, dict) and {"areas", "persona", "archivo_pendientes", "archivo_aprendizajes"} & set(datos):
        raise SystemExit("✗ Hay un config.json de una versión anterior: renómbralo a control.json (el contenido vale tal cual).")


class Config:
    """Lo que cambia de un usuario a otro: las áreas (letra → nombre) y el nombre del archivo."""

    def __init__(self, raiz: Path):
        datos = {}
        aviso_config_vieja(raiz)
        ruta = raiz / "control.json"
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise SystemExit(f"✗ control.json no es un JSON válido: {e}")
            if not isinstance(datos, dict):
                raise SystemExit("✗ control.json debe ser un objeto: {\"areas\": {...}}")
        self.areas: dict[str, str] = datos.get("areas") or {"G": "General"}
        if not isinstance(self.areas, dict):
            raise SystemExit("✗ control.json: «areas» debe ser un objeto letra → nombre")
        for letra, nombre in self.areas.items():
            if not re.fullmatch(r"[A-Z]", letra):
                raise SystemExit(f"✗ control.json: la letra de área «{letra}» debe ser una sola mayúscula")
            if not isinstance(nombre, str) or not nombre.strip():
                raise SystemExit(f"✗ control.json: el área {letra} necesita un nombre")
        if len({n.lower() for n in self.areas.values()}) != len(self.areas):
            raise SystemExit("✗ control.json: dos áreas con el mismo nombre")
        self.archivo = datos.get("archivo_aprendizajes") or "aprendizajes.csv"
        if not isinstance(self.archivo, str) or Path(self.archivo).name != self.archivo \
                or not self.archivo.endswith(".csv"):
            raise SystemExit("✗ control.json: «archivo_aprendizajes» debe ser un nombre .csv, sin carpetas")
        self.letra = {v.lower(): k for k, v in self.areas.items()}
        self.orden = "".join(self.areas)
        self.re_id = re.compile(rf"^A([{self.orden}])([1-9][0-9]*)\Z")


def leer(ruta: Path) -> tuple[list[str] | None, list[dict]]:
    """Devuelve (cabecera, filas); (None, []) si el archivo no existe."""
    if not ruta.is_file():
        return None, []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        filas = [dict(fila) for fila in lector]
        if lector.fieldnames is None:  # vacío (0 bytes o solo espacios): como si no existiera
            return None, []
        return lector.fieldnames, filas


def escribir(ruta: Path, filas: list[dict], cfg: Config) -> None:
    """UTF-8 con BOM (Excel lo abre bien) y fin de línea LF (Git no lo cambia entre sistemas). Se escribe
    en un temporal de la misma carpeta y se cambia por el bueno de una vez: si algo falla a mitad, la
    tabla anterior sigue entera."""
    def orden_id(fila: dict):
        m = cfg.re_id.match(fila.get("id") or "")
        return (cfg.orden.index(m.group(1)), int(m.group(2))) if m else (99, 0)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNAS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for fila in sorted(filas, key=orden_id):
        w.writerow({c: (fila.get(c) or "").strip() for c in COLUMNAS})
    temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
    try:
        temporal.write_text(buf.getvalue(), encoding="utf-8-sig", newline="")
        # En Windows no se puede sustituir un archivo que otro proceso tiene abierto, y las órdenes que solo
        # leen no esperan al candado: se reintenta un momento (leer dura milisegundos).
        for intento in range(40):
            try:
                os.replace(temporal, ruta)
                break
            except PermissionError:
                if intento == 39:
                    raise
                time.sleep(0.05)
    finally:
        temporal.unlink(missing_ok=True)


@contextlib.contextmanager
def candado(ruta: Path, espera: float = 10.0):
    """Una orden cada vez: la segunda espera a que acabe la primera (como mucho `espera` segundos). El
    sistema suelta el candado cuando el proceso termina, aunque sea a la fuerza: nunca queda bloqueado."""
    fh = open(ruta.with_name(ruta.name + ".lock"), "a+b")
    cogido = False
    try:
        limite = time.monotonic() + espera
        while not cogido:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                cogido = True
            except OSError:
                if time.monotonic() > limite:
                    raise SystemExit(f"✗ Otra orden está usando {ruta.name}; espera a que termine y repite.")
                time.sleep(0.05)
        yield
    finally:
        if cogido and os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        fh.close()


def forma_rota(fila: dict) -> bool:
    """Columnas de más (la clave None de csv) o de menos (valores None): la fila no se puede leer bien."""
    return None in fila or None in fila.values()


csv.field_size_limit(10_000_000)  # una comilla sin cerrar puede dejar casi todo el archivo en un campo


def leer_linea(texto, n, es_id, sep=","):
    """Las celdas si el texto, leído tal cual, es una sola fila de n columnas con código válido."""
    try:
        registros = list(csv.reader(io.StringIO(texto, newline=""), strict=True, delimiter=sep))
    except csv.Error:
        return None
    if len(registros) != 1 or len(registros[0]) != n or not es_id(registros[0][0].strip()):
        return None
    return registros[0]


def partir(linea, n, es_id, sep=","):
    """Como leer_linea, tolerando una comilla que sobra en una línea del archivo."""
    celdas = leer_linea(linea, n, es_id, sep)
    if celdas is None and linea.count('"') % 2:
        if linea.endswith('"') and (celdas := leer_linea(linea[:-1], n, es_id, sep)):
            return celdas
        posibles = [c for i, letra in enumerate(linea)
                    if letra == '"' and (c := leer_linea(linea[:i] + linea[i + 1:], n, es_id, sep))]
        if len({tuple(c) for c in posibles}) == 1:  # si cabe más de una lectura, no se adivina
            celdas = posibles[0]
    return celdas


def registros_crudos(texto):
    """(celdas, líneas del archivo) de cada registro del CSV, cabecera incluida."""
    lineas = re.split(r"\r\n|\n|\r", texto)
    lector = csv.reader(io.StringIO(texto, newline=""))
    desde = 0
    for celdas in lector:
        trozo, desde = lineas[desde:lector.line_num], lector.line_num
        yield celdas, trozo


def tragadas(texto, n, es_id):
    """True si alguna fila completa está metida dentro de un campo de otra: escribir encima la borraría."""
    return any(partir(linea, n, es_id) for _, trozo in list(registros_crudos(texto))[1:] for linea in trozo[1:])


def rehacer(texto, n, es_id, sep=","):
    """(filas, apartadas), línea a línea (las herramientas escriben una fila por línea): cada línea que es una fila
    vuelve a serlo; un texto partido en varias líneas se une con espacios; lo que no se entiende se aparta tal
    cual. Nada se descarta."""
    lineas = re.split(r"\r\n|\n|\r", texto)[1:]
    filas, apartadas, i = [], [], 0
    while i < len(lineas):
        if not lineas[i].strip():
            i += 1
            continue
        fila, fin = leer_linea(lineas[i], n, es_id, sep), i
        if fila is None:  # ¿un campo con saltos de línea? Se juntan líneas sin tragarse otra fila
            for j in range(i + 1, min(i + 60, len(lineas))):
                if partir(lineas[j], n, es_id, sep):
                    break
                fila = leer_linea("\n".join(lineas[i:j + 1]), n, es_id, sep)
                if fila:
                    fila, fin = [" ".join(c.split()) if "\n" in c else c for c in fila], j
                    break
        if fila is None:
            fila = partir(lineas[i], n, es_id, sep)
        if fila:
            filas.append(fila)
        else:
            apartadas.append(lineas[i])
        i = fin + 1
    return filas, apartadas


def leer_cabecera_rota(lineas, columnas, es_id):
    """Qué hacer con la primera línea: (separador, líneas de datos) o None si no se puede saber sin adivinar."""
    for sep in (",", ";"):
        if next(csv.reader(lineas[:1], delimiter=sep), []) == columnas:
            return sep, lineas[1:]
    falta = partir(lineas[0], len(columnas), es_id) is not None  # la primera línea ya es una fila
    datos = lineas if falta else lineas[1:]
    if not falta:
        nombres = [c.strip().strip('"').strip() for c in re.split(r"[,;]", lineas[0])]
        if len(nombres) >= len(columnas) and any(
                c in columnas and (i >= len(columnas) or columnas[i] != c) for i, c in enumerate(nombres)):
            return None  # columnas en otro orden o de más: reponer los nombres cambiaría lo que significa cada dato
    llenas = [linea for linea in datos if linea.strip()]
    completas = sum(1 for linea in llenas if partir(linea, len(columnas), es_id))
    return (",", datos) if completas and completas * 2 >= len(llenas) else None


def reparar_archivo(ruta, columnas, es_id, guardar):
    """Repara un CSV sin depender de Git: antes de tocarlo deja una copia al lado. Devuelve (bien, mensajes)."""
    texto = ruta.read_text(encoding="utf-8-sig")
    lineas = re.split(r"\r\n|\n|\r", texto)
    arreglo = leer_cabecera_rota(lineas, columnas, es_id) if texto.strip() else None
    if arreglo is None:
        return False, [f"✗ La primera línea de {ruta.name} no es la cabecera esperada y las filas no permiten "
                       "reponerla sin adivinar: no se toca."]
    sep, datos = arreglo
    cabecera_bien = sep == "," and next(csv.reader(lineas[:1]), []) == columnas
    filas, apartadas = rehacer("\n".join(["cabecera", *datos]), len(columnas), es_id, sep)
    antiguas = [celdas for celdas, _ in registros_crudos(texto)]
    if cabecera_bien and not apartadas and filas == [c for c in antiguas[1:] if c]:
        return True, [f"{ruta.name}: nada que reparar."]
    sello = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    copia = ruta.with_name(f".{ruta.name}.{sello}.copia")
    copia.write_bytes(ruta.read_bytes())
    mensajes = [f"{ruta.name}: copia de cómo estaba en {copia.name}"]
    if sep == ";":
        mensajes.append("  separado por «;»: vuelve a comas")
    elif not cabecera_bien:
        mensajes.append("  se repone la primera línea (los nombres de las columnas)")
    if apartadas:
        aparte = ruta_apartadas(ruta)
        with open(aparte, "a", encoding="utf-8", newline="") as f:
            f.write(f"# {sello}\n" + "".join(f"? {linea}\n" for linea in apartadas))
        mensajes.append(f"  ! {len(apartadas)} líneas que no se entienden, apartadas tal cual en {aparte.name}: "
                        "míralas con «apartadas»")
    guardar([dict(zip(columnas, f)) for f in filas])
    mensajes.append(f"  {ruta.name}: {len(filas)} filas")
    return True, mensajes


def ruta_apartadas(ruta):
    return ruta.with_name(ruta.name + ".apartadas.txt")


def lineas_apartadas(ruta):
    archivo = ruta_apartadas(ruta)
    return archivo.read_text(encoding="utf-8").split("\n") if archivo.is_file() else []


def sin_revisar(*rutas):
    """(tabla, posición, línea) de cada línea apartada que sigue sin revisar («? » delante)."""
    return [(r, i, linea[2:]) for r in rutas for i, linea in enumerate(lineas_apartadas(r)) if linea.startswith("? ")]


def codigo_de(linea):
    return re.split(r"[,;]", linea, maxsplit=1)[0].strip().strip('"').strip()


def codigos_apartados(*rutas):
    """Los códigos de todo lo que se apartó alguna vez: quedan reservados, «nuevo» no los reutiliza."""
    return [{"id": codigo_de(linea.split(" ", 1)[1])} for r in rutas for linea in lineas_apartadas(r)
            if linea.startswith(("? ", "# devuelta: ", "# descartada: "))]


def aviso_apartadas(*rutas):
    n = len(sin_revisar(*rutas))
    return [f"Quedan {n} líneas apartadas sin revisar: «apartadas» las enseña y «devolver» las recupera."] if n else []


def listar_apartadas(*rutas):
    return [f"  {n}. {linea}" for n, (_, _, linea) in enumerate(sin_revisar(*rutas), 1)] or ["No hay líneas apartadas."]


def devolver(rutas, numero, corregida, descartar, columnas, es_id, usados, anadir):
    """Devuelve a su tabla una línea apartada, con su mismo código. `anadir(tabla, fila)` escribe y da [] o los
    errores que lo impiden. Nada se borra del archivo de apartadas: la línea queda marcada."""
    quedan = sin_revisar(*rutas)
    if not 1 <= numero <= len(quedan):
        return 1, [f"✗ No hay línea apartada número {numero} (mira «apartadas»)."]
    ruta, posicion, original = quedan[numero - 1]

    def marcar(marca):
        lineas = lineas_apartadas(ruta)
        lineas[posicion] = f"# {marca}: {original}"
        ruta_apartadas(ruta).write_text("\n".join(lineas), encoding="utf-8", newline="")

    if descartar:
        marcar("descartada")
        return 0, [f"Descartada (queda escrita en {ruta_apartadas(ruta).name})."]
    celdas = partir(corregida or original, len(columnas), es_id)
    if celdas is None:
        return 1, [f"✗ No es una fila completa de {len(columnas)} columnas con un código válido. Pásala corregida con "
                   f"--fila. Columnas: {','.join(columnas)}"]
    codigo = celdas[0].strip()
    if es_id(codigo_de(original)) and codigo != codigo_de(original):
        return 1, [f"✗ La línea apartada es {codigo_de(original)}: conserva ese código."]
    if codigo in usados:
        return 1, [f"✗ {codigo} ya está en la tabla."]
    errores = anadir(ruta, dict(zip(columnas, celdas)))
    if errores:
        return 1, ["✗ No se devuelve: la tabla quedaría con errores:"] + errores
    marcar("devuelta")
    return 0, [f"{codigo} devuelta a {ruta.name}."]


def mete_salto(antes, despues):
    """True si un campo cambiado trae un salto de línea (aunque ese campo ya tuviera otro)."""
    return any(v != antes.get(c) and re.search(r"[\r\n]", v or "") for c, v in despues.items())


def pisa_salto(antes, despues):
    """El campo que se cambia y ya tenía un salto de línea (o None). Ese salto puede ser una comilla sin cerrar que
    se tragó líneas que no parecen filas: cambiar el campo las borraría sin avisar."""
    return next((c for c, v in despues.items() if v != antes.get(c) and re.search(r"[\r\n]", antes.get(c) or "")), None)


def ruta_de(donde: str) -> str:
    return re.split(r" → |#", donde, maxsplit=1)[0].strip().strip("`")


def comprobar(filas: list[dict], raiz: Path, cfg: Config, cabecera: list[str] | None = None) -> list[str]:
    errores = []
    if cabecera is not None and cabecera != COLUMNAS:
        errores.append(f"columnas distintas de las esperadas: {cabecera}")
    vistos = defaultdict(int)
    for n, f in enumerate(filas, start=2):
        ident = f.get("id") or ""
        vistos[ident] += 1
        if None in f:
            errores.append(f"línea {n} ({ident}): más columnas que la cabecera (¿una coma sin comillas?)")
        faltan = [c for c in COLUMNAS if c in f and f[c] is None]
        if faltan:
            errores.append(f"línea {n} ({ident}): tiene {len(COLUMNAS) - len(faltan)} columnas de {len(COLUMNAS)}; "
                           f"faltan {', '.join(faltan)} (¿una coma de menos o la fila cortada al copiarla?)")
            continue  # el resto de controles solo repetiría el mismo problema con otras palabras
        saltos = [c for c in COLUMNAS if re.search(r"[\r\n]", f.get(c) or "")]
        if saltos:
            errores.append(f"línea {n} ({ident}): salto de línea en {', '.join(saltos)} (¿una comilla sin "
                           "cerrar que se ha tragado las filas siguientes?)")
        m = cfg.re_id.match(ident)
        if not m:
            errores.append(f"línea {n}: id «{ident}» no es A + letra de área + número sin ceros delante")
        elif cfg.areas[m.group(1)] != f.get("area"):
            errores.append(f"{ident}: la letra del id no corresponde al área «{f.get('area')}»")
        try:
            dt.date.fromisoformat(f.get("fecha") or "")
        except ValueError:
            errores.append(f"{ident}: fecha «{f.get('fecha')}» no es AAAA-MM-DD")
        if f.get("estado") not in ESTADOS:
            errores.append(f"{ident}: estado «{f.get('estado')}» no es uno de {ESTADOS}")
        if not RE_PATRON.match(f.get("patron") or ""):
            errores.append(f"{ident}: patrón «{f.get('patron')}» debe ir en minúsculas con guiones")
        for campo in ("que", "causa"):
            if not (f.get(campo) or "").strip():
                errores.append(f"{ident}: falta «{campo}»")
        if f.get("estado") == "incorporada":
            if not f.get("donde"):
                errores.append(f"{ident}: incorporada sin «donde»")
            elif not (raiz / ruta_de(f["donde"])).exists():
                errores.append(f"{ident}: «donde» apunta a {ruta_de(f['donde'])}, que no existe")
        if f.get("estado") == "sin regla" and not f.get("regla"):
            errores.append(f"{ident}: «sin regla» necesita en «regla» el motivo")
    for ident, veces in vistos.items():
        if veces > 1:
            errores.append(f"{ident}: repetido {veces} veces (tras juntar ramas: «resolver»)")
    return errores


def siguiente_id(filas: list[dict], letra: str, cfg: Config) -> str:
    numeros = [int(m.group(2)) for f in filas if (m := cfg.re_id.match(f.get("id") or "")) and m.group(1) == letra]
    return f"A{letra}{max(numeros, default=0) + 1}"


def corto(texto: str, n: int = 110) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def parecidos(patron: str, existentes: set[str], n: int = 3) -> list[str]:
    """Los tipos ya usados que más se parecen a uno nuevo: por letras y por palabras compartidas."""
    palabras = set(patron.split("-"))

    def nota(p: str) -> float:
        return difflib.SequenceMatcher(None, patron, p).ratio() + len(palabras & set(p.split("-"))) * 0.5
    return sorted(existentes, key=lambda p: -nota(p))[:n]


def ver(filas: list[dict], area: str | None = None, desde: str | None = None) -> str:
    """Lo que la revisión tiene que mirar: los casos sin regla, juntando los que se repiten, y los tipos
    que vuelven aunque ya tenían regla (en cualquier estado: un caso nacido «incorporada» también cuenta)."""
    por_patron = defaultdict(list)
    for f in filas:
        por_patron[f["patron"]].append(f)
    en_ventana = [f for f in filas if (not desde or f["fecha"] >= desde) and (not area or f["area"] == area)]
    vuelven = []
    if desde:
        nuevos = defaultdict(list)
        for f in en_ventana:
            nuevos[f["patron"]].append(f)
        for p, casos in nuevos.items():
            antes = [f for f in por_patron[p] if f["fecha"] < desde and f["estado"] == "incorporada"]
            if antes:
                vuelven.append((p, casos, sorted({f["donde"] for f in antes})))
    abiertos = defaultdict(list)
    for f in en_ventana:
        if f["estado"] == "pendiente":
            abiertos[f["patron"]].append(f)
    salida = []
    repetidos = sorted(((p, c) for p, c in abiertos.items() if len(c) > 1), key=lambda x: (-len(x[1]), x[0]))
    if repetidos:
        salida.append("SE REPITE Y SIGUE SIN REGLA (proponer una, o reforzar la que ya hay)")
        for patron, casos in repetidos:
            reglas = sorted({f["donde"] for f in por_patron[patron] if f["estado"] == "incorporada"})
            salida.append(f"  {patron}: {len(casos)} casos sin regla"
                          + (f"; reglas ya escritas para otros casos: {'; '.join(reglas)}" if reglas else ""))
            for f in sorted(casos, key=lambda f: f["fecha"]):
                salida.append(f"    {f['id']} {f['fecha']} {corto(f['que'])}")
            for r in sorted({f["regla"] for f in casos if f["regla"]}):
                salida.append(f"    → {corto(r, 160)}")
    sueltos = sorted((c[0] for p, c in abiertos.items() if len(c) == 1), key=lambda f: f["fecha"])
    if sueltos:
        salida.append("SIN REGLA, UN SOLO CASO")
        for f in sueltos:
            salida.append(f"  {f['id']} {f['fecha']} {f['patron']}: {corto(f['que'])}")
    if vuelven:
        salida.append("VUELVE AUNQUE YA TENÍA REGLA (la regla escrita no basta: mejor un control que no dependa "
                      "de la memoria del modelo)")
        for patron, casos, donde in sorted(vuelven, key=lambda x: (-len(x[1]), x[0])):
            salida.append(f"  {patron}: {len(casos)} casos nuevos ({', '.join(f['id'] for f in casos)}); "
                          f"regla en: {'; '.join(donde)}")
    cuenta = defaultdict(int)
    for f in en_ventana:
        cuenta[f["estado"]] += 1
    resumen = ", ".join(f"{e} {cuenta[e]}" for e in ESTADOS if cuenta[e])
    salida.append(f"{len(en_ventana)} casos{' desde ' + desde if desde else ''}{' de ' + area if area else ''}: "
                  f"{resumen or 'ninguno'}")
    return "\n".join(salida)


def patrones(filas: list[dict]) -> str:
    por = defaultdict(list)
    for f in filas:
        por[f["patron"]].append(f)
    lineas = []
    for p, casos in sorted(por.items(), key=lambda x: (-len(x[1]), x[0])):
        estados = ", ".join(sorted({f["estado"] for f in casos}))
        lineas.append(f"{len(casos):3d}  {p}  ({estados})")
    return "\n".join(lineas)


def resolver(filas: list[dict], cfg: Config, reservados=()) -> tuple[list[dict], list[str]]:
    """Tras juntar dos ramas con `merge=union`, deja una fila por id sin perder casos.

    Si las versiones de un id cuentan casos distintos (otro `que`), son dos casos nuevos que dos ramas
    numeraron igual: el segundo recibe un código nuevo. Si cuentan el mismo caso, queda la versión más
    avanzada (incorporada > sin regla > descartada > pendiente).
    """
    grupos: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
        grupos[f.get("id") or ""].append(f)
    salida, avisos, renumerar = [], [], []
    for ident, versiones in grupos.items():
        unicas = [dict(t) for t in dict.fromkeys(tuple(sorted(v.items())) for v in versiones)]
        por_caso: dict[str, list[dict]] = defaultdict(list)
        for v in unicas:
            por_caso[" ".join((v.get("que") or "").split())].append(v)
        for k, variantes in enumerate(por_caso.values()):
            elegida = max(variantes, key=lambda f: AVANCE.get(f.get("estado"), -1))
            if len(variantes) > 1:
                avisos.append(f"{ident}: {len(variantes)} versiones del mismo caso; queda la de estado "
                              f"«{elegida.get('estado')}». Revísala con «cambiar».")
            (salida if k == 0 else renumerar).append(elegida)
    for f in renumerar:
        m = cfg.re_id.match(f.get("id") or "")
        if not m:  # un id no válido no se puede renumerar: se deja y «comprobar» lo señalará
            salida.append(f)
            continue
        nuevo = siguiente_id(salida + renumerar + list(reservados), m.group(1), cfg)
        avisos.append(f"{f['id']}: dos casos distintos con el mismo código; el segundo pasa a {nuevo}.")
        f["id"] = nuevo
        salida.append(f)
    return salida, avisos


def resolver_area(texto: str, cfg: Config) -> str:
    t = texto.strip().lower()
    for nombre in cfg.areas.values():
        if t == nombre.lower():
            return nombre
    raise SystemExit(f"Área «{texto}» desconocida: {', '.join(cfg.areas.values())}")


def crear_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--raiz", default=".")
    sub = p.add_subparsers(dest="orden")
    v = sub.add_parser("ver")
    v.add_argument("--area")
    v.add_argument("--desde")
    n = sub.add_parser("nuevo")
    for campo in ("area", "patron", "que", "causa"):
        n.add_argument("--" + campo, required=True)
    for campo in ("regla", "donde", "estado", "fecha", "fuente"):
        n.add_argument("--" + campo, default="")
    c = sub.add_parser("cambiar")
    c.add_argument("id")
    for campo in ("estado", "regla", "donde", "patron", "que", "causa", "fuente"):
        c.add_argument("--" + campo)
    for orden in (n, c):
        orden.add_argument("--patron-nuevo", action="store_true",
                           help="el tipo no existe aún y ninguno de los parecidos vale")
    sub.add_parser("patrones")
    sub.add_parser("comprobar")
    sub.add_parser("resolver")
    sub.add_parser("reparar")
    sub.add_parser("apartadas")
    p_d = sub.add_parser("devolver")
    p_d.add_argument("numero", type=int)
    p_d.add_argument("--fila")
    p_d.add_argument("--descartar", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    for flujo in (sys.stdout, sys.stderr):
        # En Windows, con la salida a un tubo, Python puede escribir en cp1252: se fuerza UTF-8 para
        # que «·», «✗» o «→» no rompan nada.
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    a = crear_parser().parse_args(argv)
    if a.self_test:
        return self_test()
    raiz = Path(a.raiz).resolve()
    if not raiz.is_dir():
        print(f"✗ La carpeta {raiz} no existe (¿--raiz mal escrito?).")
        return 1
    cfg = Config(raiz)
    ruta = raiz / cfg.archivo
    # Solo esperan su turno las órdenes que escriben; las que leen ven la tabla entera igual, porque se
    # escribe de una vez.
    with contextlib.nullcontext() if a.orden in SOLO_LEEN else candado(ruta):
        return ejecutar(a, raiz, cfg, ruta)


class Tabla:
    """Lo que necesita cada orden: la tabla leída una vez y su comprobación."""

    def __init__(self, raiz: Path, cfg: Config, ruta: Path):
        self.raiz, self.cfg, self.ruta = raiz, cfg, ruta
        self.cabecera, self.filas = leer(ruta)
        self.errores = comprobar(self.filas, raiz, cfg, self.cabecera)


def o_comprobar(t: Tabla, a: argparse.Namespace) -> int:
    print("\n".join(t.errores) if t.errores else f"OK: {len(t.filas)} casos, sin errores")
    for aviso in aviso_apartadas(t.ruta):
        print("AVISO: " + aviso)
    return 1 if t.errores else 0


def o_resolver(t: Tabla, a: argparse.Namespace) -> int:
    if t.cabecera is None:
        print(f"No existe {t.cfg.archivo}: no hay nada que resolver.")
        return 0
    if tragadas(t.ruta.read_text(encoding="utf-8-sig"), len(COLUMNAS), t.cfg.re_id.match):
        print("✗ No se escribe: hay filas metidas dentro de un campo (¿una comilla sin cerrar?). Escribir encima las "
              "borraría. Ejecuta «reparar»: guarda una copia y las devuelve a su sitio.")
        return 1
    filas, avisos = resolver(t.filas, t.cfg, codigos_apartados(t.ruta))
    escribir(t.ruta, filas, t.cfg)
    print("\n".join(avisos) or "No había filas repetidas que juntar.")
    errores = comprobar(filas, t.raiz, t.cfg, COLUMNAS)
    if errores:  # por ejemplo, un código no válido repetido: «resolver» no sabe renumerarlo
        print("✗ Quedan errores que «resolver» no arregla:\n" + "\n".join(errores))
    return 1 if errores else 0


def o_reparar(t: Tabla, a: argparse.Namespace) -> int:
    if not t.ruta.is_file():
        print(f"No existe {t.cfg.archivo}: no hay nada que reparar.")
        return 0
    bien, mensajes = reparar_archivo(t.ruta, COLUMNAS, t.cfg.re_id.match, lambda filas: escribir(t.ruta, filas, t.cfg))
    errores = Tabla(t.raiz, t.cfg, t.ruta).errores if bien else []
    print("\n".join(mensajes + errores))
    return 0 if bien and not errores else 1


def o_apartadas(t: Tabla, a: argparse.Namespace) -> int:
    print("\n".join(listar_apartadas(t.ruta)))
    return 0


def o_devolver(t: Tabla, a: argparse.Namespace) -> int:
    def anadir(destino, fila):
        if any(forma_rota(f) for f in t.filas):
            return ["la tabla tiene filas rotas: ejecuta antes «reparar»"]
        antes = comprobar(t.filas, t.raiz, t.cfg)
        nuevos = [e for e in comprobar(t.filas + [fila], t.raiz, t.cfg) if e not in antes]
        if not nuevos:
            escribir(destino, t.filas + [fila], t.cfg)
        return nuevos
    usados = {f.get("id") for f in t.filas}
    rc, mensajes = devolver((t.ruta,), a.numero, a.fila, a.descartar, COLUMNAS, t.cfg.re_id.match, usados, anadir)
    print("\n".join(mensajes))
    return rc


def o_ver(t: Tabla, a: argparse.Namespace) -> int:
    area = resolver_area(a.area, t.cfg) if getattr(a, "area", None) else None
    desde = getattr(a, "desde", None)
    if desde:
        try:
            dt.date.fromisoformat(desde)
        except ValueError:
            print(f"✗ --desde «{desde}» no es una fecha AAAA-MM-DD.")
            return 1
    print(ver(t.filas, area, desde))
    if t.errores:
        print("AVISO: la tabla tiene errores («comprobar»).")
    for aviso in aviso_apartadas(t.ruta):
        print("AVISO: " + aviso)
    return 0


def o_patrones(t: Tabla, a: argparse.Namespace) -> int:
    print(patrones(t.filas))
    return 0


def patron_conocido(t: Tabla, a: argparse.Namespace) -> bool:
    """Un sinónimo («cambio-no-pedido» por «cambiar-sin-que-lo-pida») esconde que el fallo se repite."""
    existentes = {f["patron"] for f in t.filas}
    patron = (a.patron or "").strip().lower()
    if not patron or not existentes or patron in existentes or a.patron_nuevo:
        return True
    print(f"El tipo «{patron}» no existe. Los más parecidos: {', '.join(parecidos(patron, existentes))}. "
          f"Usa uno de ellos o, si ninguno vale, repite la orden con --patron-nuevo (lista entera: «patrones»).")
    return False


def guardar(t: Tabla, fila: dict, antes: list[str]) -> int:
    """Escribe si la tabla resultante no trae ningún error que no estuviera en `antes`, y lo cuenta."""
    despues = comprobar(t.filas, t.raiz, t.cfg)
    nuevos = [e for e in despues if e not in antes]
    if nuevos:
        print("No se escribe:\n" + "\n".join(nuevos))
        return 1
    escribir(t.ruta, t.filas, t.cfg)
    if despues:
        print("AVISO: la tabla sigue con errores («comprobar»):\n" + "\n".join(despues))
    iguales = [f for f in t.filas if f["patron"] == fila["patron"]]
    print(f"{fila['id']} [{fila['estado']}] {fila['patron']}: {corto(fila['que'])}")
    if len(iguales) > 1:
        print(f"  Este patrón ya tiene {len(iguales)} casos: {', '.join(f['id'] for f in iguales)}")
    return 0


def o_nuevo(t: Tabla, a: argparse.Namespace) -> int:
    if t.errores:
        print("La tabla tiene errores; corrígelos antes (con «cambiar»):\n" + "\n".join(t.errores))
        return 1
    if not patron_conocido(t, a):
        return 1
    area = resolver_area(a.area, t.cfg)
    fila = {
        "id": siguiente_id(t.filas + codigos_apartados(t.ruta), t.cfg.letra[area.lower()], t.cfg), "fecha": a.fecha or dt.date.today().isoformat(),
        "area": area, "patron": a.patron.strip().lower(), "que": a.que, "causa": a.causa,
        "regla": a.regla, "donde": a.donde,
        "estado": a.estado or ("incorporada" if a.donde else "pendiente"), "fuente": a.fuente,
    }
    t.filas.append(fila)
    return guardar(t, fila, [])


def o_cambiar(t: Tabla, a: argparse.Namespace) -> int:
    if not patron_conocido(t, a):
        return 1
    fila = next((f for f in t.filas if f["id"] == a.id.strip().upper()), None)
    if fila is None:
        print(f"No existe {a.id}")
        return 1
    if tragadas(t.ruta.read_text(encoding="utf-8-sig"), len(COLUMNAS), t.cfg.re_id.match):
        print("✗ No se escribe: hay filas metidas dentro de un campo (¿una comilla sin cerrar?). Escribir encima las "
              "borraría. Ejecuta «reparar»: guarda una copia y las devuelve a su sitio.")
        return 1
    copia = dict(fila)
    for campo in ("estado", "regla", "donde", "patron", "que", "causa", "fuente"):
        valor = getattr(a, campo)
        if valor is not None:
            fila[campo] = valor.strip().lower() if campo == "patron" else valor
    # «cambiar» tiene que poder arreglar una tabla con errores (por ejemplo, si se borró el archivo de una
    # regla): se escribe si no añade ningún error nuevo.
    campo = pisa_salto(copia, fila)
    if campo:
        print(f"✗ No se escribe: «{campo}» tiene varias líneas (¿una comilla sin cerrar se tragó otras?) y cambiarlo "
              "las borraría. Ejecuta «reparar»: guarda una copia y aparta lo que no entiende.")
        return 1
    if mete_salto(copia, fila):
        print("✗ No se escribe: un campo no admite saltos de línea.")
        return 1
    return guardar(t, fila, t.errores)


SOLO_LEEN = {"ver", None, "patrones", "comprobar", "apartadas"}
ORDENES = {"ver": o_ver, None: o_ver, "patrones": o_patrones, "comprobar": o_comprobar,
           "resolver": o_resolver, "reparar": o_reparar, "apartadas": o_apartadas, "devolver": o_devolver, "nuevo": o_nuevo, "cambiar": o_cambiar}


def ejecutar(a: argparse.Namespace, raiz: Path, cfg: Config, ruta: Path) -> int:
    t = Tabla(raiz, cfg, ruta)
    if t.cabecera is not None and (t.cabecera != COLUMNAS or any(forma_rota(f) for f in t.filas)) \
            and a.orden not in ("comprobar", "reparar", "apartadas"):
        print(f"✗ {cfg.archivo} no tiene las columnas esperadas en todas sus filas; mira «comprobar» y "
              "corrígelo antes.")
        return 1
    return ORDENES[a.orden](t, a)


def principal() -> int:
    """Un fallo del sistema (sin permiso para escribir, un CSV que no está en UTF-8…) sale como mensaje
    y código 1, no como un volcado de Python."""
    try:
        return main()
    except UnicodeDecodeError as e:
        print(f"✗ Un archivo no está en UTF-8 (¿guardado desde otro programa?): {e}")
    except OSError as e:
        print(f"✗ {e.strerror or e}: {e.filename or ''}")
    except csv.Error as e:
        print(f"✗ La tabla no se puede leer como CSV ({e}): prueba «reparar», que guarda una copia antes.")
    return 1


def self_test() -> int:
    casos, fallos = 0, []

    def prueba(cumple: bool, que: str) -> None:
        nonlocal casos
        casos += 1
        if not cumple:
            fallos.append(que)

    with tempfile.TemporaryDirectory() as d:
        raiz = Path(d)
        (raiz / "control.json").write_text(json.dumps(
            {"areas": {"T": "Trabajo", "C": "Casa", "E": "Estudios"}}), encoding="utf-8")
        (raiz / "regla.md").write_text("x", encoding="utf-8")
        cfg = Config(raiz)
        ruta = raiz / cfg.archivo

        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                try:
                    rc = main(["--raiz", str(raiz), *args])
                except SystemExit as e:  # un dato no válido para el programa con un mensaje
                    print(e)
                    rc = 1
            return rc, out.getvalue()

        def filas_de():
            return leer(ruta)[1]

        def base(**cambio):
            fila = {"id": "AT1", "fecha": "2026-11-01", "area": "Trabajo", "patron": "fecha-equivocada",
                    "que": "a", "causa": "b", "regla": "", "estado": "pendiente", "donde": "", "fuente": ""}
            return comprobar([dict(fila, **cambio)], raiz, cfg, COLUMNAS)

        rc, _ = run("nuevo", "--area", "Trabajo", "--patron", "fecha-equivocada", "--que", "a", "--causa", "b",
                    "--fecha", "2026-11-01")
        prueba(rc == 0 and filas_de()[0]["id"] == "AT1" and filas_de()[0]["estado"] == "pendiente",
               "nuevo crea AT1 pendiente")
        prueba(not list(raiz.glob("*.tmp")), "la escritura no deja temporales")
        rc, out = run("nuevo", "--area", "Estudios", "--patron", "fecha-equivocada", "--que", "c", "--causa", "d",
                      "--fecha", "2026-11-02")
        prueba(rc == 0 and "ya tiene 2 casos" in out, "nuevo avisa de patrón repetido")
        rc, out = run("ver")
        prueba("SE REPITE Y SIGUE SIN REGLA" in out and "fecha-equivocada: 2 casos" in out,
               "ver muestra repetidos sin regla")
        rc, _ = run("cambiar", "AT1", "--estado", "incorporada", "--donde", "no-existe.md")
        prueba(rc != 0, "incorporada con ruta inexistente rechazada")
        rc, _ = run("cambiar", "AT1", "--estado", "incorporada", "--donde", "regla.md → apartado")
        prueba(rc == 0, "incorporada con ruta y apartado aceptada")
        run("nuevo", "--area", "Estudios", "--patron", "fecha-equivocada", "--que", "e", "--causa", "f",
            "--fecha", "2026-11-03")
        rc, out = run("ver", "--desde", "2026-11-02")
        prueba("reglas ya escritas para otros casos: regla.md" in out and "AE2" in out and "a\n" not in out,
               "ver junta lo repetido, cita la regla existente y respeta --desde")
        rc, _ = run("ver", "--desde", "mañana")
        prueba(rc != 0, "ver rechaza un --desde que no es fecha")
        rc, _ = run("nuevo", "--area", "Casa", "--patron", "Mal Patron", "--que", "x", "--causa", "y",
                    "--patron-nuevo")
        prueba(rc != 0, "patrón con espacios rechazado aunque sea nuevo")
        rc, _ = run("nuevo", "--area", "Casa", "--patron", "suelto", "--que", "x", "--causa", "y",
                    "--estado", "sin regla", "--patron-nuevo")
        prueba(rc != 0, "«sin regla» sin motivo rechazado")
        rc, out = run("nuevo", "--area", "Casa", "--patron", "fechas-equivocadas", "--que", "x", "--causa", "y")
        prueba(rc != 0 and "fecha-equivocada" in out and len(filas_de()) == 3,
               "tipo nuevo rechazado sin --patron-nuevo y con el parecido propuesto")
        rc, out = run("ver", "--desde", "2026-11-02")
        prueba("VUELVE AUNQUE YA TENÍA REGLA" in out and "fecha-equivocada: 2 casos nuevos (AE1, AE2)" in out,
               "ver avisa de lo que vuelve aunque ya tenía regla")
        rc, out = run("nuevo", "--area", "Casa", "--patron", "otro-tipo", "--que", "x", "--causa", "y",
                      "--patron-nuevo", "--fecha", "2026-11-04", "--donde", "regla.md")
        prueba(rc == 0 and "otro-tipo" in out, "--patron-nuevo acepta un tipo nuevo")
        prueba(next(f for f in filas_de() if f["patron"] == "otro-tipo")["estado"] == "incorporada",
               "con --donde, el estado por defecto es «incorporada»")
        rc, _ = run("nuevo", "--area", "Casa", "--patron", "otro-tipo", "--que", "x", "--causa", "y",
                    "--estado", "resuelta")
        prueba(rc != 0, "un estado que no existe se rechaza")
        rc, _ = run("nuevo", "--area", "Casa", "--patron", "otro-tipo", "--que", "dos\nlíneas", "--causa", "y")
        prueba(rc != 0, "un salto de línea en un campo no se escribe")
        prueba(base() == [] and base(area="Casa") and base(id="AT01"),
               "la letra del id corresponde al área y no lleva ceros delante")
        prueba(comprobar([], raiz, cfg, COLUMNAS[:-1]) != [], "cabecera distinta: error")
        escribir(ruta, [f for f in filas_de() if f["patron"] != "otro-tipo"], cfg)
        filas = filas_de()
        viejo = dict(filas[0], estado="pendiente", donde="")
        escribir(ruta, [viejo] + filas, cfg)  # la versión menos avanzada, primero
        rc, out = run("comprobar")
        prueba(rc != 0 and "repetido" in out, "comprobar detecta ids repetidos")
        run("resolver")
        filas = filas_de()
        prueba(len(filas) == 3 and next(f for f in filas if f["id"] == "AT1")["estado"] == "incorporada",
               "resolver deja la versión más avanzada, esté donde esté")
        otro = dict(filas[0], que="otro caso distinto")
        escribir(ruta, filas + [otro], cfg)
        run("resolver")
        filas = filas_de()
        prueba(len(filas) == 4 and sorted(f["que"] for f in filas).count("otro caso distinto") == 1,
               "resolver renumera dos casos distintos con el mismo código")
        crudo = ruta.read_bytes()
        prueba(crudo.startswith(b"\xef\xbb\xbf") and b"\r\n" not in crudo, "UTF-8 con BOM y LF")
        rc, _ = run("nuevo", "--area", "Jardín", "--patron", "fecha-equivocada", "--que", "x", "--causa", "y")
        prueba(rc != 0, "área que no está en control.json rechazada")
        # Una comilla sin cerrar se traga las filas siguientes; una columna de más se perdería al escribir
        sano = ruta.read_text(encoding="utf-8-sig")
        ruta.write_text(sano.replace(",a,b,", ',"a,b,', 1), encoding="utf-8-sig")
        rc, _ = run("comprobar")
        prueba(rc != 0, "una comilla sin cerrar es error")
        ruta.write_text(sano.replace(",a,b,", ",a,b,EXTRA,", 1), encoding="utf-8-sig")
        antes = ruta.read_bytes()
        rc, _ = run("resolver")
        prueba(rc != 0 and ruta.read_bytes() == antes, "con columnas de más no escribe")
        ruta.write_text(sano + "AT99,2026-11-25,Trabajo\n", encoding="utf-8-sig")
        rc, out = run("comprobar")
        prueba(rc != 0 and out.count("AT99") == 1 and "3 columnas de 10" in out and "faltan patron" in out,
               f"una fila con columnas de menos da un error claro: {out}")
        antes = ruta.read_bytes()
        rc1, _ = run("cambiar", "AT99", "--que", "x")
        rc2, _ = run("resolver")
        prueba(rc1 != 0 and rc2 != 0 and ruta.read_bytes() == antes, "con columnas de menos no escribe")
        ruta.write_text(sano, encoding="utf-8-sig")
        (raiz / "regla.md").unlink()
        (raiz / "regla2.md").write_text("x", encoding="utf-8")
        rc1, _ = run("comprobar")
        rc2, out = run("nuevo", "--area", "Casa", "--patron", "fecha-equivocada", "--que", "x", "--causa", "y")
        prueba(rc1 != 0 and rc2 != 0 and "con «cambiar»" in out,
               "si se borra el archivo de una regla, comprobar avisa y nuevo no escribe y dice cómo arreglarlo")
        rcs = [run("cambiar", f["id"], "--donde", "regla2.md")[0] for f in filas_de() if f["donde"]]
        rc2, _ = run("comprobar")
        prueba(len(rcs) == 2 and not any(rcs) and rc2 == 0, "cambiar arregla la tabla aunque tenga errores")
        with candado(ruta, espera=0.2):
            try:
                with candado(ruta, espera=0.2):
                    segunda = True
            except SystemExit:
                segunda = False
        prueba(not segunda, "una segunda orden espera y se niega si la primera no termina")
        with candado(ruta, espera=0.2):
            inicio = time.monotonic()
            rc_ver, _ = run("ver")
            prueba(rc_ver == 0 and time.monotonic() - inicio < 2, "una orden que solo lee no espera al candado")
        escribir(ruta, [], cfg)
        for que in ("a", "c"):
            run("nuevo", "--area", "Casa", "--patron", "uno", "--causa", "b", "--patron-nuevo", "--que", que)
        sano = ruta.read_text(encoding="utf-8-sig")
        lineas = sano.splitlines()
        lineas[1] += '"'
        ruta.write_text(chr(10).join(lineas) + chr(10), encoding="utf-8-sig")
        antes = ruta.read_bytes()
        for orden in (("cambiar", "AC1", "--fuente", ""), ("resolver",)):
            rc, out = run(*orden)
            prueba(rc != 0 and "metidas dentro" in out and ruta.read_bytes() == antes,
                   f"{orden[0]} no escribe con filas tragadas")
        escribir(ruta, [dict(f, fuente="una" + chr(10) + "otra") for f in csv.DictReader(io.StringIO(sano))], cfg)
        antes = ruta.read_bytes()
        prueba(run("cambiar", "AC1", "--fuente", "a" + chr(10) + "b")[0] != 0 and ruta.read_bytes() == antes,
               "un campo que ya tenía salto no admite otro")
        # Una comilla sin cerrar en la última columna se traga líneas que no parecen filas: tampoco se borran
        ruta.write_text(sano.rstrip(chr(10)) + '"' + chr(10) + "algo escrito a mano" + chr(10), encoding="utf-8-sig")
        antes = ruta.read_bytes()
        rc, out = run("cambiar", "AC2", "--fuente", "ok")
        prueba(rc != 0 and "reparar" in out and ruta.read_bytes() == antes,
               "cambiar no borra líneas tragadas que no son filas")
    with tempfile.TemporaryDirectory() as d:
        vacio = Path(d) / "aprendizajes.csv"
        vacio.write_text("", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--raiz", d, "nuevo", "--area", "General", "--patron", "x", "--patron-nuevo",
                       "--que", "a", "--causa", "b"])
        prueba(rc == 0 and len(leer(vacio)[1]) == 1, "un CSV vacío (0 bytes) cuenta como si no existiera")
        fila = leer(vacio)[1][0]
        escribir(vacio, [dict(fila, id="XX1"), dict(fila, id="XX1", que="otro")], Config(Path(d)))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--raiz", d, "resolver"])
        prueba(rc == 1, "resolver no dice que todo está bien si queda un código no válido repetido")
    with tempfile.TemporaryDirectory() as d:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["--raiz", d, "nuevo", "--area", "General", "--patron", "x", "--que", "a", "--causa", "b"])
            rc_raiz = main(["--raiz", str(Path(d) / "no-existe"), "comprobar"])
        prueba(rc == 0 and leer(Path(d) / "aprendizajes.csv")[1][0]["id"] == "AG1",
               "sin control.json funciona con el área General")
        prueba(rc_raiz == 1, "--raiz inexistente: error, no un OK vacío")
        for config in ("{roto", '{"archivo_aprendizajes": "../fuera.csv"}', '{"areas": {"T": "A", "C": "a"}}'):
            (Path(d) / "control.json").write_text(config, encoding="utf-8")
            try:
                Config(Path(d))
                rechazada = False
            except SystemExit:
                rechazada = True
            prueba(rechazada, f"control.json no válido se rechaza: {config}")
        (Path(d) / "control.json").unlink()
        (Path(d) / "config.json").write_text('{"areas": {"T": "Trabajo"}}', encoding="utf-8")
        try:
            Config(Path(d))
            pide = False
        except SystemExit as e:
            pide = "renómbralo" in str(e)
        prueba(pide, "un config.json de antes pide renombrarlo")
    with tempfile.TemporaryDirectory() as d:
        raiz = Path(d)
        ruta = raiz / "aprendizajes.csv"

        def rc_de(*args):
            with contextlib.redirect_stdout(io.StringIO()):
                return main(["--raiz", str(raiz), *args])

        for que in ("a, con coma", "c, también"):
            rc_de("nuevo", "--area", "General", "--patron", "uno", "--causa", "b", "--patron-nuevo", "--que", que)
        sano = ruta.read_text(encoding="utf-8-sig")
        cambio = ("cambiar", "AG1", "--fuente", "")
        # «reparar»: cada negativa tiene salida, sin Git, y no se pierde ninguna fila
        def danar(texto):
            ruta.write_text(texto, encoding="utf-8-sig")
        def quedan():
            return sorted(f["id"] for f in csv.DictReader(io.StringIO(ruta.read_text(encoding="utf-8-sig"))))
        for viejas in ruta.parent.glob("*.copia"):
            viejas.unlink()
        ids = sorted(f["id"] for f in csv.DictReader(io.StringIO(sano)))
        cab, uno_, dos_ = sano.splitlines()[:3]
        for que, texto in (
                ("comilla sin cerrar al final", [cab, uno_ + '"', dos_]),
                ("comilla que se cierra en la fila siguiente", [cab, uno_ + '"', dos_ + '"'])):
            danar(chr(10).join(texto) + chr(10))
            antes = ruta.read_bytes()
            prueba(rc_de("comprobar") != 0 and rc_de(*cambio) != 0 and ruta.read_bytes() == antes, f"{que}: no se escribe encima")
            prueba(rc_de("reparar") == 0 and quedan() == ids and rc_de("comprobar") == 0, f"{que}: reparar devuelve las filas")
            prueba(any(p.read_bytes() == antes for p in ruta.parent.glob("*.copia")), f"{que}: queda copia de cómo estaba")
        danar(chr(10).join([cab, uno_, "AG9,2026-11-25,General", dos_]) + chr(10))
        prueba(rc_de("comprobar") != 0 and rc_de(*cambio) != 0, "una fila cortada bloquea")
        prueba(rc_de("reparar") == 0 and quedan() == ids and rc_de("comprobar") == 0 and rc_de(*cambio) == 0,
                "reparar la aparta y la tabla vuelve a funcionar")
        aparte = ruta.with_name(ruta.name + ".apartadas.txt")
        prueba(aparte.is_file() and "AG9,2026-11-25,General" in aparte.read_text(encoding="utf-8"), "la fila apartada se conserva intacta")
        # Una nota legítima de dos líneas que empieza como un código no se parte ni se aparta
        filas_n = list(csv.DictReader(io.StringIO(sano)))
        filas_n[0]["causa"] = "primera" + chr(10) + "AG7, y luego seguimos"
        salida_n = io.StringIO()
        w = csv.DictWriter(salida_n, fieldnames=list(filas_n[0]), lineterminator=chr(10))
        w.writeheader()
        w.writerows(filas_n)
        danar(salida_n.getvalue())
        prueba(rc_de("reparar") == 0 and quedan() == ids and rc_de("comprobar") == 0
                and "primera AG7, y luego seguimos" in ruta.read_text(encoding="utf-8-sig"),
                "un texto de dos líneas queda en una, entero")
        dos_filas = chr(10).join(["a,b,c", "AG1,x," + chr(34), "AG2,y," + chr(34)])
        prueba([f[0] for f in rehacer(dos_filas, 3, Config(raiz).re_id.match)[0]] == ["AG1", "AG2"],
               "al unir líneas no se traga la fila siguiente")
        # Lo apartado: se avisa, su código queda reservado y vuelve con el mismo código
        for viejas in ruta.parent.glob("*.apartadas.txt"):
            viejas.unlink()
        def salida(*args):
            s = io.StringIO()
            with contextlib.redirect_stdout(s):
                main(["--raiz", str(raiz), *args])
            return s.getvalue()
        danar(sano + "AG3,2026-11-25,General" + chr(10))
        rc_de("reparar")
        prueba("apartadas sin revisar" in salida("comprobar") and "AG3,2026-11-25,General" in salida("apartadas"), "se avisa de lo apartado")
        antes = ruta.read_bytes()
        prueba(rc_de("devolver", "1") != 0 and ruta.read_bytes() == antes, "una línea sin arreglar no vuelve")
        prueba(rc_de("devolver", "1", "--fila", 'AG8,2026-11-25,General,uno,ocho,b,,pendiente,,') != 0 and ruta.read_bytes() == antes, "no vuelve con otro código")
        antes = ruta.read_bytes()
        prueba(rc_de("devolver", "1", "--fila", 'AG3,2026-11-25,General,uno,"nueve, bis",b,,rara,,') != 0 and ruta.read_bytes() == antes,
               "no vuelve algo que dejaría la tabla mal")
        rc_de(*("nuevo", "--area", "General", "--patron", "uno", "--que", "x", "--causa", "y"))
        prueba("AG3" not in quedan(), "el código apartado no lo reutiliza «nuevo»")
        prueba(rc_de("devolver", "1", "--fila", 'AG3,2026-11-25,General,uno,"nueve, bis",b,,pendiente,,') == 0 and "AG3" in quedan() and rc_de("comprobar") == 0,
                "devolver la mete con su mismo código")
        prueba("apartadas sin revisar" not in salida("comprobar") and rc_de("devolver", "1") != 0, "ya no se avisa ni se repite")
        danar(sano + "AG1,2026-11-25,General" + chr(10))
        ruta.with_name(ruta.name + ".apartadas.txt").unlink()
        rc_de("reparar")
        antes = ruta.read_bytes()
        prueba(rc_de("devolver", "1", "--fila", 'AG1,2026-11-25,General,uno,"nueve, bis",b,,pendiente,,') != 0
               and ruta.read_bytes() == antes, "no vuelve con un código que ya está en la tabla")
        ruta.with_name(ruta.name + ".apartadas.txt").unlink()
        danar(sano + "<<<<<<< HEAD" + chr(10))
        rc_de("reparar")
        prueba(rc_de("devolver", "1", "--descartar") == 0 and "apartadas sin revisar" not in salida("comprobar")
                and "<<<<<<< HEAD" in ruta.with_name(ruta.name + ".apartadas.txt").read_text(encoding="utf-8"),
                "descartar deja de avisar sin borrar nada")
        # La primera línea dañada o ausente, y el archivo separado por «;»
        cab, resto = sano.split(chr(10), 1)
        for que, texto in (("dañada", cab.replace(",", "", 2) + chr(10) + resto), ("ausente", resto),
                           ("separada por «;»", None)):
            if texto is None:
                s = io.StringIO()
                csv.writer(s, delimiter=";", lineterminator=chr(10)).writerows(csv.reader(io.StringIO(sano)))
                texto = s.getvalue()
            danar(texto)
            prueba(rc_de("reparar") == 0 and rc_de("comprobar") == 0
                    and list(csv.reader(io.StringIO(ruta.read_text(encoding="utf-8-sig")))) == list(csv.reader(io.StringIO(sano))),
                    f"reparar repone la primera línea {que}")
        columnas_ = cab.split(",")
        columnas_[1], columnas_[2] = columnas_[2], columnas_[1]
        danar(",".join(columnas_) + chr(10) + resto)
        antes = ruta.read_bytes()
        prueba(rc_de("reparar") != 0 and ruta.read_bytes() == antes, "con las columnas en otro orden no adivina")
        danar(sano)
        dobles = [{"id": "AG1", "que": q, "estado": "pendiente"} for q in ("a", "b")]
        res, _ = resolver(dobles, Config(raiz), [{"id": "AG2"}])
        prueba([f["id"] for f in res] == ["AG1", "AG3"], "resolver no reutiliza un código apartado")
        danar("otra,cabecera" + chr(10))
        antes = ruta.read_bytes()
        prueba(rc_de("reparar") != 0 and ruta.read_bytes() == antes, "con otra cabecera reparar no toca nada")
        danar(sano)
    if fallos:
        print(f"SELFTEST FAIL ({len(fallos)} de {casos}):", "; ".join(fallos))
        return 1
    print(f"SELFTEST PASS: {casos}/{casos}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
