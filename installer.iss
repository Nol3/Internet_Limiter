; ===========================================================================
;  Limitador - instalador (Inno Setup 6.x)
;
;  Compila antes el ejecutable:  build_installer.bat  (que llama a build.bat)
; ===========================================================================

#define MyAppName      "Limitador"
#define MyAppVersion   "2.0.0"
#define MyAppPublisher "Limitador Project"
#define MyAppURL       "https://github.com/Nol3/Internet_Limiter"
#define MyAppExeName   "Limitador.exe"

[Setup]
AppId={{A3F7B2C1-8D4E-4F5A-9B6C-2E1D0F3A7B8C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=installer_output
OutputBaseFilename=Setup_Limitador_{#MyAppVersion}
SetupIconFile=Limitador.ico
LicenseFile=LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=Limitador de ancho de banda (por aplicacion y por equipo)
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Apache License 2.0
MinVersion=10.0.17763
CloseApplications=yes
CloseApplicationsFilter=*{#MyAppExeName}*
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=Este asistente instalara [name/ver] en tu equipo.%n%n[name] limita el ancho de banda de internet por aplicacion o para todo el equipo, sin configurar nada en el router.%n%nNecesita permisos de administrador porque usa el driver de captura de paquetes WinDivert (se instala y se mantiene actualizado automaticamente).
FinishedHeadingLabel=Instalacion de [name] completada
FinishedLabelNoIcons=[name] se ha instalado correctamente.%n%nHaz doble clic en el icono del escritorio o en el Menu Inicio para iniciar el programa.
ClickFinish=Haz clic en Finalizar para cerrar el asistente.

[Tasks]
Name: "desktopicon"; Description: "Crear icono en el Escritorio"; GroupDescription: "Iconos adicionales:"; Flags: checkedonce
Name: "autostart"; Description: "Iniciar Limitador automaticamente con Windows (sin aviso de UAC)"; GroupDescription: "Opciones de inicio:"; Flags: unchecked

[Dirs]
; Carpeta estable del driver: escribible solo por administradores (el instalador corre
; elevado). La app vuelve a asegurar los permisos con icacls en cada arranque.
Name: "{commonappdata}\{#MyAppName}"; Permissions: users-read
Name: "{commonappdata}\{#MyAppName}\driver"; Permissions: users-read

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "Limitador.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist isreadme
Source: "CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Limitador.ico"
Name: "{group}\Diagnostico de {#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--cli --check"; IconFilename: "{app}\Limitador.ico"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Limitador.ico"; Tasks: desktopicon

[Run]
; Inicio automatico con privilegios altos (sin prompt de UAC en cada login).
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /TN ""{#MyAppName}"" /TR """"{app}\{#MyAppExeName}"" --minimized"" /SC ONLOGON /RL HIGHEST /F"; Flags: runhidden; Tasks: autostart; StatusMsg: "Configurando el inicio automatico con Windows..."
; Abrir la app al terminar.
Filename: "{app}\{#MyAppExeName}"; Description: "Iniciar {#MyAppName} ahora"; Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
; Limpieza: tarea de inicio automatico y registro del driver.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""{#MyAppName}"" /F"; Flags: runhidden; RunOnceId: "RemoveAutostart"
Filename: "{sys}\sc.exe"; Parameters: "stop WinDivert"; Flags: runhidden; RunOnceId: "StopDriver"
Filename: "{sys}\sc.exe"; Parameters: "delete WinDivert"; Flags: runhidden; RunOnceId: "DeleteDriver"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{commonappdata}\{#MyAppName}\driver"
Type: files; Name: "{userappdata}\{#MyAppName}\config.json"

[Code]
{ ------------------------------------------------------------------ }
{  Comprobaciones previas                                           }
{ ------------------------------------------------------------------ }

function IsWin10OrLater(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major >= 10);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  if not IsWin10OrLater() then
  begin
    MsgBox(
      'Limitador requiere Windows 10 o superior.' + #13#10 +
      'Tu version de Windows no es compatible.',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;

  if not FileExists(ExpandConstant('{src}\dist\{#MyAppExeName}')) then
  begin
    MsgBox(
      'No se encontro dist\{#MyAppExeName}.' + #13#10 + #13#10 +
      'Debes compilar el ejecutable antes de crear el instalador:' + #13#10 +
      'ejecuta build.bat en el directorio del proyecto.',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;
end;

{ ------------------------------------------------------------------ }
{  Aviso sobre el antivirus (falso positivo conocido)               }
{ ------------------------------------------------------------------ }

procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel2.Caption :=
    WizardForm.WelcomeLabel2.Caption + #13#10 + #13#10 +
    'Nota: Limitador usa el driver WinDivert para interceptar paquetes de red. ' +
    'Algunos antivirus lo marcan como sospechoso: es un falso positivo. Si ocurre, ' +
    'anade una exclusion para Limitador.exe y para ' +
    ExpandConstant('{commonappdata}\Limitador\driver') + '.';
end;

{ ------------------------------------------------------------------ }
{  Post-instalacion: comprobar que todo quedo en su sitio            }
{ ------------------------------------------------------------------ }

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
      MsgBox(
        'Advertencia: no se pudo verificar la instalacion del ejecutable.' + #13#10 +
        'Si el programa no inicia, intenta reinstalar.',
        mbError, MB_OK
      );
  end;
end;
