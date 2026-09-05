; Script Inno Setup pour l'installateur Windows de UVI.
; Empaquette packaging/dist/UVI (sortie de `pyinstaller packaging/uvi.spec`,
; voir packaging/EMPAQUETAGE.md) dans un uvi_setup.exe distribuable.
;
; Installe dans {localappdata} (dossier utilisateur), PAS Program Files :
; UVI écrit à côté de lui-même (.uvi_prefs.json, UVI_Storage/, voir
; packaging/uvi.spec) -- Program Files est en lecture seule sans élévation
; administrateur, ce qui casserait la sauvegarde au premier lancement.
;
; Compilation (après avoir installé Inno Setup 6, gratuit,
; https://jrsoftware.org/isdl.php) :
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\uvi_installer.iss

#define MyAppName "UVI"
#define MyAppVersion "9.0.0"
#define MyAppPublisher "oxishion-ai"
#define MyAppExeName "UVI.exe"

[Setup]
AppId={{B6E9F5B2-8C3F-4C1A-9E7D-UVI0000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist_installer
OutputBaseFilename=uvi_setup
SetupIconFile=..\assets\uvi_icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Auto-mise-à-jour (voir uvi_updater.py et docs/MISE_A_JOUR.md) : UVI.exe
; tient un mutex Windows nommé EXACTEMENT pareil (uvi_updater.hold_app_mutex,
; appelé avant UVIIDE() dans le bloc __main__) tant qu'il tourne. AppMutex
; permet à Inno Setup de détecter une instance en cours ; CloseApplications
; la ferme proprement (silencieusement en mode /SILENT, avec confirmation
; sinon) AVANT d'écrire les nouveaux fichiers -- sans ça, les .pyd/.dll de
; l'ancienne version resteraient verrouillés et l'installation échouerait.
; RestartApplications=yes la relance automatiquement une fois la mise à
; jour posée, pour que uvi_updater.launch_installer n'ait rien d'autre à
; faire que lancer cet installateur.
AppMutex=UVI_IDE_RUNNING_MUTEX
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\UVI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
