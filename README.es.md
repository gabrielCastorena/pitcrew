[English](README.md) | Español

# Pitcrew

Un plugin de Claude Code que te entrevista sobre un proyecto, investiga qué herramientas existen hoy, mide la confianza de cada una y te deja un plan de setup (un `CLAUDE.md`, skills, servidores MCP y pasos manuales) que apruebas y aplicas tú en tu terminal.

Tiene dos mitades:

- **El plugin** (`plugin/`) lleva el razonamiento: una skill que entrevista, subagentes que investigan en internet y solo pueden devolver datos, y la redacción del plan.
- **El CLI `pitcrew`** (`src/pitcrew/`) lleva lo determinista: verifica cada herramienta sin usar ningún modelo, muestra el plan y lo instala.

La regla central del diseño es que lo que lee internet nunca ejecuta comandos, y que la aprobación la das tú en tu terminal. Cómo funciona por dentro, y qué protege y qué no, está en `docs/DESIGN.md`.

## Requisitos

- **Python 3.12 o posterior.** El CLI solo usa la biblioteca estándar.
- **[pipx](https://pipx.pypa.io/)**, para instalar el CLI sin activar un entorno virtual.
- **Claude Code**, con el programa `claude` en el PATH: el plugin corre dentro de él, y el aplicador lo usa para registrar servidores MCP.
- **Node y npm**, si el plan incluye servidores MCP que se lanzan con `npx`. Sin ellos el servidor queda registrado pero no conecta.

## Instalación

### 1. El CLI `pitcrew`

Clona el repositorio en un sitio donde vaya a quedarse e instálalo con pipx en modo editable:

```bash
git clone https://github.com/gabrielCastorena/pitcrew.git ~/pitcrew
pipx install --editable ~/pitcrew
```

`pitcrew` queda en el PATH (en `~/.local/bin`, o donde tengas configurado pipx) sin activar ningún entorno. Compruébalo con `command -v pitcrew`.

Tiene que ser editable: la configuración del verificador (`config/`) vive en el checkout y todavía no se empaqueta. Con un `pipx install` sin `--editable`, `pitcrew` arranca pero cualquier subcomando que verifica termina con "configuración inválida: no existe el directorio de configuración". Por la misma razón, no muevas ni borres el checkout después de instalar; si lo mueves, vuelve a instalar con `pipx install --force --editable <nueva ruta>`.

### 2. El plugin

El repositorio es también un marketplace de plugins de Claude Code (`.claude-plugin/marketplace.json`). Desde tu terminal:

```bash
claude plugin marketplace add gabrielCastorena/pitcrew
claude plugin install pitcrew@pitcrew
```

El primer comando registra el marketplace en tu configuración de usuario de Claude Code; el segundo instala el plugin. Dentro de una sesión, `/plugin marketplace add gabrielCastorena/pitcrew` hace lo mismo que el primero. Con un checkout local también vale `claude plugin marketplace add ~/pitcrew`.

Para probar cambios del plugin sin instalarlo, lánzalo desde tu terminal normal (no desde dentro de Claude Code) con `--plugin-dir`:

```bash
cd ~/mi-proyecto
claude --plugin-dir ~/pitcrew/plugin
```

### 3. Un token de GitHub (recomendado)

El verificador consulta la API de GitHub. Sin autenticación, GitHub permite pocas consultas por hora, y con un plan mediano es fácil agotarlas: las herramientas que no se pudieron consultar salen como "no se pudo verificar" y no se instalan. Si exportas `GITHUB_TOKEN`, `pitcrew` la manda en cada consulta a la API:

```bash
export GITHUB_TOKEN=<tu token>
```

Basta un token sin permisos (fine-grained, solo lectura de repos públicos). La variable tiene que existir en la terminal desde la que lanzas Claude Code, porque la skill corre `pitcrew verify` en la shell de Claude Code, y en la terminal donde corres `pitcrew apply`, que vuelve a verificar. Sin ella, `pitcrew verify` avisa por stderr. El valor nunca pasa por la conversación con Claude.

## Uso

En Claude Code, dentro de tu proyecto, corre `/pitcrew:pitcrew`. Un ejemplo de lo que pasa:

1. **Entrevista.** La skill lee lo que el repo ya responde (README, dependencias, `CLAUDE.md`) y te pregunta, una cosa a la vez, lo que falta: para qué es el proyecto, qué servicios usa, cómo trabajas, qué repites. Si el proyecto depende de servicios que cambian por país (pagos, facturación), pregunta en qué país operas. Después elige el entorno (proyecto, global o chat) y te dice por qué.
2. **Investigación y verificación.** Lanza un investigador por categoría (skills, MCP, herramientas CLI y configuración), elige lo que resuelve algo que dijiste y lo verifica con `pitcrew verify items.json -o cards.json`. Lo bloqueado no entra al plan; te dice qué descartó y por qué.
3. **El plan.** Arma `plan.json` y te lo muestra en texto o en HTML con `pitcrew render plan.json --cards cards.json --format text` o con `--format html --open`. Las dos vistas salen del mismo archivo.
4. **Aplicar.** La skill no instala nada: te da el comando y lo corres tú, en tu terminal, desde el directorio del proyecto:

   ```bash
   pitcrew apply plan.json --project .
   ```

   Para un plan global, sin `--project`. El aplicador vuelve a verificar cada herramienta, muestra su propio resumen (con el `CLAUDE.md` completo o su diff, las fuentes investigadas que declara el plan y el hash del plan) y pide tu aprobación. Después pregunta aparte por cada pieza en Revisar, pide el valor de cada secreto en la terminal y registra cada paso en un manifiesto. Al final imprime la guía de los pasos manuales, lo que no se instaló y el comando para revertir.
5. **Revertir y auditar.** `pitcrew revert <run_id>` deshace esa corrida (con `--project .` si fue de proyecto). Más adelante, `pitcrew audit` vuelve a consultar lo instalado y señala lo que quedó obsoleto.

El plan solo puede contener cinco operaciones: `write_file` (solo a `CLAUDE.md` y bajo `.claude/skills/`), `copy_skill`, `add_mcp`, `set_env` y `manual_step`. No hay forma de expresar un comando de shell libre.

## Niveles de confianza

El verificador responde una sola pregunta: si la herramienta es confiable. Si sirve para tu proyecto lo decide Claude. La popularidad nunca decide un nivel; solo ordena herramientas del mismo nivel.

| Nivel | Qué significa | Qué pasa al aplicar |
| --- | --- | --- |
| Verificado | Publicador oficial y con actividad reciente | Se instala en bloque tras una sola aprobación |
| Confiable | De terceros, sano y de bajo riesgo | Se instala en bloque tras una sola aprobación |
| Revisar | Algo no cuadra o no se pudo comprobar, sin llegar a veto | El aplicador pregunta por cada uno y muestra sus razones |
| Bloqueado | Tiene una bandera roja; el veto siempre gana | Nunca se instala |

Lo que no se pudo verificar (red caída, límite de consultas, repo inexistente) se omite sin preguntar y aparece al final en la sección "No se instaló", con su motivo. Hay que reintentar más tarde. Una fuente que no es un repo de GitHub sí se puede confirmar, y la pregunta avisa de que nada de ese item se verificó.

## Desarrollo

Para trabajar en el código, usa un entorno virtual con una instalación editable. `pytest` es la única dependencia de desarrollo:

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q -W error
```

Ningún test toca la red ni tu `~/.claude`: usan dobles de prueba y un HOME temporal.

## Límites conocidos

Lo que esta versión no resuelve, o no ha comprobado todavía. El modelo de amenaza completo está en `docs/DESIGN.md`.

- **El `CLAUDE.md` puede mencionar como instaladas piezas que no se instalaron.** La skill lo redacta con el plan completo, antes de que apruebes nada. Si rechazas una pieza al confirmarla, o se omite porque no se pudo verificar, el `CLAUDE.md` se escribe igual. El aplicador lo avisa al final, nombrando esas piezas, y te toca quitarlas a mano del `CLAUDE.md`. Que el `CLAUDE.md` refleje solo lo instalado queda para una versión futura.
- **Un `manual_step` queda registrado como `done`.** El aplicador solo imprime su guía y no instala nada, pero el manifiesto lo anota como hecho, y `pitcrew audit` lo trata como instalado. El resumen de aprobación sí lo marca como pendiente.
- **Las formas ambiguas de `@ruta` no se comprobaron contra Claude Code.** El aplicador rechaza un `CLAUDE.md` con una `@ruta` fuera de código. Ante las formas ambiguas (un `<` en el texto, una primera línea `---` o un cierre de valla seguido de un tabulador) no se sabe si Claude Code importaría el archivo, y el validador las rechaza por precaución: puede rechazar un `CLAUDE.md` inofensivo, nunca aceptar uno peligroso por esa vía.
- **Las evals no se han corrido.** La calidad de las recomendaciones se evalúa a mano con los casos de `evals/cases/` y la checklist de `evals/README.md`; ninguno se ha corrido todavía.
- **La aprobación es una garantía procedimental, no técnica.** El aplicador lee la respuesta de la terminal y no de stdin, pero eso no impide que un programa que lo lance dentro de su propia pseudoterminal conteste por el usuario. La aprobación vale porque el comando lo corre el usuario en su terminal; por eso la skill no lo ejecuta.
- **Todo MCP y todo lo que el plan escribe se confirman aparte.** Un item con un `add_mcp`, un item con un `write_file` (el `CLAUDE.md` o cualquier archivo bajo `.claude/skills/`, declare la fuente que declare) o una skill que declara `allowed-tools` sale como máximo en Revisar: nunca se instala en bloque. El verificador comprueba el repo de GitHub de la fuente; no comprueba el paquete que ejecuta el `npx`, la imagen de docker, la URL del servidor ni el contenido que escribe el plan.
- **Sin repo de GitHub como fuente, nada se verificó.** Todo item cuya fuente está vacía o no es un repo de GitHub sale en Revisar y, además de la aprobación del plan, pide su propia confirmación con este aviso: "Nada de este item se verificó: la fuente no es un repo de GitHub soportado." El `CLAUDE.md` que arma la skill va en un item con la fuente vacía, así que lleva además ese aviso.
- **El texto copiado se compara con lo que el plan declara.** El aplicador rechaza un `CLAUDE.md` que copie ocho o más palabras seguidas de la descripción de una fuente. Compara con las descripciones de los items del plan y con las de `researched`, el campo donde el plan declara todo lo que se investigó: lo que entró, lo bloqueado, lo descartado y los patrones de configuración. Esa lista la arma la skill, y el aplicador no puede saber lo que el plan no le dice: una frase en prosa de una fuente que el plan no declara, sin URL ni comando, pasaría la validación. Por eso el resumen que apruebas muestra las fuentes declaradas, con su nombre, su fuente y su descripción, o dice de forma explícita que el plan no declara ninguna; y el `CLAUDE.md` se muestra completo y se confirma aparte.
- **Un fragmento corto pasa.** Un tramo de menos de ocho palabras seguidas tomado de una fuente, sin URL ni comando, tampoco lo detiene el validador, aunque el plan declare la fuente. Vale lo mismo: lee el `CLAUDE.md` del resumen.
- **Los secretos no se cargan solos.** `set_env` solo añade una línea `export` con el valor a `~/.config/pitcrew/secrets.env`. Nada carga ese archivo: el aplicador no lo hace ni toca la configuración de tu shell. Para que la variable exista tienes que cargarlo por tu cuenta desde tu shell.
- **MCP http sin autenticación automática.** Un MCP http se registra sin cabeceras; las variables que declare son informativas y no se piden ni se pasan.
- **Docker en forma cerrada.** Solo `docker run` con `-i`, `--rm`, `--init` y `-e NOMBRE` antes de una imagen fijada por `@sha256:`. No se admiten volúmenes, redes ni otras opciones. Los demás lanzadores exigen una versión exacta del paquete.
- **Setup mixto en dos corridas.** Con `--project` solo se escriben archivos dentro del proyecto; lo global va en un plan y una corrida aparte, sin `--project`. Si el plan registra un MCP de alcance proyecto, el aplicador y la reversión deben correrse desde el directorio del proyecto. Un MCP stdio con variables y alcance proyecto se rechaza: el secreto quedaría en el `.mcp.json` del repo.
- **`pitcrew revert` no pide confirmación y no deja el árbol exacto.** Deshace la corrida en cuanto lo lanzas, y puede dejar `.claude/skills/` vacío y un `.mcp.json` sin servidores.
- **Solo se verifican repos de GitHub**, y solo se revierte por corrida completa.

### Límites menores

- **Formas que los validadores no detectan.** El escáner de skills no veta una valla de inyección de shell con un espacio duro o U+FEFF entre los acentos graves y el `!`, ni con `~~~!`, ni en archivos `.markdown` o `.mdx`. El validador del `CLAUDE.md` no detecta una `@ruta` tras un separador de línea Unicode (U+2028, U+2029) dentro de un tramo entre acentos graves, y un frontmatter abierto con `+++` no activa el rechazo por precaución. No se ha comprobado si Claude Code interpreta esas formas.
- **Las skills escritas por el plan no pasan por el validador del `CLAUDE.md`.** Un `write_file` bajo `.claude/skills/` pasa los vetos del escáner, pero no la comprobación de texto copiado, URLs ni `@ruta`. Sale como máximo en Revisar y se muestra completo antes de confirmarlo.
- **La comprobación de texto copiado solo usa las descripciones.** No compara contra el `why_fit` de cada candidato ni contra los `steps` de un `manual_step`, y no normaliza Unicode.
- **El escáner prefiere los falsos positivos.** Puede bloquear una skill inofensiva, por ejemplo con `curl … | tee sh.txt`.
- **Sin topes de tamaño en las descargas.** No hay límite de tamaño de descarga, de tamaño descomprimido ni de número de archivos al extraer una skill. El `CLAUDE.md` tiene un tope de líneas, no de bytes.
- **El secreto de un MCP stdio se ve en la lista de procesos** mientras corre `claude mcp add -e`.
- **Claude Code y la aprobación de servidores MCP.** Sin comprobar: Claude Code podría recordar la aprobación de un servidor de `.mcp.json` por su nombre, de modo que otro servidor con el mismo nombre y otro comando no volvería a pedirla.
- **`pitcrew revert` confía en el nombre para quitar MCP y secretos.** Los archivos se anclan al árbol del manifiesto, pero quitar un servidor MCP o una línea del archivo de secretos se hace por nombre. Un manifiesto ajeno o plantado podría quitar un servidor o una variable tuya si lo reviertes con su identificador de corrida. Revierte solo corridas tuyas.
- **`pitcrew audit` puede decir "al día" de más.** Para una skill copiada, el manifiesto guarda la referencia verificada y no el commit instalado, así que una skill instalada en un commit anterior puede salir al día.
- **Lo que cambia entre la aprobación y la ejecución.** El diff del resumen se calcula contra el archivo en ese momento; si el archivo cambia antes de ejecutar, se respalda y se reemplaza igual. Las comprobaciones de enlaces simbólicos del directorio del manifiesto se hacen al empezar, no en cada escritura, y no hay bloqueo entre dos corridas simultáneas.
- **Un `$HOME` que sea un enlace simbólico** hace abortar toda corrida global.
- **Versiones de paquetes.** Los lanzadores `uvx` y `pipx` exigen una versión exacta con forma semver: se rechazan versiones válidas de PyPI como `1.2.3rc1` o `2025.04.08`. La sintaxis exacta de `pipx run` y `uvx --from` no se ha comprobado contra esas herramientas.
- **Una skill con `subdir` vacío o `.`** instala la raíz del repo, que es también lo que se verificó.
- **La skill se descarga antes de que apruebes**, porque el verificador la escanea antes de mostrarte el plan.
- **Las vistas de `pitcrew render` no muestran `researched`.** Solo lo muestra el resumen del aplicador.

## Seguridad y licencia

Para reportar una vulnerabilidad en privado, mira `SECURITY.md`. El código se publica con licencia MIT (`LICENSE`).
