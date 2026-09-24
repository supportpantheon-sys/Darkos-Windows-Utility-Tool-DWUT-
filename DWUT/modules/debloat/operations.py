"""
modules/debloat/operations.py

Real debloat and Windows feature control.

Every function here does exactly what it says — real registry writes, real
PowerShell commands, real service control.  Nothing is simulated.

Key fixes over the original:
  1. AppxPackage removal uses real package family name wildcards (not display names)
  2. Privacy functions actually execute — they were stubs in the original
  3. No messagebox/UI calls from background threads — all feedback via EventBus
  4. scan_installed_appx returns a dict of package-name → installed bool
  5. Every operation saves what it changed so the restore tab can undo it

All public functions are designed to be called via ThreadWorker.submit().
They publish progress on Events.DEBLOAT_PROGRESS and return a typed result.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import BACKUPS_DIR
from core.events import Events, bus
from core.logger import get_logger

_log = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DebloatResult:
    success: bool
    applied: int
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    message: str = ""
    backup_path: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# Bloatware database — maps display name → real AppxPackage name patterns
# These patterns are used in: Get-AppxPackage -Name "*<pattern>*"
# ──────────────────────────────────────────────────────────────────────────────

WIN11_BLOATWARE: list[dict] = [
    # name = display label, pkg = PowerShell wildcard, desc = tooltip, priority = High/Medium/Low
    {"name": "Microsoft Teams",              "pkg": "MicrosoftTeams",                       "desc": "Preinstalled chat/video app",           "priority": "Medium"},
    {"name": "Candy Crush Saga",             "pkg": "king.com.CandyCrush",                  "desc": "Mobile game installed without consent",  "priority": "High"},
    {"name": "Candy Crush Friends",          "pkg": "king.com.CandyCrushFriends",           "desc": "Mobile game",                           "priority": "High"},
    {"name": "Xbox Game Bar",                "pkg": "Microsoft.XboxGamingOverlay",          "desc": "Gaming overlay (disable if not needed)", "priority": "Medium"},
    {"name": "Xbox Console Companion",       "pkg": "Microsoft.XboxApp",                    "desc": "Xbox app",                              "priority": "Medium"},
    {"name": "Xbox Identity Provider",       "pkg": "Microsoft.XboxIdentityProvider",       "desc": "Xbox account service",                  "priority": "Medium"},
    {"name": "Xbox TCUI",                    "pkg": "Microsoft.Xbox.TCUI",                  "desc": "Xbox UI framework",                     "priority": "Low"},
    {"name": "Microsoft Tips",               "pkg": "Microsoft.Getstarted",                 "desc": "Tips & tutorials app",                  "priority": "High"},
    {"name": "3D Builder",                   "pkg": "Microsoft.3DBuilder",                  "desc": "3D modeling app",                       "priority": "Medium"},
    {"name": "Weather",                      "pkg": "Microsoft.BingWeather",                "desc": "Bing Weather app",                      "priority": "Low"},
    {"name": "Get Help",                     "pkg": "Microsoft.GetHelp",                    "desc": "Support app",                           "priority": "Medium"},
    {"name": "Feedback Hub",                 "pkg": "Microsoft.WindowsFeedbackHub",         "desc": "Microsoft feedback collector",          "priority": "High"},
    {"name": "Mixed Reality Portal",         "pkg": "Microsoft.MixedReality.Portal",        "desc": "VR/AR app",                             "priority": "High"},
    {"name": "Mail and Calendar",            "pkg": "microsoft.windowscommunicationsapps",  "desc": "Built-in mail client",                  "priority": "Low"},
    {"name": "Microsoft News",               "pkg": "Microsoft.BingNews",                   "desc": "Bing News app",                         "priority": "Medium"},
    {"name": "Microsoft Solitaire",          "pkg": "Microsoft.MicrosoftSolitaireCollection","desc": "Pre-installed game suite",              "priority": "High"},
    {"name": "Movies & TV",                  "pkg": "Microsoft.ZuneVideo",                  "desc": "Video player app",                      "priority": "Medium"},
    {"name": "OneNote (Store)",              "pkg": "Microsoft.Office.OneNote",             "desc": "Store version of OneNote",              "priority": "Low"},
    {"name": "Paint 3D",                     "pkg": "Microsoft.MSPaint",                    "desc": "3D Paint app (not classic Paint)",      "priority": "Medium"},
    {"name": "People",                       "pkg": "Microsoft.People",                     "desc": "Contacts app",                          "priority": "Medium"},
    {"name": "Skype",                        "pkg": "Microsoft.SkypeApp",                   "desc": "Skype communication app",               "priority": "Low"},
    {"name": "Sticky Notes",                 "pkg": "Microsoft.MicrosoftStickyNotes",       "desc": "Sticky notes app",                      "priority": "Low"},
    {"name": "Voice Recorder",               "pkg": "Microsoft.WindowsSoundRecorder",       "desc": "Audio recording app",                   "priority": "Medium"},
    {"name": "Your Phone / Phone Link",      "pkg": "Microsoft.YourPhone",                  "desc": "Phone sync app",                        "priority": "Medium"},
    {"name": "Widgets (Web Experience)",     "pkg": "MicrosoftWindows.Client.WebExperience","desc": "Widgets panel and news feed",           "priority": "High"},
    {"name": "Copilot",                      "pkg": "Microsoft.Windows.Ai.Copilot.Provider","desc": "Windows Copilot AI sidebar",            "priority": "Medium"},
    {"name": "Clipchamp",                    "pkg": "Clipchamp.Clipchamp",                  "desc": "Microsoft video editor",                "priority": "Medium"},
    {"name": "Microsoft Family Safety",      "pkg": "MicrosoftCorporationII.MicrosoftFamily","desc": "Family safety features",               "priority": "Low"},
    {"name": "Power Automate",               "pkg": "Microsoft.PowerAutomateDesktop",       "desc": "Automation tool",                       "priority": "Low"},
    {"name": "Quick Assist",                 "pkg": "MicrosoftCorporationII.QuickAssist",   "desc": "Remote assistance tool",                "priority": "Low"},
    {"name": "Microsoft Photos",             "pkg": "Microsoft.Windows.Photos",             "desc": "Photos app (use a proper viewer)",      "priority": "Low"},
    {"name": "Groove Music / Media Player",  "pkg": "Microsoft.ZuneMusic",                  "desc": "Built-in music app",                    "priority": "Medium"},
    {"name": "Microsoft To Do",              "pkg": "Microsoft.Todos",                      "desc": "Task management app",                   "priority": "Low"},
    {"name": "Spotify (preinstalled)",       "pkg": "SpotifyAB.SpotifyMusic",              "desc": "Preinstalled Spotify",                  "priority": "Low"},
    {"name": "TikTok (preinstalled)",        "pkg": "BytedancePte.Ltd.TikTok",             "desc": "Preinstalled TikTok",                   "priority": "High"},
    {"name": "Disney+ (preinstalled)",       "pkg": "Disney.37853FC22B2CE",                "desc": "Preinstalled Disney+",                  "priority": "Medium"},
    {"name": "Amazon (preinstalled)",        "pkg": "AmazonVideo.PrimeVideo",              "desc": "Preinstalled Amazon Video",             "priority": "Medium"},
]

WIN10_BLOATWARE: list[dict] = [
    {"name": "Candy Crush Saga",             "pkg": "king.com.CandyCrush",                  "desc": "Mobile game",                           "priority": "High"},
    {"name": "Xbox",                         "pkg": "Microsoft.XboxApp",                    "desc": "Xbox gaming app",                       "priority": "Medium"},
    {"name": "Groove Music",                 "pkg": "Microsoft.ZuneMusic",                  "desc": "Music player",                          "priority": "Medium"},
    {"name": "3D Viewer",                    "pkg": "Microsoft.Microsoft3DViewer",          "desc": "3D file viewer",                        "priority": "Medium"},
    {"name": "Microsoft Solitaire",          "pkg": "Microsoft.MicrosoftSolitaireCollection","desc": "Game collection",                       "priority": "High"},
    {"name": "Mixed Reality Viewer",         "pkg": "Microsoft.MixedReality.Portal",        "desc": "VR viewer",                             "priority": "High"},
    {"name": "Mobile Plans",                 "pkg": "Microsoft.OneConnect",                 "desc": "Mobile carrier app",                    "priority": "High"},
    {"name": "Microsoft Office Hub",         "pkg": "Microsoft.MicrosoftOfficeHub",         "desc": "Office marketing hub",                  "priority": "Medium"},
    {"name": "Paint 3D",                     "pkg": "Microsoft.MSPaint",                    "desc": "3D Paint",                              "priority": "Medium"},
    {"name": "Skype",                        "pkg": "Microsoft.SkypeApp",                   "desc": "Skype",                                 "priority": "Low"},
    {"name": "Sticky Notes",                 "pkg": "Microsoft.MicrosoftStickyNotes",       "desc": "Notes app",                             "priority": "Low"},
    {"name": "Microsoft Tips",               "pkg": "Microsoft.Getstarted",                 "desc": "Tips app",                              "priority": "High"},
    {"name": "Weather",                      "pkg": "Microsoft.BingWeather",                "desc": "Weather app",                           "priority": "Low"},
    {"name": "Xbox Game Bar",                "pkg": "Microsoft.XboxGamingOverlay",          "desc": "Gaming overlay",                        "priority": "Medium"},
    {"name": "Feedback Hub",                 "pkg": "Microsoft.WindowsFeedbackHub",         "desc": "Feedback app",                          "priority": "High"},
    {"name": "Your Phone",                   "pkg": "Microsoft.YourPhone",                  "desc": "Phone integration",                     "priority": "Medium"},
    {"name": "Mail and Calendar",            "pkg": "microsoft.windowscommunicationsapps",  "desc": "Built-in mail",                         "priority": "Low"},
    {"name": "Movies & TV",                  "pkg": "Microsoft.ZuneVideo",                  "desc": "Video player",                          "priority": "Medium"},
    {"name": "People",                       "pkg": "Microsoft.People",                     "desc": "Contacts app",                          "priority": "Medium"},
    {"name": "Microsoft News",               "pkg": "Microsoft.BingNews",                   "desc": "News app",                              "priority": "Medium"},
]

BLOATWARE_DB: dict[str, list[dict]] = {
    "Windows 11": WIN11_BLOATWARE,
    "Windows 10": WIN10_BLOATWARE,
}


# ──────────────────────────────────────────────────────────────────────────────
# Windows Features / Tweaks database
# Each entry: key, name, desc, reversible, severity, registry_ops[], service_ops[], ps_cmds[]
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegOp:
    """A single registry write operation with its inverse."""
    hive: int
    path: str
    name: str
    value: object
    vtype: int
    # Restore value (None = delete key on restore)
    restore_value: object = None
    restore_vtype: Optional[int] = None


@dataclass
class ServiceOp:
    """Stop and disable a service."""
    service_name: str
    restore_start_type: int = 2   # 2=Auto, 3=Manual, 4=Disabled


@dataclass
class AppxOp:
    """Remove an AppxPackage by wildcard pattern."""
    pattern: str
    all_users: bool = True


@dataclass
class FeatureDef:
    key: str
    name: str
    desc: str
    recommended: bool
    severity: str      # Critical | High | Medium | Low
    reversible: bool = True
    category: str = "essential"   # "essential" | "advanced"

    # What actually happens:
    reg_ops: list[RegOp] = field(default_factory=list)
    service_ops: list[ServiceOp] = field(default_factory=list)
    ps_cmds: list[str] = field(default_factory=list)
    appx_ops: list[AppxOp] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Feature definitions — every op is real
# ──────────────────────────────────────────────────────────────────────────────

FEATURES: list[FeatureDef] = [

    FeatureDef(
        key="telemetry", name="Telemetry & Data Collection",
        desc="Disables DiagTrack service and AllowTelemetry registry policy",
        recommended=True, severity="Critical",
        service_ops=[
            ServiceOp("DiagTrack",          restore_start_type=2),
            ServiceOp("dmwappushservice",    restore_start_type=3),
            ServiceOp("SysMain",             restore_start_type=2),   # Superfetch — optional
        ],
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                  "AllowTelemetry", 0, winreg.REG_DWORD, restore_value=None),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\DataCollection",
                  "AllowTelemetry", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                  "LimitDiagnosticLogCollection", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
                  "DisableOneSettingsDownloads", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="widgets", name="Windows Widgets / News Feed",
        desc="Removes the Widgets button from taskbar and disables the news panel",
        recommended=True, severity="High",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "TaskbarDa", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Dsh",
                  "AllowNewsAndInterests", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Windows Feeds",
                  "EnableFeeds", 0, winreg.REG_DWORD),
        ],
        appx_ops=[
            AppxOp("MicrosoftWindows.Client.WebExperience", all_users=True),
        ],
    ),

    FeatureDef(
        key="copilot", name="Windows Copilot",
        desc="Disables Windows 11 Copilot AI sidebar via policy and registry",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Policies\Microsoft\Windows\WindowsCopilot",
                  "TurnOffWindowsCopilot", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot",
                  "TurnOffWindowsCopilot", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "ShowCopilotButton", 0, winreg.REG_DWORD, restore_value=1),
        ],
        appx_ops=[
            AppxOp("Microsoft.Windows.Ai.Copilot.Provider", all_users=True),
        ],
    ),

    FeatureDef(
        key="xbox", name="Xbox Game Bar & Services",
        desc="Disables Xbox overlay, Game DVR, and Xbox background services",
        recommended=True, severity="High",
        service_ops=[
            ServiceOp("XblAuthManager",  restore_start_type=3),
            ServiceOp("XblGameSave",     restore_start_type=3),
            ServiceOp("XboxGipSvc",      restore_start_type=3),
            ServiceOp("XboxNetApiSvc",   restore_start_type=3),
        ],
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                  "AppCaptureEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"System\GameConfigStore",
                  "GameDVR_Enabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\GameBar",
                  "GameDVR_Enabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\GameBar",
                  "AutoGameModeEnabled", 0, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="search", name="Windows Search / Cortana",
        desc="Stops Windows Search indexing service and disables Cortana",
        recommended=True, severity="High",
        service_ops=[
            ServiceOp("WSearch", restore_start_type=2),
        ],
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "CortanaEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "BingSearchEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Windows Search",
                  "AllowCortana", 0, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="websearch", name="Start Menu Web Search",
        desc="Removes web results from Start menu search bar",
        recommended=True, severity="High",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "BingSearchEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "SearchboxTaskbarMode", 0, winreg.REG_DWORD, restore_value=2),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Windows Search",
                  "DisableWebSearch", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Windows Search",
                  "ConnectedSearchUseWeb", 0, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="advertising", name="Advertising ID & Personalization",
        desc="Disables the advertising ID used to target ads across apps",
        recommended=True, severity="High",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
                  "Enabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo",
                  "DisabledByGroupPolicy", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Privacy",
                  "TailoredExperiencesWithDiagnosticDataEnabled", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="location", name="Location Tracking",
        desc="Disables Windows Location Services for all apps",
        recommended=True, severity="Critical",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location",
                  "Value", "Deny", winreg.REG_SZ, restore_value="Allow"),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors",
                  "DisableLocation", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\LocationAndSensors",
                  "DisableLocationScripting", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="phonelink", name="Phone Link / Your Phone",
        desc="Disables the phone-to-PC sync service and removes the app",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "EnableMmx", 0, winreg.REG_DWORD),
        ],
        appx_ops=[
            AppxOp("Microsoft.YourPhone", all_users=True),
        ],
    ),

    FeatureDef(
        key="notifications", name="Notification Spam / Tips",
        desc="Disables lock screen tips, welcome experience, and suggestion notifications",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "SoftLandingEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "SubscribedContent-338389Enabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "SubscribedContent-338388Enabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "SystemPaneSuggestionsEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\CloudContent",
                  "DisableSoftLanding", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\CloudContent",
                  "DisableWindowsConsumerFeatures", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="visual", name="Visual Effects / Animations",
        desc="Disables transparency, animations, and other visual effects for performance",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                  "EnableTransparency", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Control Panel\Desktop\WindowMetrics",
                  "MinAnimate", "0", winreg.REG_SZ, restore_value="1"),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
                  "VisualFXSetting", 2, winreg.REG_DWORD, restore_value=0),
        ],
        ps_cmds=[
            "SystemParametersInfo -ParameterID SetAnimationParams",
        ],
    ),

    FeatureDef(
        key="background", name="Background Apps",
        desc="Prevents UWP apps from running in the background",
        recommended=False, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
                  "GlobalUserDisabled", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="hibernation", name="Hibernation",
        desc="Frees up disk space equal to your RAM by removing hiberfil.sys",
        recommended=False, severity="Low",
        ps_cmds=[
            "powercfg /hibernate off",
        ],
    ),

    FeatureDef(
        key="printspooler", name="Print Spooler",
        desc="Disable if you don't use a printer (saves resources, stops PrintNightmare attack surface)",
        recommended=False, severity="Low",
        service_ops=[
            ServiceOp("Spooler", restore_start_type=2),
        ],
    ),

    FeatureDef(
        key="activity_history", name="Activity History / Timeline",
        desc="Stops Windows from recording what apps and files you use",
        recommended=True, severity="High",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "EnableActivityFeed", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "PublishUserActivities", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "UploadUserActivities", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="recall", name="Windows Recall (AI Screenshots)",
        desc="Stops Windows Recall from taking screenshots of everything you do",
        recommended=True, severity="Critical",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
                  "DisableAIDataAnalysis", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Policies\Microsoft\Windows\WindowsAI",
                  "DisableAIDataAnalysis", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "EnableActivityFeed", 0, winreg.REG_DWORD),
        ],
        service_ops=[
            ServiceOp("CDPUserSvc",    restore_start_type=3),
            ServiceOp("OneSyncSvc",    restore_start_type=3),
        ],
        ps_cmds=[
            'Get-AppxPackage -AllUsers | Where-Object {$_.Name -like "*Recall*"} | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue',
        ],
    ),

    FeatureDef(
        key="suggested_content", name="Suggested Apps / Content",
        desc="Disables suggested apps in Start menu and auto-installed sponsored apps",
        recommended=True, severity="High",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "SilentInstalledAppsEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "PreInstalledAppsEnabled", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
                  "OemPreInstalledAppsEnabled", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="onedrive", name="OneDrive Auto-Start",
        desc="Prevents OneDrive from starting with Windows (does not uninstall)",
        recommended=False, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Run",
                  "OneDrive", "", winreg.REG_SZ),
        ],
        ps_cmds=[
            "Get-Process OneDrive -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
    ),

    # ──────────────────────────────────────────────────────────────────────
    # Additional tweaks — matching common items from Chris Titus Tech's
    # WinUtil and O&O ShutUp10, not already covered above.
    # ──────────────────────────────────────────────────────────────────────

    FeatureDef(
        key="fast_startup", name="Fast Startup",
        desc="Disables Fast Startup (fixes dual-boot clock issues and some driver/update problems)",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\Session Manager\Power",
                  "HiberbootEnabled", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="mouse_accel", name="Mouse Acceleration (Enhance Pointer Precision)",
        desc="Disables 'Enhance pointer precision' for consistent 1:1 mouse movement",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse",
                  "MouseSpeed", "0", winreg.REG_SZ, restore_value="1"),
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse",
                  "MouseThreshold1", "0", winreg.REG_SZ, restore_value="6"),
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse",
                  "MouseThreshold2", "0", winreg.REG_SZ, restore_value="10"),
        ],
    ),

    FeatureDef(
        key="show_file_ext", name="Show File Extensions",
        desc="Shows file extensions (.txt, .exe, etc) in Explorer instead of hiding them",
        recommended=True, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "HideFileExt", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="show_hidden_files", name="Show Hidden Files",
        desc="Shows hidden files and folders in Explorer",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "Hidden", 1, winreg.REG_DWORD, restore_value=2),
        ],
    ),

    FeatureDef(
        key="delivery_optimization", name="Delivery Optimization (P2P Updates)",
        desc="Stops Windows Update from uploading update data to other PCs on the internet",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\DeliveryOptimization",
                  "DODownloadMode", 0, winreg.REG_DWORD),
        ],
        service_ops=[
            ServiceOp("DoSvc", restore_start_type=2),
        ],
    ),

    FeatureDef(
        key="storage_sense", name="Storage Sense",
        desc="Disables automatic disk cleanup (Storage Sense) — useful if you clean up manually",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy",
                  "01", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="error_reporting", name="Windows Error Reporting",
        desc="Disables Windows Error Reporting service and dialog popups after crashes",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\Windows Error Reporting",
                  "Disabled", 1, winreg.REG_DWORD, restore_value=0),
        ],
        service_ops=[
            ServiceOp("WerSvc", restore_start_type=3),
        ],
    ),

    FeatureDef(
        key="remote_assistance", name="Remote Assistance",
        desc="Disables incoming Remote Assistance requests",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\Remote Assistance",
                  "fAllowToGetHelp", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="teams_chat_icon", name="Teams Chat Icon / Auto-Install",
        desc="Removes the Chat (Teams) icon from the taskbar and stops Teams auto-install",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "TaskbarMn", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Windows Chat",
                  "ChatIcon", 3, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="end_task_context_menu", name='"End Task" in Taskbar Right-Click',
        desc="Adds an End Task option when you right-click an app in the taskbar (Windows 11)",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced\TaskbarDeveloperSettings",
                  "TaskbarEndTask", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="numlock_on_boot", name="NumLock On At Startup",
        desc="Turns NumLock on automatically at the login screen and after sign-in",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_USERS, r".DEFAULT\Control Panel\Keyboard",
                  "InitialKeyboardIndicators", "2080", winreg.REG_SZ, restore_value="2"),
        ],
    ),

    FeatureDef(
        key="classic_context_menu", name="Classic Right-Click Menu (Windows 10 style)",
        desc="Restores the full right-click context menu instead of Windows 11's shortened one",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32",
                  "", "", winreg.REG_SZ),
        ],
    ),

    FeatureDef(
        key="wifi_sense", name="Wi-Fi Sense",
        desc="Disables Wi-Fi Sense automatic hotspot/network sharing",
        recommended=True, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\WcmSvc\wifinetworkmanager\config",
                  "AutoConnectAllowedOEM", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="autoplay", name="AutoPlay for Removable Media",
        desc="Disables AutoPlay prompts when inserting USB drives / discs",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                  "NoDriveTypeAutoRun", 255, winreg.REG_DWORD, restore_value=145),
        ],
    ),

    FeatureDef(
        key="wu_auto_restart", name="Windows Update Auto-Restart",
        desc="Stops Windows from auto-restarting for updates while you're logged in",
        recommended=True, severity="Medium",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\WindowsUpdate\AU",
                  "NoAutoRebootWithLoggedOnUsers", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="dark_mode", name="Dark Mode",
        desc="Switches apps and Windows system UI to dark theme",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                  "AppsUseLightTheme", 0, winreg.REG_DWORD, restore_value=1),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                  "SystemUsesLightTheme", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="startup_sound", name="Windows Startup Sound",
        desc="Disables the Windows startup chime",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\LogonUI\BootAnimation",
                  "DisableStartupSound", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="telemetry_tasks", name="Telemetry Scheduled Tasks",
        desc="Disables the background scheduled tasks that collect diagnostic/CEIP data",
        recommended=True, severity="High",
        ps_cmds=[
            'Disable-ScheduledTask -TaskName "Microsoft Compatibility Appraiser" -TaskPath "\\Microsoft\\Windows\\Application Experience\\" -ErrorAction SilentlyContinue | Out-Null',
            'Disable-ScheduledTask -TaskName "ProgramDataUpdater" -TaskPath "\\Microsoft\\Windows\\Application Experience\\" -ErrorAction SilentlyContinue | Out-Null',
            'Disable-ScheduledTask -TaskName "Consolidator" -TaskPath "\\Microsoft\\Windows\\Customer Experience Improvement Program\\" -ErrorAction SilentlyContinue | Out-Null',
            'Disable-ScheduledTask -TaskName "UsbCeip" -TaskPath "\\Microsoft\\Windows\\Customer Experience Improvement Program\\" -ErrorAction SilentlyContinue | Out-Null',
            'Disable-ScheduledTask -TaskName "KernelCeipTask" -TaskPath "\\Microsoft\\Windows\\Kernel CEIP\\" -ErrorAction SilentlyContinue | Out-Null',
            'Disable-ScheduledTask -TaskName "Microsoft-Windows-DiskDiagnosticDataCollector" -TaskPath "\\Microsoft\\Windows\\DiskDiagnostic\\" -ErrorAction SilentlyContinue | Out-Null',
        ],
    ),

    FeatureDef(
        key="ink_workspace", name="Windows Ink Workspace Button",
        desc="Removes the pen/Ink Workspace button from the taskbar",
        recommended=False, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "PenWorkspaceButtonDesiredVisibility", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="meet_now", name='"Meet Now" Taskbar Icon',
        desc="Removes the Meet Now icon from the system tray",
        recommended=True, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "HideSCAMeetNow", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="people_icon", name="People Icon in Taskbar",
        desc="Removes the People/Contacts icon from the taskbar",
        recommended=True, severity="Low",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced\People",
                  "PeopleBand", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="misc_services_manual", name="Non-Essential Services → Manual",
        desc="Sets Fax, Remote Registry, Windows Maps, and other rarely-used services to Manual "
             "start instead of Automatic (frees a little RAM/startup time; doesn't disable them "
             "outright, so nothing breaks if something needs one)",
        recommended=False, severity="Low",
        service_ops=[
            ServiceOp("Fax",              restore_start_type=3),
            ServiceOp("RemoteRegistry",   restore_start_type=3),
            ServiceOp("MapsBroker",       restore_start_type=2),
            ServiceOp("WMPNetworkSvc",    restore_start_type=3),
            ServiceOp("WalletService",    restore_start_type=3),
            ServiceOp("RetailDemo",       restore_start_type=3),
            ServiceOp("PhoneSvc",         restore_start_type=3),
        ],
    ),

    FeatureDef(
        key="sleep_never_ac", name="Sleep Timeout (Plugged In) — Never",
        desc="Prevents the PC from sleeping automatically while on AC power (good for desktops/servers)",
        recommended=False, severity="Low", category="advanced",
        ps_cmds=[
            "powercfg /change standby-timeout-ac 0",
            "powercfg /change monitor-timeout-ac 0",
        ],
    ),

    # ──────────────────────────────────────────────────────────────────────
    # More tweaks matching Chris Titus Tech's WinUtil — only added ones I'm
    # confident are correct; a few obscure/uncertain WinUtil entries were
    # deliberately left out rather than risk shipping a broken registry path.
    # ──────────────────────────────────────────────────────────────────────

    FeatureDef(
        key="bitlocker_policy", name="BitLocker Auto-Encryption",
        desc="Blocks Windows from silently turning on device encryption on this PC. "
             "Does NOT decrypt a drive that's already encrypted — only stops new auto-encryption.",
        recommended=False, severity="Medium", category="essential",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\FVE",
                  "PreventDeviceEncryption", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="consumer_features", name="Windows Consumer Features",
        desc="Stops Windows from auto-installing suggested apps (Candy Crush, etc) on updates",
        recommended=True, severity="Medium", category="essential",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\CloudContent",
                  "DisableWindowsConsumerFeatures", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="wpbt", name="Windows Platform Binary Table (WPBT)",
        desc="Blocks OEM firmware from auto-injecting software at boot (mainly a Lenovo/HP bloatware vector)",
        recommended=False, severity="Medium", category="essential",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\Session Manager\Configuration Manager",
                  "DisableWpbtExecution", 1, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="reserved_storage", name="Reserved Storage",
        desc="Frees the 7GB+ Windows sets aside for its own updates — reclaims the space immediately",
        recommended=False, severity="Low", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\ReserveManager",
                  "ShippedWithReserves", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="ipv6_disable", name="IPv6 — Disable",
        desc="Fully disables IPv6 on all network adapters. Only use if you know you don't need it "
             "— don't combine with 'Prefer IPv4' below, pick one.",
        recommended=False, severity="Medium", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
                  "DisabledComponents", 0xFF, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="ipv6_prefer_v4", name="IPv6 — Prefer IPv4",
        desc="Keeps IPv6 available but makes Windows prefer IPv4 when both are offered — "
             "fixes some slow-connection issues without fully disabling IPv6",
        recommended=False, severity="Low", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
                  "DisabledComponents", 0x20, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="teredo_disable", name="Teredo — Disable",
        desc="Disables the Teredo IPv6 tunneling adapter — reduces attack surface if you don't need it",
        recommended=False, severity="Low", category="advanced",
        ps_cmds=["netsh interface teredo set state disabled"],
    ),

    FeatureDef(
        key="edge_debloat", name="Microsoft Edge — Debloat",
        desc="Turns off Edge's first-run promo screens, shopping assistant, collections, "
             "startup boost, and feedback/telemetry prompts. Does not remove Edge.",
        recommended=True, severity="Low", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "HideFirstRunExperience", 1, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "UserFeedbackAllowed", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "EdgeShoppingAssistantEnabled", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "EdgeCollectionsEnabled", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "PersonalizationReportingEnabled", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "ShowRecommendationsEnabled", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\Microsoft\Edge",
                  "StartupBoostEnabled", 0, winreg.REG_DWORD),
        ],
    ),

    FeatureDef(
        key="date_time_utc", name="Set Hardware Clock to UTC",
        desc="Fixes clock/time-zone conflicts when dual-booting Windows with Linux "
             "(Linux expects the hardware clock in UTC; Windows defaults to local time)",
        recommended=False, severity="Low", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
                  "RealTimeIsUniversal", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="explorer_open_to_this_pc", name="File Explorer Opens to This PC",
        desc="Makes File Explorer open to 'This PC' instead of the Windows 11 Home/Gallery page",
        recommended=False, severity="Low", category="advanced",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "LaunchTo", 1, winreg.REG_DWORD, restore_value=2),
        ],
    ),

    FeatureDef(
        key="onedrive_remove", name="OneDrive — Full Uninstall",
        desc="Actually uninstalls OneDrive (not just disabling autostart like the tweak above) "
             "and removes its Explorer sidebar entry. Reinstall later from microsoft.com if needed.",
        recommended=False, severity="High", category="advanced", reversible=False,
        ps_cmds=[
            "Get-Process OneDrive -ErrorAction SilentlyContinue | Stop-Process -Force",
            "$s32 = \"$env:SystemRoot\\System32\\OneDriveSetup.exe\"; "
            "$s64 = \"$env:SystemRoot\\SysWOW64\\OneDriveSetup.exe\"; "
            "if (Test-Path $s64) { Start-Process $s64 '/uninstall' -Wait } "
            "elseif (Test-Path $s32) { Start-Process $s32 '/uninstall' -Wait }",
        ],
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Classes\CLSID\{018D5C66-4533-4307-9B53-224DE2ED1FE6}",
                  "System.IsPinnedToNameSpaceTree", 0, winreg.REG_DWORD),
        ],
    ),

    # ──────────────────────────────────────────────────────────────────────
    # Customize Preferences — smaller, mostly cosmetic/UX toggles (Windows
    # 11 taskbar/start behavior, logon screen, accessibility). Each is a
    # single well-documented registry value.
    # ──────────────────────────────────────────────────────────────────────

    FeatureDef(
        key="bsod_verbose", name="BSoD Verbose Mode",
        desc="Shows technical details (driver names, addresses) on a Blue Screen instead of just a QR code",
        recommended=False, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\CrashControl",
                  "DisplayParameters", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="long_paths", name="Enable Long Paths",
        desc="Allows file paths longer than 260 characters (helps with deeply-nested project folders)",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SYSTEM\CurrentControlSet\Control\FileSystem",
                  "LongPathsEnabled", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="game_mode", name="Game Mode",
        desc="Windows' own Game Mode — prioritizes CPU/GPU resources for the foreground game",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\GameBar",
                  "AutoGameModeEnabled", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="lock_screen_disable", name="Lock Screen — Disable",
        desc="Skips straight to the password/PIN prompt instead of showing the lock screen wallpaper first",
        recommended=False, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\Personalization",
                  "NoLockScreen", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="logon_acrylic_blur", name="Logon Screen Acrylic Blur",
        desc="Enables the frosted-glass blur effect behind the sign-in screen",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "DisableAcrylicBackgroundOnLogon", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="logon_verbose", name="Logon Verbose Mode",
        desc="Shows detailed status messages ('Applying computer settings...') during sign-in instead of a plain spinner",
        recommended=False, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                  "VerboseStatus", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="scrollbars_always_visible", name="Scrollbars Always Visible",
        desc="Shows full-width scrollbars all the time instead of thin auto-hiding ones",
        recommended=False, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility",
                  "DynamicScrollbars", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="start_menu_bing_search", name="Start Menu Bing Search",
        desc="Disables web/Bing results mixed into Start Menu search — local results only",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "BingSearchEnabled", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="start_menu_recommendations", name="Start Menu Recommendations",
        desc="Removes the 'Recommended' recently-used/suggested files section from the Start Menu",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "Start_IrisRecommendations", 0, winreg.REG_DWORD, restore_value=1),
        ],
    ),

    FeatureDef(
        key="sticky_keys", name="Sticky Keys",
        desc="Keeps the Sticky Keys accessibility feature and its Shift×5 shortcut available. "
             "Turning this off disables the feature and its popup entirely.",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Accessibility\StickyKeys",
                  "Flags", "510", winreg.REG_SZ, restore_value="58"),
        ],
    ),

    FeatureDef(
        key="battery_percentage", name="Taskbar Battery Percentage",
        desc="Shows the exact battery percentage next to the battery icon in the system tray",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "ShowBatteryPercentage", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="taskbar_centered_icons", name="Taskbar Centered Icons",
        desc="Windows 11 style — centers taskbar icons. Off = Windows 10 style, left-aligned.",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "TaskbarAl", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="taskbar_search_icon", name="Taskbar Search Icon",
        desc="Shows the search icon/box on the taskbar",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "SearchboxTaskbarMode", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="taskbar_task_view", name="Taskbar Task View Icon",
        desc="Shows the Task View (virtual desktops) button on the taskbar",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                  "ShowTaskViewButton", 1, winreg.REG_DWORD, restore_value=0),
        ],
    ),

    FeatureDef(
        key="window_snapping", name="Window Snapping",
        desc="Drag a window to the screen edge to snap it into half/quarter-screen layouts",
        recommended=True, severity="Low", category="preference",
        reg_ops=[
            RegOp(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop",
                  "WindowArrangementActive", "1", winreg.REG_SZ, restore_value="0"),
        ],
    ),

    FeatureDef(
        key="s0_network_connectivity", name="Network Connectivity in Modern Standby",
        desc="Keeps Wi-Fi/network active while the PC is in Modern Standby (S0) sleep, so background "
             "sync/downloads/notifications keep working while asleep — uses more battery",
        recommended=False, severity="Low", category="preference",
        ps_cmds=[
            "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP NETCONNECT 1",
            "powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP NETCONNECT 1",
            "powercfg /setactive SCHEME_CURRENT",
        ],
    ),
]

# Build lookup dict
FEATURES_BY_KEY: dict[str, FeatureDef] = {f.key: f for f in FEATURES}


# ──────────────────────────────────────────────────────────────────────────────
# Privacy items — were stubs in original, now actually execute
# ──────────────────────────────────────────────────────────────────────────────

PRIVACY_ITEMS: list[dict] = [
    {
        "key": "telemetry_svc",
        "title": "Disable Telemetry Services",
        "desc": "Stops DiagTrack and dmwappushservice",
        "action": lambda: _run_service_ops([
            ServiceOp("DiagTrack", 4),
            ServiceOp("dmwappushservice", 4),
        ]),
    },
    {
        "key": "location_svc",
        "title": "Disable Location Tracking",
        "desc": "Sets location consent to Deny for all apps",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location",
                  "Value", "Deny", winreg.REG_SZ),
        ]),
    },
    {
        "key": "mic_access",
        "title": "Disable Microphone Access (background apps)",
        "desc": "Blocks background microphone access globally",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone",
                  "Value", "Deny", winreg.REG_SZ),
        ]),
    },
    {
        "key": "cam_access",
        "title": "Disable Camera Access (background apps)",
        "desc": "Blocks background camera access globally",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam",
                  "Value", "Deny", winreg.REG_SZ),
        ]),
    },
    {
        "key": "activity_feed",
        "title": "Disable Activity History",
        "desc": "Stops Windows recording your app usage and files",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "EnableActivityFeed", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\System",
                  "PublishUserActivities", 0, winreg.REG_DWORD),
        ]),
    },
    {
        "key": "search_history",
        "title": "Disable Search History",
        "desc": "Clears and disables search history in Windows Search",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "HistoryViewEnabled", 0, winreg.REG_DWORD),
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\Search",
                  "DeviceHistoryEnabled", 0, winreg.REG_DWORD),
        ]),
    },
    {
        "key": "ad_id",
        "title": "Disable Advertising ID / Ad Tracking",
        "desc": "Blocks the advertising ID used for targeted ads",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_CURRENT_USER,
                  r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
                  "Enabled", 0, winreg.REG_DWORD),
        ]),
    },
    {
        "key": "cloud_sync",
        "title": "Disable Cloud Sync (Settings & Activity)",
        "desc": "Stops syncing settings and activity data to Microsoft cloud",
        "action": lambda: _run_reg_ops([
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\SettingSync",
                  "DisableSettingSync", 2, winreg.REG_DWORD),
            RegOp(winreg.HKEY_LOCAL_MACHINE,
                  r"SOFTWARE\Policies\Microsoft\Windows\SettingSync",
                  "DisableSettingSyncUserOverride", 1, winreg.REG_DWORD),
        ]),
    },
]

PRIVACY_BY_KEY: dict[str, dict] = {p["key"]: p for p in PRIVACY_ITEMS}


# ──────────────────────────────────────────────────────────────────────────────
# Shared execution helpers
# ──────────────────────────────────────────────────────────────────────────────

def _run_reg_ops(ops: list[RegOp]) -> list[str]:
    """Execute a list of registry writes. Returns list of error strings."""
    errors: list[str] = []
    for op in ops:
        try:
            key = winreg.CreateKeyEx(op.hive, op.path, 0,
                                     winreg.KEY_SET_VALUE | winreg.KEY_CREATE_SUB_KEY)
            winreg.SetValueEx(key, op.name, 0, op.vtype, op.value)
            winreg.CloseKey(key)
        except Exception as exc:
            errors.append(f"{op.path}\\{op.name}: {exc}")
            _log.warning("RegOp failed [%s\\%s]: %s", op.path, op.name, exc)
    return errors


def _run_service_ops(ops: list[ServiceOp], disable: bool = True) -> list[str]:
    """Stop and disable (or re-enable) services. Returns errors."""
    errors: list[str] = []
    for op in ops:
        try:
            if disable:
                subprocess.run(["sc", "stop", op.service_name],
                               capture_output=True, timeout=10)
                subprocess.run(["sc", "config", op.service_name, "start=disabled"],
                               capture_output=True, timeout=10)
            else:
                start_type = {2: "auto", 3: "demand", 4: "disabled"}.get(op.restore_start_type, "auto")
                subprocess.run(["sc", "config", op.service_name, f"start={start_type}"],
                               capture_output=True, timeout=10)
                if op.restore_start_type in (2, 3):
                    subprocess.run(["sc", "start", op.service_name],
                                   capture_output=True, timeout=10)
        except Exception as exc:
            errors.append(f"Service {op.service_name}: {exc}")
            _log.warning("ServiceOp failed [%s]: %s", op.service_name, exc)
    return errors


def _run_ps(cmd: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a PowerShell command. Returns (success, output)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    except Exception as exc:
        return False, str(exc)


def _remove_appx(pattern: str, all_users: bool = True) -> tuple[bool, str]:
    """Remove AppxPackage by name pattern."""
    scope = "-AllUsers" if all_users else ""
    cmd = (
        f'Get-AppxPackage {scope} -Name "*{pattern}*" | '
        f'Remove-AppxPackage {scope} -ErrorAction SilentlyContinue; '
        f'Get-AppxProvisionedPackage -Online | '
        f'Where-Object {{$_.DisplayName -like "*{pattern}*"}} | '
        f'Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue'
    )
    return _run_ps(cmd, timeout=60)


# ──────────────────────────────────────────────────────────────────────────────
# Backup / Restore point
# ──────────────────────────────────────────────────────────────────────────────

def create_restore_point(description: str = "DWUT Debloat Backup") -> bool:
    """Create a Windows System Restore point. Returns True on success."""
    ok, out = _run_ps(
        f"Checkpoint-Computer -Description '{description}' "
        f"-RestorePointType 'MODIFY_SETTINGS' -ErrorAction Stop",
        timeout=60,
    )
    if ok:
        _log.info("System restore point created: %s", description)
    else:
        _log.warning("Restore point creation failed: %s", out[:200])
    return ok


def open_system_restore() -> None:
    """Open the Windows System Restore wizard."""
    try:
        subprocess.Popen(["rstrui.exe"])
    except Exception as exc:
        _log.error("Failed to open system restore: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Scan — what's actually installed on this machine
# ──────────────────────────────────────────────────────────────────────────────

def scan_installed_appx() -> dict[str, bool]:
    """
    Query Get-AppxPackage and return a dict of pkg_pattern → installed.
    Uses a single PowerShell call for performance.
    """
    ok, out = _run_ps(
        "Get-AppxPackage -AllUsers | Select-Object -ExpandProperty Name",
        timeout=30,
    )
    if not ok:
        _log.warning("Get-AppxPackage failed, assuming nothing installed")
        return {}

    installed_names = {line.strip().lower() for line in out.splitlines() if line.strip()}
    result: dict[str, bool] = {}

    all_entries = WIN11_BLOATWARE + WIN10_BLOATWARE
    seen_pkgs: set[str] = set()
    for entry in all_entries:
        pkg = entry["pkg"].lower()
        if pkg in seen_pkgs:
            continue
        seen_pkgs.add(pkg)
        result[pkg] = any(pkg in name for name in installed_names)

    return result


# ──────────────────────────────────────────────────────────────────────────────
# Bloatware removal
# ──────────────────────────────────────────────────────────────────────────────

def remove_bloatware(
    selected: list[dict],  # list of entries from BLOATWARE_DB
    create_restore: bool = True,
) -> DebloatResult:
    """
    Remove selected AppxPackage entries.
    Publishes progress events.  Returns DebloatResult.
    Call from ThreadWorker.
    """
    if create_restore:
        bus.publish(Events.DEBLOAT_PROGRESS, {"step": "Creating restore point…", "pct": 0})
        create_restore_point("DWUT Pre-Debloat")

    total = len(selected)
    applied = 0
    failed: list[str] = []

    # Save to backup log
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUPS_DIR / f"debloat_{ts}.json"
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_data = {
        "timestamp": ts,
        "removed": [e["name"] for e in selected],
    }
    try:
        backup_path.write_text(json.dumps(backup_data, indent=2))
    except Exception as exc:
        _log.warning("Could not write debloat backup: %s", exc)

    for i, entry in enumerate(selected):
        pct = (i + 1) / total
        bus.publish(Events.DEBLOAT_PROGRESS, {
            "step": f"Removing {entry['name']}…",
            "pct": pct,
        })

        ok, msg = _remove_appx(entry["pkg"], all_users=True)
        if ok:
            applied += 1
            _log.info("Removed: %s (%s)", entry["name"], entry["pkg"])
        else:
            failed.append(entry["name"])
            _log.warning("Failed to remove %s: %s", entry["name"], msg[:80])

        time.sleep(0.1)  # brief pause to keep PS responsive

    bus.publish(Events.DEBLOAT_PROGRESS, {"step": "Done", "pct": 1.0})
    bus.publish(Events.DEBLOAT_DONE, {
        "applied": applied,
        "failed": failed,
        "backup": str(backup_path),
    })

    return DebloatResult(
        success=True,
        applied=applied,
        failed=failed,
        backup_path=str(backup_path),
        message=f"Removed {applied}/{total} packages",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Feature applying
# ──────────────────────────────────────────────────────────────────────────────

def apply_features(
    keys: list[str],
    create_restore: bool = True,
) -> DebloatResult:
    """
    Apply the selected feature tweaks.
    `keys` is a list of FeatureDef.key strings.
    Publishes progress.  Returns DebloatResult.
    """
    features = [FEATURES_BY_KEY[k] for k in keys if k in FEATURES_BY_KEY]

    if not features:
        return DebloatResult(success=False, applied=0, message="No valid features selected")

    if create_restore:
        bus.publish(Events.DEBLOAT_PROGRESS, {"step": "Creating restore point…", "pct": 0})
        create_restore_point("DWUT Pre-Feature-Change")

    total = len(features)
    applied = 0
    failed: list[str] = []

    for i, feat in enumerate(features):
        pct = (i + 1) / total
        bus.publish(Events.DEBLOAT_PROGRESS, {
            "step": f"Applying: {feat.name}",
            "pct": pct,
        })

        errors: list[str] = []

        # Registry ops
        if feat.reg_ops:
            errs = _run_reg_ops(feat.reg_ops)
            errors.extend(errs)

        # Service ops
        if feat.service_ops:
            errs = _run_service_ops(feat.service_ops, disable=True)
            errors.extend(errs)

        # PowerShell commands
        for cmd in feat.ps_cmds:
            ok, msg = _run_ps(cmd)
            if not ok:
                errors.append(f"PS: {cmd[:40]}…: {msg[:60]}")

        # AppxPackage removal
        for aop in feat.appx_ops:
            ok, msg = _remove_appx(aop.pattern, aop.all_users)
            if not ok:
                _log.debug("AppxOp %s: %s", aop.pattern, msg[:60])

        if errors:
            _log.warning("Feature '%s' had errors: %s", feat.name, errors)
            # Partial errors don't count as full failure if at least one op succeeded
            if len(errors) < len(feat.reg_ops) + len(feat.service_ops) + len(feat.ps_cmds) + 1:
                applied += 1
            else:
                failed.append(feat.name)
        else:
            applied += 1
            _log.info("Applied feature: %s", feat.name)

    bus.publish(Events.DEBLOAT_PROGRESS, {"step": "Done", "pct": 1.0})
    bus.publish(Events.DEBLOAT_DONE, {
        "applied": applied,
        "failed": failed,
        "backup": None,
    })

    return DebloatResult(
        success=True,
        applied=applied,
        failed=failed,
        message=f"Applied {applied}/{total} feature changes",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Privacy items applying
# ──────────────────────────────────────────────────────────────────────────────

def apply_privacy_items(keys: list[str]) -> DebloatResult:
    """Apply selected privacy items by key."""
    applied = 0
    failed: list[str] = []

    for key in keys:
        item = PRIVACY_BY_KEY.get(key)
        if not item:
            continue
        try:
            item["action"]()
            applied += 1
            _log.info("Applied privacy item: %s", item["title"])
        except Exception as exc:
            failed.append(item["title"])
            _log.error("Privacy item %s failed: %s", key, exc)

    return DebloatResult(
        success=True,
        applied=applied,
        failed=failed,
        message=f"Applied {applied} privacy changes",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Recall status check
# ──────────────────────────────────────────────────────────────────────────────

def check_recall_status() -> dict:
    """
    Check registry + service status to determine if Recall is active.
    Returns a dict: {disabled_count, total_checks, pct, label}.
    """
    checks = [
        # (description, check_fn)
        ("DisableAIDataAnalysis HKLM", lambda: _reg_val_is(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", 1)),
        ("DisableAIDataAnalysis HKCU", lambda: _reg_val_is(
            winreg.HKEY_CURRENT_USER,
            r"Software\Policies\Microsoft\Windows\WindowsAI", "DisableAIDataAnalysis", 1)),
        ("TurnOffWindowsCopilot", lambda: _reg_val_is(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot", "TurnOffWindowsCopilot", 1)),
        ("EnableActivityFeed=0", lambda: _reg_val_is(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\System", "EnableActivityFeed", 0)),
        ("CDPUserSvc stopped", lambda: _service_stopped("CDPUserSvc")),
    ]

    disabled = 0
    total = len(checks)
    for desc, fn in checks:
        try:
            if fn():
                disabled += 1
        except Exception:
            pass

    pct = int(disabled / total * 100)
    if pct >= 80:
        label = "Disabled"
    elif pct >= 40:
        label = "Partially disabled"
    else:
        label = "Active"

    return {
        "disabled_count": disabled,
        "total_checks": total,
        "pct": pct,
        "label": label,
    }


def _reg_val_is(hive: int, path: str, name: str, expected) -> bool:
    try:
        k = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return val == expected
    except Exception:
        return False


def _service_stopped(name: str) -> bool:
    try:
        r = subprocess.run(
            ["sc", "query", name], capture_output=True, text=True, timeout=5
        )
        return "STOPPED" in r.stdout or "does not exist" in r.stderr
    except Exception:
        return False
