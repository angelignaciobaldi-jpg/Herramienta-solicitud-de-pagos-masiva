"""Smoke test de imports: importa app + todos los módulos de core/ y ui/.

Sirve para detectar imports rotos o módulos faltantes (p. ej. un .py referenciado
pero NO versionado) ANTES de compilar y publicar. Lo corre el CI: si algo no
importa, el build FALLA aquí y el Release roto no llega a los usuarios.

Ejecuta:  python scripts/smoke_import.py
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys

# Raíz del proyecto en el path (para poder importar core/ y ui/).
_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _RAIZ)


def _modulos_de(paquete: str) -> list[str]:
    ruta = os.path.join(_RAIZ, paquete)
    return [f"{paquete}.{m.name}" for m in pkgutil.iter_modules([ruta])]


def _nombres_no_definidos(ruta: str) -> list[str]:
    """Nombres que un módulo USA y no define ni importa en ninguna parte.

    Existe por un fallo real: `ui/captura_solicitud.py` usaba `VERDE` sin
    importarlo, y no reventaba al importar ni al abrir la pantalla, porque
    estaba dentro de un `A if cond else B` que solo evaluaba esa rama cuando la
    solicitud traía archivos adjuntos. El error salía al editar una solicitud
    con carátula, y ninguna prueba pasaba por ahí.

    Es una aproximación deliberadamente generosa: se recogen TODOS los nombres
    que el módulo liga en cualquier ámbito y se marca solo lo que no aparece
    ligado en ninguno. Así no hay falsos positivos por variables locales, a
    cambio de que un nombre ligado en otra función no se detecte. Para lo que
    interesa —constantes que se olvidó importar— basta.
    """
    import ast
    import builtins

    with open(ruta, encoding="utf-8") as fh:
        arbol = ast.parse(fh.read(), filename=ruta)

    ligados: set[str] = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, (ast.Store, ast.Del)):
            ligados.add(nodo.id)
        elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ligados.add(nodo.name)
        elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
            for alias in nodo.names:
                ligados.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(nodo, ast.arg):
            ligados.add(nodo.arg)
        elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
            ligados.add(nodo.name)
        elif isinstance(nodo, (ast.Global, ast.Nonlocal)):
            ligados.update(nodo.names)

    usados: dict[str, int] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Load):
            if nodo.id not in ligados:
                usados.setdefault(nodo.id, nodo.lineno)
    return [f"{n} (línea {l})" for n, l in sorted(usados.items())]


def main() -> int:
    objetivos = ["app"] + _modulos_de("core") + _modulos_de("ui")
    fallidos: list[tuple[str, str]] = []
    for nombre in objetivos:
        try:
            importlib.import_module(nombre)
            print(f"OK  {nombre}")
        except Exception as exc:  # noqa: BLE001 — se reporta el módulo que falla
            fallidos.append((nombre, f"{type(exc).__name__}: {exc}"))
            print(f"XX  {nombre}  ->  {type(exc).__name__}: {exc}")

    print()
    for paquete in ("core", "ui", "core/adaptadores"):
        carpeta = os.path.join(_RAIZ, paquete)
        if not os.path.isdir(carpeta):
            continue
        for archivo in sorted(os.listdir(carpeta)):
            if not archivo.endswith(".py"):
                continue
            ruta = os.path.join(carpeta, archivo)
            try:
                sueltos = _nombres_no_definidos(ruta)
            except SyntaxError as exc:
                fallidos.append((ruta, f"SyntaxError: {exc}"))
                continue
            if sueltos:
                rel = os.path.relpath(ruta, _RAIZ)
                fallidos.append((rel, "usa sin definir: " + ", ".join(sueltos)))
                print(f"XX  {rel}  ->  usa sin definir: {', '.join(sueltos)}")
    for archivo in ("app.py",):
        sueltos = _nombres_no_definidos(os.path.join(_RAIZ, archivo))
        if sueltos:
            fallidos.append((archivo, "usa sin definir: " + ", ".join(sueltos)))
            print(f"XX  {archivo}  ->  usa sin definir: {', '.join(sueltos)}")

    if fallidos:
        print("\nProblemas:")
        for nombre, err in fallidos:
            print(f"  - {nombre}: {err}")
        return 1
    print(f"Todos los módulos importan y no usan nombres sin definir "
          f"({len(objetivos)} módulos).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
