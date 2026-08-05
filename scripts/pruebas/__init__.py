"""Pruebas de la herramienta, sin dependencias externas.

Se corren todas con `python scripts/probar.py`. Cada módulo `test_*.py` expone
funciones que empiezan por `probar_`; el corredor las descubre, las ejecuta
aisladas y reporta.

Por qué no pytest: el proyecto se empaqueta con `flet pack` y su
`requirements.txt` es lo que se instala en el equipo del usuario. Añadir un
framework de pruebas ahí para algo que solo se usa en desarrollo no se paga, y
un corredor de cincuenta líneas hace lo mismo para este tamaño.

Estas pruebas **no tocan SIPP**. Las que sí lo hacen viven en `scripts/` como
programas aparte (`prueba_llenado_stage.py`, `prueba_guardado_stage.py`,
`prueba_autorizacion_stage.py`), porque necesitan credenciales, tardan minutos y
algunas escriben en el portal.
"""
