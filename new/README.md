# muve (production app)

Same as old app. New UI + Bluetooth + optional AUX.

## Modes (automatic on Play)

### 1) Bluetooth (priority when connected in app)
- **Scan** — finds nearby unpaired phones (AssociationEndpoint inquiry)
- **Pair** — `DeviceInformationCustomPairing` with `CONFIRM_ONLY` (confirm on phone when prompted)
- **Paired phones** refresh automatically (no separate Scan for already-paired)
- **Connect** — `TryCreateFromId` → `StartAsync` → `OpenAsync` (APC flow)
- **Disconnect** closes the audio link only (pairing kept)
- **Forget** / **Forget all** call Windows `UnpairAsync` (real remove from Bluetooth)
- App sets default playback to **CABLE Input**, captures CABLE → **Gigaport ASIO**
- Song title / seek are not available for phone→PC Bluetooth (Windows limitation)
- While Live is on, the timer shows **session elapsed** (`LIVE`)
- **Prev / Next** may not control the phone on this path

Phone/Windows may still show a short confirm toast during Pair — that cannot be removed.

Console:
```
[windows_cable] default playback → 'CABLE Input'
[live_audio] mode=cable prefer_bt=True capture='…CABLE…' output='…ASIO…Gigaport…'
```

### 2) AUX (Behringer / USB interface)
- Disconnect Bluetooth in the app (or skip Connect)
- Plug phone AUX into Behringer → USB to tablet
- **Play** captures Behringer → **Gigaport ASIO**

Console:
```
[live_audio] mode=aux capture='…Behringer…' output='…ASIO…Gigaport…'
```

## 3) Demo tab
- Plays **`assets/demo.wav`** through the **same Live → Gigaport** path (audio ch1–2 + vibration ch3–6)
- Same volume / vibration / bass-cutoff controls as Now Playing
- Seek / restart work on the demo file
- Starting Demo stops Live (and vice versa) so ASIO isn’t contested


**Easiest fix for WinRT errors on the tablet:** double-click `install_winrt.bat` in `D:\muvi_new`, wait for `SUCCESS`, then:

```bat
python app.py
```

`app.py` also auto-installs missing WinRT packages on startup into the same Python.

Manual:

```bat
cd D:\muvi_new
python -m pip install -r requirements.txt
python app.py
```

Always use `python -m pip` (not bare `pip`) so packages go into the same Python as `app.py`.

Close the old muve app before Play (ASIO exclusive).

**Output is always Gigaport — never set app output to VB-Cable.**


cd D:\muvi_new
python -c "import sys; print(sys.executable)"
python -m pip install --force-reinstall winrt-runtime winrt-Windows.Foundation.Collections winrt-Windows.Media.Control winrt-Windows.Foundation
python -c "import winrt.windows.foundation.collections; print('OK')"
python app.py




Option 1: Disable the Windows Update service (works on Pro and Home)
Press Win + R.
Type:
services.msc
Find Windows Update.
Double-click it.
Click Stop.
Set Startup type to Disabled.
Click Apply → OK.
To prevent Windows from restarting it:
Go to the Recovery tab.
Set First failure, Second failure, and Subsequent failures to Take No Action.
Click Apply.





Yes. If this is a dedicated production tablet, you can run the following commands as Administrator.
First, stop and disable the update-related services:
sc stop wuauserv
sc config wuauserv start= disabled

sc stop UsoSvc
sc config UsoSvc start= disabled

sc stop bits
sc config bits start= disabled

sc stop dosvc
sc config dosvc start= disabled
However, on Windows 10 22H2, this alone is usually not enough because Windows Update Medic Service (WaaSMedicSvc) is protected and will often re-enable Windows Update.
If you try:
sc config WaaSMedicSvc start= disabled
you'll most likely get:
[SC] ChangeServiceConfig FAILED 5:
Access is denied.
That's by design.
To truly disable updates permanently
You need TrustedInstaller permissions to disable WaaSMedicSvc. There is no built-in CMD command that can permanently do this because Windows protects the service.
You have two practical options:
Use a TrustedInstaller tool (such as NSudo) and then run:
reg add "HKLM\SYSTEM\CurrentControlSet\Services\WaaSMedicSvc" /v Start /t REG_DWORD /d 4 /f
(4 = Disabled)
Use Windows Update Blocker (WUB), which automates the required changes.
If your goal is a production kiosk where updates should never run, these are the reliable approaches. Microsoft intentionally prevents standard administrator commands from permanently disabling the update infrastructure, so there isn't a single CMD command that can bypass those protections.





pyinstaller --noconfirm --windowed --add-data "web;web" --add-data "assets;assets" app.py





@echo off
setlocal

timeout /t 15 /nobreak >nul
cd /d "%~dp0app"

"C:\Users\RLX-Satori\AppData\Local\Programs\Python\Python313\python.exe" ^
"%~dp0app\app.py" > "%~dp0startup_log.txt" 2>&1

endlocal