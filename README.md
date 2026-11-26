# Control antes que memoria

*English summary below.*

Tres herramientas pequeñas para trabajar con un asistente de IA (Claude, ChatGPT u otro) durante meses
sin que lo aprendido se pierda ni se degrade. Nacen de un sistema personal real con cuatro áreas de
trabajo, más de 170 tareas y hechos apuntados y más de cien fallos registrados.

## La idea

Un modelo de lenguaje olvida. La solución habitual es escribirle reglas: «no hagas X», «comprueba Y».
Funciona un tiempo, hasta que el archivo de reglas crece, el modelo deja de leerlo entero y el fallo vuelve.

En ese sistema, 4 de los 8 tipos de fallo que ya tenían su regla escrita volvieron a aparecer. Por eso el
principio es **control antes que memoria**: si algo se puede comprobar con una herramienta, se comprueba
con una herramienta, no se le pide al modelo que lo recuerde.

- Una lista que nadie pone al día no sirve. Una tabla que solo se toca con una herramienta, y que la
  herramienta comprueba en cada cambio, se mantiene sola.
- Un fallo apuntado con otras palabras parece nuevo. Si los tipos de fallo son fijos, la repetición se ve.

## Reglas que hacen cumplir

- **Sin edición a mano:** cada cambio se comprueba antes de escribirse; menos errores.
- **El permiso de la persona manda:** una tarea solo pasa a «aprobada» si la nota cita a esa persona y una fecha.
- **Quien pide, confirma:** lo terminado espera, a la vista de quien lo pidió, hasta que lo confirme.
- **Fallos por tipo fijo:** un sinónimo no esconde una repetición.
- **Fallo que vuelve pese a su regla:** se señala aparte, como aviso de que hace falta un control y no otra frase.
- **El estado se repasa:** los hechos vigentes tienen su propia lista para retirar los que dejan de ser verdad.
- **Cada regla, localizable:** un fallo resuelto apunta al archivo donde está su regla.

Sin dependencias: solo Python 3.10 o posterior. Los datos son CSV que se versionan bien en Git.

## Para empezar

En la carpeta donde quieras guardar los CSV, copia `control.ejemplo.json` como `control.json` y cambia las
áreas (una letra mayúscula y un nombre cada una) y el nombre de la persona que decide. Los ejemplos de
abajo usan ese archivo tal cual (áreas Trabajo, Casa y Estudios; persona «Alex»). Sin `control.json` hay
una sola área, «General», y la persona se llama «Persona».

Para que el asistente las use, dile en sus instrucciones (por ejemplo, en `CLAUDE.md`) que los pendientes y
los fallos se cambian **solo** con estas herramientas, nunca editando el CSV.

Los ejemplos usan `py`, la forma habitual en Windows; en macOS y Linux, escribe `python3`.

### Úsalas por separado

Cada herramienta es un solo archivo y funciona sin las otras: copia solo la que quieras. `control.json` es
opcional y lo comparten: cada una lee sus claves y no hace caso del resto, así que lo común (las áreas, el
nombre de los CSV) se escribe una vez aunque uses varias.

| Herramienta | Lee de `control.json` | Usa, si existen |
|---|---|---|
| `pendientes.py` | `areas`, `persona`, `archivo_pendientes` | — |
| `aprendizajes.py` | `areas`, `archivo_aprendizajes` | — |
| `revision.py` | `normas`, `cargados`, `no_cargados`, `max_lineas`, `herramientas`, `omitir`, `archivo_pendientes`, `archivo_aprendizajes` | `pendientes.csv` (parados y vigentes) y `aprendizajes.csv` (sitios de las reglas); sin ellos, esas partes dicen que no miran |

## `pendientes.py`: una sola lista de tareas, y de hechos

**Qué problema resuelve.** Las tareas repartidas en notas y listas se quedan viejas: nadie sabe qué está
hecho, qué espera a quién ni qué vence. El modelo, además, «recuerda» estados que ya cambiaron.

**Cómo funciona.**
- Una fila por tarea en `pendientes.csv`: para quién es, estado, prioridad, esfuerzo, plazo y permiso.
- El código dice quién la pidió (`T12` = la pidió el área Trabajo). Quien la termina la marca «hecha» y
  `ver` se la enseña a quien la pidió en un apartado propio, «confírmalas», hasta que la confirma.
- **Permisos.** Una tarea «aprobada» exige que la nota nombre a la persona que decide y dé una fecha real.
  Si no, la herramienta no la acepta. Comprueba el formato, no quién lo escribió.
- **Hechos vigentes.** Lo que no es tarea sino estado («la web está en pausa») va en la misma tabla, sin
  plazo ni prioridad, hasta que deja de ser verdad.
- `ver <área>` lo ordena por urgencia: vencido, plazo cercano, prioridad y, a igualdad, lo rápido primero.
- Antes de escribir, comprueba la tabla entera, y escribe de forma que un corte a mitad no la deje rota.
- Si la tabla ya trae un error (por ejemplo, tras juntar ramas), `cambiar` puede corregirlo sin editar el CSV a mano. Si una comilla sin cerrar ha metido filas dentro de un campo, o hay filas con columnas de más o de menos, no escribe encima: `reparar` guarda una copia, devuelve las filas a su sitio y aparta en un archivo al lado lo que no entiende. También repone la primera línea (los nombres de las columnas) y vuelve a comas un archivo separado por `;`. Mientras quede algo apartado, `comprobar` avisa; `apartadas` lo enseña y `devolver <n> --fila "…"` lo recupera con su mismo código (`--descartar` si no valía). No necesita Git.
- Si dos ramas de Git tocan la tabla a la vez, `resolver` junta los cambios de cada una, campo a campo.

**Cómo se prueba.** `py pendientes.py --self-test` (en una carpeta temporal; no toca tus datos).

```
py pendientes.py nuevo --de Trabajo --para Alex --que "Decidir el proveedor" --vence 2026-12-01
py pendientes.py ver Trabajo
py pendientes.py estado T1 hecha --nota "elegido el más barato"
```

## `aprendizajes.py`: los fallos, por tipo, para ver lo que vuelve

**Qué problema resuelve.** Si un fallo se apunta cada vez con otras palabras, nunca se ve que se repite.
Y una regla escrita para evitarlo no garantiza que el modelo la cumpla.

**Cómo funciona.**
- Una fila por fallo en `aprendizajes.csv`: qué pasó, por qué, qué regla lo evita, dónde está esa regla y
  su **tipo** (`afirmar-sin-probar`, `cambiar-sin-que-lo-pida`…).
- **Tipos fijos.** Un tipo nuevo se rechaza y se proponen los más parecidos. Para crearlo hay que pedirlo
  expresamente, con `--patron-nuevo`. Así un sinónimo no esconde una repetición por descuido.
- **Reglas que existen.** Un fallo «incorporado» exige que el archivo de su regla exista; si luego se borra, `comprobar` lo avisa y `cambiar --donde` lo arregla.
- **La revisión periódica (cuando tú elijas).** `ver --desde <fecha>` saca lo que se repite sin regla y, sobre todo, lo que
  **vuelve aunque ya tenía regla**: la señal de que hace falta un control, no otra frase.

**Cómo se prueba.** `py aprendizajes.py --self-test` (en una carpeta temporal).

```
py aprendizajes.py nuevo --area Trabajo --patron afirmar-sin-probar --patron-nuevo --que "Dijo que el informe estaba enviado sin comprobarlo" --causa "No miró la carpeta de enviados"
py aprendizajes.py ver --desde 2026-11-01
```

## `revision.py`: la revisión periódica de las reglas, las tareas y las herramientas

**Qué problema resuelve.** Las instrucciones para el asistente (`CLAUDE.md`, `AGENTS.md`…) crecen con cada
fallo. Sin repaso, la misma regla acaba en dos sitios y una copia se queda vieja cuando se cambia la otra;
citan archivos que ya no existen; y engordan hasta que el modelo deja de leerlas enteras. Mirarlo a ojo
cada semana es justo el tipo de tarea que un modelo hace mal.

**Cómo funciona.** Solo lee y da listas para que alguien decida; no cambia nada.
- `duplicados`: reglas muy parecidas en archivos distintos (`--mismo-archivo` mira también dentro de cada
  uno). Repetir una regla no es un error en sí: el riesgo es que las copias se desfasen. Si la quieres en
  un solo sitio, la lista te dice cuáles juntar; si la copias a propósito, cuáles cambiar a la vez.
- `rutas`: archivos citados que ya no existen. Entiende tres formas de citar: `carpeta/archivo.md` entre
  comillas invertidas, enlaces de Markdown (`[texto](ruta)`) y enlaces de nombre al estilo de las wikis
  (`[[nombre]]`), también con apartado o línea (`archivo.md#apartado`, `archivo.py:12`). Lo que Git ignora a
  propósito no cuenta, ni lo que va dentro de un bloque de código.
  Dice cuántas citas ha mirado; si no encuentra ninguna, lo dice, en vez de dar un cero que parezca «todo bien».
- `largos [--lineas 200]`: archivos que el asistente lee enteros y pasan del tope (200 líneas por defecto,
  o `max_lineas` en `control.json`); cuanto más largo, más fácil que se salte una parte. Mira las normas y
  lo que pongas en `cargados` (skills, guías). No adivina qué es historia: lo que se consulta por partes
  (diarios, actas) no se pone ahí, porque no hace falta dividirlo. Para que la lista no se quede vieja,
  termina con los `.md` largos que no están en ninguna lista y pregunta si alguno se lee entero: si no, se
  apuntan en `no_cargados` (vale una carpeta entera, como `actas/**/*.md`) y no se vuelve a preguntar.
- `crecimiento --desde <fecha>`: de las normas y las herramientas, lo que más engordó y lo que solo crece
  (quita menos de una línea por cada diez que añade). Necesita Git con la historia completa (un clon
  `--depth 1` lo haría parecer todo nuevo); sin Git lo dice, no da una lista vacía.
- `parados` y `vigentes`: tareas de `pendientes.csv` con el plazo pasado o «en curso» mucho tiempo, y
  hechos vigentes antiguos que quizá ya no son verdad. Si la tabla es de otro programa o no está en UTF-8
  (por ejemplo, guardada con Excel), lo dice y sigue con lo demás; las filas con datos raros (columnas de
  menos, fechas que no son AAAA-MM-DD) no se comparan a ciegas: se cuentan y se avisa.
- `pruebas`: pasa el `--self-test` de cada herramienta y da un solo resultado.
- `semanal --desde <fecha>`: todo seguido. Termina recordando `aprendizajes.py ver --desde`, que enseña los
  fallos que vuelven aunque ya tenían regla.
- **Se enlaza con las otras dos.** Además de los archivos de `normas`, mira cada `.md` que `aprendizajes.csv`
  da como sitio de una regla incorporada: si una regla nueva cae en otro archivo, la revisión lo ve sola.
- Qué archivos mira y qué comprobaciones se salta van en `control.json` (`normas`, `cargados`,
  `no_cargados`, `max_lineas`, `herramientas`, `omitir`).

**Cómo se prueba.** `py revision.py --self-test` (en una carpeta temporal).

```
py revision.py duplicados
py revision.py semanal --desde 2026-11-01
```

## Límites

- Están en castellano: órdenes, mensajes y columnas.
- No sustituyen el criterio de quien decide: ordenan, comprueban y avisan. Los controles cierran el paso al
  descuido, no a quien quiera saltárselos a propósito.
- Una orden que escribe cada vez: si otra está escribiendo, la segunda espera unos segundos y, si no puede, se
  niega. Las que solo leen (`ver`, `comprobar`…) no esperan.
  Para eso dejan un archivo `.lock` junto al CSV: si guardas tus CSV en Git, añade `*.lock`, `.*.tmp` y `.*.copia` a tu
  `.gitignore`. El archivo `*.apartadas.txt`, en cambio, va a Git con su tabla: guarda filas que ya no están en ella.
- Probadas en Windows, Linux y macOS con Python 3.10 y 3.13, en cada cambio (GitHub Actions): las autopruebas y
  20 órdenes que escriben y 20 que leen a la vez.
- `resolver` de `pendientes.py` usa Git para encontrar la versión de antes de juntar dos ramas, y
  `crecimiento` de `revision.py`, para ver qué cambió. Lo demás funciona sin Git.
- Si vienes de una versión anterior: el archivo de configuración se llamaba `config.json`; ahora es
  `control.json`. Las herramientas lo detectan y te piden renombrarlo.
- `revision.py pruebas` ejecuta cada script de `herramientas` que tenga `--self-test`: pon ahí solo
  los tuyos. `duplicados` compara palabras, no significados: da candidatos, no veredictos.
- Un CSV con comas se abre bien en Excel con la configuración en inglés; con la española, mejor con
  «Datos > Desde texto/CSV».

Licencia MIT.

---

## English summary

Three small, dependency-free Python tools for working with an AI assistant over months without losing
what was learned. The principle is **control over memory**: whatever can be checked by a tool is checked
by a tool instead of asking the model to remember a written rule. In the real system they come from,
4 of 8 failure types that already had a written rule came back.

- **`pendientes.py`**: a single validated CSV of tasks and current facts across areas, with requester
  and owner, priority, effort, due date and permission (an "approved" task must name the approver and a
  date). It sorts by urgency, refuses any write that would leave the table invalid, writes atomically,
  runs one writing command at a time, and merges rows changed on two Git branches field by field. `--self-test`.
- **`aprendizajes.py`**: a CSV of failures with a fixed **type** per failure. New types are rejected
  unless explicitly requested, and similar existing ones are suggested, so synonyms do not hide a
  repetition by accident. The periodic review view highlights failures that **came back despite having a
  rule**, the signal that a tool-based check is needed. `--self-test`.
- **`revision.py`**: a read-only periodic review of the assistant's rule files: near-duplicate rules
  (copies that can drift apart), cited paths that no longer exist, files read in full that pass a line limit, files that only grow (needs Git),
  stalled tasks, old current facts, and every tool's self-test in one result. It also checks every file
  that `aprendizajes.csv` names as the home of a rule. `--self-test`.

The README lists the working rules the tools enforce (no hand edits, recorded approvals, fixed failure types, and more). Configure areas and the decision-maker's name in
`control.json` (see `control.ejemplo.json`). MIT licence.
