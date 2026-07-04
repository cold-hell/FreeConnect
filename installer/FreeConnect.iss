; Установщик FreeConnect (Inno Setup).
; Собирает dist\FreeConnect.exe в один инсталлятор с ярлыками и деинсталлятором.
; Сборка: ISCC.exe installer\FreeConnect.iss  ->  installer\Output\FreeConnect-Setup.exe

#define MyAppName "FreeConnect"
#define MyAppVersion "1.0"
#define MyAppPublisher "FreeConnect"
#define MyAppExeName "FreeConnect.exe"

[Setup]
; Уникальный AppId (не менять между версиями — по нему находится прошлая установка).
AppId={{7E2B1F4C-9A3D-4C1E-8F5A-FREECONNECT01}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\FreeConnect
DefaultGroupName=FreeConnect
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=Output
OutputBaseFilename=FreeConnect-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Приложению нужен админ (winws/WinDivert) + установка в Program Files.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\ui\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; SmartScreen: инсталлятор не подписан — при первом запуске скажет «неизвестный
; издатель». Это ожидаемо (подпись кода стоит денег).

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: checkedonce

[Files]
Source: "..\dist\FreeConnect.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\FreeConnect"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Удалить FreeConnect"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FreeConnect"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить FreeConnect"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Снимаем задачу автозапуска, чтобы она не указывала на удалённый .exe.
Filename: "schtasks.exe"; Parameters: "/Delete /TN FreeConnect /F"; Flags: runhidden runascurrentuser; RunOnceId: "DelAutostart"

[UninstallDelete]
; Полная очистка данных приложения (рантайм winws, конфиг, логи, свои стратегии).
Type: filesandordirs; Name: "C:\FreeConnect"

[Code]
{ Перед удалением гасим запущенные процессы, иначе файлы заблокированы. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec('taskkill.exe', '/F /IM FreeConnect.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/F /IM winws.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
