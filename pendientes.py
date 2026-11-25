#!/usr/bin/env python3
"""Lista única de pendientes y de hechos vigentes de un trabajo con IA: `pendientes.csv`.

Ni las personas ni el modelo editan el CSV a mano: usan esta herramienta, que comprueba cada fila antes
de escribir y guarda siempre el mismo formato (UTF-8 con BOM, comas, fin de línea LF).

Uso (desde la carpeta del proyecto, o con --raiz):
  pendientes.py ver <quien> [--todo]   un área, la persona que decide o «todas» (a la persona, lo de
                                       prioridad baja solo se le cuenta, salvo con --todo)
  pendientes.py nuevo --de <área> --para <quien> --que "..." [--vence AAAA-MM-DD]
                [--prioridad alta|normal|baja] [--esfuerzo rápido|medio|largo]
                [--permiso preguntar|aprobada|libre] [--detalle ruta#apartado] [--nota "..."]
  pendientes.py nuevo --vigente ...    apunta un hecho vigente (no es tarea: es estado)
  pendientes.py detalle <id>           muestra la fila y solo el apartado de su ficha
  pendientes.py estado <id> <estado> [--nota "..."]
  pendientes.py cambiar <id> [--para] [--que] [--vence|--sin-vence] [--prioridad] [--esfuerzo]
                [--permiso (con --nota)] [--detalle] [--nota]
  pendientes.py buscar <texto>
  pendientes.py hechos [área]          lista los hechos vigentes
  pendientes.py comprobar              solo comprueba; código 1 si hay errores
  pendientes.py resolver [--base REF]  arregla las filas repetidas tras juntar ramas de Git
  pendientes.py archivar [--dias 30]   pasa las cerradas hace más de N días a pendientes_cerrados.csv
  pendientes.py --self-test
Opciones comunes: --raiz RUTA, --hoy AAAA-MM-DD.

Las áreas y el nombre de la persona que decide salen de `config.json` (ver `config.ejemplo.json`).
Una orden cada vez: si otra está escribiendo, la segunda espera unos segundos (archivo `.lock`).
"""
import argparse
import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import date
from pathlib import Path

ARCHIVO = "pendientes.csv"
COLUMNAS = ["id", "para", "estado", "prioridad", "esfuerzo", "permiso", "creada", "vence", "cerrada",
            "que", "detalle", "nota"]
ESTADOS = ["abierta", "en curso", "bloqueada", "hecha", "confirmada", "descartada", "vigente"]
# «vigente» no es una tarea: es un hecho que sigue siendo verdad y que no es una regla (por ejemplo,
# «la web está en pausa»). No lleva prioridad, esfuerzo, permiso ni plazo. Cuando deja de ser verdad
# pasa a «descartada» con una nota.
VIGENTE = "vigente"
ACTIVOS = {"abierta", "en curso", "bloqueada"}
CERRADOS = {"confirmada", "descartada"}
PRIORIDADES = ["alta", "normal", "baja"]
ESFUERZOS = ["rápido", "medio", "largo"]
# permiso: «preguntar» (por defecto), «aprobada» (la persona ya dio el OK; la nota dice cuándo y dónde)
# o «libre» (no necesita OK: local, reversible y dentro del ámbito de quien la hace).
PERMISOS = ["preguntar", "aprobada", "libre"]
RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
DIAS_PRONTO = 14
ESCRIBEN = {"nuevo", "estado", "cambiar", "archivar"}
SOLO_LEEN = {"ver", "hechos", "detalle", "buscar", "comprobar"}


class Config:
    """Lo que cambia de un usuario a otro: las áreas (letra → nombre), quién decide y el archivo."""

    def __init__(self, raiz):
        datos = {}
        ruta = Path(raiz) / "config.json"
        if ruta.is_file():
            try:
                datos = json.loads(ruta.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise SystemExit(f"✗ config.json no es un JSON válido: {e}")
            if not isinstance(datos, dict):
                raise SystemExit("✗ config.json debe ser un objeto: {\"areas\": {...}, \"persona\": \"...\"}")
        self.areas = datos.get("areas") or {"G": "General"}
        if not isinstance(self.areas, dict):
            raise SystemExit("✗ config.json: «areas» debe ser un objeto letra → nombre")
        for letra, nombre in self.areas.items():
            if not re.fullmatch(r"[A-Z]", letra):
                raise SystemExit(f"✗ config.json: la letra de área «{letra}» debe ser una sola mayúscula")
            if not isinstance(nombre, str) or not nombre.strip():
                raise SystemExit(f"✗ config.json: el área {letra} necesita un nombre")
        self.persona = datos.get("persona") or "Persona"
        if not isinstance(self.persona, str) or not self.persona.strip():
            raise SystemExit("✗ config.json: «persona» debe ser un nombre")
        nombres = [n.lower() for n in self.areas.values()] + [self.persona.lower()]
        if len(set(nombres)) != len(nombres):
            raise SystemExit("✗ config.json: las áreas y la persona necesitan nombres distintos")
        self.archivo = datos.get("archivo_pendientes") or ARCHIVO
        if not isinstance(self.archivo, str) or Path(self.archivo).name != self.archivo \
                or not self.archivo.endswith(".csv"):
            raise SystemExit("✗ config.json: «archivo_pendientes» debe ser un nombre .csv, sin carpetas")
        self.archivo_cerrados = Path(self.archivo).stem + "_cerrados.csv"
        self.letra = {v.lower(): k for k, v in self.areas.items()}
        self.orden = "".join(self.areas)
        self.destinos = list(self.areas.values()) + [self.persona]
        self.re_id = re.compile(rf"^([{self.orden}])([1-9][0-9]*)\Z")

    def ok_registrado(self, nota):
        """Un permiso «aprobada» se apoya en un OK registrado: la nota nombra a la persona y da una fecha
        real. Es un requisito de formato: la herramienta no puede saber quién lo escribió."""
        nombre = re.search(rf"(?<!\w){re.escape(self.persona)}(?!\w)", nota)
        return bool(nombre) and any(fecha(d) for d in re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", nota))


def es_hecho(f):
    """Un hecho, vigente o ya descartado: se reconoce por no tener prioridad, esfuerzo ni permiso."""
    return f.get("estado") == VIGENTE or (f.get("estado") == "descartada" and not any(
        f.get(c) for c in ("prioridad", "esfuerzo", "permiso")))


def normalizar(texto):
    t = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def fecha(txt):
    if not txt or not RE_FECHA.match(txt):
        return None
    try:
        return date.fromisoformat(txt)
    except ValueError:
        return None


def canonico(nombre, validos):
    """Acepta mayúsculas y tildes distintas: «rapido» → «rápido»."""
    for v in validos:
        if normalizar(nombre) == normalizar(v):
            return v
    return None


# ---- Lectura y escritura ------------------------------------------------------
def leer(ruta):
    """Devuelve (cabecera, filas); (None, []) si el archivo no existe."""
    if not ruta.is_file():
        return None, []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        filas = [dict(fila) for fila in lector]
        if lector.fieldnames is None:  # vacío (0 bytes o solo espacios): como si no existiera
            return None, []
        return lector.fieldnames, filas


def forma_rota(fila):
    """Columnas de más (la clave None de csv) o de menos (valores None): la fila no se puede leer bien."""
    return None in fila or None in fila.values()


def orden_id(fila, cfg):
    m = cfg.re_id.match(fila.get("id") or "")
    return (cfg.orden.index(m.group(1)), int(m.group(2))) if m else (99, 0)


def escribir(ruta, filas, cfg):
    """Escribe en un archivo temporal de la misma carpeta y lo cambia por el bueno de una vez: si algo
    falla a mitad, la tabla anterior sigue entera."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNAS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for fila in sorted(filas, key=lambda f: orden_id(f, cfg)):
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
def candado(ruta, espera=10.0):
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


# ---- Comprobación ---------------------------------------------------------------
def comprobar(filas, raiz, cfg, cabecera=None, cerrados=None):
    errores, avisos = [], []
    archivados = {f.get("id") for f in (cerrados or [])}
    if cabecera is not None and cabecera != COLUMNAS:
        errores.append(f"cabecera distinta de la esperada: {','.join(COLUMNAS)}")
    vistos = {}
    for n, f in enumerate(filas, 2):
        sitio = f"fila {n} ({f.get('id') or 'sin id'})"
        if None in f:
            errores.append(f"{sitio}: tiene más columnas que la cabecera (¿una coma sin comillas?)")
        faltan = [c for c in COLUMNAS if c in f and f[c] is None]
        if faltan:
            errores.append(f"{sitio}: tiene {len(COLUMNAS) - len(faltan)} columnas de {len(COLUMNAS)}; "
                           f"faltan {', '.join(faltan)} (¿una coma de menos o la fila cortada al copiarla?)")
            continue  # el resto de controles solo repetiría el mismo problema con otras palabras
        saltos = [c for c in COLUMNAS if re.search(r"[\r\n]", f.get(c) or "")]
        if saltos:
            errores.append(f"{sitio}: salto de línea en {', '.join(saltos)} (¿una comilla sin cerrar que "
                           "se ha tragado las filas siguientes?)")
        ident = f.get("id") or ""
        if not cfg.re_id.match(ident):
            errores.append(f"{sitio}: id no válido; debe ser una letra de área ({', '.join(cfg.areas)}) "
                           "seguida de un número sin ceros delante")
        elif ident in vistos:
            errores.append(f"{sitio}: id repetido (también en la fila {vistos[ident]}). Suele salir al "
                           "juntar dos ramas: ejecuta «resolver», que se queda con lo que cambió cada una")
        else:
            vistos[ident] = n
        if ident in archivados:
            errores.append(f"{sitio}: el id ya está en el archivo de cerradas (archivado); usa «resolver»")
        hecho = es_hecho(f)
        for campo, validos in (("para", cfg.destinos), ("estado", ESTADOS), ("prioridad", PRIORIDADES),
                               ("esfuerzo", ESFUERZOS), ("permiso", PERMISOS)):
            if hecho and campo in ("prioridad", "esfuerzo", "permiso"):
                if f.get(campo):
                    errores.append(f"{sitio}: un hecho vigente no lleva {campo}")
                continue
            if f.get(campo) not in validos:
                errores.append(f"{sitio}: {campo} «{f.get(campo)}» no válido ({', '.join(validos)})")
        for campo in ("creada", "vence", "cerrada"):
            v = f.get(campo) or ""
            if v and fecha(v) is None:
                errores.append(f"{sitio}: {campo} «{v}» no es una fecha AAAA-MM-DD real")
        if not f.get("creada"):
            errores.append(f"{sitio}: falta la fecha de creación")
        if hecho and f.get("vence"):
            errores.append(f"{sitio}: un hecho vigente no tiene plazo")
        if hecho and f.get("para") == cfg.persona:
            errores.append(f"{sitio}: un hecho vigente es del área a la que afecta, no de {cfg.persona}")
        if not (f.get("que") or "").strip():
            errores.append(f"{sitio}: el campo «que» está vacío")
        if f.get("estado") in CERRADOS and not f.get("cerrada"):
            errores.append(f"{sitio}: está {f.get('estado')} y le falta la fecha de cierre")
        if f.get("estado") not in CERRADOS and f.get("cerrada"):
            errores.append(f"{sitio}: tiene fecha de cierre pero su estado es «{f.get('estado')}»")
        if f.get("permiso") == "aprobada" and not cfg.ok_registrado(f.get("nota") or ""):
            errores.append(f"{sitio}: permiso «aprobada» sin el OK de {cfg.persona} en la nota; escribe "
                           f"«{cfg.persona} AAAA-MM-DD» y dónde lo dijo")
        m = cfg.re_id.match(ident)
        if f.get("permiso") == "libre" and m and cfg.areas.get(m.group(1)) != f.get("para"):
            errores.append(f"{sitio}: «libre» solo vale para lo que un área se pide a sí misma; "
                           "lo que hace otra área va con «preguntar» o «aprobada»")
        if f.get("permiso") in ("aprobada", "libre") and f.get("para") == cfg.persona:
            errores.append(f"{sitio}: una pendiente de {cfg.persona} no puede ir «{f.get('permiso')}»: "
                           "la hace esa persona")
        if f.get("estado") == "bloqueada" and not (f.get("nota") or "").strip():
            avisos.append(f"{sitio}: bloqueada sin nota; di en «nota» qué la bloquea")
        det = (f.get("detalle") or "").strip()
        if det and not (Path(raiz) / det.split("#")[0]).exists():
            avisos.append(f"{sitio}: la ficha «{det}» no existe (ruta desde la raíz)")
    return errores, avisos


# ---- Ordenar por urgencia ---------------------------------------------------------
def urgencia(f, hoy, cfg):
    """Menor = más urgente: vencidas, luego plazo cercano, prioridad y, a igualdad, lo rápido."""
    v = fecha(f.get("vence"))
    if v and v < hoy:
        grupo = 0
    elif v and (v - hoy).days <= DIAS_PRONTO:
        grupo = 1
    else:
        grupo = 2
    prio = PRIORIDADES.index(f["prioridad"]) if f.get("prioridad") in PRIORIDADES else 1
    esf = ESFUERZOS.index(f["esfuerzo"]) if f.get("esfuerzo") in ESFUERZOS else 1
    bloq = 1 if f.get("estado") == "bloqueada" else 0
    return (grupo, bloq, prio, v or date.max, esf, orden_id(f, cfg))


def linea(f, hoy, con_para=False):
    f = {c: (f.get(c) or "") for c in COLUMNAS}
    v = fecha(f.get("vence"))
    marca = ""
    if v:
        dif = (v - hoy).days
        marca = " ⚠ VENCIDA" if dif < 0 else (f" ← faltan {dif} días" if dif <= DIAS_PRONTO else "")
    extra = f" → {f['para']}" if con_para else ""
    estado = "" if f["estado"] == "abierta" else f" [{f['estado']}]"
    plazo = f["vence"] or "sin plazo"
    return (f"  {f['id']:<5} {plazo:<10} {f['prioridad']:<6} {f['esfuerzo']:<6} "
            f"{f['que']}{extra}{estado}{marca}")


def ver_todas(filas, activos, hoy, cfg):
    hechos = [f for f in filas if f.get("estado") == VIGENTE]
    print(f"HECHOS VIGENTES ({len(hechos)})")
    for f in hechos:
        print(f"  {f['id']:<5} {f['que']} → {f['para']}")
    print(f"PENDIENTES ACTIVOS ({len(activos)}), por urgencia")
    for f in sorted(activos, key=lambda f: urgencia(f, hoy, cfg)):
        print(linea(f, hoy, con_para=True))
    hechas = [f for f in filas if f.get("estado") == "hecha"]
    print(f"HECHAS, A FALTA DE QUE LAS CONFIRME QUIEN LAS PIDIÓ ({len(hechas)})")
    for f in hechas:
        print(f"  {f['id']:<5} {f['que']} (pidió {cfg.areas[f['id'][0]]}; hizo {f['para']})")


def ver(filas, quien, hoy, cfg, todo=False):
    filas = [{c: (f.get(c) or "") for c in COLUMNAS} for f in filas if cfg.re_id.match(f.get("id") or "")]
    activos = [f for f in filas if f.get("estado") in ACTIVOS]
    if quien == "todas":
        return ver_todas(filas, activos, hoy, cfg)
    es_persona = quien == cfg.persona
    print(f"== {quien} ==")
    if not es_persona:
        letra = cfg.letra[quien.lower()]
        hechos = [f for f in filas if f.get("estado") == VIGENTE and f["para"] == quien]
        print(f"0. HECHOS VIGENTES ({len(hechos)}): estado, no tareas; descarta el que deje de valer")
        for f in hechos:
            print(f"  {f['id']:<5} {f['que']}")
        por_confirmar = [f for f in filas if f.get("estado") == "hecha" and f["id"].startswith(letra)]
        print(f"1. HECHAS POR OTROS: CONFÍRMALAS ({len(por_confirmar)})")
        for f in por_confirmar:
            print(f"  {f['id']:<5} {f['que']} (hecha por {f['para']}; nota: {f.get('nota') or '-'})")
    mias = sorted((f for f in activos if f["para"] == quien), key=lambda f: urgencia(f, hoy, cfg))
    if not es_persona:
        ya = [f for f in mias if f["estado"] == "abierta" and f["esfuerzo"] == "rápido"
              and f["permiso"] in ("aprobada", "libre")]
        print(f"2. HAZLAS YA, SIN PREGUNTAR, Y DESPUÉS INFORMA ({len(ya)}): rápidas y con permiso")
        for f in ya:
            print(linea(f, hoy) + f"  [permiso: {f['permiso']}]")
        mias = [f for f in mias if f not in ya]
    titulo = "LO QUE TE TOCA" if not es_persona else f"LO QUE ESPERAMOS DE {cfg.persona.upper()}"
    ocultas = []
    if es_persona and not todo:
        # A la persona no se le enseña lo de prioridad baja sin plazo cercano: se cuenta y se ve con --todo.
        ocultas = [f for f in mias if f["prioridad"] == "baja" and urgencia(f, hoy, cfg)[0] == 2]
        mias = [f for f in mias if f not in ocultas]
    print(f"{3 if not es_persona else 1}. {titulo} ({len(mias)}), por urgencia")
    for f in mias:
        print(linea(f, hoy, con_para=es_persona))
    if ocultas:
        print(f"   Y {len(ocultas)} de prioridad baja, para cuando apetezca: «ver {cfg.persona} --todo»")
    rapidas = [f for f in mias if f["esfuerzo"] == "rápido" and f["estado"] != "bloqueada"]
    if rapidas:
        print(f"   Rápidas (se quitan en un momento): {', '.join(f['id'] for f in rapidas)}")
    if not es_persona:
        fuera = sorted((f for f in activos if f["id"].startswith(letra) and f["para"] != quien),
                       key=lambda f: urgencia(f, hoy, cfg))
        print(f"4. LO QUE ESPERAS DE OTROS ({len(fuera)})")
        for f in fuera:
            print(linea(f, hoy, con_para=True))


# ---- Órdenes que escriben -------------------------------------------------------------
def siguiente_id(filas, cerrados, letra, cfg):
    nums = [int(m.group(2)) for f in filas + cerrados if (m := cfg.re_id.match(f.get("id") or ""))
            and m.group(1) == letra]
    return f"{letra}{max(nums, default=0) + 1}"


def buscar_fila(filas, ident):
    encontradas = [f for f in filas if f.get("id") == ident.upper()]
    if not encontradas:
        raise SystemExit(f"No existe el pendiente {ident}.")
    if len(encontradas) > 1:
        raise SystemExit(f"El id {ident} está repetido: arréglalo antes (ejecuta «comprobar»).")
    return encontradas[0]


def validar_campo(nombre, valor, validos):
    v = canonico(valor, validos)
    if v is None:
        raise SystemExit(f"{nombre} «{valor}» no válido; usa {', '.join(validos)}.")
    return v


def validar_fecha(valor):
    if fecha(valor) is None:
        raise SystemExit(f"Fecha «{valor}» no válida: usa AAAA-MM-DD.")
    return valor


def orden_nuevo(filas, cerrados, a, hoy, cfg):
    de = validar_campo("--de", a.de, list(cfg.areas.values()))
    fila = {c: "" for c in COLUMNAS}
    fila.update(id=siguiente_id(filas, cerrados, cfg.letra[de.lower()], cfg),
                para=validar_campo("--para", a.para, cfg.destinos), creada=hoy.isoformat(),
                que=a.que.strip(), detalle=(a.detalle or "").strip(), nota=(a.nota or "").strip())
    if not fila["que"]:
        raise SystemExit("--que no puede estar vacío.")
    if a.vigente:
        if a.vence or a.prioridad or a.esfuerzo or a.permiso:
            raise SystemExit("Un hecho vigente no lleva plazo, prioridad, esfuerzo ni permiso.")
        fila["estado"] = VIGENTE
    else:
        fila.update(estado="abierta",
                    prioridad=validar_campo("--prioridad", a.prioridad or "normal", PRIORIDADES),
                    esfuerzo=validar_campo("--esfuerzo", a.esfuerzo or "medio", ESFUERZOS),
                    permiso=validar_campo("--permiso", a.permiso or "preguntar", PERMISOS),
                    vence=validar_fecha(a.vence) if a.vence else "")
    filas.append(fila)
    return fila


def orden_estado(filas, a, hoy, cfg):
    f = buscar_fila(filas, a.id)
    nuevo = validar_campo("estado", a.estado, ESTADOS)
    if es_hecho(f) and nuevo not in (VIGENTE, "descartada"):
        raise SystemExit("Un hecho solo está «vigente» o «descartada» (si deja de ser verdad; si se "
                         "descartó por error, vuelve a «vigente»). Una tarea se crea con «nuevo».")
    if not es_hecho(f) and nuevo == VIGENTE:
        raise SystemExit("Una tarea no se convierte en hecho: crea el hecho con «nuevo --vigente».")
    propio = cfg.areas.get(f["id"][:1]) == f["para"]
    if nuevo == "confirmada" and f["estado"] != "hecha" and not propio:
        raise SystemExit(f"{f['id']} está «{f['estado']}»: solo se confirma una pendiente hecha. "
                         "Si se terminó, márcala primero «hecha» (o usa «descartada»).")
    if nuevo == "hecha" and propio:
        nuevo = "confirmada"  # lo pidió y lo hizo la misma área: no hay nadie más a quien avisar
    if nuevo == "bloqueada" and not (a.nota or f.get("nota")):
        raise SystemExit("Una pendiente bloqueada necesita --nota con lo que la bloquea.")
    f["estado"] = nuevo
    f["cerrada"] = hoy.isoformat() if nuevo in CERRADOS else ""
    if a.nota:
        f["nota"] = a.nota.strip()
    return f


def orden_cambiar(filas, a, cfg):
    f = buscar_fila(filas, a.id)
    if a.para:
        f["para"] = validar_campo("--para", a.para, cfg.destinos)
    if a.que:
        f["que"] = a.que.strip()
    if a.vence:
        f["vence"] = validar_fecha(a.vence)
    if a.sin_vence:
        f["vence"] = ""
    if a.prioridad:
        f["prioridad"] = validar_campo("--prioridad", a.prioridad, PRIORIDADES)
    if a.esfuerzo:
        f["esfuerzo"] = validar_campo("--esfuerzo", a.esfuerzo, ESFUERZOS)
    if a.detalle is not None:
        f["detalle"] = a.detalle.strip()
    if a.nota is not None:
        f["nota"] = a.nota.strip()
    if a.permiso:
        nuevo = validar_campo("--permiso", a.permiso, PERMISOS)
        if nuevo != "preguntar" and a.nota is None:
            raise SystemExit("Para dar permiso «aprobada» o «libre» hace falta --nota en la misma orden "
                             f"(con «aprobada», el OK: «{cfg.persona} AAAA-MM-DD, dónde lo dijo»).")
        f["permiso"] = nuevo
    return f


def orden_archivar(filas, cerrados, dias, hoy):
    mover = [f for f in filas if f.get("estado") in CERRADOS
             and (c := fecha(f.get("cerrada"))) and (hoy - c).days > dias]
    quedan = [f for f in filas if f not in mover]
    return quedan, cerrados + mover, mover


# ---- Fichas de detalle -----------------------------------------------------------------
def slug(texto):
    """Ancla al estilo de GitHub: minúsculas, sin signos, espacios por guiones."""
    t = re.sub(r"[^\w\- ]", "", texto.strip().lower())
    return t.replace(" ", "-")


def apartado(texto, ancla):
    """Devuelve solo el apartado cuyo encabezado da esa ancla, hasta el siguiente del mismo nivel o mayor."""
    lineas = texto.splitlines()
    for i, l in enumerate(lineas):
        m = re.match(r"^(#{1,6})\s+(.*)$", l)
        if m and slug(m.group(2)) == ancla.lower():
            nivel, fin = len(m.group(1)), len(lineas)
            for j in range(i + 1, len(lineas)):
                n = re.match(r"^(#{1,6})\s", lineas[j])
                if n and len(n.group(1)) <= nivel:
                    fin = j
                    break
            return "\n".join(lineas[i:fin]).strip()
    return None


def orden_detalle(filas, cerrados, raiz, ident):
    f = buscar_fila(filas + cerrados, ident)
    for c in COLUMNAS:
        if f.get(c):
            print(f"{c}: {f[c]}")
    det = (f.get("detalle") or "").strip()
    if not det:
        print("(sin ficha de detalle)")
        return
    ruta, _, ancla = det.partition("#")
    archivo = Path(raiz) / ruta
    if not archivo.is_file():
        print(f"(la ficha {ruta} no existe)")
        return
    if not ancla:
        print(f"--- la ficha {ruta} no tiene apartado indicado: ábrela o busca en ella «{f['id']}»")
        return
    texto = apartado(archivo.read_text(encoding="utf-8", errors="replace"), ancla)
    print(f"--- {det}")
    print(texto if texto else f"(no encuentro el apartado «#{ancla}» en {ruta})")


# ---- Juntar ramas de Git ----------------------------------------------------------------
def resolver_fusion(filas, base, cerrados, cfg):
    """Deshace lo que deja `merge=union` al juntar dos ramas que tocaron pendientes.csv.

    Para cada id repetido compara cada versión con la de antes de juntar (`base`): se queda con lo
    que cambió cada rama, campo a campo. Si las dos cambiaron el mismo campo de forma distinta, se
    queda con la primera y lo avisa para decidirlo con «cambiar». Si el id no existía antes, son dos
    pendientes nuevos con el mismo código: el segundo recibe un código nuevo. Una fila viva cuyo id
    ya está archivado se quita de la tabla viva y se avisa. Devuelve (filas, mensajes).
    """
    base_id = {f.get("id"): f for f in base}
    archivados = {f.get("id") for f in cerrados}
    grupos, orden = {}, []
    for f in filas:
        i = f.get("id")
        if i not in grupos:
            orden.append(i)
        grupos.setdefault(i, []).append(f)
    salida, msgs, nuevas = [], [], []
    for i in orden:
        versiones = grupos[i]
        if i in archivados:
            b = base_id.get(i)
            cambios = [v for v in versiones if b is None or any(v.get(c) != b.get(c) for c in COLUMNAS)]
            msgs.append(f"{i}: estaba archivado; se quita de la tabla viva"
                        + (f" (otra rama lo cambió: «{cambios[0].get('estado')}», {cambios[0].get('que')}; "
                           "si ese cambio vale, créalo de nuevo con «nuevo»)" if cambios else ""))
            continue
        if len(versiones) == 1:
            salida.append(versiones[0])
            continue
        b = base_id.get(i)
        if b is None:
            salida.append(versiones[0])
            nuevas.extend(versiones[1:])
            continue
        distintas = []
        for v in versiones:
            if v not in distintas:
                distintas.append(v)
        fusion = dict(b)
        for c in COLUMNAS:
            valores = []
            for v in distintas:
                if v.get(c) != b.get(c) and v.get(c) not in valores:
                    valores.append(v.get(c))
            if valores:
                fusion[c] = valores[0]
            if len(valores) > 1:
                msgs.append(f"{i}: las dos ramas cambiaron «{c}» ({' / '.join(valores)}); queda "
                            f"«{valores[0]}». Si no es lo bueno, usa «cambiar».")
        salida.append(fusion)
        msgs.append(f"{i}: juntadas {len(versiones)} versiones")
    for v in nuevas:
        letra = (v.get("id") or "")[:1]
        nuevo = siguiente_id(salida + nuevas, cerrados, letra, cfg) if letra in cfg.areas else v["id"]
        msgs.append(f"{v['id']}: dos pendientes nuevos con el mismo código; «{v.get('que')}» pasa a {nuevo}")
        v["id"] = nuevo
        salida.append(v)
    return salida, msgs


def base_de_la_fusion(raiz, ref, archivo):
    """Filas de la tabla antes de juntar: en `ref`, o en la base común del último merge."""
    def git(*a):
        try:
            return subprocess.run(["git", "-C", str(raiz), *a], capture_output=True, text=True,
                                  encoding="utf-8")
        except FileNotFoundError:
            raise SystemExit("✗ «resolver» necesita Git instalado.")
    if not ref:
        r = git("merge-base", "HEAD^1", "HEAD^2")
        if r.returncode != 0:
            raise SystemExit("✗ El último commit no es una fusión: indica con --base el commit previo a "
                             "juntar las ramas.")
        ref = r.stdout.strip()
    elif git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise SystemExit(f"✗ --base «{ref}» no es un commit de este repositorio.")
    r = git("show", f"{ref}:./{archivo}")
    if r.returncode != 0:  # la tabla no existía antes de juntar: todo lo repetido es nuevo
        return []
    return list(csv.DictReader(io.StringIO(r.stdout.lstrip("﻿"))))


# ---- Programa --------------------------------------------------------------------------
class Tabla:
    """La tabla viva y la de cerradas, leídas una vez, con su comprobación."""

    def __init__(self, raiz, cfg, hoy):
        self.raiz, self.cfg, self.hoy = raiz, cfg, hoy
        self.ruta, self.ruta_cerr = raiz / cfg.archivo, raiz / cfg.archivo_cerrados
        self.cabecera, self.filas = leer(self.ruta)
        _, self.cerrados = leer(self.ruta_cerr)
        self.errores, self.avisos = comprobar(self.filas, raiz, cfg, self.cabecera, self.cerrados)

    def guardar(self):
        """Se comprueba la tabla resultante ANTES de escribir: una orden no puede dejarla en un error que
        luego impida corregirla con la propia herramienta."""
        errores, _ = comprobar(self.filas, self.raiz, self.cfg, COLUMNAS, self.cerrados)
        if errores:
            print("✗ No se escribe: la tabla quedaría con errores:")
            for e in errores:
                print("  ✗ " + e)
            return 1
        escribir(self.ruta, self.filas, self.cfg)
        return 0

    def informe(self):
        print(f"ERRORES ({len(self.errores)})")
        for e in self.errores:
            print("  ✗ " + e)
        print(f"AVISOS ({len(self.avisos)})")
        for e in self.avisos:
            print("  ! " + e)
        return 1 if self.errores else 0


def o_nuevo(t, a):
    f = orden_nuevo(t.filas, t.cerrados, a, t.hoy, t.cfg)
    if t.guardar():
        return 1
    print(f"Creada {f['id']} → {f['para']}: {f['que']}")
    return 0


def o_estado(t, a):
    f = orden_estado(t.filas, a, t.hoy, t.cfg)
    if t.guardar():
        return 1
    print(f"{f['id']} → {f['estado']}")
    return 0


def o_cambiar(t, a):
    f = orden_cambiar(t.filas, a, t.cfg)
    if t.guardar():
        return 1
    print(f"{f['id']} cambiada")
    return 0


def o_archivar(t, a):
    filas, cerrados, mover = orden_archivar(t.filas, t.cerrados, a.dias, t.hoy)
    if mover:
        # Primero las cerradas: si algo falla entre las dos escrituras, la fila queda en ambas y
        # «comprobar» lo detecta (id vivo y archivado); nunca se pierde.
        escribir(t.ruta_cerr, cerrados, t.cfg)
        escribir(t.ruta, filas, t.cfg)
    print(f"Archivadas {len(mover)}: {', '.join(f['id'] for f in mover) or '-'}")
    return 0


def o_resolver(t, a):
    if t.cabecera != COLUMNAS or any(forma_rota(f) for f in t.filas):
        print("✗ «resolver» solo arregla filas repetidas; la tabla tiene otro problema (cabecera o columnas "
              "de más o de menos) que hay que corregir antes. Mira «comprobar».")
        return 1
    filas, msgs = resolver_fusion(t.filas, base_de_la_fusion(t.raiz, a.base, t.cfg.archivo), t.cerrados, t.cfg)
    escribir(t.ruta, filas, t.cfg)
    for m in msgs:
        print("  " + m)
    errores, _ = comprobar(filas, t.raiz, t.cfg, COLUMNAS, t.cerrados)
    print(f"Resuelto: {len(msgs)} avisos. ERRORES ({len(errores)})")
    for e in errores:
        print("  ✗ " + e)
    return 1 if errores else 0


def o_ver(t, a):
    quien = "todas" if a.quien.lower() == "todas" else canonico(a.quien, t.cfg.destinos)
    if quien is None:
        print(f"«{a.quien}» no vale; usa {', '.join(t.cfg.destinos)} o todas")
        return 2
    ver(t.filas, quien, t.hoy, t.cfg, todo=a.todo)
    return t.informe()


def o_hechos(t, a):
    quien = canonico(a.quien, t.cfg.destinos) if a.quien else None
    if a.quien and quien is None:
        print(f"«{a.quien}» no vale; usa {', '.join(t.cfg.destinos)}")
        return 2
    hechos = [f for f in t.filas if f.get("estado") == VIGENTE and (quien is None or f["para"] == quien)]
    print(f"HECHOS VIGENTES ({len(hechos)}): si alguno ya no es verdad, «estado <id> descartada --nota ...»")
    for f in hechos:
        print(f"  {f['id']:<5} {f['para']:<10} {f['que']}")
    return t.informe() if t.errores else 0


def o_detalle(t, a):
    orden_detalle(t.filas, t.cerrados, t.raiz, a.id)
    return 0


def o_buscar(t, a):
    aguja = normalizar(" ".join(a.texto))
    total = 0
    for f in t.filas + t.cerrados:
        if aguja in normalizar(" ".join(f.get(c) or "" for c in COLUMNAS)):
            total += 1
            print(f"  {f['id']:<5} [{f['estado']}] → {f['para']}: {f['que']}")
    print(f"{total} coincidencias")
    return t.informe() if t.errores else 0


ORDENES = {"nuevo": o_nuevo, "estado": o_estado, "cambiar": o_cambiar, "archivar": o_archivar,
           "resolver": o_resolver, "ver": o_ver, "hechos": o_hechos, "detalle": o_detalle,
           "buscar": o_buscar, "comprobar": lambda t, a: t.informe()}


def crear_parser():
    general = argparse.ArgumentParser(add_help=False)
    general.add_argument("--raiz", default=".")
    general.add_argument("--hoy", default=None)
    # En las órdenes, --raiz y --hoy no tienen valor por defecto propio: si no, pisarían en silencio
    # el que se puso antes de la orden y se escribiría en la tabla equivocada.
    comun = argparse.ArgumentParser(add_help=False)
    comun.add_argument("--raiz", default=argparse.SUPPRESS)
    comun.add_argument("--hoy", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(description="Lista única de pendientes (pendientes.csv).", parents=[general])
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="orden")
    p = sub.add_parser("ver", parents=[comun]); p.add_argument("quien")
    p.add_argument("--todo", action="store_true", help="a la persona, enseña también lo de prioridad baja")
    p = sub.add_parser("nuevo", parents=[comun])
    p.add_argument("--de", required=True); p.add_argument("--para", required=True)
    p.add_argument("--que", required=True); p.add_argument("--vence")
    p.add_argument("--prioridad", help="por defecto, normal"); p.add_argument("--esfuerzo", help="por defecto, medio")
    p.add_argument("--permiso", help="por defecto, preguntar")
    p.add_argument("--detalle"); p.add_argument("--nota")
    p.add_argument("--vigente", action="store_true", help="un hecho que sigue siendo verdad, no una tarea")
    p = sub.add_parser("estado", parents=[comun])
    p.add_argument("id"); p.add_argument("estado"); p.add_argument("--nota")
    p = sub.add_parser("cambiar", parents=[comun])
    p.add_argument("id")
    for opcion in ("--para", "--que", "--vence", "--prioridad", "--esfuerzo", "--permiso", "--detalle", "--nota"):
        p.add_argument(opcion)
    p.add_argument("--sin-vence", action="store_true")
    p = sub.add_parser("buscar", parents=[comun]); p.add_argument("texto", nargs="+")
    p = sub.add_parser("detalle", parents=[comun]); p.add_argument("id")
    sub.add_parser("comprobar", parents=[comun])
    p = sub.add_parser("resolver", parents=[comun]); p.add_argument("--base")
    p = sub.add_parser("archivar", parents=[comun]); p.add_argument("--dias", type=int, default=30)
    p = sub.add_parser("hechos", parents=[comun]); p.add_argument("quien", nargs="?")
    return ap


def main(argv=None):
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")
    ap = crear_parser()
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.orden:
        ap.print_help()
        return 2
    raiz = Path(a.raiz).resolve()
    if not raiz.is_dir():
        print(f"✗ La carpeta {raiz} no existe (¿--raiz mal escrito?).")
        return 1
    cfg = Config(raiz)
    hoy = fecha(a.hoy) if a.hoy else date.today()
    if hoy is None:
        ap.error("--hoy debe ser AAAA-MM-DD")
    # Solo esperan su turno las órdenes que escriben; las que leen ven la tabla entera igual, porque se
    # escribe de una vez.
    with contextlib.nullcontext() if a.orden in SOLO_LEEN else candado(raiz / cfg.archivo):
        t = Tabla(raiz, cfg, hoy)
        if t.cabecera is None and a.orden != "nuevo":
            print(f"✗ No existe {cfg.archivo} en {raiz} (o está vacío). Crea el primero con «nuevo».")
            return 1
        if a.orden in ESCRIBEN and t.errores:
            print("✗ La tabla tiene errores; corrígelos antes de escribir:")
            for e in t.errores:
                print("  ✗ " + e)
            return 1
        return ORDENES[a.orden](t, a)


def principal():
    """Un fallo del sistema (sin permiso para escribir, un CSV que no está en UTF-8…) sale como mensaje
    y código 1, no como un volcado de Python."""
    try:
        return main()
    except UnicodeDecodeError as e:
        print(f"✗ Un archivo no está en UTF-8 (¿guardado desde otro programa?): {e}")
    except OSError as e:
        print(f"✗ {e.strerror or e}: {e.filename or ''}")
    return 1


# ---- Control propio --------------------------------------------------------------------
def self_test():
    casos = 0

    def ok(cond, msg):
        nonlocal casos
        casos += 1
        if not cond:
            raise AssertionError(msg)

    def callado(fn, *args, **kw):
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            fn(*args, **kw)
        return salida.getvalue()

    def falla(fn, *args):
        """El mensaje con el que se niega una orden; None si no se niega."""
        try:
            callado(fn, *args)
        except SystemExit as e:
            return str(e)
        return None

    def ns(**kw):
        return argparse.Namespace(**{"nota": None, **kw})

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        (raiz / "config.json").write_text(json.dumps(
            {"areas": {"T": "Trabajo", "C": "Casa", "E": "Estudios"}, "persona": "Alex"}), encoding="utf-8")
        cfg = Config(raiz)
        (raiz / "ficha.md").write_text("x", encoding="utf-8")
        hoy = "2026-11-17"
        ruta = raiz / ARCHIVO
        cerradas = raiz / cfg.archivo_cerrados

        def run(*args, dia=hoy):
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                return main([*args, "--raiz", str(raiz), "--hoy", dia])

        def filas_de(r=ruta):
            return leer(r)[1]

        ok(run("nuevo", "--de", "Estudios", "--para", "Casa", "--que", "Montar, con comas, el vídeo",
               "--vence", "2026-11-20", "--prioridad", "alta", "--detalle", "ficha.md") == 0,
           "nuevo E1 (crea el CSV si no existe)")
        ok(run("nuevo", "--de", "estudios", "--para", "estudios", "--que", "Propia", "--esfuerzo", "rapido") == 0,
           "nuevo E2 sin tildes")
        ok(run("nuevo", "--de", "Trabajo", "--para", "alex", "--que", "Decidir") == 0, "nuevo T1")
        filas = filas_de()
        ok([f["id"] for f in filas] == ["T1", "E1", "E2"], f"ids y orden: {[f['id'] for f in filas]}")
        ok(filas[1]["que"] == "Montar, con comas, el vídeo", "las comas sobreviven")
        ok(filas[2]["esfuerzo"] == "rápido" and filas[0]["para"] == "Alex", "normaliza tildes y mayúsculas")
        bruto = ruta.read_bytes()
        ok(bruto.startswith(b"\xef\xbb\xbf") and b"\r\n" not in bruto, "BOM y LF")
        ok(not list(raiz.glob("*.tmp")), "la escritura no deja temporales")
        ok(falla(run, "estado", "E1", "confirmada") is not None, "confirmar una abierta falla")
        ok(run("estado", "E1", "hecha", "--nota", "montado") == 0, "hecha")
        ok(run("estado", "E2", "hecha") == 0, "propia")
        filas = {f["id"]: f for f in filas_de()}
        ok(filas["E2"]["estado"] == "confirmada" and filas["E2"]["cerrada"] == hoy,
           "una pendiente propia hecha se cierra sola")
        texto = callado(ver, filas_de(), "Estudios", date(2026, 11, 17), cfg)
        ok("CONFÍRMALAS (1)" in texto and "E1" in texto, "Estudios ve que Casa terminó E1")
        ok(run("estado", "E1", "confirmada") == 0, "confirmar")
        ok(falla(run, "estado", "T1", "bloqueada") is not None, "bloqueada sin nota falla")
        ok(run("archivar", dia="2026-12-20") == 0, "archivar")
        ok([f["id"] for f in filas_de()] == ["T1"], "quedan las activas")
        ok(len(filas_de(cerradas)) == 2, "dos archivadas")
        ok(run("nuevo", "--de", "Estudios", "--para", "Estudios", "--que", "Otra") == 0, "nuevo tras archivar")
        ok(any(f["id"] == "E3" for f in filas_de()), "no reutiliza números archivados")
        # Choque de dos ramas (unión de Git): id repetido → error y no se escribe
        texto = ruta.read_text(encoding="utf-8-sig")
        ultima = texto.splitlines()[-1]
        ruta.write_text(texto + ultima.replace("Otra", "Otra distinta") + "\n", encoding="utf-8-sig")
        ok(run("comprobar") == 1, "id repetido es error")
        ok(run("nuevo", "--de", "Trabajo", "--para", "Trabajo", "--que", "x") == 1, "no escribe con errores")
        ok(falla(buscar_fila, filas_de(), "E3") is not None, "buscar_fila se niega con un id repetido")
        # Resolver: filas cambiadas por dos ramas y dos nuevas con el mismo código
        b = [{c: "" for c in COLUMNAS} | {"id": i, "para": "Trabajo", "estado": "abierta",
              "prioridad": "normal", "esfuerzo": "medio", "permiso": "preguntar", "creada": hoy, "que": q}
             for i, q in (("T5", "cinco"), ("T6", "seis"))]
        a5 = dict(b[0], estado="hecha"); a6 = dict(b[1], prioridad="alta")
        n1 = dict(b[0], id="T7", que="nueva A"); n2 = dict(b[0], id="T7", que="nueva B")
        res, _ = resolver_fusion([b[0], a6, a5, b[1], n1, n2], b, [], cfg)
        r = {f["id"]: f for f in res}
        ok(len(res) == 4 and r["T5"]["estado"] == "hecha" and r["T6"]["prioridad"] == "alta",
           "resolver junta los cambios de cada rama")
        ok(r["T7"]["que"] == "nueva A" and r["T8"]["que"] == "nueva B", "resolver renumera la nueva repetida")
        res, m = resolver_fusion([dict(b[1], estado="hecha")], b, [b[1]], cfg)
        ok(res == [] and "archivado" in m[0], "resolver quita lo ya archivado")
        e, _ = comprobar([b[1]], raiz, cfg, COLUMNAS, [b[1]])
        ok(any("archivado" in x for x in e), "comprobar ve un id vivo y archivado")
        # Comprobación fila a fila
        def errores_de(**cambio):
            return comprobar([dict(b[0], **cambio)], raiz, cfg, COLUMNAS)[0]
        ok(errores_de() == [], "fila de base válida")
        ok(errores_de(estado="confirmada"), "cerrada sin fecha de cierre: error")
        ok(errores_de(cerrada=hoy), "fecha de cierre en una abierta: error")
        ok(errores_de(que="  "), "«que» vacío: error")
        ok(errores_de(id="T05"), "id con ceros delante: error")
        ok(errores_de(que="dos\nlíneas"), "salto de línea en un campo: error")
        ok(comprobar([b[0]], raiz, cfg, COLUMNAS[:-1])[0], "cabecera distinta: error")
        ok(any("no existe" in x for x in comprobar([dict(b[0], detalle="falta.md")], raiz, cfg)[1]),
           "aviso de ficha inexistente")
        # Permisos y sección «hazlas ya»
        escribir(ruta, [], cfg)
        ok(run("nuevo", "--de", "Trabajo", "--para", "Trabajo", "--que", "Rápida libre", "--esfuerzo", "rápido",
               "--permiso", "libre") == 0, "nuevo con permiso libre")
        texto = callado(ver, filas_de(), "Trabajo", date(2026, 11, 17), cfg)
        ok("SIN PREGUNTAR" in texto and "Rápida libre" in texto.split("3. LO QUE")[0]
           and "Rápida libre" not in texto.split("3. LO QUE")[1],
           "las rápidas con permiso salen en la sección 2, no repetidas en la 3")
        ok(any("OK de Alex" in x for x in errores_de(permiso="aprobada", nota="Espera a T8")),
           "aprobada exige el OK de la persona con fecha")
        ok(not errores_de(permiso="aprobada", nota="Alex 2026-11-17, en la reunión"),
           "aprobada con OK registrado vale")
        ok(errores_de(permiso="aprobada", nota="NoAlex 2026-11-17") and
           errores_de(permiso="aprobada", nota="Alex 2026-02-30"),
           "el OK exige el nombre entero y una fecha real")
        cfg_punto = Config(raiz)
        cfg_punto.persona = "A.B."
        ok(cfg_punto.ok_registrado("A.B. 2026-11-17"), "un nombre que acaba en punto también puede aprobar")
        ok(any("a sí misma" in x for x in errores_de(para="Casa", permiso="libre")),
           "libre no vale para trabajo de otra área")
        ok(falla(run, "cambiar", "T1", "--permiso", "aprobada") is not None, "dar permiso sin nota falla")
        ok(any("la hace esa persona" in x for x in errores_de(para="Alex", permiso="libre")),
           "lo de la persona no puede ser libre")
        antes = ruta.read_bytes()
        ok(run("nuevo", "--de", "Trabajo", "--para", "Trabajo", "--que", "dos\nlíneas") == 1
           and ruta.read_bytes() == antes, "un salto de línea en --que no se escribe")
        (raiz / "ficha.md").write_text("# T\n\n## T1\n\nuno\n\n### sub\n\nsigue\n\n## T2\n\ndos\n", encoding="utf-8")
        ok(apartado((raiz / "ficha.md").read_text(encoding="utf-8"), "t1") == "## T1\n\nuno\n\n### sub\n\nsigue",
           "apartado corta en el siguiente del mismo nivel")
        filas_p = [dict(b[0], id="T40", para="Alex", prioridad="baja", que="baja sin plazo"),
                   dict(b[0], id="T41", para="Alex", que="normal")]
        corto = callado(ver, filas_p, "Alex", date(2026, 11, 17), cfg)
        largo = callado(ver, filas_p, "Alex", date(2026, 11, 17), cfg, todo=True)
        ok("baja sin plazo" not in corto and "Y 1 de prioridad baja" in corto and "baja sin plazo" in largo,
           "a la persona se le oculta lo de prioridad baja salvo con --todo")
        ruta.write_text(",".join(COLUMNAS) + "\nX1,Nadie,rara,urgente,mucho,todo,2026-13-01,,,,,\n", encoding="utf-8-sig")
        e, _ = comprobar(filas_de(), raiz, cfg, COLUMNAS)
        ok(len(e) >= 7, f"detecta id, para, estado, prioridad, esfuerzo, permiso, fecha y qué: {len(e)}")
        # Columnas de menos: un solo error que lo dice, no uno por cada campo vacío
        ruta.write_text(",".join(COLUMNAS) + "\nT1,Trabajo,abierta\n", encoding="utf-8-sig")
        e, _ = comprobar(filas_de(), raiz, cfg, COLUMNAS)
        ok(len(e) == 1 and "3 columnas de 12" in e[0] and "faltan prioridad" in e[0],
           f"una fila con columnas de menos da un error claro: {e}")
        antes = ruta.read_bytes()
        ok(run("resolver", "--base", "HEAD") == 1 and ruta.read_bytes() == antes,
           "resolver no escribe una tabla con columnas de menos")
        # Una comilla sin cerrar se traga las filas siguientes: tiene que salir como error
        escribir(ruta, [b[0], b[1]], cfg)
        ruta.write_text(ruta.read_text(encoding="utf-8-sig").replace(",cinco,", ',"cinco,'), encoding="utf-8-sig")
        ok(run("comprobar") == 1, "una comilla sin cerrar es error")
        # Una columna de más: «resolver» no escribe
        ruta.write_text(ruta.read_text(encoding="utf-8-sig").replace('"cinco', "cinco,EXTRA"),
                        encoding="utf-8-sig")
        antes = ruta.read_bytes()
        ok(run("resolver", "--base", "HEAD") == 1 and ruta.read_bytes() == antes,
           "resolver no escribe una tabla con columnas de más")
        h = date(2026, 11, 17)
        base = {"estado": "abierta", "prioridad": "normal", "esfuerzo": "medio", "vence": ""}
        a1 = {**base, "id": "T1", "vence": "2026-11-10"}
        a2 = {**base, "id": "T2", "prioridad": "alta"}
        a3 = {**base, "id": "T3", "esfuerzo": "rápido"}
        a4 = {**base, "id": "T4"}
        ok([f["id"] for f in sorted([a4, a3, a2, a1], key=lambda f: urgencia(f, h, cfg))]
           == ["T1", "T2", "T3", "T4"], "orden de urgencia")
        # Hechos vigentes: sin prioridad, esfuerzo, permiso ni plazo; solo pasan a descartada
        hv = {c: "" for c in COLUMNAS}
        hv.update(id="T50", para="Trabajo", estado="vigente", creada="2026-11-18", que="La web está en pausa")
        ok(comprobar([hv], raiz, cfg)[0] == [], "hecho vigente válido")
        ok(comprobar([{**hv, "prioridad": "alta"}], raiz, cfg)[0], "hecho con prioridad: error")
        ok(comprobar([{**hv, "vence": "2026-12-01"}], raiz, cfg)[0], "hecho con plazo: error")
        ok(comprobar([{**hv, "para": "Alex"}], raiz, cfg)[0], "hecho de la persona: error")
        ok(comprobar([{**hv, "estado": "descartada", "cerrada": "2026-11-19"}], raiz, cfg)[0] == [],
           "hecho descartado válido")
        ok(comprobar([{**hv, "estado": "abierta"}], raiz, cfg)[0], "tarea sin prioridad: error")
        ok("solo está" in (falla(orden_estado, [dict(hv)], ns(id="T50", estado="abierta"), h, cfg) or ""),
           "un hecho no pasa a tarea")
        ok("no se convierte" in (falla(orden_estado, [dict(b[0])], ns(id="T5", estado="vigente"), h, cfg) or ""),
           "una tarea no pasa a hecho")
        nuevo_hecho = dict(de="Trabajo", para="Trabajo", que="x", vigente=True, vence=None, prioridad=None,
                           esfuerzo=None, permiso=None, detalle=None)
        ok(falla(orden_nuevo, [], [], ns(**{**nuevo_hecho, "vence": "2026-12-01"}), h, cfg) is not None
           and falla(orden_nuevo, [], [], ns(**{**nuevo_hecho, "prioridad": "alta"}), h, cfg) is not None,
           "un hecho nuevo no admite plazo ni prioridad")
        escribir(ruta, [], cfg)
        run("nuevo", "--de", "Trabajo", "--para", "Trabajo", "--vigente", "--que", "Hecho", dia="2026-11-18")
        hid = filas_de()[-1]["id"]
        antes = ruta.read_bytes()
        for orden in (["nuevo", "--de", "Trabajo", "--para", "Alex", "--vigente", "--que", "x"],
                      ["cambiar", hid, "--prioridad", "alta"], ["cambiar", hid, "--vence", "2026-12-01"],
                      ["estado", hid, "abierta"]):
            falla(run, *orden)
            ok(ruta.read_bytes() == antes, f"no escribe una tabla inválida: {orden}")
        run("estado", hid, "descartada", "--nota", "ya no", dia="2026-11-18")
        falla(run, "estado", hid, "abierta")
        ok(filas_de()[-1]["estado"] == "descartada", "un hecho descartado no pasa a tarea")
        run("estado", hid, "vigente", dia="2026-11-18")
        ok(filas_de()[-1]["estado"] == "vigente" and not filas_de()[-1]["cerrada"],
           "un hecho descartado por error vuelve a vigente")
        # buscar, hechos y detalle (solo leen)
        def con_salida(*args):
            s = io.StringIO()
            with contextlib.redirect_stdout(s):
                rc = main([*args, "--raiz", str(raiz), "--hoy", "2026-11-18"])
            return rc, s.getvalue()
        run("nuevo", "--de", "Trabajo", "--para", "Casa", "--que", "Comprar el sillón", "--detalle", "ficha.md#t1",
            dia="2026-11-18")
        run("nuevo", "--de", "Trabajo", "--para", "Casa", "--que", "Pintar", "--detalle", "ficha.md#nada",
            dia="2026-11-18")
        sillon = next(f["id"] for f in filas_de() if f["que"] == "Comprar el sillón")
        pintar = next(f["id"] for f in filas_de() if f["que"] == "Pintar")
        rc, out = con_salida("buscar", "SILLON")
        ok(rc == 0 and "1 coincidencias" in out and sillon in out, "buscar no distingue mayúsculas ni tildes")
        rc, out = con_salida("buscar", "montar")
        ok(rc == 0 and "1 coincidencias" in out and "E1" in out, "buscar mira también las archivadas")
        rc, out = con_salida("buscar", "nada", "de", "esto")
        ok(rc == 0 and "0 coincidencias" in out, "buscar sin resultados no es un error")
        rc, out = con_salida("hechos")
        ok(rc == 0 and "HECHOS VIGENTES (1)" in out and hid in out and sillon not in out,
           "hechos enseña los vigentes y no las tareas")
        rc, out = con_salida("hechos", "casa")
        ok(rc == 0 and "HECHOS VIGENTES (0)" in out, "hechos filtra por área")
        ok(con_salida("hechos", "Nadie")[0] == 2, "hechos con un área que no existe: error")
        rc, out = con_salida("detalle", sillon.lower())
        ok(rc == 0 and "que: Comprar el sillón" in out and "uno" in out and "sigue" in out and "dos" not in out,
           "detalle enseña la fila y solo su apartado de la ficha")
        ok("no encuentro el apartado «#nada»" in con_salida("detalle", pintar)[1],
           "detalle avisa si el apartado no está")
        ok(falla(run, "detalle", "T999") is not None, "detalle de un id que no existe se niega")
        # Una orden cada vez
        with candado(ruta, espera=0.2):
            ok(falla(lambda: candado(ruta, espera=0.2).__enter__()) is not None,
               "una segunda orden espera y se niega si la primera no termina")
        with candado(ruta, espera=0.2):
            ok(True, "el candado se suelta al terminar la orden")
            inicio = time.monotonic()
            rc, _ = con_salida("ver", "todas")
            ok(rc == 0 and time.monotonic() - inicio < 2, "una orden que solo lee no espera al candado")
        # --base que no es un commit (solo si hay Git)
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", str(raiz)], capture_output=True)
            ok("no es un commit" in (falla(base_de_la_fusion, raiz, "no-existe", ARCHIVO) or ""),
               "resolver se niega con un --base que no existe")
    if shutil.which("git"):
        # Fusión real: dos ramas cambian campos distintos de T1; «resolver» se queda con los dos cambios
        with tempfile.TemporaryDirectory() as tmp:
            def git(*a):
                return subprocess.run(["git", "-C", tmp, "-c", "user.name=Prueba", "-c", "user.email=prueba@local",
                                       "-c", "core.autocrlf=false", *a], capture_output=True, text=True)

            def orden(*a):
                with contextlib.redirect_stdout(io.StringIO()):
                    return main([*a, "--raiz", tmp, "--hoy", "2026-01-01"])
            git("init", "-q")
            (Path(tmp) / ".gitattributes").write_text(f"{ARCHIVO} merge=union\n", encoding="utf-8")
            orden("nuevo", "--de", "General", "--para", "General", "--que", "x")
            git("add", ARCHIVO, ".gitattributes")
            git("commit", "-q", "-m", "base")
            principal_rama = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            git("checkout", "-q", "-b", "otra")
            orden("estado", "G1", "en curso")
            git("commit", "-q", "-am", "otra")
            git("checkout", "-q", principal_rama)
            orden("cambiar", "G1", "--prioridad", "alta")
            git("commit", "-q", "-am", "principal")
            fusion = git("merge", "-q", "--no-ff", "otra", "-m", "junta")
            rc = orden("resolver")
            filas = leer(Path(tmp) / ARCHIVO)[1]
            ok(fusion.returncode == 0 and rc == 0 and len(filas) == 1 and filas[0]["estado"] == "en curso"
               and filas[0]["prioridad"] == "alta", f"resolver junta una fusión real de Git: {filas}")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ARCHIVO).write_text("", encoding="utf-8")  # vacío (0 bytes): como si no existiera
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["nuevo", "--de", "General", "--para", "Persona", "--que", "x", "--raiz", tmp])
            ok(main(["comprobar", "--raiz", str(Path(tmp) / "no-existe")]) == 1, "--raiz inexistente: error")
        ok(rc == 0 and leer(Path(tmp) / ARCHIVO)[1][0]["id"] == "G1", "sin config.json: área General y «Persona»")
        for config in ("{roto", '{"archivo_pendientes": "../fuera.csv"}', '{"persona": 7}',
                       '{"areas": {"T": "Trabajo", "C": "trabajo"}}'):
            (Path(tmp) / "config.json").write_text(config, encoding="utf-8")
            ok(falla(Config, Path(tmp)) is not None, f"config.json no válido se rechaza: {config}")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.json").write_text('{"archivo_pendientes": "tareas.csv"}', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = main(["nuevo", "--de", "General", "--para", "General", "--que", "x", "--raiz", tmp,
                       "--hoy", "2026-01-01"])
            main(["estado", "G1", "hecha", "--raiz", tmp, "--hoy", "2026-01-01"])
            main(["archivar", "--raiz", tmp, "--hoy", "2026-03-01"])
        ok(rc == 0 and not (Path(tmp) / ARCHIVO).exists() and len(leer(Path(tmp) / "tareas_cerrados.csv")[1]) == 1,
           "el nombre del archivo sale de config.json (y el de cerradas, de él)")
    print(f"SELFTEST PASS: {casos}/{casos}")
    return 0


if __name__ == "__main__":
    sys.exit(principal())
