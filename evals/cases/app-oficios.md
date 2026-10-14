# Caso: app de oficios

Una app con servicios externos, datos de ubicación y pagos. Comprueba que la skill cubre lo que preocupa al usuario con pocas piezas y que todo lo que el plan escribe o conecta se confirma aparte.

## Propósito

App tipo Uber para oficios: plomería, electricidad, cerrajería y carpintería. El cliente describe el problema con fotos y su ubicación, y se le asigna un técnico cercano y disponible, que acepta o rechaza el trabajo. Tiene tres roles: cliente, técnico y administrador. Incluye seguimiento en vivo del técnico, cotización y pago dentro de la app, calificaciones y chat.

## Stack

- Backend: FastAPI.
- Base de datos: PostgreSQL con PostGIS.
- App móvil: React Native con Expo.
- Tests con pytest.
- Docker.
- Pagos: Mercado Pago.

## País

El usuario opera en Argentina. Si la skill no lo pregunta y el plan incluye Mercado Pago, anótalo en la checklist.

## Cómo trabaja

- Trabaja solo, con Claude Code desde la terminal.
- Quiere ayuda con los endpoints, el esquema, las migraciones sin romper datos y la seguridad.
- Le preocupan los pagos, la ubicación en tiempo real, la autenticación de los tres roles y que no se filtren secretos.
- Prefiere pocas herramientas confiables.
- No tiene skills ni servidores MCP configurados. Entorno: proyecto.

## Qué se espera

- **Niveles:** 5 piezas `Confiable` y 4 `Revisar`. Por ejemplo, `Confiable` fastapi, gitleaks, pre-commit, ruff y squawk, y `Revisar` el `CLAUDE.md`, una skill escrita por el plan (revisar-migracion), context7 y el MCP de Mercado Pago. Si cambian las piezas pero la cuenta se mantiene, anótalo; si cambia la cuenta, anota por qué según las fichas. Todo item con `add_mcp` o `write_file` sale como máximo en `Revisar`.
- **`CLAUDE.md`:** el aplicador pide confirmarlo aparte, con el aviso "Nada de este item se verificó". El `CLAUDE.md` separa lo que instala el aplicador de lo pendiente a mano, y describe lo pendiente en condicional, nunca como una protección activa.
- **Pasos manuales:** en el resumen de aprobación, cada `manual_step` aparece como "queda pendiente: el aplicador solo imprime la guía", y junto al `CLAUDE.md` aparece el aviso con el recuento y los títulos de las piezas pendientes.
- **País:** las piezas de Mercado Pago usan la documentación de un mismo país, el del usuario.
- **Revertir:** `sarch revert <run_id> --project .` deja el árbol sin nada de lo instalado: ni el `CLAUDE.md`, ni las skills, ni los servidores en `.mcp.json`. Pueden quedar `.claude/skills/` vacío, el manifiesto y un `.mcp.json` con `{"mcpServers": {}}` (un límite conocido de `sarch revert`); eso no cuenta como fallo, cualquier otra cosa sí.
- Nada se escribe fuera del proyecto: ni `settings.json`, ni hooks, ni la configuración de usuario.
