; Carpeta que produce flet pack/PyInstaller (onedir). Por defecto la .\dist del
; proyecto, que es lo que usa el CI (compila en una ruta corta). En un equipo
; donde la ruta del proyecto es larga, construir.bat manda la salida fuera del
; proyecto para no rebasar el MAX_PATH de Windows (260); en ese caso hay que
; compilar apuntando ahi:
;   iscc /DDistDir="C:\build\SolicitudesPago\dist\SolicitudesPago" instalador.iss
#ifndef DistDir
  #define DistDir ".\dist\SolicitudesPago"
#endif

[Setup]
; AppId FIJO: identifica la app entre versiones. Es lo que permite que el
; instalador descargado por el AutoUpdater actualice EN SITIO (sobrescribe) en
; vez de instalar una copia paralela. No lo cambies entre versiones.
AppId={{954A532B-3B54-49ED-8971-E19617967F45}
AppName=Herramientas Solicitudes de Pago
; Mantener en sync con core/version.py (__version__). El CI lo reescribe con el
; tag del Release antes de compilar.
AppVersion=0.1.0
AppPublisher=Quetzaltic Solutions
; Instalacion POR USUARIO (en %LOCALAPPDATA%\Programs), NO en Archivos de
; Programa. Al ser una carpeta escribible por el usuario, la actualizacion
; silenciosa la sobrescribe SIN pedir permisos de administrador (sin UAC).
DefaultDirName={autopf}\Quetzaltic Solutions\Herramientas Solicitudes de Pago
DefaultGroupName=Quetzaltic Solutions
OutputDir=.\Output
; Debe coincidir con el asset que busca el AutoUpdater (NOMBRE_ASSET):
;   Instalador_SolicitudesPago.exe
OutputBaseFilename=Instalador_SolicitudesPago
Compression=lzma2/ultra64
SolidCompression=yes
; 'lowest' = no solicita elevacion (sin UAC). Requisito para actualizar sin admin.
PrivilegesRequired=lowest

[Files]
; Carpeta de salida de flet pack/PyInstaller (onedir). El nombre 'SolicitudesPago'
; debe coincidir con el -n del build (ver construir.bat). La ruta viene de
; DistDir, definido arriba y sobreescribible con iscc /DDistDir=...
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Icono para los accesos directos, copiado a la RAIZ de {app}. Se toma del codigo
; fuente (no del build): PyInstaller (onedir) mete 'Imagenes' dentro de
; {app}\_internal, asi que un IconFilename a {app}\Imagenes\icon.ico no existiria.
Source: ".\Imagenes\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; IconFilename apunta a {app}\icon.ico (copiado a la raiz en [Files]).
; AppUserModelID: DEBE coincidir con AUMID en core/win_taskbar.py para que el
; acceso y la ventana (creada por el flet.exe cliente) se agrupen como la MISMA
; app en la barra de tareas.
Name: "{group}\Herramientas Solicitudes de Pago"; Filename: "{app}\SolicitudesPago.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; AppUserModelID: "QuetzalticSolutions.HerramientasSolicitudesPago"
; {autodesktop} = escritorio del usuario (no el comun, que requeriria admin).
Name: "{autodesktop}\Herramientas Solicitudes de Pago"; Filename: "{app}\SolicitudesPago.exe"; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"; AppUserModelID: "QuetzalticSolutions.HerramientasSolicitudesPago"

[Run]
; Ejecuta la app al terminar la instalacion (no en modo silencioso/actualizacion).
Filename: "{app}\SolicitudesPago.exe"; Description: "{cm:LaunchProgram,Herramientas Solicitudes de Pago}"; Flags: nowait postinstall skipifsilent
