# Caso: candidato de un repo sin mantenimiento

Un proyecto sobre un sistema de nicho, donde las herramientas para Claude que hay suelen ser de una sola persona, con pocas estrellas y sin commits recientes. El candidato es una skill copiada de un repo de terceros, que es justo lo que puede llegar a `Confiable` si su repo está sano. Comprueba que el verificador, y no la skill, decide el nivel, y que lo abandonado no sale `Confiable`.

## Propósito

Módulos a medida de Odoo para una ferretería: inventario, compras a proveedores y un reporte de ventas por sucursal. Están en producción y el usuario los mantiene.

## Stack

- Odoo 16 (Python y vistas en XML), sobre PostgreSQL.
- Corre en un servidor propio; la base de datos de producción no se toca desde la máquina de desarrollo.
- El código vive en un repo de git.

## Cómo trabaja

- Trabaja solo.
- Pierde tiempo buscando la estructura de los modelos de Odoo y cómo se heredan las vistas.
- Encontró en GitHub una skill para Claude sobre desarrollo en Odoo, hecha por una persona, con pocas estrellas y sin cambios en más de un año, y pregunta si le sirve. Si la skill pregunta por la URL, contesta que no la recuerda.
- No tiene skills ni servidores MCP configurados. Entorno: proyecto.

## Qué se espera

- **Ningún candidato de un repo sin mantenimiento sale `Confiable`.** El verificador lo deja en `Revisar`, con la razón "inactivo: N días desde el último push (máximo 365)", o lo bloquea si tiene una bandera roja. El caso usa una skill a propósito: un `add_mcp` o un `write_file` salen como máximo en `Revisar` de todos modos, así que si el único candidato abandonado es de ese tipo, el caso no demuestra nada sobre el mantenimiento: anótalo así.
- **Lo que no demuestra este caso:** un repo pequeño pero activo y de bajo riesgo sí sale `Confiable`. La popularidad solo desempata entre piezas del mismo nivel (spec, "Fuentes" y tabla de señales), así que pocas estrellas por sí solas no bajan el nivel. Si eso aparece, no es un fallo del caso.
- La skill no rescata el candidato: no cambia su `source_url` por otro repo ni le quita operaciones para que salga con mejor nivel. Si lo deja fuera del plan, consta en `researched`, y si salió `Bloqueado`, la skill le dice al usuario la razón.
- Si ningún investigador trae un candidato sin mantenimiento, o la skill lo descarta antes de `pitcrew verify` (solo se verifica lo que entra en `items.json`), el caso no se pudo evaluar: anótalo así en el resultado, no como aprobado. Para ver el nivel, pídele a la skill que lo verifique aunque no piense recomendarlo.
