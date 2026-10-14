# Política de seguridad

setup-architect instala piezas en tu configuración de Claude Code, así que un fallo de seguridad aquí puede acabar ejecutando código en la máquina de quien lo usa. Si encuentras uno, repórtalo en privado, no en un issue público.

## Cómo reportar

Usa los avisos de seguridad privados de GitHub (GitHub Security Advisories):

1. Entra en la página **Advisories** de este repositorio.
2. Pulsa **Report a vulnerability**.
3. Describe el problema: qué versión o commit, cómo reproducirlo y qué efecto tiene.

El reporte solo lo ven tú y quien mantiene el repositorio. Si el botón no aparece, abre un issue que diga solo que tienes un reporte de seguridad, sin ningún detalle, y se acordará contigo un canal privado.

## Qué cuenta como vulnerabilidad

Por ejemplo:

- Que algo se instale sin pasar por el verificador o sin la aprobación del usuario en su terminal.
- Que texto de internet (una descripción, un README, una página) acabe como una operación ejecutable o dentro de un `CLAUDE.md` aceptado.
- Que `sarch apply` o `sarch revert` escriban, borren o restauren algo fuera de lo que declara el plan o el manifiesto.
- Que un secreto acabe en el manifiesto, en un registro o en la conversación con Claude.

Los límites ya documentados en la sección "Límites conocidos" del `README.md` no son vulnerabilidades nuevas, salvo que encuentres una forma de ir más allá de lo que describen.

## Versiones con soporte

Solo la última versión publicada recibe correcciones.
