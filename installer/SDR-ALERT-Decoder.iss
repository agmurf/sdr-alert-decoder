; Inno Setup script for the SDR ALERT Decoder desktop app.
;
; Build with:  python installer/build_installer.py
; That stages the payload (PyInstaller one-dir build + rtl-sdr tools) into
; installer/payload and then runs ISCC over this file. Building this script
; directly will fail unless that staging has already happened.
;
; Installs per-user by default so no administrator rights are needed - the
; app is a decoder, not a service, and requiring elevation just to receive
; radio telemetry puts it out of reach on locked-down agency machines.

#define AppName        "SDR ALERT Decoder"
#define AppVersion     "1.1.0"
#define AppPublisher   "Adam Murphy"
#define AppURL         "https://github.com/agmurf/sdr-alert-decoder"
#define AppExe         "SDR ALERT Decoder.exe"

[Setup]
AppId={{7F3B2A64-9C41-4E8D-B5A7-2E6D1C40F9A3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\SDR ALERT Decoder
DefaultGroupName=SDR ALERT Decoder
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
InfoAfterFile=NOTICES.txt
OutputDir=..\dist
OutputBaseFilename=SDR-ALERT-Decoder-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Per-user by default; an operator who wants it machine-wide can elevate.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; The PyInstaller one-dir build. _internal must be replaced wholesale, never
; merged - mixing python311/312 runtimes has broken this app before.
Source: "payload\{#AppExe}";        DestDir: "{app}"; Flags: ignoreversion
Source: "payload\_internal\*";      DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; rtl-sdr command-line tools. The app looks on PATH first, then in this
; folder beside the exe, so a fresh machine needs no PATH surgery.
Source: "payload\rtl-sdr\*";        DestDir: "{app}\rtl-sdr"; Flags: ignoreversion
Source: "payload\sensor_overrides.json"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist
Source: "NOTICES.txt";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";             DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion

[Icons]
Name: "{group}\SDR ALERT Decoder";  Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall SDR ALERT Decoder"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SDR ALERT Decoder"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch SDR ALERT Decoder"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Written by the app at runtime, so Inno does not track them.
Type: files; Name: "{app}\tuner_config.json"
Type: files; Name: "{app}\calibration_profile.json"
Type: dirifempty; Name: "{app}"
