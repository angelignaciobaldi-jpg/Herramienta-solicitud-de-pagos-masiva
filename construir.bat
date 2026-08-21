@echo off
rem ============================================================
rem  Construye el ejecutable (dist\SolicitudesPago\SolicitudesPago.exe),
rem  listo para meterlo en el instalador (instalador.iss con Inno Setup).
rem
rem  NO empaqueta Chromium: el navegador se descarga en la primera
rem  ejecucion del RPA (a %LOCALAPPDATA%\...). Asi el instalador es
rem  liviano. El driver de Playwright (node) si va incluido via
rem  --collect-all, para poder lanzar/descargar el navegador.
rem
rem  Ejecutar dentro del entorno virtual activado, desde la carpeta
rem  del proyecto.
rem
rem  SALIDA FUERA DEL PROYECTO (MAX_PATH): la ruta de este proyecto vive
rem  en OneDrive y mide ~182 caracteres; el arbol que PyInstaller copia a
rem  dist\SolicitudesPago\_internal\... llega a ~313, por encima del limite
rem  de 260 de Windows, y el empaquetado falla. Por eso dist se escribe en
rem  una ruta CORTA fuera del proyecto (variable SALIDA_BUILD).
rem
rem  El .\build de trabajo SI se queda en el proyecto: solo guarda los .toc,
rem  el PYZ y localpycs (rutas planas), no el arbol de dependencias, asi que
rem  no se acerca al limite. No se mueve porque --workpath tendria que ir
rem  dentro de --pyinstaller-build-args, y ahi solo cabe UN argumento: al
rem  meter dos, flet los pasa pegados como uno solo y PyInstaller entiende
rem  "--collect-all=playwright --workpath=..." como el nombre de un modulo,
rem  con lo que el driver de Playwright deja de empaquetarse EN SILENCIO.
rem
rem  El CI (.github/workflows/compilar.yml) NO usa este .bat: compila en
rem  una ruta corta y se queda con el .\dist por defecto.
rem ============================================================
setlocal
cd /d "%~dp0"

if not defined SALIDA_BUILD set "SALIDA_BUILD=C:\build\SolicitudesPago"
set "DISTPATH=%SALIDA_BUILD%\dist"

where flet >nul 2>&1
if errorlevel 1 (
  echo *** No se encontro 'flet'. Activa el entorno virtual primero:
  echo ***   C:\venvs\solicitudes-pago\Scripts\activate
  pause & exit /b 1
)

echo Empaquetando con flet pack ...
echo   salida: %DISTPATH%\SolicitudesPago
set DATAARGS=
if exist "Imagenes\" set DATAARGS=%DATAARGS% --add-data "Imagenes:Imagenes"
rem  datos\sipp.json trae el mapa de selectores y las URLs del portal. NO esta
rem  versionado (repo publico): si falta, el .exe compila pero no sabe hablar
rem  con SIPP. Ver core\sipp_datos.py.
if exist "datos\sipp.json" (
  set DATAARGS=%DATAARGS% --add-data "datos:datos"
) else (
  echo *** AVISO: falta datos\sipp.json; el ejecutable no podra operar SIPP. ***
)

flet pack app.py -n "SolicitudesPago" -D ^
  --icon "Imagenes\icon.ico" ^
  --distpath "%DISTPATH%" ^
  %DATAARGS% ^
  --hidden-import openpyxl ^
  --pyinstaller-build-args="--collect-all=playwright" ^
  -y
if errorlevel 1 (
  echo *** Fallo el empaquetado. ***
  pause & exit /b 1
)

echo.
echo ============================================================
echo   Listo: %DISTPATH%\SolicitudesPago\SolicitudesPago.exe
echo.
echo   Siguiente: compila el instalador apuntando a esa carpeta
echo   (el /D es necesario porque dist quedo fuera del proyecto):
echo.
echo     iscc /DDistDir="%DISTPATH%\SolicitudesPago" instalador.iss
echo ============================================================
pause
