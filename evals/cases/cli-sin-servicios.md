# Caso: CLI sin servicios externos

Un proyecto pequeño y local. Comprueba que la skill propone pocas piezas y no añade servidores MCP que el proyecto no necesita.

## Propósito

Una CLI en Python, de uso personal, que ordena las fotos de una carpeta: las renombra con la fecha en que se tomaron (leída de los datos EXIF) y las mueve a subcarpetas por año y mes. Funciona, y el usuario quiere mantenerla y añadirle opciones.

## Stack

- Python 3.12, con `argparse` y la librería Pillow para leer los EXIF.
- Tests con pytest.
- Sin base de datos, sin red y sin servicios externos. No necesita credenciales.
- El código vive en un repo de git local, sin remoto.

## Cómo trabaja

- Trabaja solo, en ratos libres.
- Lo que repite: correr los tests y probar la CLI sobre una carpeta de fotos de ejemplo antes de cada cambio.
- Le preocupa que un cambio mueva o renombre mal fotos reales.
- No tiene skills ni servidores MCP configurados. Entorno: proyecto.

## Qué se espera

- **Pocas piezas:** como mucho cinco, contando el `CLAUDE.md`. Cada una resuelve algo que el usuario dijo en la entrevista.
- **Ningún `add_mcp`.** El proyecto no tiene servicios a los que conectarse. Un MCP en el plan es un fallo de este caso, aunque su ficha sea buena.
- Ningún `set_env`: no hay credenciales.
- La entrevista no pregunta por el país: el proyecto no depende de servicios que cambian por país.
- Lo que el plan proponga para proteger las fotos reales (por ejemplo, probar contra una copia) va en el `CLAUDE.md` con palabras de la skill, no como una herramienta de más.
