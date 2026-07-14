# muve (production app)

Same as old app. New UI + Bluetooth + optional AUX.

## Modes (automatic on Play)

### 1) Bluetooth (priority when connected in app)
- **Find nearby** — AssociationEndpoint inquiry for unpaired phones (in-app list)
- **Pair** — `DeviceInformationCustomPairing` with `CONFIRM_ONLY` (confirm on phone when prompted)
- **Scan** — lists already-paired phones via `AudioPlaybackConnection.GetDeviceSelector()`
- **Connect** — `TryCreateFromId` → `StartAsync` → `OpenAsync` (APC flow, unchanged)
- **Disconnect & forget** closes the link and unpairs for the next guest
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

## Run

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