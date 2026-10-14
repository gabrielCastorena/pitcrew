# Diseño

Cómo funciona setup-architect por dentro, qué protege y qué no. Para instalarlo y usarlo, mira el `README.md`.

## Objetivo

Armar el setup de Claude Code de un proyecto (un `CLAUDE.md`, skills, servidores MCP y pasos manuales) a partir de herramientas que existen hoy, explicando por qué está cada pieza y con un nivel de confianza que no depende de lo que diga ningún texto de internet.

## Arquitectura

Hay dos mitades con responsabilidades separadas:

- **El plugin** (`plugin/`) razona. Una skill entrevista al usuario, elige el entorno (proyecto, global o chat), lanza subagentes investigadores y redacta el plan. Los investigadores (`plugin/agents/researcher.md`) solo tienen búsqueda y lectura web, sin shell ni escritura, y solo pueden devolver un JSON con items.
- **El CLI `sarch`** (`src/setup_architect/`) hace todo lo determinista, sin usar ningún modelo: verifica (`verify`), muestra el plan (`render`), lo aplica (`apply`), lo deshace (`revert`) y revisa lo instalado (`audit`).

El flujo es lineal: entrevista → investigación → `sarch verify` → `plan.json` → `sarch render` → `sarch apply`, que corre el usuario en su terminal. `plan.json` es la única fuente de verdad: de él salen las dos vistas y es lo único que lee el aplicador.

## Modelo de amenaza

El riesgo principal es que un texto de internet (un README, una página, una descripción de paquete) acabe ejecutándose o instalado como instrucciones para Claude. Las defensas:

1. **Lo que lee internet no ejecuta nada.** Los investigadores no tienen shell ni escritura. Lo que devuelven son datos: la skill no sigue órdenes que aparezcan en ellos, y descarta cualquier `write_file` que un investigador proponga.
2. **La confianza la calcula un programa.** El verificador no usa ningún modelo, así que ningún texto puede convencerlo de subir un nivel.
3. **Lista cerrada de operaciones.** El plan solo puede expresar cinco operaciones tipadas. No hay forma de expresar un comando de shell libre, y una clave desconocida invalida el plan entero.
4. **La aprobación es del usuario.** El aplicador lee la respuesta de la terminal (`/dev/tty`), no de stdin, y vacía la entrada pendiente antes de cada pregunta. Calcula el hash del plan por su cuenta y muestra un resumen generado del propio archivo, no de lo que diga Claude. Es una garantía procedimental: un programa que lance `sarch apply` dentro de su propia pseudoterminal podría contestar. Por eso la skill nunca corre el aplicador.
5. **El `CLAUDE.md` es una vía de inyección persistente.** Se carga en cada sesión futura, así que se valida antes de escribirlo y siempre se confirma aparte.

## Verificación

`sarch verify` evalúa cada item del plan contra el repo de GitHub de su fuente y devuelve una ficha con nivel, razones, banderas, la referencia fijada (`pinned_ref`) y la popularidad. Los umbrales viven en `config/thresholds.toml`; los dueños oficiales y los nombres conocidos, en `config/official_owners.txt` y `config/known_names.txt`.

**Vetos** (cualquiera da `Bloqueado`, sin importar lo demás):

- Nombre casi idéntico al de un repo conocido de otro dueño (typosquatting).
- Repo de menos de 30 días con 500 estrellas o más.
- Fork con menos de 5 commits propios sobre su repo padre.
- Una skill que ejecuta shell al invocarse (`` !`comando` `` o un bloque ```` ```! ```` en un `.md`), que trae `curl … | sh` o una evaluación remota, que declara `hooks` en su frontmatter, que tiene un frontmatter que no se puede analizar o que incluye archivos de settings, hooks o `.mcp.json`.
- Un `copy_skill` que apunta a otro repo que la fuente, o a un commit que no pertenece a la rama principal del repo.
- Un archivo comprimido con rutas que escapan del destino.

**Niveles** (si no hay veto):

| Nivel | Cuándo |
| --- | --- |
| Verificado | Dueño oficial y actividad en los últimos 365 días |
| Confiable | De terceros, con actividad reciente, sin scripts incluidos ni MCP stdio o con variables, y sin un fork sin comparar |
| Revisar | Todo lo demás que no es veto |
| Bloqueado | Un veto |

**Topes a Revisar**, que nunca le ganan a un veto:

- Un item con cualquier `add_mcp`: se verifica el repo, no el paquete de npm o PyPI, la imagen de docker ni la URL que ejecutará el servidor.
- Un item con cualquier `write_file`, sea el `CLAUDE.md` o un archivo bajo `.claude/skills/`, declare la fuente que declare: el nivel saldría del repo, no del contenido que escribe el plan.
- Una skill con `allowed-tools`: pre-aprueba herramientas que Claude usaría sin pedir permiso.

**Lo que no se pudo verificar** (red caída, límite de consultas, repo inexistente) sale en `Revisar` con la bandera `unverified`, y el aplicador lo omite sin preguntar. **Una fuente que no es un repo de GitHub** sale en `Revisar` con la bandera `unsupported_source`: se puede confirmar, pero la pregunta avisa de que nada del item se verificó.

La popularidad nunca decide un nivel; solo ordena herramientas del mismo nivel.

## El plan

`plan.json` lleva el entorno, los items (cada uno con su candidato y sus operaciones) y `researched`, la lista de todo lo que se investigó, entrara o no al plan, con nombre, fuente y descripción. Las cinco operaciones:

| Operación | Qué hace |
| --- | --- |
| `write_file` | Escribe solo `CLAUDE.md` o archivos bajo `.claude/skills/`. Rechaza `settings.json`, hooks y `.mcp.json`, y lo que escribe bajo `.claude/skills/` pasa por los vetos del escáner |
| `copy_skill` | Copia una skill de un commit fijo de un repo de GitHub |
| `add_mcp` | Registra un servidor MCP con `claude mcp add`; es la única vía para tocar la configuración de MCP. Un servidor stdio exige versión exacta del paquete o imagen de docker fijada por `sha256` |
| `set_env` | Pide un secreto en la terminal y lo guarda en `~/.config/setup-architect/secrets.env` |
| `manual_step` | Imprime una guía para lo que no se puede automatizar |

Los hooks y `settings.json` quedan fuera a propósito: ejecutan comandos de shell, y si el aplicador pudiera escribirlos, la lista cerrada no serviría de nada.

## Aplicación

`sarch apply` sigue este orden, y aborta sin ejecutar nada ante cualquier error inesperado antes de ejecutar:

1. Lee el plan una vez y calcula su hash.
2. Vuelve a verificar cada item. No confía en las fichas que haya generado la skill.
3. Valida el `CLAUDE.md` y pre-valida todas las operaciones.
4. Muestra el resumen: cada item con su nivel y razones, el `CLAUDE.md` completo o su diff, las piezas que quedan pendientes de instalar a mano y las fuentes investigadas. Pide la aprobación.
5. Vuelve a descargar cada skill y exige que su contenido sea idéntico al que se escaneó al verificar.
6. Ejecuta primero lo `Verificado` y `Confiable`, en bloque, y después pregunta por cada `Revisar`. Lo `Bloqueado` y lo no verificado nunca se ejecutan.
7. Registra cada paso en un manifiesto antes y después de ejecutarlo. Si un paso falla, lo marca `failed`, se detiene y ofrece revertir la corrida.
8. Al final imprime la guía de los pasos manuales, el comando para revertir y lo que no se instaló, con su motivo.

**Validación del `CLAUDE.md`.** Se rechaza si supera el tope de líneas, si contiene una URL, un patrón de comando peligroso (`curl`, `wget`, `| sh`, `sudo`, `rm -rf`, `eval`, `chmod +x`) o caracteres ocultos, si copia ocho o más palabras seguidas de la descripción de una fuente (de los items o de `researched`), o si importa un archivo con `@ruta` fuera de un bloque de código. Si el texto es ambiguo para decidir qué es código (contiene `<`, empieza con `---` o tiene un cierre de valla seguido de un tabulador), no se admite ninguna `@ruta`.

**Secretos.** Se piden en la terminal sin eco y nunca pasan por la conversación con Claude, el `CLAUDE.md` ni el repo. En el manifiesto solo queda el nombre de la variable. Un MCP stdio con variables y alcance proyecto se rechaza, porque el secreto quedaría en el `.mcp.json` del repo.

**Alcance.** Con `--project`, las operaciones de archivo solo pueden caer dentro del proyecto; lo global va en un plan y una corrida aparte, sin `--project`.

## Manifiesto y reversión

El manifiesto vive en `.claude/setup-architect/manifest.json` (proyecto) o en `~/.claude/setup-architect/manifest.json` (global), con permisos 700 y 600, y los respaldos en `backups/<run_id>/` junto a él. Cada paso guarda la corrida, el candidato, la fuente, la operación, el nivel, la referencia fijada, el estado y cómo deshacerlo.

`sarch revert <run_id>` deshace los pasos de esa corrida en orden inverso. No confía en el manifiesto: antes de borrar o restaurar un archivo comprueba que el destino esté dentro del árbol al que pertenece el manifiesto y sea un `CLAUDE.md` o algo bajo `.claude/skills/`, y que el respaldo esté dentro de su directorio de respaldos. Un manifiesto corrupto o con forma inesperada no se interpreta ni se sobrescribe.

`sarch audit` vuelve a consultar lo instalado y señala lo que tiene una versión nueva, quedó inactivo o ya no existe.

## Fuera de alcance

- Configurar automáticamente el chat de claude.ai: para ese entorno el plan solo lleva pasos manuales.
- Verificar fuentes que no son repos de GitHub.
- Revertir una parte de una corrida.

Los límites conocidos de esta versión están en la sección "Límites conocidos" del `README.md`.
