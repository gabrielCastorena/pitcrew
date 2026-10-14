# setup-architect
- Python >= 3.12, solo stdlib. Única dependencia de desarrollo: pytest.
- Ningún test toca la red ni tu ~/.claude real: usa fixtures y HOME temporal.
- Ciclo obligatorio: test que falla → código → test que pasa → commit.
