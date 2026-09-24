## Darko-s-Windows-Utility-Tool (DWUT)
DWUT is a utility tool designed for windows with features ranging from boosts to optimization to tweaks to privacy changes to disk cleanup.
## Requirements
- Windows 10 / 11
- Python 3.10+
- Auto Elevated Administrator privileges (required for most features)
## Install
```bat
pip install customtkinter pillow psutil pywin32 pymem requests
```
For GPU stats on the Dashboard, install and run [OpenHardwareMonitor](https://openhardwaremonitor.org/) first.
## Launch
```bat
python run.py
```
## Themes (12 total)
| Theme | Description |
|----------------|--------------------------------------|
| Dark | Default dark palette, blue accent |
| Midnight | Deeper blacks than Dark |
| Carbon | Warm charcoal |
| Slate | Blue-tinted dark |
| Violet | Dark base, purple accent |
| Rose | Dark base, pink/red accent |
| Amber | Dark base, warm orange accent |
| Teal | Dark base, cyan accent |
| Nord | Nordic blue palette |
| Dracula | Classic Dracula colour scheme |
| Solarized Dark | Solarized dark palette |
| High Contrast | Maximum readability |
## What's happening
| Feature | What actually happens |
|---|---|
| Dashboard | psutil CPU/RAM/Disk every 2s, non-blocking |
| System Health Score | Computed from real metrics |
| Bloatware Removal | Get-AppxPackage | Remove-AppxPackage -AllUsers |
| Feature Tweaks | Real registry writes + sc stop/config for services |
| Privacy Controls | Real registry writes to HKCU/HKLM policy paths |
| Windows Recall | Registry policy disable + service stop + package removal |
| SFC | sfc /scannow — output streams live |
| DISM | DISM /Online /Cleanup-Image /RestoreHealth — live output |
| Network Reset | netsh int ip reset, netsh winsock reset, ipconfig /flushdns |
| Windows Update Repair | Stops WU services, clears SoftwareDistribution, restarts |
| System Tools | devmgmt.msc, services.msc, etc. via correct mmc.exe/rundll32 launch |
| Disk Cleanup | Real byte measurement before and after, per-category |
| Registry Cleaner | Real scan of uninstall/startup/file assoc keys, real deletion with .reg backup |
| Startup Manager | Real winreg + schtasks reads, real StartupApproved disable |
| Game Booster | Real SetPriorityClass, real power plan switch, real RAM measurement |
| FPS Unlocker | Real pymem heap scan via VirtualQueryEx, continuous patching |
| Proxy Fetch | Parallel HTTP fetch from 22 real sources |
| Proxy Checker | 9-stage: TCP, protocol, exit IP, geo, anonymity, pool analysis |
| DNS Change | Set-DnsClientServerAddress on all active adapters |
| WinGet Upgrade | winget upgrade --all with live output |
## What was fixed vs the original
- All .msc tool launches fixed (cmd /c start instead of direct Popen — fixes WinError 193)
- All .cpl launches fixed (rundll32 shell32.dll,Control_RunDLL)
- CPU stats non-blocking (interval=None instead of interval=0.5)
- Dashboard periodic worker created once, not on every page visit
- 12 themes with live swatches in Settings
- (i) info badges on every setting and table column
## Architecture
```
dwut/
app/ config, state, permissions, main
core/ EventBus, ThreadWorker, Logger
modules/ pure Python — no UI imports
ui/ CustomTkinter — no business logic
widgets/ reusable (InfoBadge, attach_tooltip)
pages/ dashboard, optimizer, debloat, gaming, proxy, tools, settings
```
## Portable mode
If a settings/ folder exists next to run.py, all data is stored there.
Otherwise uses %APPDATA%\\DWUT\\.
feel free to run off of a usb
## IN PROGRESS
- scrolling lag fixes
- page load lag fixes
- auto updates from the github
- exe standalone version
- exe installer for the exe
- creating a file for the command irm "https://(repo) | iex . allowing for instant launching from powershell requiring little manual work
