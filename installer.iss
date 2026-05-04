; Limitador - Inno Setup Installer Script
; Requiere Inno Setup 6.x: https://jrsoftware.org/isinfo.php
; Compila primero con build.bat para generar dist\Limitador.exe

#define MyAppName      "Limitador"
#define MyAppVersion   "1.0.0"
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
VersionInfoDescription=Limitador de ancho de banda PC-wide
VersionInfoProductName={#MyAppName}
VersionInfoCopyright=Apache License 2.0
MinVersion=10.0
CloseApplications=yes
CloseApplicationsFilter=*{#MyAppExeName}*
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Bienvenido al asistente de instalaci%ón de [name]
WelcomeLabel2=Este asistente instalar%á [name/ver] en tu equipo.%n%n[name] limita el ancho de banda de internet a nivel del sistema, sin necesitar configurar nada en el router.%n%nSe recomienda cerrar otras aplicaciones antes de continuar.
FinishedHeadingLabel=Instalaci%ón de [name] completada
FinishedLabelNoIcons=[name] se ha instalado correctamente.%n%nHaz doble clic en el icono del escritorio o en el Men%ú Inicio para iniciar el programa.
ClickFinish=Haz clic en Finalizar para cerrar el asistente.

[Tasks]
Name: "desktopicon"; Description: "Crear icono en el Escritorio"; GroupDescription: "Iconos adicionales:"; Flags: checkedonce
Name: "autostart"; Description: "Iniciar Limitador autom%áticamente con Windows"; GroupDescription: "Opciones de inicio:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "Limitador.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Limitador.ico"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\Limitador.ico"; Tasks: desktopicon

[Run]
; Registrar tarea de inicio automático en Task Scheduler
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /TN ""{#MyAppName}"" /TR ""{app}\{#MyAppExeName}"" /SC ONLOGON /RL HIGHEST /F"; Flags: runhidden; Tasks: autostart; StatusMsg: "Configurando inicio automático con Windows..."
; Ofrecer lanzar la app al finalizar
Filename: "{app}\{#MyAppExeName}"; Description: "Iniciar {#MyAppName} ahora"; Flags: nowait postinstall skipifsilent shellexec

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""{#MyAppName}"" /F"; Flags: runhidden; RunOnceId: "RemoveAutostart"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
{ ------------------------------------------------------------------ }
{  Verificaciones previas a la instalación                           }
{ ------------------------------------------------------------------ }

function IsWin10OrLater(): Boolean;
var
  Version: TWindowsVersion;
begin
  GetWindowsVersionEx(Version);
  Result := (Version.Major >= 10);
end;

function InitializeSetup(): Boolean;
var
  ErrMsg: String;
begin
  Result := True;

  if not IsWin10OrLater() then
  begin
    MsgBox(
      'Limitador requiere Windows 10 o superior.' + #13#10 +
      'Tu versión de Windows no es compatible.',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;

  if not FileExists(ExpandConstant('{src}\dist\{#MyAppExeName}')) then
  begin
    MsgBox(
      'No se encontró dist\{#MyAppExeName}.' + #13#10 + #13#10 +
      'Debes compilar el ejecutable antes de crear el instalador.' + #13#10 +
      'Ejecuta build.bat en el directorio del proyecto.',
      mbError, MB_OK
    );
    Result := False;
    Exit;
  end;
end;

{ ------------------------------------------------------------------ }
{  Página de bienvenida personalizada con info del sistema           }
{ ------------------------------------------------------------------ }

procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel2.Caption :=
    WizardForm.WelcomeLabel2.Caption + #13#10 + #13#10 +
    'Nota: Limitador usa el driver WinDivert para interceptar ' +
    'paquetes de red. Algunos antivirus pueden marcarlo como ' +
    'sospechoso — es un falso positivo. Agrega una exclusión ' +
    'para Limitador.exe si ocurre.';
end;

{ ------------------------------------------------------------------ }
{  Post-instalación: verificar que el exe se instaló correctamente   }
{ ------------------------------------------------------------------ }

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
    begin
      MsgBox(
        'Advertencia: no se pudo verificar la instalación del ejecutable.' + #13#10 +
        'Si el programa no inicia, intenta reinstalar.',
        mbError, MB_OK
      );
    end;
  end;
end;
