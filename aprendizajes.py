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
    py aprendizajes.py --self-test
Opción común, antes de la orden: --raiz RUTA (por defecto, la carpeta actual).

Un tipo (`--patron`) que no exista se rechaza y se proponen los parecidos: un sinónimo esconde la
repetición. Para crearlo hay que pedirlo expresamente con `--patron-nuevo`.

Estados: `pendiente` (falta una regla), `incorporada` (la regla está en `donde`), `sin regla` (caso
suelto: `regla` dice por qué no hace falta) y `descartada`. Una fila incorporada exige `donde` con una
ruta que exista; lo que vaya tras « → » o «#» en `donde` es el apartado.

Las áreas salen de `config.json` en la raíz (ver `config.ejemplo.json`); sin él, hay una sola: General.
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


class Config:
    """Lo que cambia de un usuario a otro: las áreas (letra → nombre) y el nombre del archivo."""

    def __init__(self, raiz: Path):
        datos = {}
        ruta = raiz / "config.json"
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise SystemExit(f"✗ config.json no es un JSON válido: {e}")
            if not isinstance(datos, dict):
                raise SystemExit("✗ config.json debe ser un objeto: {\"areas\": {...}}")
        self.areas: dict[str, str] = datos.get("areas") or {"G": "General"}
        if not isinstance(self.areas, dict):
            raise SystemExit("✗ config.json: «areas» debe ser un objeto letra → nombre")
        for letra, nombre in self.areas.items():
            if not re.fullmatch(r"[A-Z]", letra):
                raise SystemExit(f"✗ config.json: la letra de área «{letra}» debe ser una sola mayúscula")
            if not isinstance(nombre, str) or not nombre.strip():
                raise SystemExit(f"✗ config.json: el área {letra} necesita un nombre")
        if len({n.lower() for n in self.areas.values()}) != len(self.areas):
            raise SystemExit("✗ config.json: dos áreas con el mismo nombre")
        self.archivo = datos.get("archivo_aprendizajes") or "aprendizajes.csv"
        if not isinstance(self.archivo, str) or Path(self.archivo).name != self.archivo \
                or not self.archivo.endswith(".csv"):
            raise SystemExit("✗ config.json: «archivo_aprendizajes» debe ser un nombre .csv, sin carpetas")
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


def resolver(filas: list[dict], cfg: Config) -> tuple[list[dict], list[str]]:
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
        nuevo = siguiente_id(salida + renumerar, m.group(1), cfg)
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
    return 1 if t.errores else 0


def o_resolver(t: Tabla, a: argparse.Namespace) -> int:
    if t.cabecera is None:
        print(f"No existe {t.cfg.archivo}: no hay nada que resolver.")
        return 0
    filas, avisos = resolver(t.filas, t.cfg)
    escribir(t.ruta, filas, t.cfg)
    print("\n".join(avisos) or "No había filas repetidas que juntar.")
    errores = comprobar(filas, t.raiz, t.cfg, COLUMNAS)
    if errores:  # por ejemplo, un código no válido repetido: «resolver» no sabe renumerarlo
        print("✗ Quedan errores que «resolver» no arregla:\n" + "\n".join(errores))
    return 1 if errores else 0


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
        "id": siguiente_id(t.filas, t.cfg.letra[area.lower()], t.cfg), "fecha": a.fecha or dt.date.today().isoformat(),
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
    for campo in ("estado", "regla", "donde", "patron", "que", "causa", "fuente"):
        valor = getattr(a, campo)
        if valor is not None:
            fila[campo] = valor.strip().lower() if campo == "patron" else valor
    # «cambiar» tiene que poder arreglar una tabla con errores (por ejemplo, si se borró el archivo de una
    # regla): se escribe si no añade ningún error nuevo.
    return guardar(t, fila, t.errores)


SOLO_LEEN = {"ver", None, "patrones", "comprobar"}
ORDENES = {"ver": o_ver, None: o_ver, "patrones": o_patrones, "comprobar": o_comprobar,
           "resolver": o_resolver, "nuevo": o_nuevo, "cambiar": o_cambiar}


def ejecutar(a: argparse.Namespace, raiz: Path, cfg: Config, ruta: Path) -> int:
    t = Tabla(raiz, cfg, ruta)
    if t.cabecera is not None and (t.cabecera != COLUMNAS or any(forma_rota(f) for f in t.filas)) \
            and a.orden != "comprobar":
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
        (raiz / "config.json").write_text(json.dumps(
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
        prueba(rc != 0, "área que no está en config.json rechazada")
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
               "sin config.json funciona con el área General")
        prueba(rc_raiz == 1, "--raiz inexistente: error, no un OK vacío")
        for config in ("{roto", '{"archivo_aprendizajes": "../fuera.csv"}', '{"areas": {"T": "A", "C": "a"}}'):
            (Path(d) / "config.json").write_text(config, encoding="utf-8")
            try:
                Config(Path(d))
                rechazada = False
            except SystemExit:
                rechazada = True
            prueba(rechazada, f"config.json no válido se rechaza: {config}")
    if fallos:
        print(f"SELFTEST FAIL ({len(fallos)} de {casos}):", "; ".join(fallos))
        return 1
    print(f"SELFTEST PASS: {casos}/{casos}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
