# Evals de setup-architect

Las piezas deterministas del proyecto (verificador, renderizador y aplicador) se cubren con tests automáticos. La calidad de las recomendaciones no: depende del razonamiento de Claude durante la entrevista, la investigación y el armado del plan, y eso no se puede comprobar con un test. Por eso se evalúa a mano, corriendo `/setup-architect:setup-architect` sobre proyectos de ejemplo y revisando el resultado con una checklist.

Los casos y sus resultados se guardan para comparar cuando cambie la skill o salgan herramientas nuevas.

## Casos

Cada caso es un archivo en `evals/cases/` que describe un proyecto de ejemplo: qué es, su stack, cómo trabaja quien lo lleva y qué tiene ya instalado. Es lo que se le contesta a la skill durante la entrevista. Cada caso termina con "Qué se espera": lo que debe cumplir el resultado, además de la checklist.

Hay tres casos:

- `evals/cases/app-oficios.md`: una app con servicios externos y pagos. Comprueba que todo lo que el plan escribe o conecta se confirma aparte.
- `evals/cases/cli-sin-servicios.md`: una CLI en Python sin base de datos ni servicios externos. Comprueba que la skill no infla el setup.
- `evals/cases/repo-sin-mantenimiento.md`: un proyecto cuya herramienta más a mano viene de un repo pequeño y sin mantenimiento. Comprueba que el verificador no la deja en `Confiable`.

Para correr un caso: crea un directorio vacío y desechable, entra en él desde tu terminal normal, lanza `claude --plugin-dir <ruta al checkout>/plugin`, corre `/setup-architect:setup-architect` y contesta la entrevista con lo que dice el caso. Lo que el caso no dice, contéstalo como "no sé" o "no aplica".

## Checklist

Después de correr `/setup-architect:setup-architect` con un caso, se revisa el setup propuesto con estas cuatro preguntas:

- [ ] ¿Las preguntas fueron pertinentes y no de relleno?
- [ ] ¿Recomendó al menos una herramienta útil que no conocía?
- [ ] ¿Sobró algo en el setup?
- [ ] ¿Las justificaciones son concretas?

## Resultados

El resultado de cada corrida va en `evals/results/YYYY-MM-DD-<caso>.md`, con la fecha de la corrida y el nombre del archivo del caso (por ejemplo, `evals/results/2026-10-15-app-oficios.md`). Cada resultado lleva la checklist llena, con una nota breve por pregunta, y el `plan.json` que propuso la skill o un resumen de él.

Un resultado no se sobrescribe: una corrida nueva del mismo caso es un archivo nuevo, con su fecha. Así se puede ver si un cambio en la skill mejoró o empeoró las recomendaciones.
