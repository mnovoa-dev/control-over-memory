# Control antes que memoria

*English summary below.*

Dos herramientas pequeñas para trabajar con un asistente de IA (Claude, ChatGPT u otro) durante meses
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

En la carpeta donde quieras guardar los CSV, copia `config.ejemplo.json` como `config.json` y cambia las
áreas (una letra mayúscula y un nombre cada una) y el nombre de la persona que decide. Los ejemplos de
abajo usan ese archivo tal cual (áreas Trabajo, Casa y Estudios; persona «Alex»). Sin `config.json` hay
una sola área, «General», y la persona se llama «Persona».

Para que el asistente las use, dile en sus instrucciones (por ejemplo, en `CLAUDE.md`) que los pendientes y
los fallos se cambian **solo** con estas herramientas, nunca editando el CSV.

Los ejemplos usan `py`, la forma habitual en Windows; en macOS y Linux, escribe `python3`.

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

## Límites

- Están en castellano: órdenes, mensajes y columnas.
- No sustituyen el criterio de quien decide: ordenan, comprueban y avisan. Los controles cierran el paso al
  descuido, no a quien quiera saltárselos a propósito.
- Una orden que escribe cada vez: si otra está escribiendo, la segunda espera unos segundos y, si no puede, se
  niega. Las que solo leen (`ver`, `comprobar`…) no esperan.
  Para eso dejan un archivo `.lock` junto al CSV: si guardas tus CSV en Git, añade `*.lock` y `.*.tmp` a tu
  `.gitignore`.
- Probadas en Windows, Linux y macOS con Python 3.10 y 3.13, en cada cambio (GitHub Actions): las autopruebas y
  20 órdenes que escriben y 20 que leen a la vez.
- `resolver` de `pendientes.py` usa Git para encontrar la versión de antes de juntar dos ramas.
- Un CSV con comas se abre bien en Excel con la configuración en inglés; con la española, mejor con
  «Datos > Desde texto/CSV».

Licencia MIT.

---

## English summary

Two small, dependency-free Python tools for working with an AI assistant over months without losing
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

The README lists the working rules the tools enforce (no hand edits, recorded approvals, fixed failure types, and more). Configure areas and the decision-maker's name in
`config.json` (see `config.ejemplo.json`). MIT licence.
