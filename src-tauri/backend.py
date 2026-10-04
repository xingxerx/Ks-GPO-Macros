import os
import sys
import time
import json
import threading
from threading import Lock
import re
import shutil
from datetime import datetime, timezone
import traceback
import logging
import webbrowser
import psutil

from flask import Flask, request, jsonify
from flask_cors import CORS
import numpy as np
import mss
import pyautogui
from pynput import mouse
from PIL import Image as PILImage
import requests
from difflib import get_close_matches, SequenceMatcher
from scipy.fft import fft
import argparse
import socket
import queue
import contextvars
import subprocess

import ctypes

import cv2

import tkinter as tk
from tkinter import filedialog, messagebox

IsMac = sys.platform == 'darwin'

if IsMac:
    import sounddevice
    import Quartz
    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    from ApplicationServices import AXIsProcessTrusted
    from pynput import keyboard as PynputKeyboard
    from mss.screenshot import ScreenShot
    from mss.models import Size
else:
    import keyboard
    import pyaudiowpatch as pyaudio
    import win32gui
    import win32con
    import win32api


# ---- Platform layer: everything below the macro logic that differs between Windows and macOS ----

if IsMac:
    class MacKeyboard:
        # Stands in for the `keyboard` package, which needs root on macOS; pynput only needs the Accessibility and
        # Input Monitoring permissions. Covers just the calls this file makes, with the same key names
        Aliases = {'control': 'ctrl', 'option': 'alt', 'command': 'cmd', 'windows': 'cmd', 'return': 'enter',
                   'escape': 'esc', 'del': 'delete', 'spacebar': 'space'}

        def __init__(self):
            self.Controller = PynputKeyboard.Controller()
            self.Hotkeys = {}
            self.ReleaseHandlers = []
            self.Pressed = set()
            self.Listener = None
            self.Lock = threading.Lock()

        def Normalize(self, Name):
            Name = Name.strip().lower()
            return self.Aliases.get(Name, Name)

        def ToKey(self, Name):
            Name = self.Normalize(Name)
            if len(Name) == 1:
                return PynputKeyboard.KeyCode.from_char(Name)
            Key = getattr(PynputKeyboard.Key, Name, None)
            if Key is None:
                raise ValueError(f"Unknown key: {Name}")
            return Key

        @staticmethod
        def NameOf(Key):
            # Left/right variants (shift_r, cmd_l) count as the plain key, like the `keyboard` package
            if isinstance(Key, PynputKeyboard.Key):
                return re.sub(r'_[lr]$', '', Key.name)
            return Key.char.lower() if getattr(Key, 'char', None) else None

        def EnsureListener(self):
            if self.Listener is None:
                self.Listener = PynputKeyboard.Listener(on_press=self.OnPress, on_release=self.OnRelease)
                self.Listener.daemon = True
                self.Listener.start()

        def OnPress(self, Key):
            Name = self.NameOf(Key)
            if Name is None:
                return
            with self.Lock:
                IsRepeat = Name in self.Pressed
                self.Pressed.add(Name)
                Matches = [] if IsRepeat else [Cb for Combo, Cb in self.Hotkeys.items() if Combo == self.Pressed]
            # Callbacks can block (ToggleMacro waits for the old loop), and a blocked listener stalls all input
            for Callback in Matches:
                threading.Thread(target=Callback, daemon=True).start()

        def OnRelease(self, Key):
            Name = self.NameOf(Key)
            if Name is None:
                return
            with self.Lock:
                self.Pressed.discard(Name)
                Handlers = list(self.ReleaseHandlers)
            Event = type('KeyEvent', (), {'name': Name})()
            for Handler in Handlers:
                Handler(Event)

        def add_hotkey(self, Combo, Callback):
            Keys = frozenset(self.Normalize(Part) for Part in Combo.split('+'))
            with self.Lock:
                self.Hotkeys[Keys] = Callback
            self.EnsureListener()

        def on_release(self, Callback, suppress=False):
            with self.Lock:
                self.ReleaseHandlers.append(Callback)
            self.EnsureListener()

        def unhook_all_hotkeys(self):
            with self.Lock:
                self.Hotkeys.clear()

        def unhook_all(self):
            with self.Lock:
                self.Hotkeys.clear()
                self.ReleaseHandlers.clear()

        def press(self, Name):
            self.Controller.press(self.ToKey(Name))

        def release(self, Name):
            self.Controller.release(self.ToKey(Name))

        def press_and_release(self, Name):
            self.press(Name)
            self.release(Name)

        def write(self, Text):
            self.Controller.type(Text)

    keyboard = MacKeyboard()


def SetCursorPos(X, Y):
    if IsMac:
        # A plain move while the button is held is ignored by games, so send a drag then (the fruit store drags)
        Held = Quartz.CGEventSourceButtonState(Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGMouseButtonLeft)
        EventType = Quartz.kCGEventLeftMouseDragged if Held else Quartz.kCGEventMouseMoved
        Event = Quartz.CGEventCreateMouseEvent(None, EventType, (X, Y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Event)
    else:
        ctypes.windll.user32.SetCursorPos(X, Y)


def NudgeMouse(Dy):
    # A relative move of a pixel or so, so the game sees real mouse movement and not just a teleported cursor
    if IsMac:
        Location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        SetCursorPos(Location.x, Location.y + Dy)
    else:
        ctypes.windll.user32.mouse_event(0x0001, 0, Dy, 0, 0)


def GetScreenSize():
    if IsMac:
        # Points, the same space as mouse coordinates and (via NewScreenCapture) captured images
        return pyautogui.size()
    DisplayMetrics = ctypes.windll.user32
    return DisplayMetrics.GetSystemMetrics(0), DisplayMetrics.GetSystemMetrics(1)


def SetHighPriority(High):
    if IsMac:
        # Raising priority needs root on macOS, so leave the scheduler alone
        return True
    kernel32 = ctypes.windll.kernel32
    Handle = kernel32.OpenProcess(0x0200, False, os.getpid())
    if not Handle:
        print("Failed to open process handle")
        return False
    # HIGH, not REALTIME: a realtime busy loop (the minigame) can starve Windows input handling as admin
    Result = kernel32.SetPriorityClass(Handle, 0x00000080 if High else 0x00000020)
    kernel32.CloseHandle(Handle)
    if not Result:
        print(f"Failed to set priority. Error: {ctypes.get_last_error()}")
    return bool(Result)


def HasInputPermission():
    # Windows needs admin to send input to an elevated Roblox; macOS needs Accessibility to send input and Input
    # Monitoring to hear the hotkeys instead
    try:
        if IsMac:
            return bool(AXIsProcessTrusted()) and bool(Quartz.CGPreflightListenEventAccess())
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def OpenInFileManager(Path):
    if IsMac:
        subprocess.Popen(['open', Path])
    else:
        os.startfile(Path)


def GetDataDir():
    # Settings, port files and debug output. On macOS the launcher points this at Application Support, since the
    # .app bundle holding backend.pyc can be read-only
    Override = os.environ.get('GPO_DATA_DIR')
    if Override:
        return Override
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class RetinaSafeCapture:
    # On Retina screens mss returns 2x the requested size, while regions and click points (pyautogui, pynput,
    # tkinter) are in points, and every detector here assumes one pixel per point. Sampling back down with
    # nearest-neighbour keeps colours exact, which the exact-match colour masks rely on
    def __init__(self, Inner):
        self.Inner = Inner

    def __getattr__(self, Name):
        return getattr(self.Inner, Name)

    def __enter__(self):
        return self

    def __exit__(self, *Args):
        self.Inner.close()

    def grab(self, Monitor):
        Shot = self.Inner.grab(Monitor)
        if isinstance(Monitor, tuple):
            Monitor = {'left': Monitor[0], 'top': Monitor[1], 'width': Monitor[2] - Monitor[0], 'height': Monitor[3] - Monitor[1]}
        Width, Height = Monitor['width'], Monitor['height']
        if (Shot.width, Shot.height) == (Width, Height):
            return Shot
        Pixels = np.frombuffer(Shot.raw, dtype=np.uint8).reshape(Shot.height, Shot.width, 4)
        Rows = np.arange(Height) * Shot.height // Height
        Cols = np.arange(Width) * Shot.width // Width
        Pixels = np.ascontiguousarray(Pixels[Rows][:, Cols])
        return ScreenShot(bytearray(Pixels.tobytes()), Monitor, size=Size(Width, Height))


def NewScreenCapture():
    return RetinaSafeCapture(mss.mss()) if IsMac else mss.mss()


MainThreadTasks = queue.Queue()


def RunOnMainThread(Function):
    # macOS only lets the main thread create windows, so tkinter calls from request/worker threads are queued for
    # the main thread (see __main__) and waited on. The caller's context goes along so Flask's jsonify still works
    if not IsMac or threading.current_thread() is threading.main_thread():
        return Function()
    Context = contextvars.copy_context()
    Done = threading.Event()
    Outcome = {}

    def Task():
        try:
            Outcome['Result'] = Context.run(Function)
        except BaseException as E:
            Outcome['Error'] = E
        finally:
            Done.set()

    MainThreadTasks.put(Task)
    Done.wait()
    if 'Error' in Outcome:
        raise Outcome['Error']
    return Outcome.get('Result')


def PumpCocoaEvents(Timeout):
    # Once tkinter has started the macOS app, its events must keep being handled after the Tk window closes: until
    # they are, a destroyed window stays on screen frozen and macOS shows the spinning cursor over it
    from AppKit import NSApplication, NSDate, NSDefaultRunLoopMode
    App = NSApplication.sharedApplication()
    Until = NSDate.dateWithTimeIntervalSinceNow_(Timeout)
    while True:
        Event = App.nextEventMatchingMask_untilDate_inMode_dequeue_(0xFFFFFFFFFFFFFFFF, Until, NSDefaultRunLoopMode, True)
        if Event is None:
            return
        App.sendEvent_(Event)
        Until = NSDate.date()


def FloatOverFullscreen(Root):
    # A Tk window opens on the desktop Space, under the always-on-top app window, so it's hidden behind a full-screen
    # Roblox. Joining every Space as a full-screen auxiliary, above the status level, puts it over the game; macOS only
    # allows that for accessory apps, which the backend should be anyway (no Dock icon for it)
    if not IsMac:
        return
    try:
        from AppKit import NSApp
        NSApp.setActivationPolicy_(1)
        Root.update_idletasks()
        for Window in NSApp.windows():
            if Window.isVisible():
                Window.setCollectionBehavior_(Window.collectionBehavior() | (1 << 0) | (1 << 8))
                Window.setLevel_(25)
        NSApp.activateIgnoringOtherApps_(True)
    except Exception as E:
        print(f"FloatOverFullscreen failed: {E}")


def OnMainThread(Function):
    def Wrapper(*Args, **Kwargs):
        return RunOnMainThread(lambda: Function(*Args, **Kwargs))
    Wrapper.__name__ = Function.__name__
    return Wrapper

LogDir = os.path.join(os.getcwd(), 'logs')
VisionDir = os.path.join(LogDir, 'vision')
os.makedirs(VisionDir, exist_ok=True)


class TeeStream:
    # Mirrors console output into logs/backend.txt so failures can be read after the fact
    def __init__(self, Stream, File):
        self.Stream = Stream
        self.File = File

    def write(self, Data):
        try:
            if self.Stream:
                self.Stream.write(Data)
            self.File.write(Data)
            self.File.flush()
        except Exception:
            pass

    def flush(self):
        try:
            if self.Stream:
                self.Stream.flush()
        except Exception:
            pass


_LogFile = open(os.path.join(LogDir, 'backend.txt'), 'w', encoding='utf-8', errors='replace')
sys.stdout = TeeStream(sys.stdout, _LogFile)
sys.stderr = TeeStream(sys.stderr, _LogFile)


def LogLine(Message):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {Message}")


def FindFreePort(Start=8765, MaxAttempts=50):
    for Port in range(Start, Start + MaxAttempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as S:
            try:
                S.bind(('127.0.0.1', Port))
                return Port
            except OSError:
                continue
    raise RuntimeError("No free port found in range")

def CleanupOrphanedPortFiles(AppPath):
    for FileName in os.listdir(AppPath):
        if FileName.startswith('port_') and FileName.endswith('.json'):
            try:
                Pid = int(FileName.replace('port_', '').replace('.json', ''))
                if not psutil.pid_exists(Pid):
                    os.remove(os.path.join(AppPath, FileName))
            except (ValueError, OSError):
                pass

ArgParser = argparse.ArgumentParser()
ArgParser.add_argument('--pid', type=str, default='unknown')
ParsedArgs, _ = ArgParser.parse_known_args()
LauncherPid = ParsedArgs.pid

class ConfigurationManager:

    def __init__(self, ConfigPath):
        self.ConfigPath = ConfigPath
        self.Settings = self.InitializeDefaults()
    
    def InitializeDefaults(self):
        MonitorWidth, MonitorHeight = GetScreenSize()
        
        return {
            'Hotkeys': {'StartStop': 'f1', 'Exit': 'f3'},
            'WindowSettings': {'AlwaysOnTop': True, 'ShowDebugOverlay': False},
            'ScanArea': {
                'X1': int(MonitorWidth * 0.52461),
                'Y1': int(MonitorHeight * 0.29167),
                'X2': int(MonitorWidth * 0.68477),
                'Y2': int(MonitorHeight * 0.79097)
            },
            'ClickPoints': {
                'Water': None,
                'ShopLeft': None,
                'ShopCenter': None,
                'ShopRight': None,
                'Bait': None,
                'StoreFruit': None,
                'BackpackLocations': [],
            },
            'InventoryHotkeys': {
                'Rod': '1',
                'Alternate': '2',
                'DevilFruits': ['3'],
            },
            'AutomationFeatures': {
                'AutoBuyBait': False,
                'AutoStoreFruit': False,
                # Only run the store routine after the catch-time OCR spots a fruit (plus a periodic safety sweep)
                'StoreOnlyWhenDetected': True,
                'AutoSelectTopBait': False,
                'SmartBaitSelect': False,
            },
            'AutomationFrequencies': {
                'LoopsPerTopBait': 1,
                'LoopsPerPurchase': 100,
                'LoopsPerStore': 50,
                'FruitSweepLoops': 25,
            },
            'DevilFruitStorage': {
                'StoreToBackpack': False,
                # Off: a fruit the game refuses to store (a duplicate) stays in the hotbar instead of being dropped
                'DropUnstorable': False,
                'WebhookUrl': ''
            },
            'LoggingOptions': {
                'DiscordUserId': '',
                'LogDevilFruit': False,
                'PingDevilFruit': False,
                'LogRecastTimeouts': True,
                'PingRecastTimeouts': False,
                'LogPeriodicStats': True,
                'PingPeriodicStats': False,
                'LogGeneralUpdates': True,
                'PingGeneralUpdates': False,
                'LogMacroState': False,
                'PingMacroState': False,
                'LogErrors': True,
                'PingErrors': False,
                'PeriodicStatsIntervalMinutes': 5
            },
            'FishingModes': {
                'MegalodonSound': False,
                'SoundSensitivity': 0.1
            },
            'AudioDevice': {
                'SelectedDeviceIndex': None,
                'DeviceName': ''
            },
            'FishingControl': {
                'PdController': {
                    'Kp': 1.0,
                    'Kd': 0.6,
                    'PdClamp': 1.0,
                    'PdApproachingDamping': 2.0,
                    'PdChasingDamping': 0.5
                },
                'Timing': {
                    'CastHoldDuration': 0.1,
                    'RecastTimeout': 25.0,
                    'FishEndDelay': 0.5,
                    'CatchEndGrace': 0.25,
                    'StateResendInterval': 0.5
                },
                'Detection': {
                    'GapToleranceMultiplier': 2.0,
                    'BlackScreenThreshold': 0.5,
                    'ScanLoopDelay': 0.1
                }
            },
            'TimingDelays': {
                'RobloxWindow': {
                    'RobloxFocusDelay': 0.2,
                    'RobloxPostFocusDelay': 0.2
                },
                'PreCast': {
                    'SetPrecastEDelay': 1.25,
                    'PreCastClickDelay': 0.5,
                    'PreCastTypeDelay': 0.25,
                    'PreCastAntiDetectDelay': 0.05
                },
                'Inventory': {
                    'RodSelectDelay': 0.2,
                    'AutoSelectBaitDelay': 0.5
                },
                'DevilFruitStorage': {
                    'StoreFruitHotkeyDelay': 0.2,
                    'StoreFruitClickDelay': 0.25,
                    'StoreFruitShiftDelay': 0.35,
                    'StoreFruitBackspaceDelay': 0.2
                },
                'AntiDetection': {
                    'CursorAntiDetectDelay': 0.05,
                    'AntiMacroSpamDelay': 0.05
                },
            },
            'OCRSettings': {
                'X1': int(MonitorWidth * 0.40),
                'Y1': 60,
                'X2': int(MonitorWidth * 0.60),
                'Y2': int(MonitorHeight * 0.20)
            },
            'BaitSelector': {
                # Used best first. Devil fruits don't come from common bait, so it goes last; Auto Buy
                # restocks the shop bait (ShopBaitName) once it runs out
                'TierOrder': ['Rare Fish Bait', 'Legendary Fish Bait', 'Common Fish Bait'],
                'Region': {
                    'X1': int(MonitorWidth * 0.40),
                    'Y1': int(MonitorHeight * 0.67),
                    'X2': int(MonitorWidth * 0.60),
                    'Y2': int(MonitorHeight * 0.84)
                }
            },
        }
    
    def LoadFromDisk(self):
        if not os.path.exists(self.ConfigPath):
            print(f"No configuration file found at {self.ConfigPath}. Creating new one with defaults.")
            self.SaveToDisk()
            return
        
        try:
            with open(self.ConfigPath, 'r', encoding='utf-8') as ConfigFile:
                FileContent = ConfigFile.read().strip()
            
            if not FileContent:
                print(f"Configuration file at {self.ConfigPath} is empty. Initializing with defaults.")
                self.SaveToDisk()
                return
            
            try:
                ParsedData = json.loads(FileContent)
            except json.JSONDecodeError as JsonError:
                print(f"Configuration file corrupted: {JsonError}")
                print(f"File location: {self.ConfigPath}")
                print("Using defaults. Old file will be backed up.")
                
                try:
                    BackupPath = self.ConfigPath + f".backup_{int(time.time())}"
                    os.rename(self.ConfigPath, BackupPath)
                    print(f"Backup created at: {BackupPath}")
                except Exception as BackupError:
                    print(f"Could not create backup: {BackupError}")
                
                self.SaveToDisk()
                return
            
            self._MergeSettings(ParsedData)
            print(f"Configuration loaded successfully from {self.ConfigPath}")
            
        except Exception as LoadError:
            print(f"Error loading configuration: {LoadError}")
            print(f"File location: {self.ConfigPath}")
            traceback.print_exc()
            print("Using default values.")
    
    def _MergeSettings(self, LoadedData):
        if "Hotkeys" in LoadedData:
            self.Settings['Hotkeys'].update(LoadedData["Hotkeys"])
        
        if "WindowSettings" in LoadedData:
            self.Settings['WindowSettings'].update(LoadedData["WindowSettings"])
        
        if "ScanArea" in LoadedData:
            self.Settings['ScanArea'].update(LoadedData["ScanArea"])
        
        if "ClickPoints" in LoadedData:
            ClickPoints = LoadedData["ClickPoints"]
            self.Settings['ClickPoints']['Water'] = ClickPoints.get("WaterPoint", None)
            self.Settings['ClickPoints']['Bait'] = ClickPoints.get("BaitPoint", None)
            
            if "Shop" in ClickPoints:
                Shop = ClickPoints["Shop"]
                self.Settings['ClickPoints']['ShopLeft'] = Shop.get("LeftPoint", None)
                self.Settings['ClickPoints']['ShopCenter'] = Shop.get("MiddlePoint", None)
                self.Settings['ClickPoints']['ShopRight'] = Shop.get("RightPoint", None)
            
            if "DevilFruit" in ClickPoints:
                Fruit = ClickPoints["DevilFruit"]
                self.Settings['ClickPoints']['StoreFruit'] = Fruit.get("StoreFruitPoint", None)
                self.Settings['ClickPoints']['BackpackLocations'] = Fruit.get("BackpackLocations", [])

        if "InventoryHotkeys" in LoadedData:
            Inv = LoadedData["InventoryHotkeys"]
            self.Settings['InventoryHotkeys']['Rod'] = Inv.get("RodHotkey", '1')
            self.Settings['InventoryHotkeys']['Alternate'] = Inv.get("AnythingElseHotkey", '2')
            self.Settings['InventoryHotkeys']['DevilFruits'] = Inv.get("DevilFruitHotkeys", ['3'])

        if "AutomationFeatures" in LoadedData:
            Auto = LoadedData["AutomationFeatures"]
            self.Settings['AutomationFeatures']['AutoBuyBait'] = Auto.get("AutoBuyCommonBait", False)
            self.Settings['AutomationFeatures']['AutoStoreFruit'] = Auto.get("AutoStoreDevilFruit", False)
            self.Settings['AutomationFeatures']['StoreOnlyWhenDetected'] = Auto.get("StoreOnlyWhenDetected", True)
            self.Settings['AutomationFeatures']['AutoSelectTopBait'] = Auto.get("AutoSelectTopBait", False)
            self.Settings['AutomationFeatures']['SmartBaitSelect'] = Auto.get("SmartBaitSelect", False)

        if "AutomationFrequencies" in LoadedData:
            Freq = LoadedData["AutomationFrequencies"]
            self.Settings['AutomationFrequencies']['LoopsPerTopBait'] = Freq.get("LoopsPerTopBait", 1)
            self.Settings['AutomationFrequencies']['LoopsPerPurchase'] = Freq.get("LoopsPerPurchase", 100)
            self.Settings['AutomationFrequencies']['LoopsPerStore'] = Freq.get("LoopsPerStore", 50)
            self.Settings['AutomationFrequencies']['FruitSweepLoops'] = Freq.get("FruitSweepLoops", 25)

        if "DevilFruitStorage" in LoadedData:
            Df = LoadedData["DevilFruitStorage"]
            self.Settings['DevilFruitStorage']['StoreToBackpack'] = Df.get("StoreToBackpack", False)
            self.Settings['DevilFruitStorage']['DropUnstorable'] = Df.get("DropUnstorable", False)
            self.Settings['DevilFruitStorage']['WebhookUrl'] = Df.get("WebhookUrl", '')
        
        if "LoggingOptions" in LoadedData:
            Log = LoadedData["LoggingOptions"]
            self.Settings['LoggingOptions'].update({
                'DiscordUserId': Log.get("DiscordUserId", ""),
                'LogDevilFruit': Log.get("LogDevilFruit", False),
                'PingDevilFruit': Log.get("PingDevilFruit", False),
                'LogRecastTimeouts': Log.get("LogRecastTimeouts", True),
                'PingRecastTimeouts': Log.get("PingRecastTimeouts", False),
                'LogPeriodicStats': Log.get("LogPeriodicStats", True),
                'PingPeriodicStats': Log.get("PingPeriodicStats", False),
                'LogGeneralUpdates': Log.get("LogGeneralUpdates", True),
                'PingGeneralUpdates': Log.get("PingGeneralUpdates", False),
                'PeriodicStatsIntervalMinutes': Log.get("PeriodicStatsIntervalMinutes", 5),
                'LogMacroState': Log.get("LogMacroState", False),
                'PingMacroState': Log.get("PingMacroState", False),
                'LogErrors': Log.get("LogErrors", True),
                'PingErrors': Log.get("PingErrors", False)
            })

        if "FishingModes" in LoadedData:
            Modes = LoadedData["FishingModes"]
            self.Settings['FishingModes']['MegalodonSound'] = Modes.get("MegalodonSound", False)
            self.Settings['FishingModes']['SoundSensitivity'] = Modes.get("SoundSensitivity", 0.1)

        if "AudioDevice" in LoadedData:
            self.Settings['AudioDevice'].update(LoadedData["AudioDevice"])

        if "FishingControl" in LoadedData:
            Control = LoadedData["FishingControl"]
            if "PdController" in Control:
                self.Settings['FishingControl']['PdController'].update(Control["PdController"])
            if "Timing" in Control:
                self.Settings['FishingControl']['Timing'].update(Control["Timing"])
            if "Detection" in Control:
                self.Settings['FishingControl']['Detection'].update(Control["Detection"])
        
        if "TimingDelays" in LoadedData:
            Timing = LoadedData["TimingDelays"]
            for Category in ['RobloxWindow', 'PreCast', 'Inventory', 'DevilFruitStorage', 'AntiDetection']:
                if Category in Timing:
                    self.Settings['TimingDelays'][Category].update(Timing[Category])
        
        if "OCRSettings" in LoadedData:
            self.Settings['OCRSettings'].update(LoadedData["OCRSettings"])

        if "BaitSelector" in LoadedData:
            Selector = LoadedData["BaitSelector"]
            if Selector.get("TierOrder"):
                self.Settings['BaitSelector']['TierOrder'] = Selector["TierOrder"]
            if "Region" in Selector:
                self.Settings['BaitSelector']['Region'].update(Selector["Region"])
    
    def SaveToDisk(self):
        try:
            OutputData = {
                "Hotkeys": self.Settings['Hotkeys'],
                "WindowSettings": self.Settings['WindowSettings'],
                "ScanArea": self.Settings['ScanArea'],
                "ClickPoints": {
                    "WaterPoint": self.Settings['ClickPoints']['Water'],
                    "Shop": {
                        "LeftPoint": self.Settings['ClickPoints']['ShopLeft'],
                        "MiddlePoint": self.Settings['ClickPoints']['ShopCenter'],
                        "RightPoint": self.Settings['ClickPoints']['ShopRight']
                    },
                    "BaitPoint": self.Settings['ClickPoints']['Bait'],
                    "DevilFruit": {
                        "StoreFruitPoint": self.Settings['ClickPoints']['StoreFruit'],
                        "BackpackLocations": self.Settings['ClickPoints']['BackpackLocations'],
                    },
                },
                "InventoryHotkeys": {
                    "RodHotkey": self.Settings['InventoryHotkeys']['Rod'],
                    "AnythingElseHotkey": self.Settings['InventoryHotkeys']['Alternate'],
                    "DevilFruitHotkeys": self.Settings['InventoryHotkeys']['DevilFruits']
                },
                "AutomationFeatures": {
                    "AutoBuyCommonBait": self.Settings['AutomationFeatures']['AutoBuyBait'],
                    "AutoStoreDevilFruit": self.Settings['AutomationFeatures']['AutoStoreFruit'],
                    "StoreOnlyWhenDetected": self.Settings['AutomationFeatures']['StoreOnlyWhenDetected'],
                    "AutoSelectTopBait": self.Settings['AutomationFeatures']['AutoSelectTopBait'],
                    "SmartBaitSelect": self.Settings['AutomationFeatures']['SmartBaitSelect'],
                },
                "AutomationFrequencies": self.Settings['AutomationFrequencies'],
                "DevilFruitStorage": self.Settings['DevilFruitStorage'],
                "LoggingOptions": self.Settings['LoggingOptions'],
                "FishingModes": self.Settings['FishingModes'],
                "AudioDevice": self.Settings['AudioDevice'],
                "FishingControl": self.Settings['FishingControl'],
                "TimingDelays": self.Settings['TimingDelays'],
                "OCRSettings": self.Settings['OCRSettings'],
                "BaitSelector": self.Settings['BaitSelector'],
            }

            # Write-then-rename so a crash mid-save can't leave a truncated settings file
            TempPath = self.ConfigPath + ".tmp"
            with open(TempPath, 'w', encoding='utf-8') as ConfigFile:
                json.dump(OutputData, ConfigFile, indent=4)
            os.replace(TempPath, self.ConfigPath)

        except Exception as SaveError:
            print(f"Error saving settings: {SaveError}")


class MacroStateManager:
    
    def __init__(self):
        self.IsRunning = False
        self.CurrentStatus = "Idle"

        self.TotalFishCaught = 0
        self.TotalDevilFruits = 0
        self.DevilFruitsByRarity = dict.fromkeys(FruitRarityOrder + ["Unknown"], 0)
        self.LastDevilFruit = None
        # Every fruit counted this session, newest last, for the dashboard history
        self.FruitHistory = []
        # Catch scans run on their own threads; counting under a lock keeps two scans of one popup from both counting
        self.FruitLock = Lock()
        # Set when the catch-time scan sees a fruit; the next pre-cast stores it
        self.FruitPendingStore = False
        self.CumulativeUptime = 0
        self.SessionStartTime = None
        self.LastFishCaptureTime = None

        self.TotalRecastTimeouts = 0
        self.ConsecutiveRecastTimeouts = 0
        self.LastPeriodicStatsTime = None
        self.FishAtLastStats = 0

        self.BaitPurchaseCounter = 0
        self.FruitStorageCounter = 0
        self.TopBaitCounter = 0

        # Bumped on every start; a macro loop exits once its token is stale
        self.LoopId = 0
        # Bumped on every catch; an older catch scan still running stops so only one OCR scan competes for CPU
        self.FruitScanId = 0
        # The last counted drop banner, to tell a lingering banner from a new drop
        self.LastFruitDrop = None
        # (name, time) of the last "New Item <Fruit>" applied to the history, since that popup lingers across scans
        self.LastNamedFruit = (None, 0)
        # Hotbar slots whose fruit the game refused to store (duplicates); skipped until the macro restarts
        self.UnstorableSlots = set()

        # Smart bait tracking: remaining is decremented per cast and triggers a rescan at zero
        self.SelectedBait = None
        self.BaitRemaining = None
        self.SelectedBaitPoint = None
        self.BaitRescanNeeded = True
        self.LastBaitPurchaseTime = 0
        
        self.RobloxWindowFocused = False
        self.FastModeEnabled = False
        self.MousePressed = False
        self.RodEquipped = False

        self.PreviousError = None
        self.PreviousTargetY = None
        self.LastScanTime = time.time()
        self.LastStateChangeTime = time.time()
        self.LastInputResendTime = time.time()

    def UpdateStatus(self, Status):
        if Status != self.CurrentStatus:
            LogLine(f"STATUS {Status}")
        self.CurrentStatus = Status

    def IncrementFishCount(self):
        self.TotalFishCaught += 1
        self.LastFishCaptureTime = time.time()

    def IncrementDevilFruitCount(self, FruitName=None, Rarity=None, Pity=None):
        Rarity = Rarity or FruitRarities.get(FruitName, "Unknown")
        self.TotalDevilFruits += 1
        self.DevilFruitsByRarity[Rarity] = self.DevilFruitsByRarity.get(Rarity, 0) + 1
        self.FruitHistory.append({
            "name": FruitName or "Unknown",
            "rarity": Rarity,
            "pity": Pity,
            "time": datetime.now().strftime('%H:%M:%S'),
            "timestamp": time.time(),
            "catch": self.TotalFishCaught,
        })
        del self.FruitHistory[:-50]
        self.LastDevilFruit = self.FruitHistory[-1]
        return Rarity

    def UpdateFruit(self, Entry, Name=None, Rarity=None):
        # Fill in a counted drop once more is known (its name from a later popup, or a pity reset read late),
        # moving it between rarity buckets without changing the total
        if Rarity and Rarity != Entry['rarity']:
            self.DevilFruitsByRarity[Entry['rarity']] = max(self.DevilFruitsByRarity.get(Entry['rarity'], 0) - 1, 0)
            self.DevilFruitsByRarity[Rarity] = self.DevilFruitsByRarity.get(Rarity, 0) + 1
            Entry['rarity'] = Rarity
        if Name:
            Entry['name'] = Name

    def ResetDevilFruitCounts(self):
        self.TotalDevilFruits = 0
        self.DevilFruitsByRarity = dict.fromkeys(self.DevilFruitsByRarity, 0)
        self.LastDevilFruit = None
        self.FruitHistory = []
        self.FruitPendingStore = False
        self.LastFruitDrop = None
        self.LastNamedFruit = (None, 0)
    
    def HandleRecastTimeout(self):
        self.TotalRecastTimeouts += 1
        self.ConsecutiveRecastTimeouts += 1
    
    def ResetConsecutiveTimeouts(self):
        self.ConsecutiveRecastTimeouts = 0
    
    def GetElapsedTime(self):
        Accumulated = self.CumulativeUptime
        if self.SessionStartTime:
            Accumulated += time.time() - self.SessionStartTime
        return Accumulated
    
    def GetFormattedElapsedTime(self):
        Total = self.GetElapsedTime()
        Hours = int(Total // 3600)
        Minutes = int((Total % 3600) // 60)
        Seconds = int(Total % 60)
        return f"{Hours}:{Minutes:02d}:{Seconds:02d}"
    
    def GetFishPerHour(self):
        Elapsed = self.GetElapsedTime()
        if Elapsed > 0:
            return (self.TotalFishCaught / Elapsed) * 3600
        return 0.0


class OCRManager:
    
    def __init__(self):
        self.Reader = None
        self.Enabled = True
        self.Loading = False
        # Catch scans and the bait scan run on different threads, so serialize readtext calls on the shared reader
        self.Lock = Lock()

    def Initialize(self):
        # Called from the startup warm-up timer and lazily by scans; only ever start one loader
        with self.Lock:
            if self.Loading:
                return
            self.Loading = self.Reader is None and self.Enabled
        if self.Loading:
            try:
                def LoadOCR():
                    try:
                        import easyocr
                        self.Reader = easyocr.Reader(
                            ['en'],
                            gpu=True,
                            verbose=False,
                            recognizer='standard',
                        )
                    except Exception as E:
                        print(f"OCR Initialization Error: {E}")
                        self.Enabled = False
                    finally:
                        # Lets a later Initialize retry, e.g. after fast mode turned OCR back on
                        self.Loading = False

                threading.Thread(target=LoadOCR, daemon=True).start()
            except Exception as E:
                print(f"OCR Thread Error: {E}")
                self.Enabled = False
                self.Loading = False

    def IsReady(self):
        return self.Enabled and self.Reader is not None

    def WaitForInitialization(self, TimeoutSeconds=30):
        # A slow first load (model download) must not switch OCR off for the whole session; just skip this scan
        StartTime = time.time()
        while self.Reader is None and self.Enabled and (time.time() - StartTime) < TimeoutSeconds:
            time.sleep(0.5)
        return self.Reader is not None


FruitRarityOrder = ["Common", "Rare", "Epic", "Legendary", "Mythical"]

FruitRarities = {
    **dict.fromkeys(["Suke", "Kilo", "Spin", "Heal"], "Common"),
    **dict.fromkeys(["Bari", "Mero", "Horo", "Gomu", "Bomu"], "Rare"),
    **dict.fromkeys(["Yomi", "Spring", "Kira"], "Epic"),
    **dict.fromkeys(["Mera", "Pika", "Hie", "Magu", "Goro", "Gura", "Zushi", "Suna", "Ito",
                     "Paw", "Yuki", "Kage", "Yami", "Goru", "Smoke", "Biscuit"], "Legendary"),
    **dict.fromkeys(["Tori", "Mochi", "Ope", "Venom", "Buddha", "Pteranodon",
                     "Dragon", "Soul", "Leopard"], "Mythical"),
}


class DevilFruitDetector:
    # Reads GPO's on-screen notices after a catch. A fruit drop shows
    #   "All Seeing Eye: YOU GOT A DEVIL FRUIT DROP, CHECK YOUR BACKPACK!" / "LEGENDARY PITY: 10/40"
    # and the fruit goes straight to the backpack. That banner is the only thing counted. "New Item <Name>" is shown
    # for fish too (and for fruits when they are stored), so it only ever supplies a name for a counted drop

    ItemPattern = re.compile(r'\b(?:new|nev|ncv|ncw|naw|ner)\s*[:;,.]?\s*(?:item|ltem|itcm|ltcm|iten|lten)\b(.*)', re.IGNORECASE)
    # "12/40", "4/4O", "2/L0", "9/O"; OCR often reads the colon after PITY as a symbol
    PityPattern = re.compile(r'P[A-Za-z]{0,3}Y[^A-Za-z0-9]{0,3}(\d{1,2})\s*/\s*[4AL]?\s*[0Oo]\b')
    # A reset reads worst ("0lo", "0Lo", "040"), and it's the one that tells a legendary drop apart
    PityZeroPattern = re.compile(r'P[A-Za-z]{0,3}Y[^A-Za-z0-9]{0,3}0\s*[/lLI]?\s*[4AL]?[0Oo]\b')

    def __init__(self, OcrManager, Config):
        self.OcrManager = OcrManager
        self.Config = Config
        self.FruitNames = {Name.lower(): Name for Name in FruitRarities}
        # Raw OCR text and capture from the last ReadRegion call, for diagnosing missed fruits
        self.LastRawText = ""
        self.LastScanImage = None

    def ReadRegion(self):
        # OCR the Fruit Detection Area; returns the text, or None when OCR is unavailable
        if self.OcrManager.Reader is None:
            if not self.OcrManager.Enabled:
                return None
            self.OcrManager.Initialize()
            if not self.OcrManager.WaitForInitialization():
                return None

        Region = self.Config.Settings['OCRSettings']
        ScanRegion = {
            "top": Region['Y1'],
            "left": Region['X1'],
            "width": Region['X2'] - Region['X1'],
            "height": Region['Y2'] - Region['Y1']
        }

        with NewScreenCapture() as ScreenCapture:
            Image = np.array(ScreenCapture.grab(ScanRegion))

        ImageRGB = Image[:, :, [2, 1, 0]]
        Upscaled = cv2.resize(ImageRGB, None, fx=3, fy=3, interpolation=cv2.INTER_LANCZOS4)
        # Brightest channel instead of luminance, so rarity-coloured text (red, purple, blue) survives the threshold
        Gray = Upscaled.max(axis=2).astype(np.uint8)
        _, WhiteOnly = cv2.threshold(Gray, 180, 255, cv2.THRESH_BINARY)
        Dilated = cv2.dilate(WhiteOnly, np.ones((2, 2), np.uint8), iterations=1)
        ProcessedImage = cv2.cvtColor(Dilated, cv2.COLOR_GRAY2RGB)

        with self.OcrManager.Lock:
            Results = self.OcrManager.Reader.readtext(
                ProcessedImage,
                detail=1,
                paragraph=True,
                text_threshold=0.6,
                contrast_ths=0.1,
                adjust_contrast=0.8,
                blocklist='@#$%^&*()+=[]{}|\\~`',
            )

        # paragraph=True makes easyocr return (box, text) without a confidence score
        FullText = " ".join(R[1] for R in Results if len(R) < 3 or R[2] > 0.4).strip()
        self.LastRawText = FullText
        self.LastScanImage = ImageRGB
        return FullText

    def ParseNotices(self, FullText):
        # Returns {'Drop': bool, 'Pity': int or None, 'ItemFruit': fruit name or None}
        Drop, Pity = self.MatchDropBanner(FullText)
        ItemMatch = self.ItemPattern.search(FullText)
        return {
            'Drop': Drop,
            'Pity': Pity,
            'ItemFruit': self.MatchItemName(ItemMatch.group(1)) if ItemMatch else None,
        }

    def MatchDropBanner(self, FullText):
        # Returns (Found, Pity). OCR garbles the banner heavily ("YOUGOTADEWL ERWT DROP; CHCKYoUR BACKACk"), so
        # look for DROP preceded by letters resembling DEVILFRUIT. DROPPED is the unrelated
        # "You can only store one of each fruit! Dropped Bomb will despawn" message
        Compact = re.sub(r'[^A-Z]', '', FullText.upper())
        for Match in re.finditer(r'DROP(?!PED)', Compact):
            Before = Compact[max(Match.start() - 12, 0):Match.start()]
            if any(SequenceMatcher(None, Before[-Length:], 'DEVILFRUIT').ratio() >= 0.6 for Length in range(6, 13)):
                break
        else:
            return False, None

        # The pity line sits under the banner, so only read after it (the stats overlay above holds other numbers)
        After = re.split(r'drop(?!ped)', FullText, flags=re.IGNORECASE)[-1]
        PityMatch = self.PityPattern.search(After)
        if PityMatch:
            return True, int(PityMatch.group(1))
        if self.PityZeroPattern.search(After):
            return True, 0
        return True, None

    def MatchItemName(self, Remainder):
        # "<Pika>" -> "Pika". Fish have multi-word names ("<Zebra Ribbon Angelfish>"), and loosely matching one of
        # those words ("zebra" ~ "Mera") is what counted fish as fruits, so require one word and a close match
        # The store notice ("... You can only store one of each fruit!") often follows without a closing bracket
        Name = re.split(r'[>)\]}]|\byou\b', Remainder.strip().lstrip('<([{ :;,.'), maxsplit=1, flags=re.IGNORECASE)[0]
        Words = re.findall(r'[A-Za-z]+', Name)
        if len(Words) != 1 or not 3 <= len(Words[0]) <= 12:
            return None
        Word = Words[0].lower()
        # OCR reads the angle brackets as a stray letter ("sKage", "zPikaz"); only those letters are trimmed, so a
        # real word like "Sunken" can't be cut down to "Suke"
        Candidates = {Word}
        if Word[0] in 'zsc':
            Candidates.add(Word[1:])
        if Word[-1] in 'zsp':
            Candidates |= {C[:-1] for C in Candidates}
        Best, BestRatio = None, 0.0
        for Candidate in Candidates:
            if len(Candidate) < 3:
                continue
            for Lower, Proper in self.FruitNames.items():
                Ratio = SequenceMatcher(None, Candidate, Lower).ratio()
                if Ratio > BestRatio:
                    Best, BestRatio = Proper, Ratio
        return Best if BestRatio >= 0.8 else None


# What Auto Buy purchases from the bait shop
ShopBaitName = 'Common Fish Bait'


class BaitListReader:
    # Reads the "Fishing Baits" panel shown while the rod is equipped, e.g. "Rare Fish Bait X1"

    Scale = 2
    CountPattern = re.compile(r'[xX×*]\s*([0-9OoIlSs]+)\s*$')

    def __init__(self, OcrManager, Config):
        self.OcrManager = OcrManager
        self.Config = Config

    @staticmethod
    def ParseCount(Raw):
        Digits = Raw.translate(str.maketrans('OoIlSs', '001155'))
        return int(Digits) if Digits.isdigit() else None

    def MatchTier(self, Name, TierOrder):
        Lowered = [T.lower() for T in TierOrder]
        Matches = get_close_matches(Name.lower(), Lowered, n=1, cutoff=0.75)
        return Lowered.index(Matches[0]) if Matches else None

    def ScanBaits(self):
        # Returns in-stock baits best tier first: [{Name, Tier, Count, Point}]; None when OCR is unavailable
        try:
            if self.OcrManager.Reader is None:
                if not self.OcrManager.Enabled:
                    return None
                self.OcrManager.Initialize()
                if not self.OcrManager.WaitForInitialization():
                    return None

            Region = self.Config.Settings['BaitSelector']['Region']
            ScanRegion = {
                "top": Region['Y1'],
                "left": Region['X1'],
                "width": Region['X2'] - Region['X1'],
                "height": Region['Y2'] - Region['Y1']
            }

            with NewScreenCapture() as ScreenCapture:
                Image = np.array(ScreenCapture.grab(ScanRegion))

            # Bait names mix white and coloured text, so OCR the upscaled grayscale instead of a white threshold
            Gray = cv2.cvtColor(Image, cv2.COLOR_BGRA2GRAY)
            Gray = cv2.resize(Gray, None, fx=self.Scale, fy=self.Scale, interpolation=cv2.INTER_CUBIC)

            with self.OcrManager.Lock:
                Results = self.OcrManager.Reader.readtext(Gray, detail=1, paragraph=False, text_threshold=0.5)

            Boxes = []
            for Bbox, Text, Conf in Results:
                if Conf < 0.3 or not Text.strip():
                    continue
                Xs = [P[0] for P in Bbox]
                Ys = [P[1] for P in Bbox]
                Boxes.append({'Text': Text.strip(), 'X0': min(Xs), 'X1': max(Xs), 'Cy': (min(Ys) + max(Ys)) / 2, 'H': max(Ys) - min(Ys)})

            # Name and count are often separate boxes, so merge boxes sharing a line
            Rows = []
            for Box in sorted(Boxes, key=lambda B: B['Cy']):
                if Rows and abs(Box['Cy'] - Rows[-1][-1]['Cy']) < max(Box['H'], Rows[-1][-1]['H']) * 0.6:
                    Rows[-1].append(Box)
                else:
                    Rows.append([Box])

            TierOrder = self.Config.Settings['BaitSelector']['TierOrder']
            Baits = []
            for RowIndex, Row in enumerate(Rows):
                Row.sort(key=lambda B: B['X0'])
                Text = " ".join(B['Text'] for B in Row)
                CountMatch = self.CountPattern.search(Text)
                Count = self.ParseCount(CountMatch.group(1)) if CountMatch else None
                Name = (Text[:CountMatch.start()] if CountMatch else Text).strip()
                Lower = Name.lower()

                # Skip the "Fishing Baits" header and the "Craft more bait types" footer before fuzzy matching,
                # since the header is close enough to match a real bait name
                if 'baits' in Lower or 'craft' in Lower or 'smith' in Lower:
                    continue

                Tier = self.MatchTier(Name, TierOrder)
                if Tier is None:
                    if 'bait' not in Lower:
                        continue
                    # Unlisted baits rank below every listed tier, keeping on-screen order
                    Tier = len(TierOrder) + RowIndex
                else:
                    Name = TierOrder[Tier]

                if Count == 0:
                    continue

                Baits.append({
                    'Name': Name,
                    'Tier': Tier,
                    'Count': Count,
                    'Point': {
                        'x': int(Region['X1'] + (Row[0]['X0'] + Row[-1]['X1']) / 2 / self.Scale),
                        'y': int(Region['Y1'] + sum(B['Cy'] for B in Row) / len(Row) / self.Scale)
                    }
                })

            Baits.sort(key=lambda B: B['Tier'])
            LogLine(f"Bait scan: {[(B['Name'], B['Count']) for B in Baits]}")
            return Baits

        except Exception as E:
            print(f"Bait scan error: {E}")
            traceback.print_exc()
            return None


class WebhookNotifier:
    
    def __init__(self, Config, State):
        self.Config = Config
        self.State = State
    
    def SendNotification(self, Message, Color=None, Title=None, Category=None):
        WebhookUrl = self.Config.Settings['DevilFruitStorage']['WebhookUrl']
        if not WebhookUrl:
            return
        
        try:
            ColorInfo = 0x00d4ff
            ColorSuccess = 0x10b981
            ColorWarning = 0xf59e0b
            ColorError = 0xef4444
            ColorFruit = 0xbf40bf
            ColorStats = 0x3b82f6
            ColorMega = 0xfbbf24
            
            PingUser = False
            ShouldSend = True
            
            LogOpts = self.Config.Settings['LoggingOptions']
            
            if Color is None or Title is None:
                MessageLower = Message.lower()
                
                if "megalodon" in MessageLower:
                    Color = ColorMega
                    Title = "🎣 Megalodon Detected"
                    Category = "general"
                    ShouldSend = LogOpts['LogGeneralUpdates']
                    PingUser = LogOpts['PingGeneralUpdates']
                elif "devil fruit" in MessageLower and ("stored successfully" in MessageLower or " caught!" in MessageLower):
                    Color = ColorFruit
                    Title = "🎣 Devil Fruit Found"
                    Category = "devil_fruit"
                    ShouldSend = LogOpts['LogDevilFruit']
                    PingUser = LogOpts['PingDevilFruit']
                elif "stats" in MessageLower or "caught:" in MessageLower or "total:" in MessageLower:
                    Color = ColorStats
                    Title = "🎣 Fishing Statistics"
                    Category = "periodic_stats"
                    ShouldSend = LogOpts['LogPeriodicStats']
                    PingUser = LogOpts['PingPeriodicStats']
                elif "started" in MessageLower or "stopped" in MessageLower:
                    Color = ColorSuccess if "started" in MessageLower else ColorWarning
                    Title = "🎣 Macro State"
                    Category = "macro_state"
                    ShouldSend = LogOpts['LogMacroState']
                    PingUser = LogOpts['PingMacroState']
                elif "crash" in MessageLower or "error" in MessageLower or "failed" in MessageLower:
                    Color = ColorError
                    Title = "🎣 Error"
                    Category = "errors"
                    ShouldSend = LogOpts['LogErrors']
                    PingUser = LogOpts['PingErrors']
                elif "timeout" in MessageLower and "consecutive" in MessageLower:
                    Color = ColorWarning
                    Title = "🎣 Warning"
                    Category = "recast_timeouts"
                    ShouldSend = LogOpts['LogRecastTimeouts']
                    PingUser = LogOpts['PingRecastTimeouts']
                else:
                    Color = ColorInfo
                    Title = "🎣 GPO Fishing Macro"
                    Category = "general"
                    ShouldSend = LogOpts['LogGeneralUpdates']
                    PingUser = LogOpts['PingGeneralUpdates']
            
            if not ShouldSend:
                return
            
            EmbedData = {
                "title": Title,
                "description": f"**{Message}**",
                "color": Color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {
                    "text": "Macro Notification System",
                    "icon_url": "https://cdn.discordapp.com/avatars/1351127835175288893/208dc6bfcc148a0c3ad2482b12520f43.webp"
                },
                "fields": [
                    {
                        "name": "Time",
                        "value": f"<t:{int(time.time())}:R>",
                        "inline": True
                    }
                ],
            }
            
            PayloadData = {
                "username": "K's GPO Macro Bot",
                "embeds": [EmbedData]
            }

            DiscordUserId = LogOpts['DiscordUserId']
            if PingUser and DiscordUserId and DiscordUserId.strip():
                PayloadData["content"] = f"<@{DiscordUserId.strip()}>"

            # Callers include the fishing loop, which must not stall on a slow Discord response
            threading.Thread(target=self.Post, args=(WebhookUrl, PayloadData), daemon=True).start()
        except Exception as E:
            print(f"Webhook error: {E}")

    @staticmethod
    def Post(WebhookUrl, PayloadData):
        try:
            requests.post(WebhookUrl, json=PayloadData, timeout=5)
        except Exception as E:
            print(f"Webhook error: {E}")


class ColorDetector:
    
    @staticmethod
    def DetectBlackScreen(ScanRegion, ImageArray=None, Threshold=0.5):
        if ImageArray is None:
            with NewScreenCapture() as ScreenCapture:
                CaptureRegion = {
                    "top": ScanRegion["Y1"],
                    "left": ScanRegion["X1"],
                    "width": ScanRegion["X2"] - ScanRegion["X1"],
                    "height": ScanRegion["Y2"] - ScanRegion["Y1"]
                }
                Captured = ScreenCapture.grab(CaptureRegion)
                ImageArray = np.array(Captured)
        
        BlackMask = ((ImageArray[:, :, 2] == 0) & (ImageArray[:, :, 1] == 0) & (ImageArray[:, :, 0] == 0))
        TotalBlack = np.sum(BlackMask)
        TotalPixels = ImageArray.shape[0] * ImageArray.shape[1]
        BlackRatio = TotalBlack / TotalPixels
        
        return BlackRatio >= Threshold
    
    @staticmethod
    def DetectGreenish(TargetPoint, Tolerance=20):
        if not TargetPoint:
            return False
        
        try:
            with NewScreenCapture() as ScreenCapture:
                CaptureRegion = {
                    "top": TargetPoint['y'] - Tolerance,
                    "left": TargetPoint['x'] - Tolerance,
                    "width": Tolerance * 2,
                    "height": Tolerance * 2
                }
                Captured = ScreenCapture.grab(CaptureRegion)
                ImageArray = np.array(Captured)
            
            Green = ImageArray[:, :, 1]
            Red = ImageArray[:, :, 2]
            Blue = ImageArray[:, :, 0]
            
            GreenMask = (
                (Green > Red + 20) & 
                (Green > Blue + 20) &
                (Green > 80)
            )
            
            GreenCount = np.sum(GreenMask)
            Total = ImageArray.shape[0] * ImageArray.shape[1]
            GreenRatio = GreenCount / Total
            
            return GreenRatio > 0.10
            
        except Exception as E:
            print(f"Error detecting green color: {E}")
            return False


MacLoopbackNames = ('blackhole', 'loopback', 'soundflower', 'background music')


def FindMacLoopbackDevice(SelectedIndex=None):
    Devices = sounddevice.query_devices()
    if SelectedIndex is not None and 0 <= SelectedIndex < len(Devices) and Devices[SelectedIndex]['max_input_channels'] > 0:
        return SelectedIndex, Devices[SelectedIndex]
    for Index, Device in enumerate(Devices):
        if Device['max_input_channels'] > 0 and any(Name in Device['name'].lower() for Name in MacLoopbackNames):
            return Index, Device
    return None, None


class MegalodonSoundDetector:
    
    def __init__(self, Config):
        self.Config = Config
        
        if getattr(sys, 'frozen', False):
            AppPath = os.path.dirname(sys.executable)
        else:
            AppPath = os.path.dirname(os.path.abspath(__file__))
        
        self.SoundPath = os.path.join(AppPath, "sounds", "Megalodon.wav")
        
        self.ModelCoefficients = [1.0902, 0.7471, 0.3720, -1.1829, -1.0433, -0.6251, -0.4898]
        self.ModelIntercept = -3.2025
        self.ScalerMeans = [0.1308, 0.1496, 0.0916, 0.0797, 0.1209, 0.1816, 0.2457]
        self.ScalerScales = [0.0775, 0.0748, 0.0200, 0.0317, 0.0344, 0.0438, 0.0960]
        self.FrequencyBands = [(20, 60), (60, 120), (120, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000)]
        
        self.NoiseFloor = 0.02
    
    def ReduceNoise(self, AudioData):
        Magnitude = np.abs(AudioData)
        Mask = Magnitude > self.NoiseFloor
        return AudioData * Mask
    
    def CalculateSignalQuality(self, AudioData):
        SignalPower = np.mean(AudioData ** 2)
        NoisePower = np.var(AudioData)
        
        if NoisePower < 1e-10:
            return 1.0
        
        SNR = 10 * np.log10(SignalPower / NoisePower) if NoisePower > 0 else 0
        Quality = np.clip((SNR + 10) / 40, 0, 1)
        return Quality
    
    def ExtractFeatures(self, AudioData, AudioSampleRate):
        FftMag = np.abs(fft(AudioData))[:len(AudioData)//2]
        FreqArray = np.fft.fftfreq(len(AudioData), 1/AudioSampleRate)[:len(AudioData)//2]
        
        Features = []
        for LowFreq, HighFreq in self.FrequencyBands:
            Mask = (FreqArray >= LowFreq) & (FreqArray < HighFreq)
            Energy = np.sum(FftMag[Mask]) if np.any(Mask) else 0
            Features.append(Energy)
        
        TotalEnergy = sum(Features) + 1e-10
        Features = [F/TotalEnergy for F in Features]
        
        return Features
    
    def PredictProbability(self, Features):
        Scaled = [(F - M) / S for F, M, S in zip(Features, self.ScalerMeans, self.ScalerScales)]
        Logit = self.ModelIntercept + sum(C * F for C, F in zip(self.ModelCoefficients, Scaled))
        Prob = 1 / (1 + np.exp(-Logit))
        return Prob
    
    def ScoreAttempt(self, AudioData, AudioSampleRate, Attempt):
        # One recording through the model; the same steps as the Windows loop in Listen
        MaxAudio = np.max(np.abs(AudioData)) if len(AudioData) else 0
        if MaxAudio < 0.01:
            print(f"  Attempt {Attempt+1}: Too quiet (level: {MaxAudio:.4f})")
            return False

        AudioData = self.ReduceNoise(AudioData / MaxAudio)
        SignalQuality = self.CalculateSignalQuality(AudioData)
        WindowSamples = int(0.5 * AudioSampleRate)
        HopSamples = int(0.1 * AudioSampleRate)

        MaxProb = 0
        for WindowStart in range(0, max(1, len(AudioData) - WindowSamples), HopSamples):
            Chunk = AudioData[WindowStart:WindowStart + WindowSamples]
            if len(Chunk) < WindowSamples:
                continue
            MaxProb = max(MaxProb, self.PredictProbability(self.ExtractFeatures(Chunk, AudioSampleRate)))

        AdaptiveThreshold = self.Config.Settings['FishingModes']['SoundSensitivity'] * (0.7 + 0.3 * SignalQuality)
        print(f"  Attempt {Attempt+1}: MaxProb={MaxProb:.4f}, Threshold={AdaptiveThreshold:.4f}, Quality={SignalQuality:.2f}")
        return MaxProb > AdaptiveThreshold

    def ListenMac(self):
        # macOS has no output loopback like WASAPI's, so this records an input device carrying the game audio
        # (BlackHole or similar, fed by a Multi-Output Device)
        Index, Device = FindMacLoopbackDevice(self.Config.Settings['AudioDevice']['SelectedDeviceIndex'])
        if Index is None:
            print("No loopback input found - Megalodon sound detection disabled")
            print("Install BlackHole and route system output through it with a Multi-Output Device")
            return True

        AudioSampleRate = int(Device.get('default_samplerate') or 44100)
        if AudioSampleRate < 8000 or AudioSampleRate > 192000:
            AudioSampleRate = 44100
        Channels = min(2, Device['max_input_channels'])
        MultipleAttempts = 2
        PositiveDetections = 0

        for Attempt in range(MultipleAttempts):
            try:
                Recording = sounddevice.rec(int(AudioSampleRate * 2.0), samplerate=AudioSampleRate, channels=Channels,
                                            dtype='float32', device=Index)
                sounddevice.wait()
            except Exception as E:
                print(f"Audio recording error: {E}")
                return True
            if self.ScoreAttempt(Recording.mean(axis=1), AudioSampleRate, Attempt):
                PositiveDetections += 1

        RequiredPositive = max(1, MultipleAttempts // 2)
        print(f"Final result: {PositiveDetections}/{MultipleAttempts} positive detections (need {RequiredPositive})")
        return PositiveDetections >= RequiredPositive

    def Listen(self, TimeoutDuration=5.0):
        if not self.Config.Settings['FishingModes']['MegalodonSound']:
            return True

        if IsMac:
            try:
                return self.ListenMac()
            except Exception as E:
                print(f"Sound recognition error: {E}")
                traceback.print_exc()
                return True
        
        try:
            AudioInterface = pyaudio.PyAudio()
            AudioStream = None
            
            try:
                AudioSampleRate = 44100
                RecordingDuration = 2.0
                MultipleAttempts = 2
                DeviceToUse = None
                
                SelectedIndex = self.Config.Settings['AudioDevice']['SelectedDeviceIndex']

                if SelectedIndex is not None:
                    try:
                        DeviceToUse = AudioInterface.get_device_info_by_index(SelectedIndex)
                        print(f"Using manually selected device: {DeviceToUse.get('name', 'Unknown')}")
                    except Exception as E:
                        print(f"Selected device not available: {E}")
                        print("Falling back to auto-detect")

                if DeviceToUse is None:
                    try:
                        WasapiInfo = AudioInterface.get_host_api_info_by_type(pyaudio.paWASAPI)
                        DefaultOutputIndex = WasapiInfo.get("defaultOutputDevice")
                        
                        if DefaultOutputIndex is not None and DefaultOutputIndex >= 0:
                            try:
                                DefaultDevice = AudioInterface.get_device_info_by_index(DefaultOutputIndex)
                                DefaultName = DefaultDevice.get("name", "")
                                
                                for Loopback in AudioInterface.get_loopback_device_info_generator():
                                    if DefaultName in Loopback.get("name", ""):
                                        if Loopback.get('maxInputChannels', 0) > 0:
                                            DeviceToUse = Loopback
                                            print(f"Found matching loopback device: {Loopback.get('name', 'Unknown')}")
                                            break
                            except Exception as E:
                                print(f"Error matching default output device: {E}")
                        
                        if DeviceToUse is None:
                            print("No matching loopback device found, trying any available loopback device...")
                            try:
                                for Loopback in AudioInterface.get_loopback_device_info_generator():
                                    if Loopback.get('maxInputChannels', 0) > 0:
                                        DeviceToUse = Loopback
                                        print(f"Using loopback device: {Loopback.get('name', 'Unknown')}")
                                        break
                            except Exception as E:
                                print(f"Error finding any loopback device: {E}")
                        
                    except Exception as E:
                        print(f"WASAPI not available: {E}")
                
                if DeviceToUse is None:
                    print("No loopback device found - Megalodon sound detection disabled")
                    print("Make sure you have audio output devices enabled in Windows Sound settings")
                    AudioInterface.terminate()
                    return True
                
                DeviceIndex = DeviceToUse.get("index")
                if DeviceIndex is None:
                    print("Invalid device index - Megalodon sound detection disabled")
                    AudioInterface.terminate()
                    return True
                
                AudioSampleRate = int(DeviceToUse.get('defaultSampleRate', 44100))
                if AudioSampleRate < 8000 or AudioSampleRate > 192000:
                    AudioSampleRate = 44100
                
                MaxChannels = DeviceToUse.get('maxInputChannels', 0)
                if MaxChannels < 1:
                    print("Device has no input channels - Megalodon sound detection disabled")
                    AudioInterface.terminate()
                    return True
                
                Channels = min(2, MaxChannels)
                
                FormatToUse = pyaudio.paFloat32
                IsInt16 = False
                
                try:
                    AudioStream = AudioInterface.open(
                        format=pyaudio.paFloat32,
                        channels=Channels,
                        rate=AudioSampleRate,
                        input=True,
                        frames_per_buffer=1024,
                        input_device_index=DeviceIndex
                    )
                except OSError as E:
                    print(f"paFloat32 failed, trying paInt16: {E}")
                    try:
                        AudioStream = AudioInterface.open(
                            format=pyaudio.paInt16,
                            channels=Channels,
                            rate=AudioSampleRate,
                            input=True,
                            frames_per_buffer=1024,
                            input_device_index=DeviceIndex
                        )
                        FormatToUse = pyaudio.paInt16
                        IsInt16 = True
                        print("Using paInt16 format")
                    except Exception as E2:
                        print(f"paInt16 also failed, trying single channel: {E2}")
                        try:
                            AudioStream = AudioInterface.open(
                                format=pyaudio.paFloat32,
                                channels=1,
                                rate=AudioSampleRate,
                                input=True,
                                frames_per_buffer=1024,
                                input_device_index=DeviceIndex
                            )
                            Channels = 1
                            print("Using single channel")
                        except Exception as E3:
                            print(f"All formats failed: {E3}")
                            AudioInterface.terminate()
                            return True
                
                PositiveDetections = 0
                
                for Attempt in range(MultipleAttempts):
                    Frames = []
                    
                    for _ in range(int(AudioSampleRate * RecordingDuration / 1024)):
                        try:
                            Data = AudioStream.read(1024, exception_on_overflow=False)
                            Frames.append(Data)
                        except Exception as E:
                            print(f"Error reading audio data: {E}")
                            break
                    
                    if not Frames:
                        print("No audio data captured - skipping detection")
                        continue
                    
                    if IsInt16:
                        AudioData = np.frombuffer(b''.join(Frames), dtype=np.int16).astype(np.float32) / 32768.0
                    else:
                        AudioData = np.frombuffer(b''.join(Frames), dtype=np.float32)
                    
                    if Channels == 2:
                        AudioData = AudioData.reshape(-1, 2).mean(axis=1)
                    
                    MaxAudio = np.max(np.abs(AudioData))
                    if MaxAudio < 0.01:
                        print(f"  Attempt {Attempt+1}: Too quiet (level: {MaxAudio:.4f})")
                        continue
                    
                    AudioData = AudioData / MaxAudio
                    AudioData = self.ReduceNoise(AudioData)
                    SignalQuality = self.CalculateSignalQuality(AudioData)
                    
                    WindowDuration = 0.5
                    HopDuration = 0.1
                    WindowSamples = int(WindowDuration * AudioSampleRate)
                    HopSamples = int(HopDuration * AudioSampleRate)
                    
                    MaxProb = 0
                    
                    for WindowStart in range(0, max(1, len(AudioData) - WindowSamples), HopSamples):
                        Chunk = AudioData[WindowStart:WindowStart + WindowSamples]
                        if len(Chunk) < WindowSamples:
                            continue
                        
                        Features = self.ExtractFeatures(Chunk, AudioSampleRate)
                        Prob = self.PredictProbability(Features)
                        
                        if Prob > MaxProb:
                            MaxProb = Prob
                    
                    BaseSensitivity = self.Config.Settings['FishingModes']['SoundSensitivity']
                    AdaptiveThreshold = BaseSensitivity * (0.7 + 0.3 * SignalQuality)
                    
                    print(f"  Attempt {Attempt+1}: MaxProb={MaxProb:.4f}, Threshold={AdaptiveThreshold:.4f}, Quality={SignalQuality:.2f}")
                    
                    if MaxProb > AdaptiveThreshold:
                        PositiveDetections += 1
                
                if AudioStream:
                    AudioStream.stop_stream()
                    AudioStream.close()
                AudioInterface.terminate()
                
                RequiredPositive = max(1, MultipleAttempts // 2)
                IsMegalodon = PositiveDetections >= RequiredPositive
                
                print(f"Final result: {PositiveDetections}/{MultipleAttempts} positive detections (need {RequiredPositive})")
                
                return IsMegalodon
                
            except Exception as E:
                print(f"PyAudioWPatch error: {E}")
                traceback.print_exc()
                if AudioStream:
                    try:
                        AudioStream.stop_stream()
                        AudioStream.close()
                    except:
                        pass
                AudioInterface.terminate()
                return True
                
        except Exception as E:
            print(f"Sound recognition error: {E}")
            traceback.print_exc()
            return True


class InputController:
    
    def __init__(self, Config):
        self.Config = Config
    
    def FocusRobloxWindow(self):
        if IsMac:
            return self.FocusRobloxWindowMac()

        def FindRobloxWindow(Handle, Windows):
            if win32gui.IsWindowVisible(Handle):
                Title = win32gui.GetWindowText(Handle)
                if "Roblox" in Title:
                    Windows.append(Handle)
        
        Windows = []
        win32gui.EnumWindows(FindRobloxWindow, Windows)
        
        if not Windows:
            return False

        Handle = Windows[0]
        if win32gui.GetForegroundWindow() == Handle:
            return True

        try:
            if win32gui.IsIconic(Handle):
                win32gui.ShowWindow(Handle, win32con.SW_RESTORE)
            # Windows refuses focus changes from background processes; a synthetic Alt tap lifts that lock
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32gui.SetForegroundWindow(Handle)
        except Exception as E:
            LogLine(f"FocusRobloxWindow: SetForegroundWindow failed ({E}), trying BringWindowToTop")
            try:
                win32gui.BringWindowToTop(Handle)
            except Exception:
                pass

        time.sleep(self.Config.Settings['TimingDelays']['RobloxWindow']['RobloxFocusDelay'])
        return win32gui.GetForegroundWindow() == Handle

    @staticmethod
    def FrontmostRobloxPids():
        # The window list is ordered front to back and needs no permission for owner names. NSWorkspace's
        # frontmostApplication would be simpler but goes stale in a process with no Cocoa run loop
        Windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
        AppWindows = [W for W in Windows if W.get('kCGWindowLayer') == 0]
        FrontPid = AppWindows[0].get('kCGWindowOwnerPID') if AppWindows else None
        RobloxPids = [W.get('kCGWindowOwnerPID') for W in AppWindows if 'Roblox' in (W.get('kCGWindowOwnerName') or '')]
        return FrontPid, RobloxPids

    def FocusRobloxWindowMac(self):
        FrontPid, RobloxPids = self.FrontmostRobloxPids()
        if not RobloxPids:
            return False
        if FrontPid in RobloxPids:
            return True

        Pid = RobloxPids[0]
        App = NSRunningApplication.runningApplicationWithProcessIdentifier_(Pid)
        Delay = self.Config.Settings['TimingDelays']['RobloxWindow']['RobloxFocusDelay']
        if App is not None:
            App.unhide()
            App.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(Delay)
            if self.FrontmostRobloxPids()[0] == Pid:
                return True
            # Newer macOS can refuse activation from a background process; LaunchServices still honours "open"
            BundleUrl = App.bundleURL()
            if BundleUrl is not None:
                LogLine("FocusRobloxWindow: activate was refused, retrying through open")
                subprocess.run(['open', BundleUrl.path()], timeout=5)
                time.sleep(Delay)
        return self.FrontmostRobloxPids()[0] == Pid
    
    def ClickPoint(self, Point):
        if not Point:
            return False
        
        SetCursorPos(Point['x'], Point['y'])
        time.sleep(self.Config.Settings['TimingDelays']['PreCast']['PreCastAntiDetectDelay'])
        NudgeMouse(1)
        time.sleep(self.Config.Settings['TimingDelays']['PreCast']['PreCastAntiDetectDelay'])
        pyautogui.click()
        time.sleep(self.Config.Settings['TimingDelays']['PreCast']['PreCastClickDelay'])
        return True
    
    def FastClickPoint(self, Point):
        if not Point:
            return False
        
        SetCursorPos(Point['x'], Point['y'])
        time.sleep(0.015)
        NudgeMouse(1)
        time.sleep(0.015)
        pyautogui.click()
        return True
    
    def PressKey(self, Key):
        keyboard.press_and_release(Key)
    
    def TypeText(self, Text):
        keyboard.write(Text)


class RegionSelectionWindow:
    
    def __init__(self, ParentWindow, InitialBounds, CompletionCallback):
        self.CompletionCallback = CompletionCallback
        self.ParentWindow = ParentWindow
        self.IsWindowClosed = False

        self.RootWindow = tk.Tk()
        self.RootWindow.attributes('-alpha', 0.85)
        self.RootWindow.attributes('-topmost', True)
        self.RootWindow.overrideredirect(True)

        self.LeftBoundary = InitialBounds["X1"]
        self.TopBoundary = InitialBounds["Y1"]
        self.RightBoundary = InitialBounds["X2"]
        self.BottomBoundary = InitialBounds["Y2"]

        Width = self.RightBoundary - self.LeftBoundary
        Height = self.BottomBoundary - self.TopBoundary

        self.RootWindow.geometry(f"{Width}x{Height}+{self.LeftBoundary}+{self.TopBoundary}")
        self.RootWindow.configure(bg='#1e293b')

        HeaderFrame = tk.Frame(self.RootWindow, bg='#0f172a', height=40)
        HeaderFrame.pack(side='top', fill='x')
        HeaderFrame.pack_propagate(False)

        TitleLabel = tk.Label(
            HeaderFrame,
            text="📐 Select Region",
            bg='#0f172a',
            fg='#e2e8f0',
            font=('Segoe UI', 10, 'bold'),
            padx=15
        )
        TitleLabel.pack(side='left', pady=10)

        ButtonContainer = tk.Frame(HeaderFrame, bg='#0f172a')
        ButtonContainer.pack(side='right', padx=10, pady=5)

        if IsMac:
            # A native macOS button swallows the first click while the window is inactive (and ignores bg), so the
            # selector needed two clicks to confirm; a Tk-drawn label acts on the first
            self.ConfirmButton = tk.Label(
                ButtonContainer,
                text="✓ Confirm",
                bg='#10b981',
                fg='white',
                font=('Helvetica', 12, 'bold'),
                padx=20,
                pady=6,
                cursor='hand2'
            )
            self.ConfirmButton.bind('<ButtonRelease-1>', lambda e: self.CloseWindow())
        else:
            self.ConfirmButton = tk.Button(
                ButtonContainer,
                text="✓ Confirm",
                command=self.CloseWindow,
                bg='#10b981',
                fg='white',
                font=('Segoe UI', 9, 'bold'),
                padx=20,
                pady=8,
                cursor='hand2',
                relief='flat',
                borderwidth=0,
                activebackground='#059669',
                activeforeground='white'
            )
        self.ConfirmButton.pack(side='right')

        self.ConfirmButton.bind('<Enter>', lambda e: self.ConfirmButton.config(bg='#059669'))
        self.ConfirmButton.bind('<Leave>', lambda e: self.ConfirmButton.config(bg='#10b981'))

        self.Canvas = tk.Canvas(
            self.RootWindow,
            bg='#1e293b',
            highlightthickness=2,
            highlightbackground='#3b82f6',
            relief='flat'
        )
        self.Canvas.pack(fill='both', expand=True, padx=2, pady=2)

        self.CreateCornerIndicators()

        self.IsDragging = False
        self.IsResizing = False
        self.ActiveEdge = None

        self.MouseDownX = 0
        self.MouseDownY = 0
        self.EdgeThreshold = 10

        self.Canvas.bind('<Button-1>', self.HandleMousePress)
        self.Canvas.bind('<B1-Motion>', self.HandleMouseDrag)
        self.Canvas.bind('<ButtonRelease-1>', self.HandleMouseRelease)
        self.Canvas.bind('<Motion>', self.HandleMouseHover)
        
        self.RootWindow.protocol("WM_DELETE_WINDOW", self.CloseWindow)

        if IsMac:
            # Tk 9 on macOS keeps the title bar of a window made borderless before it's first shown, and ignores where
            # it was placed, so redo both once it's up
            self.RootWindow.update()
            self.RootWindow.withdraw()
            self.RootWindow.overrideredirect(True)
            self.RootWindow.deiconify()
            self.RootWindow.geometry(f"{Width}x{Height}+{self.LeftBoundary}+{self.TopBoundary}")
            self.RootWindow.update()
            FloatOverFullscreen(self.RootWindow)

        self.RootWindow.mainloop()

    def CreateCornerIndicators(self):
        Size = 15
        Color = '#3b82f6'
        
        self.RootWindow.update_idletasks()
        W = self.Canvas.winfo_width()
        H = self.Canvas.winfo_height()
        
        self.Canvas.create_rectangle(0, 0, Size, Size, fill=Color, outline='')
        self.Canvas.create_rectangle(W - Size, 0, W, Size, fill=Color, outline='')
        self.Canvas.create_rectangle(0, H - Size, Size, H, fill=Color, outline='')
        self.Canvas.create_rectangle(W - Size, H - Size, W, H, fill=Color, outline='')

    def HandleMouseHover(self, Event):
        X, Y = Event.x, Event.y
        W = self.RootWindow.winfo_width()
        H = self.RootWindow.winfo_height()
        
        Left = X < self.EdgeThreshold
        Right = X > W - self.EdgeThreshold
        Top = Y < self.EdgeThreshold
        Bottom = Y > H - self.EdgeThreshold

        if Left and Top:
            self.Canvas.config(cursor='top_left_corner')
        elif Right and Top:
            self.Canvas.config(cursor='top_right_corner')
        elif Left and Bottom:
            self.Canvas.config(cursor='bottom_left_corner')
        elif Right and Bottom:
            self.Canvas.config(cursor='bottom_right_corner')
        elif Left or Right:
            self.Canvas.config(cursor='sb_h_double_arrow')
        elif Top or Bottom:
            self.Canvas.config(cursor='sb_v_double_arrow')
        else:
            self.Canvas.config(cursor='fleur')

    def HandleMousePress(self, Event):
        self.MouseDownX = Event.x
        self.MouseDownY = Event.y
        X, Y = Event.x, Event.y
        W = self.RootWindow.winfo_width()
        H = self.RootWindow.winfo_height()
        
        Left = X < self.EdgeThreshold
        Right = X > W - self.EdgeThreshold
        Top = Y < self.EdgeThreshold
        Bottom = Y > H - self.EdgeThreshold

        if Left or Right or Top or Bottom:
            self.IsResizing = True
            self.ActiveEdge = {'left': Left, 'right': Right, 'top': Top, 'bottom': Bottom}
        else:
            self.IsDragging = True

    def HandleMouseDrag(self, Event):
        if self.IsDragging:
            Dx = Event.x - self.MouseDownX
            Dy = Event.y - self.MouseDownY
            NewX = self.RootWindow.winfo_x() + Dx
            NewY = self.RootWindow.winfo_y() + Dy
            self.RootWindow.geometry(f"+{NewX}+{NewY}")
        elif self.IsResizing:
            X = self.RootWindow.winfo_x()
            Y = self.RootWindow.winfo_y()
            W = self.RootWindow.winfo_width()
            H = self.RootWindow.winfo_height()
            NewX = X
            NewY = Y
            NewW = W
            NewH = H

            if self.ActiveEdge['left']:
                Dx = Event.x - self.MouseDownX
                NewX = X + Dx
                NewW = W - Dx
            elif self.ActiveEdge['right']:
                NewW = Event.x

            if self.ActiveEdge['top']:
                Dy = Event.y - self.MouseDownY
                NewY = Y + Dy
                NewH = H - Dy
            elif self.ActiveEdge['bottom']:
                NewH = Event.y

            if NewW < 50:
                NewW = 50
                NewX = X
            if NewH < 50:
                NewH = 50
                NewY = Y

            self.RootWindow.geometry(f"{NewW}x{NewH}+{NewX}+{NewY}")

    def HandleMouseRelease(self, Event):
        self.IsDragging = False
        self.IsResizing = False
        self.ActiveEdge = None

    def CloseWindow(self):
        if self.IsWindowClosed:
            return
        self.IsWindowClosed = True
        
        try:
            # The content's screen position, which on macOS sits below the window frame's top
            Left = self.RootWindow.winfo_rootx()
            Top = self.RootWindow.winfo_rooty()
            Right = Left + self.RootWindow.winfo_width()
            Bottom = Top + self.RootWindow.winfo_height()
            Coords = {"X1": Left, "Y1": Top, "X2": Right, "Y2": Bottom}
            
            self.RootWindow.quit()
            self.RootWindow.destroy()
            
            if self.CompletionCallback:
                self.CompletionCallback(Coords)
        except Exception as E:
            print(f"Error closing area selector: {E}")


class PointSelector:
    
    def __init__(self):
        self.MouseListener = None
        self.CurrentlySettingPoint = None
    
    def StartSelection(self, PointName, Callback):
        if self.MouseListener:
            self.MouseListener.stop()

        self.CurrentlySettingPoint = PointName
        StartTime = time.time()
        
        def HandleClick(X, Y, Button, Pressed):
            if Pressed and self.CurrentlySettingPoint == PointName:
                if time.time() - StartTime < 0.25:
                    return True
                
                # pynput reports macOS points as floats
                Callback(PointName, {"x": int(X), "y": int(Y)})
                self.CurrentlySettingPoint = None
                return False
        
        self.MouseListener = mouse.Listener(on_click=HandleClick)
        self.MouseListener.start()
    
    def StopSelection(self):
        if self.MouseListener:
            self.MouseListener.stop()
            self.MouseListener = None


class FishingMinigameController:
    
    def __init__(self, Config, State):
        self.Config = Config
        self.State = State
    
    def WaitForBobber(self):
        StartTime = time.time()
        BarFrames = 0
        BlackScreenCount = 0

        ScanArea = self.Config.Settings['ScanArea']
        MaxTimeout = self.Config.Settings['FishingControl']['Timing']['RecastTimeout']
        Capture = self.GetCapture()

        while self.State.IsRunning:
            Elapsed = time.time() - StartTime

            if Elapsed >= MaxTimeout:
                return False

            Image = np.array(Capture.grab({
                "top": ScanArea["Y1"],
                "left": ScanArea["X1"],
                "width": ScanArea["X2"] - ScanArea["X1"],
                "height": ScanArea["Y2"] - ScanArea["Y1"]
            }))

            if ColorDetector.DetectBlackScreen(ScanArea, Image, self.Config.Settings['FishingControl']['Detection']['BlackScreenThreshold']):
                BlackScreenCount += 1
                if BlackScreenCount >= 3:
                    self.State.UpdateStatus("Multiple black screens detected - recasting")
                    return False
                time.sleep(0.5)
                continue
            else:
                BlackScreenCount = 0

            # Require a real bar (same check the minigame uses) on consecutive frames. Matching single pixels
            # anywhere fired on stray blue in the water right after casting, so the next cycle's cast click
            # reeled the fresh line back in
            Reading, _ = self.LocateBar(Image)
            BarFrames = BarFrames + 1 if Reading else 0
            if BarFrames >= 2:
                return True
            
            SleepTime = self.Config.Settings['FishingControl']['Detection']['ScanLoopDelay']
            if self.State.FastModeEnabled:
                SleepTime += 0.2
            time.sleep(SleepTime)

        return False
    
    def ResetMinigame(self):
        self.BarLeft = None
        self.BarRight = None
        self.PrevWhiteY = None
        self.PrevTargetY = None
        self.WhiteVel = 0.0
        self.TargetVel = 0.0
        self.State.PreviousError = None
        self.State.PreviousTargetY = None
        self.State.LastScanTime = time.time()
        self.BarMissingSince = None
        self.SnapshotReasons = set()
        self.LastTraceTime = 0.0
        self.MinigameStart = time.time()

    def Snapshot(self, Image, Reason):
        # One screenshot per reason per minigame, capped per session, so the folder stays small
        self.SnapshotCount = getattr(self, 'SnapshotCount', 0)
        if Reason in self.SnapshotReasons or self.SnapshotCount >= 60:
            return
        self.SnapshotReasons.add(Reason)
        self.SnapshotCount += 1
        Path = os.path.join(VisionDir, f"{datetime.now().strftime('%H%M%S_%f')[:-3]}_{Reason}.png")
        try:
            cv2.imwrite(Path, Image)
            LogLine(f"VISION snapshot {Reason} -> {Path}")
        except Exception as E:
            LogLine(f"VISION snapshot failed: {E}")

    def Trace(self, Now, Message):
        if Now - self.LastTraceTime >= 0.25:
            self.LastTraceTime = Now
            LogLine(f"MINIGAME t={Now - self.MinigameStart:.2f}s {Message}")

    def BarMissing(self, Now):
        # A single frame without the bar (flicker, capture tearing) must not end the minigame,
        # otherwise the loop recasts and swaps items while the fish is still hooked
        if self.BarMissingSince is None:
            self.BarMissingSince = Now
        if Now - self.BarMissingSince < self.Config.Settings['FishingControl']['Timing'].get('CatchEndGrace', 0.25):
            return True
        self.SetMouse(False, Now)
        return False

    def GetCapture(self):
        # mss handles are thread-bound and slow to create, so keep one per thread
        Local = getattr(self, '_CaptureLocal', None)
        if Local is None:
            Local = self._CaptureLocal = threading.local()
        if getattr(Local, 'Sct', None) is None:
            Local.Sct = NewScreenCapture()
        return Local.Sct

    @staticmethod
    def ColorMask(Pixels, Color):
        # Pixels are BGRA, Color is RGB
        return (Pixels[..., 2] == Color[0]) & (Pixels[..., 1] == Color[1]) & (Pixels[..., 0] == Color[2])

    @staticmethod
    def NearColorMask(Pixels, Color, Tolerance):
        Diff = np.abs(Pixels[..., :3].astype(np.int16) - np.array(Color[::-1], dtype=np.int16))
        return np.all(Diff <= Tolerance, axis=-1)

    @staticmethod
    def Groups(Rows, MaxGap):
        Breaks = np.where(np.diff(Rows) > MaxGap)[0] + 1
        return np.split(Rows, Breaks)

    @classmethod
    def LocateBar(cls, Image, PrevWhiteY=None, GapMultiplier=2.0):
        # Returns (Reading, None) or (None, FailureReason). Reading holds the fish line (white) and catch zone
        # (dark gray) centres within the blue bar column
        BlueMask = cls.ColorMask(Image, (85, 170, 255))
        BlueCols = BlueMask.sum(axis=0)
        # Averaging every blue pixel let stray blue (water, effects) drag the column off the bar,
        # so centre on the columns that hold most of the bar's blue instead
        if BlueCols.max() < 10:
            return None, 'no_blue_bar'
        Strong = np.where(BlueCols >= BlueCols.max() * 0.5)[0]
        CenterX = int((Strong[0] + Strong[-1]) // 2)

        # A few columns around the centre so a thin line landing between pixels is still seen
        Band = Image[:, max(CenterX - 2, 0):CenterX + 3, :]
        IsGray = cls.NearColorMask(Band, (25, 25, 25), 4).any(axis=1)
        IsBlue = cls.ColorMask(Band, (85, 170, 255)).any(axis=1)
        IsWhite = (Band[..., :3].min(axis=-1) >= 235).any(axis=1)

        # The bar is the contiguous blue/gray/white run holding the most blue; this drops dark UI below the bar
        # (the item count boxes) that would otherwise be read as catch zone
        Runs = cls.Groups(np.where(IsGray | IsBlue | IsWhite)[0], 3)
        Bar = max(Runs, key=lambda R: IsBlue[R].sum())
        Top, Bottom = int(Bar[0]), int(Bar[-1])
        GrayRows = Bar[IsGray[Bar]]
        if len(GrayRows) == 0:
            return None, 'no_gray'

        # Search the whole bar, not just the catch zone: the fish line leaving the zone is exactly when it matters
        WhiteRows = Bar[IsWhite[Bar]]
        if len(WhiteRows) == 0:
            return None, 'no_white'

        # The fish line is ~3px thick; prefer it over 1px UI dividers that cross the bar
        WhiteGroups = cls.Groups(WhiteRows, 2)
        Thick = [G for G in WhiteGroups if len(G) >= 2] or WhiteGroups
        if PrevWhiteY is not None:
            White = min(Thick, key=lambda G: abs((G[0] + G[-1]) / 2 - PrevWhiteY))
        else:
            White = max(Thick, key=len)
        WhiteHeight = White[-1] - White[0] + 1
        WhiteCenter = int((White[0] + White[-1]) // 2)

        # Split the catch zone into contiguous groups and follow the largest
        ZoneGroups = cls.Groups(GrayRows, max(WhiteHeight * GapMultiplier, 3))
        Zone = max(ZoneGroups, key=len)
        TargetCenter = int((Zone[0] + Zone[-1]) // 2)

        return {
            'CenterX': CenterX, 'WhiteCenter': WhiteCenter, 'WhiteHeight': WhiteHeight,
            'TargetCenter': TargetCenter, 'Groups': len(ZoneGroups), 'Top': int(Top), 'Bottom': int(Bottom)
        }, None

    def SetMouse(self, Hold, Now):
        if Hold and not self.State.MousePressed:
            pyautogui.mouseDown()
            self.State.MousePressed = True
            self.State.LastInputResendTime = Now
        elif not Hold and self.State.MousePressed:
            pyautogui.mouseUp()
            self.State.MousePressed = False
            self.State.LastInputResendTime = Now
        elif Now - getattr(self.State, 'LastInputResendTime', 0) >= self.Config.Settings['FishingControl']['Timing']['StateResendInterval']:
            if self.State.MousePressed:
                pyautogui.mouseDown()
            else:
                pyautogui.mouseUp()
            self.State.LastInputResendTime = Now

    def ControlMinigame(self):
        ScanArea = self.Config.Settings['ScanArea']
        Capture = self.GetCapture()
        Height = ScanArea["Y2"] - ScanArea["Y1"]

        Image = np.array(Capture.grab({"top": ScanArea["Y1"], "left": ScanArea["X1"],
                                       "width": ScanArea["X2"] - ScanArea["X1"], "height": Height}))
        Now = time.time()
        self.Snapshot(Image, 'start')

        if ColorDetector.DetectBlackScreen(ScanArea, Image, self.Config.Settings['FishingControl']['Detection']['BlackScreenThreshold']):
            self.Snapshot(Image, 'black_screen')
            self.Trace(Now, "black screen -> release")
            self.SetMouse(False, Now)
            time.sleep(0.2)
            return True

        Reading, Failure = self.LocateBar(Image, self.PrevWhiteY,
                                          self.Config.Settings['FishingControl']['Detection']['GapToleranceMultiplier'])

        if Failure == 'no_blue_bar':
            self.Snapshot(Image, 'no_blue_bar')
            FirstMiss = self.BarMissingSince is None
            Still = self.BarMissing(Now)
            # The loop runs every few ms, so log only the transitions instead of every grace frame
            if not Still:
                LogLine(f"MINIGAME blue bar not visible for {Now - self.BarMissingSince:.2f}s (ending minigame)")
            elif FirstMiss:
                LogLine("MINIGAME blue bar not visible (waiting)")
            return Still
        self.BarMissingSince = None

        if Failure == 'no_gray':
            self.Snapshot(Image, 'no_gray')
            self.Trace(Now, "no catch zone in bar -> keep state")
            self.SetMouse(self.State.MousePressed, Now)
            return True

        if Failure == 'no_white':
            # Fish line hidden (e.g. under the HOLD CLICK prompt): steer by where it was last seen
            # instead of holding blindly, which drove the zone to the top when the fish was below it
            self.Snapshot(Image, 'no_white')
            if self.PrevWhiteY is None or self.PrevTargetY is None:
                self.Trace(Now, "no fish line -> keep state")
                self.SetMouse(self.State.MousePressed, Now)
            else:
                ShouldHold = self.PrevWhiteY < self.PrevTargetY
                self.Trace(Now, f"no fish line -> last seen {'above' if ShouldHold else 'below'} zone, hold={ShouldHold}")
                self.SetMouse(ShouldHold, Now)
            return True

        CenterX = Reading['CenterX']
        WhiteHeight = Reading['WhiteHeight']
        WhiteCenter = Reading['WhiteCenter']
        TargetCenter = Reading['TargetCenter']

        Pd = self.Config.Settings['FishingControl']['PdController']

        # Velocities are sampled over >=25ms windows: consecutive grabs often show the same frame,
        # and dividing a 1px jump by a 1ms gap produced huge spikes that flipped the hold decision
        if self.PrevWhiteY is None:
            self.PrevWhiteY, self.PrevTargetY, self.VelSampleTime = WhiteCenter, TargetCenter, Now
        elif Now - self.VelSampleTime >= 0.025:
            Dt = Now - self.VelSampleTime
            Alpha = 0.5
            RawWhite = max(-1500.0, min(1500.0, (WhiteCenter - self.PrevWhiteY) / Dt))
            RawTarget = max(-1500.0, min(1500.0, (TargetCenter - self.PrevTargetY) / Dt))
            self.WhiteVel = Alpha * RawWhite + (1 - Alpha) * self.WhiteVel
            self.TargetVel = Alpha * RawTarget + (1 - Alpha) * self.TargetVel
            self.PrevWhiteY, self.PrevTargetY, self.VelSampleTime = WhiteCenter, TargetCenter, Now

        # Hold raises the catch zone (target); aim where fish line and zone will be shortly so momentum doesn't overshoot
        Error = WhiteCenter - TargetCenter
        ErrorRate = self.WhiteVel - self.TargetVel
        Closing = (Error > 0 and ErrorRate < 0) or (Error < 0 and ErrorRate > 0)
        LeadTime = Pd['Kd'] * 0.1 * (Pd['PdApproachingDamping'] if Closing else Pd['PdChasingDamping'])
        # Max Correction Clamp caps how far ahead the prediction may look, as a fraction of the bar's length
        MaxLead = max(Pd['PdClamp'], 0.0) * (Reading['Bottom'] - Reading['Top'])
        Predicted = Error + max(-MaxLead, min(MaxLead, ErrorRate * LeadTime))

        # Hysteresis: inside the deadband keep doing what we were doing, which stops click chatter when centred.
        # Kp is the proportional gain: higher reacts to smaller offsets (1.0 = a half line-height deadband)
        Deadband = max(2.0, WhiteHeight * 0.5) / max(Pd['Kp'], 0.1)
        if abs(Predicted) < Deadband:
            ShouldHold = self.State.MousePressed
        else:
            ShouldHold = Predicted < 0
        self.SetMouse(ShouldHold, Now)

        self.Trace(Now, f"x={CenterX} white={WhiteCenter} target={TargetCenter} err={Error} "
                        f"vW={self.WhiteVel:.0f} vT={self.TargetVel:.0f} pred={Predicted:.1f} hold={ShouldHold} "
                        f"groups={Reading['Groups']} bounds={Reading['Top']}-{Reading['Bottom']}")

        self.State.PreviousError = Error
        self.State.PreviousTargetY = TargetCenter
        self.State.LastScanTime = Now

        return True


class AutomatedFishingSystem:
    
    def __init__(self):
        pyautogui.PAUSE = 0

        if not IsMac:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except:
                    pass

        try:
            SetHighPriority(True)
        except Exception as E:
            print(f"Could not set process priority: {E}")

        ConfigPath = os.path.join(GetDataDir(), "Auto Fish Settings.json")
        
        self.Config = ConfigurationManager(ConfigPath)
        self.Config.LoadFromDisk()
        
        self.State = MacroStateManager()
        
        self.OcrManager = OCRManager()
        self.FruitDetector = DevilFruitDetector(self.OcrManager, self.Config)
        self.BaitReader = BaitListReader(self.OcrManager, self.Config)
        self.Notifier = WebhookNotifier(self.Config, self.State)
        self.SoundDetector = MegalodonSoundDetector(self.Config)
        self.InputController = InputController(self.Config)
        self.MinigameController = FishingMinigameController(self.Config, self.State)
        self.PointSelector = PointSelector()
        self.IsAdmin = HasInputPermission()
        
        self.RegionSelectorActive = False
        self.ActiveRegionSelector = None
        self.FastMode = False
        
        self.CurrentlyRebindingHotkey = None
        self.ToggleLock = Lock()
        self.LoopThread = None

        self.RegisterHotkeys()

    def RegisterHotkeys(self):
        try:
            Hotkeys = self.Config.Settings['Hotkeys']
            keyboard.add_hotkey(Hotkeys['StartStop'], self.ToggleMacro)
            keyboard.add_hotkey(Hotkeys['Exit'], self.TerminateApp)
        except Exception as E:
            print(f"Error setting up hotkeys: {E}")
    
    def ToggleMacro(self):
        # The hotkey and the UI can both toggle; serialize so a double press can't start two loops
        with self.ToggleLock:
            self.State.IsRunning = not self.State.IsRunning

            if self.State.IsRunning:
                # After a quick stop/start the previous loop may still be finishing a step (every step checks
                # IsRunning, so this is short). Retire it before setting up the new session so its exit can't
                # close out the new session's timer; the token covers a loop that outlives the wait
                if self.LoopThread and self.LoopThread.is_alive():
                    self.State.IsRunning = False
                    self.LoopThread.join(timeout=5)
                self.State.LoopId += 1
                self.State.IsRunning = True

                self.State.UpdateStatus("Starting macro...")
                self.State.SessionStartTime = time.time()
                self.State.RobloxWindowFocused = False
                self.State.RodEquipped = False
                self.State.BaitRescanNeeded = True
                self.State.ConsecutiveRecastTimeouts = 0
                self.State.LastPeriodicStatsTime = time.time()
                self.State.FishAtLastStats = self.State.TotalFishCaught
                # The player may have cleared their hotbar while stopped
                self.State.UnstorableSlots.clear()

                if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and self.Config.Settings['LoggingOptions']['LogMacroState']:
                    self.Notifier.SendNotification("Macro started.")

                self.LoopThread = threading.Thread(target=self.ExecuteMacroLoop, args=(self.State.LoopId,), daemon=True)
                self.LoopThread.start()
            else:
                self.State.UpdateStatus("Stopping macro...")
                if self.State.SessionStartTime:
                    self.State.CumulativeUptime += time.time() - self.State.SessionStartTime
                    self.State.SessionStartTime = None
                if self.State.MousePressed:
                    pyautogui.mouseUp()
                    self.State.MousePressed = False

                if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and self.Config.Settings['LoggingOptions']['LogMacroState']:
                    self.Notifier.SendNotification(f"Macro stopped. Fish this session: {self.State.TotalFishCaught} | Devil Fruits: {self.State.TotalDevilFruits}")

                self.State.UpdateStatus("Idle")
    
    def ModifyScanArea(self):
        if self.RegionSelectorActive:
            if self.ActiveRegionSelector:
                try:
                    self.ActiveRegionSelector.RootWindow.after(10, self.ActiveRegionSelector.CloseWindow)
                except:
                    pass
            return
        
        self.RegionSelectorActive = True
        
        def RunSelector():
            try:
                self.ActiveRegionSelector = RunOnMainThread(lambda: RegionSelectionWindow(None, self.Config.Settings['ScanArea'], self.HandleRegionComplete))
            finally:
                self.RegionSelectorActive = False
                self.ActiveRegionSelector = None
        
        threading.Thread(target=RunSelector, daemon=True).start()

    def HandleRegionComplete(self, Coords):
        self.Config.Settings['ScanArea'] = Coords
        self.Config.SaveToDisk()
        self.ActiveRegionSelector = None
        self.RegionSelectorActive = False
    
    def TerminateApp(self):
        os._exit(0)
    
    def CheckPeriodicStats(self):
        LogOpts = self.Config.Settings['LoggingOptions']
        if not self.Config.Settings['DevilFruitStorage']['WebhookUrl'] or not LogOpts['LogPeriodicStats'] or self.State.LastPeriodicStatsTime is None:
            return
        
        IntervalSeconds = LogOpts['PeriodicStatsIntervalMinutes'] * 60
        if (time.time() - self.State.LastPeriodicStatsTime) < IntervalSeconds:
            return
        
        FishThisInterval = self.State.TotalFishCaught - self.State.FishAtLastStats
        FishPerMin = FishThisInterval / LogOpts['PeriodicStatsIntervalMinutes'] if LogOpts['PeriodicStatsIntervalMinutes'] > 0 else 0
        
        Elapsed = self.State.GetElapsedTime()
        H = int(Elapsed // 3600)
        M = int((Elapsed % 3600) // 60)
        S = int(Elapsed % 60)
        
        OverallFPH = self.State.GetFishPerHour()
        RarityBreakdown = ", ".join(f"{R}: {C}" for R, C in self.State.DevilFruitsByRarity.items() if C)

        self.Notifier.SendNotification(
            f"Stats (last {LogOpts['PeriodicStatsIntervalMinutes']}m)\n"
            f"Caught: {FishThisInterval} ({FishPerMin:.1f}/min)\n"
            f"Total: {self.State.TotalFishCaught} | Uptime: {H}:{M:02d}:{S:02d}\n"
            f"Rate: {OverallFPH:.1f}/hr | Timeouts: {self.State.TotalRecastTimeouts}\n"
            f"Devil Fruits: {self.State.TotalDevilFruits}" + (f" ({RarityBreakdown})" if RarityBreakdown else "")
        )
        
        self.State.LastPeriodicStatsTime = time.time()
        self.State.FishAtLastStats = self.State.TotalFishCaught
    
    def ExecuteCastSequence(self):
        Points = self.Config.Settings['ClickPoints']
        
        if not Points['Water']:
            return False
        
        self.EquipRod()
                
        if not self.State.IsRunning:
            return False
        
        self.State.UpdateStatus("Casting fishing line")
        SetCursorPos(Points['Water']['x'], Points['Water']['y'])
        time.sleep(self.Config.Settings['TimingDelays']['AntiDetection']['CursorAntiDetectDelay'])
        NudgeMouse(1)
        
        if not self.State.IsRunning:
            return False
        
        pyautogui.mouseDown()
        time.sleep(self.Config.Settings['FishingControl']['Timing']['CastHoldDuration'])
        
        if not self.State.IsRunning:
            pyautogui.mouseUp()
            return False
        
        pyautogui.mouseUp()
        return True
    
    def ExecuteMacroLoop(self, LoopId):
        LastActivity = time.time()
        ErrorCount = 0
        MaxConsecutiveErrors = 5

        while self.State.IsRunning and LoopId == self.State.LoopId:
            try:
                LastActivity = time.time()

                self.State.UpdateStatus("Starting new fishing cycle")
                LastActivity = time.time()
                self.MinigameController.ResetMinigame()
                
                if self.State.MousePressed:
                    self.State.UpdateStatus("Releasing mouse from previous cycle")
                    pyautogui.mouseUp()
                    self.State.MousePressed = False
                
                if not self.State.IsRunning:
                    break
                
                self.State.UpdateStatus("Beginning pre-cast sequence")
                if not self.ExecutePreCast():
                    self.State.UpdateStatus("Pre-cast sequence failed - restarting cycle")
                    continue
                
                self.State.UpdateStatus("Pre-cast sequence complete")
                
                if not self.State.IsRunning:
                    break

                self.State.UpdateStatus("Casting fishing line")
                if not self.ExecuteCastSequence():
                    self.State.UpdateStatus("Cast failed - restarting cycle")
                    continue

                # Count down on cast rather than catch so a miscount rescans early instead of fishing baitless
                if self.State.BaitRemaining is not None:
                    self.State.BaitRemaining -= 1

                self.State.UpdateStatus("Waiting for bobber to appear")
                if not self.MinigameController.WaitForBobber():
                    self.State.UpdateStatus("Bobber timeout - recasting")
                    self.State.HandleRecastTimeout()
                    # A cast that never produced a bobber usually means the rod wasn't in hand (a dropped key
                    # press), and EquipRod skips when it thinks the rod is out, so force a real re-equip now
                    # instead of waiting for three full timeouts
                    self.State.RodEquipped = False

                    LogOpts = self.Config.Settings['LoggingOptions']
                    if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and LogOpts['LogRecastTimeouts']:
                        if self.State.ConsecutiveRecastTimeouts == 3:
                            self.Notifier.SendNotification(f"3 consecutive recast timeouts ({self.Config.Settings['FishingControl']['Timing']['RecastTimeout']}s). Total: {self.State.TotalRecastTimeouts}")
                        elif self.State.ConsecutiveRecastTimeouts == 10:
                            self.Notifier.SendNotification(f"10 consecutive recast timeouts — macro may be stuck. Total: {self.State.TotalRecastTimeouts}")
                        elif self.State.ConsecutiveRecastTimeouts > 10 and self.State.ConsecutiveRecastTimeouts % 10 == 0:
                            self.Notifier.SendNotification(f"{self.State.ConsecutiveRecastTimeouts} consecutive timeouts. Total: {self.State.TotalRecastTimeouts}")
                    
                    if self.State.ConsecutiveRecastTimeouts >= 3:
                        print(f"{self.State.ConsecutiveRecastTimeouts} consecutive recast timeouts - attempting pre-execution reset")
                        self.State.UpdateStatus(f"[{self.State.ConsecutiveRecastTimeouts} timeouts] Running pre-execution reset...")
                        self.State.RobloxWindowFocused = False
                        self.State.RodEquipped = False
                        if not self.ExecutePreCast(ForcePreCast=True):
                            self.State.UpdateStatus("Pre-execution reset failed - continuing anyway")
                    
                    continue

                self.State.ResetConsecutiveTimeouts()
                self.State.UpdateStatus("Bobber ready - starting minigame")
                
                if self.Config.Settings['FishingModes']['MegalodonSound']:
                    SoundDetectionComplete = threading.Event()
                    IsMegalodon = [True]
                    
                    def CheckSound():
                        if not self.SoundDetector.Listen():
                            IsMegalodon[0] = False
                        SoundDetectionComplete.set()
                    
                    SoundThread = threading.Thread(target=CheckSound, daemon=True)
                    SoundThread.start()
                    
                    self.State.UpdateStatus("Entering minigame control loop (checking for megalodon)")
                    while self.State.IsRunning:
                        if not self.MinigameController.ControlMinigame():
                            self.State.UpdateStatus("Minigame control loop ended")
                            break
                        
                        if SoundDetectionComplete.is_set():
                            if not IsMegalodon[0]:
                                self.State.UpdateStatus("Not megalodon - recasting")
                                if self.State.MousePressed:
                                    pyautogui.mouseUp()
                                    self.State.MousePressed = False
                                break
                            else:
                                self.State.UpdateStatus("Megalodon detected - continuing minigame")
                                SoundDetectionComplete.clear()
                    
                    if not IsMegalodon[0]:
                        continue
                else:
                    self.State.UpdateStatus("Entering minigame control loop")
                    while self.State.IsRunning:
                        if not self.MinigameController.ControlMinigame():
                            self.State.UpdateStatus("Minigame control loop ended")
                            break
                
                if self.State.IsRunning:
                    ErrorCount = 0
                    self.State.UpdateStatus("Fish caught successfully!")
                    self.State.IncrementFishCount()
                    threading.Thread(target=self.ScanForCaughtFruit, daemon=True).start()
                    self.State.UpdateStatus(f"Total fish: {self.State.TotalFishCaught}")
                    self.CheckPeriodicStats()
                    
                    FishEndDelay = self.Config.Settings['FishingControl']['Timing']['FishEndDelay']
                    self.State.UpdateStatus(f"Waiting {FishEndDelay}s before next cast")
                    Remaining = FishEndDelay
                    while Remaining > 0 and self.State.IsRunning:
                        Increment = min(0.1, Remaining)
                        time.sleep(Increment)
                        Remaining -= Increment
            
            except Exception as E:
                ErrorCount += 1
                self.State.UpdateStatus(f"Error: {str(E)[:30]}")
                print(f"Error in Main: {E}")
                print(f"Error occurred after {time.time() - LastActivity:.1f}s of inactivity")
                print(f"Last status: {self.State.CurrentStatus}")
                traceback.print_exc()
                
                LogOpts = self.Config.Settings['LoggingOptions']
                if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and LogOpts['LogErrors']:
                    self.Notifier.SendNotification(f"Macro error (#{ErrorCount}): {E}")
                
                if self.State.MousePressed:
                    try:
                        pyautogui.mouseUp()
                        self.State.MousePressed = False
                    except:
                        pass
                
                if ErrorCount >= MaxConsecutiveErrors:
                    self.State.UpdateStatus(f"Too many errors ({MaxConsecutiveErrors}) - stopping")
                    if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and LogOpts['LogErrors']:
                        self.Notifier.SendNotification(f"Macro stopped after {MaxConsecutiveErrors} consecutive errors")
                    break
                
                self.State.UpdateStatus(f"Recovering from error... ({ErrorCount}/{MaxConsecutiveErrors})")
                time.sleep(2)
                self.State.RobloxWindowFocused = False
                self.State.RodEquipped = False
                continue

        # A newer loop owns the session now; closing out the timer here would stop its clock
        if LoopId != self.State.LoopId:
            return

        # Ended by errors rather than the hotkey: flip the switch so the UI and a later F1 agree with reality
        self.State.IsRunning = False
        if self.State.SessionStartTime:
            self.State.CumulativeUptime += time.time() - self.State.SessionStartTime
            self.State.SessionStartTime = None

        if self.State.MousePressed:
            try:
                pyautogui.mouseUp()
                self.State.MousePressed = False
            except:
                pass

        self.State.UpdateStatus("Idle")

    def ExecutePreCast(self, ForcePreCast=False):
        if not self.State.RobloxWindowFocused:
            self.State.UpdateStatus("Focusing Roblox window")
            FocusRetries = 0
            MaxFocusRetries = 3
            
            while FocusRetries < MaxFocusRetries and self.State.IsRunning:
                if self.InputController.FocusRobloxWindow():
                    self.State.RobloxWindowFocused = True
                    self.State.UpdateStatus("Window focused")
                    time.sleep(self.Config.Settings['TimingDelays']['RobloxWindow']['RobloxPostFocusDelay'])
                    break
                else:
                    FocusRetries += 1
                    self.State.UpdateStatus(f"Failed to focus window (attempt {FocusRetries}/{MaxFocusRetries})")
                    time.sleep(1)
            
            if not self.State.RobloxWindowFocused:
                self.State.UpdateStatus("Could not focus Roblox window - will retry next cycle")
                return False

        if not self.State.IsRunning:
            return False

        if self.Config.Settings['AutomationFeatures']['AutoBuyBait']:
            Points = self.Config.Settings['ClickPoints']
            if Points['ShopLeft'] and Points['ShopCenter'] and Points['ShopRight']:
                if ForcePreCast or self.State.BaitPurchaseCounter == 0 or self.State.BaitPurchaseCounter >= self.Config.Settings['AutomationFrequencies']['LoopsPerPurchase']:
                    self.ExecuteBaitPurchase()
                    self.State.BaitRescanNeeded = True
                    if not self.State.IsRunning:
                        return False
                    self.State.BaitPurchaseCounter = 1
                else:
                    self.State.UpdateStatus(f"Skipping bait purchase ({self.State.BaitPurchaseCounter}/{self.Config.Settings['AutomationFrequencies']['LoopsPerPurchase']})")
                    self.State.BaitPurchaseCounter += 1

        if not self.State.IsRunning:
            return False
        
        if self.Config.Settings['AutomationFeatures']['AutoStoreFruit']:
            if self.FruitStorageDue(ForcePreCast):
                self.ExecuteFruitStorage()
                if not self.State.IsRunning:
                    return False
                self.State.FruitStorageCounter = 1
                self.State.FruitPendingStore = False
            else:
                self.State.FruitStorageCounter += 1

        if not self.State.IsRunning:
            return False

        # Bait goes last: the shop and fruit storage both swap items, which undid an earlier pick
        if self.Config.Settings['AutomationFeatures']['AutoSelectTopBait'] and self.Config.Settings['AutomationFeatures']['SmartBaitSelect']:
            self.ExecuteSmartBaitSelect(ForcePreCast)

        # Without a Top Bait Point there's nothing to click; fish with whatever bait is equipped instead of
        # failing every pre-cast (which left the macro running but never casting)
        elif self.Config.Settings['AutomationFeatures']['AutoSelectTopBait'] and self.Config.Settings['ClickPoints']['Bait']:
            LoopsPerTopBait = self.Config.Settings['AutomationFrequencies'].get('LoopsPerTopBait', 1)
            if ForcePreCast or self.State.TopBaitCounter == 0 or self.State.TopBaitCounter >= LoopsPerTopBait:
                self.ExecuteSelectTopBait()
                self.State.TopBaitCounter = 1
            else:
                self.State.TopBaitCounter += 1

        if not self.State.IsRunning:
            return False

        self.State.UpdateStatus("Pre-cast complete")
        return True
    
    def FruitStorageDue(self, ForcePreCast=False):
        Freq = self.Config.Settings['AutomationFrequencies']
        Counter = self.State.FruitStorageCounter
        if ForcePreCast or Counter == 0:
            return True
        # Storing walks every fruit slot and swaps the rod out, so after each catch it cost seconds even with
        # no fruit caught. With OCR loaded the catch-time scan says when a fruit landed; sweep occasionally for misses
        if self.Config.Settings['AutomationFeatures']['StoreOnlyWhenDetected'] and self.OcrManager.IsReady():
            if self.State.FruitPendingStore:
                return True
            SweepLoops = Freq.get('FruitSweepLoops', 25)
            return SweepLoops > 0 and Counter >= SweepLoops
        return Counter >= Freq['LoopsPerStore']

    def ExecuteBaitPurchase(self):
        self.State.LastBaitPurchaseTime = time.time()
        self.State.UpdateStatus("Opening Shop")
        keyboard.press_and_release('e')
        time.sleep(self.Config.Settings['TimingDelays']['PreCast']['SetPrecastEDelay'])
        if not self.State.IsRunning:
            return
        
        Points = self.Config.Settings['ClickPoints']

        self.UnequipAll()
        
        self.State.UpdateStatus("Clicking shop left button")
        self.InputController.ClickPoint(Points['ShopLeft'])
        if not self.State.IsRunning:
            return
        
        self.State.UpdateStatus("Clicking shop center button")
        self.InputController.ClickPoint(Points['ShopCenter'])
        if not self.State.IsRunning:
            return
        
        Quantity = self.Config.Settings['AutomationFrequencies']['LoopsPerPurchase']
        self.State.UpdateStatus(f"Entering quantity: {Quantity}")
        keyboard.write(str(Quantity))
        time.sleep(self.Config.Settings['TimingDelays']['PreCast']['PreCastTypeDelay'])
        if not self.State.IsRunning:
            return
        
        self.State.UpdateStatus("Confirming left button")
        self.InputController.ClickPoint(Points['ShopLeft'])
        if not self.State.IsRunning:
            return
        
        self.State.UpdateStatus("Clicking shop right button")
        self.InputController.ClickPoint(Points['ShopRight'])
        if not self.State.IsRunning:
            return
        
        self.State.UpdateStatus("Final shop center click")
        self.InputController.ClickPoint(Points['ShopCenter'])
        
        self.State.UpdateStatus("Bait purchased successfully")
    
    def ScanForCaughtFruit(self):
        # Runs off the fishing thread after each catch and watches for GPO's devil fruit drop banner
        if not self.OcrManager.Enabled:
            return
        self.State.FruitScanId += 1
        ScanId = self.State.FruitScanId
        CatchNumber = self.State.TotalFishCaught
        # OCR runs on CPU without CUDA (~1-4s per read), so allow a couple of reads for the banner to appear.
        # A newer catch takes over, so only one scan competes with the minigame for CPU at a time
        Deadline = time.time() + 7.0
        Attempt = 0
        while time.time() < Deadline and self.State.IsRunning and ScanId == self.State.FruitScanId:
            try:
                Text = self.FruitDetector.ReadRegion()
            except Exception as E:
                LogLine(f"FRUIT scan error: {E}")
                return
            if Text is None:
                return
            Attempt += 1
            Notices = self.FruitDetector.ParseNotices(Text)
            LogLine(f"FRUIT scan catch#{CatchNumber} try{Attempt}: {Text!r} -> {Notices}")
            if Notices['Drop']:
                if self.RecordFruitDrop(Notices['Pity'], Notices['ItemFruit']):
                    self.SaveFruitScanDebug(CatchNumber)
                return
            if Notices['ItemFruit']:
                self.NameRecentFruit(Notices['ItemFruit'])
            time.sleep(0.5)

    def RecordFruitDrop(self, Pity, Name=None):
        # Returns True for a new drop. The banner stays up for several seconds and back-to-back catches can both
        # read it, so a sighting within 12s of the last one is the same drop unless the pity counter moved, and the
        # same pity value within 45s is the same drop too. Two drops can't share a pity value that close together:
        # every drop moves it (up by one, or back to 0 on a legendary)
        with self.State.FruitLock:
            Now = time.time()
            Last = self.State.LastFruitDrop
            if Last:
                Age = Now - Last['Time']
                BothKnown = Pity is not None and Last['Pity'] is not None
                if (Age < 12 and not (BothKnown and Pity != Last['Pity'])) or (Age < 45 and BothKnown and Pity == Last['Pity']):
                    if Last['Pity'] is None and Pity is not None:
                        Last['Pity'] = Pity
                        Last['Entry']['pity'] = Pity
                        if Pity == 0:
                            self.State.UpdateFruit(Last['Entry'], Rarity="Legendary")
                    if Name and Last['Entry']['name'] == "Unknown":
                        self.State.UpdateFruit(Last['Entry'], Name=Name, Rarity=FruitRarities[Name])
                    return False

            # "LEGENDARY PITY" counts drops since the last legendary, so a reset to 0 means this drop was legendary
            # and any other value means it wasn't. A name seen with the banner is more specific still
            Rarity = FruitRarities[Name] if Name else ("Legendary" if Pity == 0 else "Unknown")
            self.State.IncrementDevilFruitCount(Name, Rarity, Pity)
            self.State.LastFruitDrop = {'Time': Now, 'Pity': Pity, 'Entry': self.State.FruitHistory[-1]}
            self.State.FruitPendingStore = True

        Label = f"{Name} ({Rarity})" if Name else ("(Legendary)" if Rarity == "Legendary" else "(not legendary)")
        PityText = f", legendary pity {Pity}/40" if Pity is not None else ""
        LogLine(f"FRUIT drop #{self.State.TotalDevilFruits}: {Label}{PityText}")
        self.Notifier.SendNotification(f"Devil Fruit {Label} caught!{PityText}")
        return True

    def NameRecentFruit(self, Name):
        # "New Item <Fruit>" shows the first time a fruit type is obtained or stored, often a few catches after its
        # drop banner. It never counts on its own; it names the newest unnamed drop whose rarity fits
        with self.State.FruitLock:
            Now = time.time()
            LastName, LastTime = self.State.LastNamedFruit
            # The popup lingers, so a repeat of the same name within a minute is the same popup
            if Name == LastName and Now - LastTime < 60:
                return
            Rarity = FruitRarities[Name]
            for Entry in reversed(self.State.FruitHistory):
                if Now - Entry['timestamp'] > 600:
                    break
                if Entry['name'] != "Unknown":
                    continue
                Fits = Entry['rarity'] == Rarity or (
                    Entry['rarity'] == "Unknown" and (Entry['pity'] is None or Rarity not in ("Legendary", "Mythical")))
                if Fits:
                    self.State.LastNamedFruit = (Name, Now)
                    self.State.UpdateFruit(Entry, Name=Name, Rarity=Rarity)
                    LogLine(f"FRUIT named {Name} ({Rarity}) from New Item popup")
                    return

    def SaveFruitScanDebug(self, CatchNumber, Keep=20):
        # Save the full screen, the scanned region and the OCR text for a counted drop, so a count can be checked
        # against what was on screen and the Fruit Detection Area corrected if needed
        try:
            DebugDir = os.path.join(GetDataDir(), "FruitScanDebug")
            os.makedirs(DebugDir, exist_ok=True)
            Base = os.path.join(DebugDir, f"catch_{CatchNumber:05d}")
            with NewScreenCapture() as ScreenCapture:
                Shot = ScreenCapture.grab(ScreenCapture.monitors[1])
            Full = PILImage.frombytes('RGB', Shot.size, Shot.rgb)
            Full.thumbnail((Full.width // 2, Full.height // 2))
            Full.save(Base + "_screen.jpg", quality=80)
            if self.FruitDetector.LastScanImage is not None:
                PILImage.fromarray(self.FruitDetector.LastScanImage).save(Base + "_region.png")
            with open(Base + "_ocr.txt", "w", encoding="utf-8") as F:
                F.write(f"region={self.Config.Settings['OCRSettings']}\ntext={self.FruitDetector.LastRawText!r}\n")
            Catches = sorted({N.split("_")[1] for N in os.listdir(DebugDir) if N.startswith("catch_")})
            for Old in Catches[:-Keep]:
                for N in os.listdir(DebugDir):
                    if N.startswith(f"catch_{Old}_"):
                        os.remove(os.path.join(DebugDir, N))
        except Exception as E:
            print(f"Fruit scan debug save failed: {E}")

    def ExecuteFruitStorage(self):
        # Moves caught fruits into storage. Counting happens at catch time from the drop banner, never here: the
        # store button's colour check can't tell which fruit (or whether one) was stored
        self.State.UpdateStatus("Storing Devil Fruit")

        Points = self.Config.Settings['ClickPoints']
        DevilFruitSlots = self.Config.Settings['InventoryHotkeys']['DevilFruits']
        Delays = self.Config.Settings['TimingDelays']['DevilFruitStorage']

        if self.Config.Settings['DevilFruitStorage']['StoreToBackpack']:
            BackpackLocations = Points.get('BackpackLocations', [])
            keyboard.press_and_release('`')

            for SlotIdx, Slot in enumerate(DevilFruitSlots):
                if not self.State.IsRunning:
                    return

                TargetLocation = BackpackLocations[SlotIdx] if SlotIdx < len(BackpackLocations) else None
                if not TargetLocation or not Points['StoreFruit']:
                    continue

                self.State.UpdateStatus(f"Opening inventory for slot {Slot} ({SlotIdx+1}/{len(DevilFruitSlots)})")
                time.sleep(Delays['StoreFruitHotkeyDelay'])
                if not self.State.IsRunning:
                    return

                self.State.UpdateStatus(f"Clicking fruit location for slot {Slot}")
                self.InputController.ClickPoint(TargetLocation)
                time.sleep(Delays['StoreFruitClickDelay'])
                if not self.State.IsRunning:
                    return

                self.State.UpdateStatus(f"Checking Fruit Status for slot {Slot}")
                InitGreen = ColorDetector.DetectGreenish(Points['StoreFruit'])

                self.InputController.ClickPoint(Points['StoreFruit'])
                if not self.State.IsRunning:
                    return

                SetCursorPos(TargetLocation['x'], TargetLocation['y'])
                time.sleep(0.1)
                self.HumanizeMovement()
                if not self.State.IsRunning:
                    return

                pyautogui.mouseDown()
                time.sleep(0.1)
                if not self.State.IsRunning:
                    pyautogui.mouseUp()
                    return

                SetCursorPos(TargetLocation['x'], TargetLocation['y'] - 150)
                self.HumanizeMovement()
                pyautogui.mouseUp()

                time.sleep(Delays['StoreFruitClickDelay'] + 0.5)

                if InitGreen and not ColorDetector.DetectGreenish(Points['StoreFruit']):
                    self.State.UpdateStatus(f"Fruit stored (slot {Slot})")
                    self.Notifier.SendNotification(f"Devil Fruit stored successfully! (Slot {Slot})")
                elif InitGreen:
                    self.State.UpdateStatus(f"Fruit storage failed (slot {Slot})")
                    self.Notifier.SendNotification(f"Devil Fruit could not be stored. (Slot {Slot})")

            keyboard.press_and_release('`')

        elif Points['StoreFruit']:
            Hotkeys = self.Config.Settings['InventoryHotkeys']
            SelectDelay = self.Config.Settings['TimingDelays']['Inventory']['RodSelectDelay']
            for Slot in DevilFruitSlots:
                if not self.State.IsRunning:
                    return
                # A duplicate the game refused earlier is still in its slot; retrying it every sweep only costs time
                if Slot in self.State.UnstorableSlots:
                    continue

                # Hold another item first: the slot keys toggle, so pressing an already-held slot would put it away
                self.State.RodEquipped = False
                self.TapKey(Hotkeys['Alternate'])
                time.sleep(SelectDelay)
                self.TapKey(Slot)
                time.sleep(Delays['StoreFruitHotkeyDelay'])
                if not self.State.IsRunning:
                    return

                # The store button only shows (green) while holding a storable fruit; clicking without it would
                # click into the world with whatever the slot holds
                self.State.UpdateStatus(f"Checking slot {Slot} for a fruit")
                if not ColorDetector.DetectGreenish(Points['StoreFruit']):
                    continue

                Stored = False
                for _ in range(2):
                    self.State.UpdateStatus(f"Storing fruit from slot {Slot}")
                    self.InputController.ClickPoint(Points['StoreFruit'])
                    time.sleep(Delays['StoreFruitClickDelay'] + 0.5)
                    if not self.State.IsRunning:
                        return
                    if not ColorDetector.DetectGreenish(Points['StoreFruit']):
                        Stored = True
                        break

                if Stored:
                    self.State.UpdateStatus(f"Fruit stored (slot {Slot})")
                    self.Notifier.SendNotification(f"Devil Fruit stored successfully! (Slot {Slot})")
                    continue

                # Usually "You can only store one of each fruit!": the storage already has this one
                self.State.UnstorableSlots.add(Slot)
                LogLine(f"FRUIT slot {Slot} could not be stored (likely a duplicate); skipping it until restart")
                if not self.Config.Settings['DevilFruitStorage'].get('DropUnstorable', False):
                    self.State.UpdateStatus(f"Slot {Slot} can't be stored - keeping it")
                    self.Notifier.SendNotification(f"Devil Fruit in slot {Slot} could not be stored (duplicate?) - kept in inventory.")
                    continue

                # Opt-in only: a dropped fruit despawns after 10 minutes
                self.State.UpdateStatus(f"Slot {Slot} can't be stored - dropping it")
                self.Notifier.SendNotification(f"Devil Fruit in slot {Slot} could not be stored (duplicate?) - dropped.")
                if self.Config.Settings['AutomationFeatures']['AutoBuyBait']:
                    keyboard.press_and_release('shift')
                    time.sleep(Delays['StoreFruitShiftDelay'])
                keyboard.press_and_release('backspace')
                time.sleep(Delays['StoreFruitBackspaceDelay'])
                if self.Config.Settings['AutomationFeatures']['AutoBuyBait']:
                    keyboard.press_and_release('shift')
                self.State.UnstorableSlots.discard(Slot)

    def ExecuteSelectTopBait(self):
        Points = self.Config.Settings['ClickPoints']
        if not Points['Bait']:
            return False
        
        self.EquipRod()

        self.State.UpdateStatus("Selecting Top Bait")
        self.InputController.ClickPoint(Points['Bait'])
        time.sleep(self.Config.Settings['TimingDelays']['Inventory']['AutoSelectBaitDelay'])

        return True

    def ExecuteSmartBaitSelect(self, ForcePreCast=False):
        LoopsPerTopBait = self.Config.Settings['AutomationFrequencies'].get('LoopsPerTopBait', 1)
        OutOfBait = self.State.BaitRemaining is not None and self.State.BaitRemaining <= 0
        # With a readable count the rescan waits for it to run out; otherwise fall back to the loop interval
        CountUnknown = self.State.BaitRemaining is None and self.State.TopBaitCounter >= LoopsPerTopBait

        if not (ForcePreCast or self.State.BaitRescanNeeded or OutOfBait or CountUnknown):
            self.State.TopBaitCounter += 1
            # Something swapped items since the last pick; re-equipping can fall back to another bait, so click it again
            if not self.State.RodEquipped and self.State.SelectedBaitPoint:
                self.EquipRod()
                if not self.State.IsRunning:
                    return False
                self.State.UpdateStatus(f"Re-selecting {self.State.SelectedBait}")
                self.InputController.ClickPoint(self.State.SelectedBaitPoint)
                time.sleep(self.Config.Settings['TimingDelays']['Inventory']['AutoSelectBaitDelay'])
            return True

        self.EquipRod()
        if not self.State.IsRunning:
            return False

        self.State.UpdateStatus("Scanning bait list")
        Baits = self.BaitReader.ScanBaits()
        self.State.TopBaitCounter = 1
        self.State.BaitRescanNeeded = False

        ShopBait, ShopTier = self.ShopBaitTier()
        PurchaseFailed = False
        if self.ShouldRestockShopBait(Baits, ShopTier):
            # The shop bait is gone and nothing better is left: buy more before falling back to worse tiers
            self.State.UpdateStatus(f"Out of {ShopBait} - buying more")
            self.ExecuteBaitPurchase()
            # Counts as this cycle's purchase so the regular Auto Buy doesn't immediately buy again
            self.State.BaitPurchaseCounter = 1
            if not self.State.IsRunning:
                return False

            self.EquipRod()
            if not self.State.IsRunning:
                return False

            self.State.UpdateStatus("Rescanning bait list")
            Baits = self.BaitReader.ScanBaits()
            PurchaseFailed = Baits is not None and not any(B['Tier'] == ShopTier for B in Baits)

        if not Baits:
            self.State.BaitRemaining = None
            if Baits is not None and self.State.SelectedBait:
                self.State.UpdateStatus("No bait left in stock")
                self.NotifyBaitChange(f"Ran out of {self.State.SelectedBait} and no other bait was detected.")
                self.State.SelectedBait = None
            self.State.SelectedBaitPoint = None
            # OCR unavailable or nothing read: behave like the plain top bait selector
            if self.Config.Settings['ClickPoints']['Bait']:
                return self.ExecuteSelectTopBait()
            return True

        Best = Baits[0]
        Previous = self.State.SelectedBait
        self.State.UpdateStatus(f"Selecting {Best['Name']}" + (f" (x{Best['Count']})" if Best['Count'] is not None else ""))
        self.InputController.ClickPoint(Best['Point'])
        time.sleep(self.Config.Settings['TimingDelays']['Inventory']['AutoSelectBaitDelay'])

        self.State.SelectedBait = Best['Name']
        self.State.BaitRemaining = Best['Count']
        self.State.SelectedBaitPoint = Best['Point']

        if Previous and Previous != Best['Name']:
            Reason = f" (couldn't buy {ShopBait} - out of money?)" if PurchaseFailed else ""
            self.NotifyBaitChange(f"Bait switched: {Previous} → {Best['Name']}" + (f" (x{Best['Count']})" if Best['Count'] is not None else "") + Reason)

        return True

    def ShopBaitTier(self):
        # Auto Buy purchases the bait the shop sells; find where it sits in the tier order. Without it in the list,
        # treat the first tier as the one to restock (the original behaviour)
        TierOrder = self.Config.Settings['BaitSelector']['TierOrder']
        Tier = self.BaitReader.MatchTier(ShopBaitName, TierOrder)
        return (TierOrder[Tier], Tier) if Tier is not None else (TierOrder[0], 0)

    def ShouldRestockShopBait(self, Baits, ShopTier):
        # Buying only helps when the shop bait is out and nothing ranked above it is left. With the default
        # Rare > Legendary > Common order that means all three are out; buying Common while Rare is merely gone
        # would spend money on bait the order says to use last
        if Baits is None or any(B['Tier'] == ShopTier for B in Baits):
            return False
        if Baits and Baits[0]['Tier'] < ShopTier:
            return False
        if not self.Config.Settings['AutomationFeatures']['AutoBuyBait']:
            return False
        Points = self.Config.Settings['ClickPoints']
        if not (Points['ShopLeft'] and Points['ShopCenter'] and Points['ShopRight']):
            return False
        # A purchase that just happened and still left us without shop bait means we are broke;
        # wait for the regular purchase cycle instead of retrying every cast
        return time.time() - self.State.LastBaitPurchaseTime > 60

    def NotifyBaitChange(self, Message):
        LogOpts = self.Config.Settings['LoggingOptions']
        if self.Config.Settings['DevilFruitStorage']['WebhookUrl'] and LogOpts['LogGeneralUpdates']:
            self.Notifier.SendNotification(Message)

    def TapKey(self, Key):
        # Roblox drops zero-length taps; hold the key briefly (Key Spam Prevention) so it registers
        keyboard.press(Key)
        time.sleep(max(self.Config.Settings['TimingDelays']['AntiDetection']['AntiMacroSpamDelay'], 0.02))
        keyboard.release(Key)

    def EquipRod(self):
        if not self.State.IsRunning:
            return False

        # Pressing the rod key again would unequip it, so skip when nothing has switched slots since the last equip
        if self.State.RodEquipped:
            return True

        # Rod key toggles, so first swap to another slot to guarantee the rod press equips instead of unequips
        SelectDelay = max(self.Config.Settings['TimingDelays']['Inventory']['RodSelectDelay'], 0.25)
        self.State.UpdateStatus("Switching to Alternate Slot")
        self.TapKey(self.Config.Settings['InventoryHotkeys']['Alternate'])
        time.sleep(SelectDelay)

        if not self.State.IsRunning:
            return False

        self.State.UpdateStatus("Switching to Fishing Rod")
        self.TapKey(self.Config.Settings['InventoryHotkeys']['Rod'])
        time.sleep(SelectDelay)

        self.State.RodEquipped = True
        return True
    
    def UnequipAll(self):
        if not self.State.IsRunning:
            return False
        
        self.State.UpdateStatus("Un-Equipping all items")
        self.State.RodEquipped = False
        self.TapKey(self.Config.Settings['InventoryHotkeys']['Alternate'])
        time.sleep(self.Config.Settings['TimingDelays']['Inventory']['RodSelectDelay'])

        if not self.State.IsRunning:
            return False

        self.TapKey(self.Config.Settings['InventoryHotkeys']['Rod'])
        time.sleep(self.Config.Settings['TimingDelays']['Inventory']['RodSelectDelay'])

        if not self.State.IsRunning:
            return False

        self.TapKey(self.Config.Settings['InventoryHotkeys']['Rod'])
        time.sleep(self.Config.Settings['TimingDelays']['Inventory']['RodSelectDelay'])

        return True
    
    def HumanizeMovement(self):
        for _ in range(5):
            NudgeMouse(1)
            time.sleep(0.05)
            if not self.State.IsRunning:
                return
        
        for _ in range(5):
            NudgeMouse(-1)
            time.sleep(0.05)
            if not self.State.IsRunning:
                return
    
    def GetLiveState(self):
        # What changes while fishing. The UI and overlay poll this twice a second, so it stays small: every
        # request takes the GIL from the minigame loop
        State = self.State
        Window = self.Config.Settings['WindowSettings']
        return {
            "isRunning": State.IsRunning,
            "currentStatus": State.CurrentStatus,
            "fishCaught": State.TotalFishCaught,
            "devilFruitsCaught": State.TotalDevilFruits,
            "devilFruitsByRarity": State.DevilFruitsByRarity,
            "lastDevilFruit": State.LastDevilFruit,
            "fruitHistory": State.FruitHistory[-10:],
            "timeElapsed": State.GetFormattedElapsedTime(),
            "fishPerHour": round(State.GetFishPerHour(), 1),
            "totalRecastTimeouts": State.TotalRecastTimeouts,
            "selectedBait": State.SelectedBait,
            "baitRemaining": State.BaitRemaining,
            "fruitPendingStore": State.FruitPendingStore,
            "ocrStatus": "ready" if self.OcrManager.IsReady() else ("loading" if self.OcrManager.Enabled else "off"),
            "alwaysOnTop": Window['AlwaysOnTop'],
            "showDebugOverlay": Window['ShowDebugOverlay'],
        }

    def GetStateForAPI(self):
        Settings = self.Config.Settings
        Points = Settings['ClickPoints']
        Features = Settings['AutomationFeatures']
        Freq = Settings['AutomationFrequencies']
        Pd = Settings['FishingControl']['PdController']
        FishTiming = Settings['FishingControl']['Timing']
        Detection = Settings['FishingControl']['Detection']
        Delays = Settings['TimingDelays']
        LogOpts = Settings['LoggingOptions']

        return {
            **self.GetLiveState(),
            "storeToBackpack": Settings['DevilFruitStorage']['StoreToBackpack'],
            "dropUnstorableFruit": Settings['DevilFruitStorage'].get('DropUnstorable', False),
            "loopsPerStore": Freq['LoopsPerStore'],
            "fruitSweepLoops": Freq.get('FruitSweepLoops', 25),
            "storeOnlyWhenDetected": Features['StoreOnlyWhenDetected'],
            "waterPoint": Points['Water'],
            "leftPoint": Points['ShopLeft'],
            "middlePoint": Points['ShopCenter'],
            "rightPoint": Points['ShopRight'],
            "storeFruitPoint": Points['StoreFruit'],
            "baitPoint": Points['Bait'],
            "backpackLocations": Points['BackpackLocations'],
            "hotkeys": Settings['Hotkeys'],
            "rodHotkey": Settings['InventoryHotkeys']['Rod'],
            "anythingElseHotkey": Settings['InventoryHotkeys']['Alternate'],
            "devilFruitHotkeys": Settings['InventoryHotkeys']['DevilFruits'],
            "autoBuyCommonBait": Features['AutoBuyBait'],
            "autoStoreDevilFruit": Features['AutoStoreFruit'],
            "autoSelectTopBait": Features['AutoSelectTopBait'],
            "smartBaitSelect": Features['SmartBaitSelect'],
            "baitTierOrder": Settings['BaitSelector']['TierOrder'],
            "kp": Pd['Kp'],
            "kd": Pd['Kd'],
            "pdClamp": Pd['PdClamp'],
            "pdApproachingDamping": Pd['PdApproachingDamping'],
            "pdChasingDamping": Pd['PdChasingDamping'],
            "castHoldDuration": FishTiming['CastHoldDuration'],
            "recastTimeout": FishTiming['RecastTimeout'],
            "fishEndDelay": FishTiming['FishEndDelay'],
            "catchEndGrace": FishTiming.get('CatchEndGrace', 0.25),
            "stateResendInterval": FishTiming['StateResendInterval'],
            "gapToleranceMultiplier": Detection['GapToleranceMultiplier'],
            "blackScreenThreshold": Detection['BlackScreenThreshold'],
            "scanLoopDelay": Detection['ScanLoopDelay'],
            "loopsPerPurchase": Freq['LoopsPerPurchase'],
            "loopsPerTopBait": Freq['LoopsPerTopBait'],
            "robloxFocusDelay": Delays['RobloxWindow']['RobloxFocusDelay'],
            "robloxPostFocusDelay": Delays['RobloxWindow']['RobloxPostFocusDelay'],
            "preCastEDelay": Delays['PreCast']['SetPrecastEDelay'],
            "preCastClickDelay": Delays['PreCast']['PreCastClickDelay'],
            "preCastTypeDelay": Delays['PreCast']['PreCastTypeDelay'],
            "preCastAntiDetectDelay": Delays['PreCast']['PreCastAntiDetectDelay'],
            "storeFruitHotkeyDelay": Delays['DevilFruitStorage']['StoreFruitHotkeyDelay'],
            "storeFruitClickDelay": Delays['DevilFruitStorage']['StoreFruitClickDelay'],
            "storeFruitShiftDelay": Delays['DevilFruitStorage']['StoreFruitShiftDelay'],
            "storeFruitBackspaceDelay": Delays['DevilFruitStorage']['StoreFruitBackspaceDelay'],
            "autoSelectBaitDelay": Delays['Inventory']['AutoSelectBaitDelay'],
            "rodSelectDelay": Delays['Inventory']['RodSelectDelay'],
            "antiMacroSpamDelay": Delays['AntiDetection']['AntiMacroSpamDelay'],
            "cursorAntiDetectDelay": Delays['AntiDetection']['CursorAntiDetectDelay'],
            "webhookUrl": Settings['DevilFruitStorage']['WebhookUrl'],
            "discordUserId": LogOpts['DiscordUserId'],
            "logDevilFruit": LogOpts['LogDevilFruit'],
            "pingDevilFruit": LogOpts['PingDevilFruit'],
            "logRecastTimeouts": LogOpts['LogRecastTimeouts'],
            "pingRecastTimeouts": LogOpts['PingRecastTimeouts'],
            "logPeriodicStats": LogOpts['LogPeriodicStats'],
            "pingPeriodicStats": LogOpts['PingPeriodicStats'],
            "logGeneralUpdates": LogOpts['LogGeneralUpdates'],
            "pingGeneralUpdates": LogOpts['PingGeneralUpdates'],
            "periodicStatsInterval": LogOpts['PeriodicStatsIntervalMinutes'],
            "logMacroState": LogOpts['LogMacroState'],
            "pingMacroState": LogOpts['PingMacroState'],
            "logErrors": LogOpts['LogErrors'],
            "pingErrors": LogOpts['PingErrors'],
            "megalodonSoundEnabled": Settings['FishingModes']['MegalodonSound'],
            "soundSensitivity": Settings['FishingModes']['SoundSensitivity'],
            "audioDeviceIndex": Settings['AudioDevice']['SelectedDeviceIndex'],
            "is_admin": self.IsAdmin,
            "platform": sys.platform,
        }

FlaskApp = Flask(__name__)
# Only the app's own windows may call the API from a browser context; any other web page could otherwise drive
# the macro (or rewrite the webhook URL) through localhost. Dev builds serve the pages from a loopback dev server
# (http://127.0.0.1:1430), installed builds from the tauri.localhost origins
CORS(FlaskApp, origins=[
    "http://tauri.localhost", "https://tauri.localhost", "tauri://localhost",
    r"^http://(127\.0\.0\.1|localhost):\d+$",
])

MacroSystem = AutomatedFishingSystem()

Port = FindFreePort()

AppPath = GetDataDir()

CleanupOrphanedPortFiles(AppPath)

PortFile = os.path.join(AppPath, f"port_{LauncherPid}.json")
with open(PortFile, 'w') as Pf:
    json.dump({"port": Port, "pid": LauncherPid}, Pf)

@FlaskApp.route('/state', methods=['GET'])
def GetState():
    # ?live=1 is the cheap poll; the full settings are only needed when the UI syncs its controls
    if request.args.get('live'):
        return jsonify(MacroSystem.GetLiveState())
    return jsonify(MacroSystem.GetStateForAPI())


@FlaskApp.route('/health', methods=['GET'])
def HealthCheck():
    return jsonify({"status": "ok", "message": "Backend running"})


@FlaskApp.route('/check_audio_device', methods=['GET'])
def CheckAudioDevice():
    if IsMac:
        try:
            Index, Device = FindMacLoopbackDevice()
            return jsonify({"found": Index is not None, "deviceName": Device['name'] if Device else None})
        except Exception as E:
            return jsonify({"found": False, "deviceName": None, "error": str(E)})
    try:
        AudioInterface = pyaudio.PyAudio()
        DeviceFound = False
        DeviceName = None
        
        try:
            WasapiInfo = AudioInterface.get_host_api_info_by_type(pyaudio.paWASAPI)
            DefaultOutputIndex = WasapiInfo.get("defaultOutputDevice")
            
            if DefaultOutputIndex is not None and DefaultOutputIndex >= 0:
                try:
                    DefaultDevice = AudioInterface.get_device_info_by_index(DefaultOutputIndex)
                    DefaultName = DefaultDevice.get("name", "")
                    
                    for Loopback in AudioInterface.get_loopback_device_info_generator():
                        if DefaultName in Loopback.get("name", ""):
                            if Loopback.get('maxInputChannels', 0) > 0:
                                DeviceFound = True
                                DeviceName = Loopback.get("name", "Unknown")
                                break
                except Exception:
                    pass
            
            if not DeviceFound:
                for Loopback in AudioInterface.get_loopback_device_info_generator():
                    if Loopback.get('maxInputChannels', 0) > 0:
                        DeviceFound = True
                        DeviceName = Loopback.get("name", "Unknown")
                        break
                        
        except Exception:
            DeviceFound = False
        finally:
            AudioInterface.terminate()
        
        return jsonify({"found": DeviceFound, "deviceName": DeviceName})
    except Exception as E:
        return jsonify({"found": False, "deviceName": None, "error": str(E)})


@FlaskApp.route('/get_audio_devices', methods=['GET'])
def GetAudioDevices():
    if IsMac:
        # Every input is offered: the loopback driver can have any name
        try:
            return jsonify({"devices": [
                {'index': Index, 'name': Device['name'], 'sampleRate': int(Device.get('default_samplerate') or 44100)}
                for Index, Device in enumerate(sounddevice.query_devices()) if Device['max_input_channels'] > 0
            ]})
        except Exception as E:
            return jsonify({"devices": [], "error": str(E)})
    try:
        AudioInterface = pyaudio.PyAudio()
        Devices = []
        
        try:
            WasapiInfo = AudioInterface.get_host_api_info_by_type(pyaudio.paWASAPI)
            
            for Loopback in AudioInterface.get_loopback_device_info_generator():
                if Loopback.get('maxInputChannels', 0) > 0:
                    Devices.append({
                        'index': Loopback.get('index'),
                        'name': Loopback.get('name', 'Unknown Device'),
                        'sampleRate': int(Loopback.get('defaultSampleRate', 44100))
                    })
        except Exception as E:
            print(f"Error getting audio devices: {E}")
        finally:
            AudioInterface.terminate()
        
        return jsonify({"devices": Devices})
    except Exception as E:
        return jsonify({"devices": [], "error": str(E)})
    

@FlaskApp.route('/set_fast_mode', methods=['POST'])
def SetFastMode():
    try:
        Data = request.json
        Enabled = Data.get('enabled', False)
        
        if Enabled:
            MacroSystem.OcrManager.Enabled = False
            MacroSystem.State.FastModeEnabled = True

            SetHighPriority(False)
        else:
            MacroSystem.OcrManager.Enabled = True
            MacroSystem.OcrManager.Initialize()
            MacroSystem.State.FastModeEnabled = False
            
            SetHighPriority(True)
        
        return jsonify({"status": "success", "fastMode": Enabled})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


@FlaskApp.route('/command', methods=['POST'])
def ProcessCommand():
    try:
        Data = request.json
        Action = Data.get('action')
        Payload = Data.get('payload')

        if not Action:
            return jsonify({"status": "error", "message": "Missing action parameter"}), 400
        
        def HandlePointSelection(AttrName):
            def OnPointSelected(PointName, Point):
                if AttrName.startswith('ClickPoints.'):
                    Key = AttrName.split('.')[1]
                    MacroSystem.Config.Settings['ClickPoints'][Key] = Point
                MacroSystem.Config.SaveToDisk()
            
            MacroSystem.PointSelector.StartSelection(AttrName, OnPointSelected)
            return jsonify({"status": "waiting_for_click"})
        
        def HandleBoolToggle(Path):
            if Payload is None:
                return jsonify({"status": "error", "message": "Missing payload"}), 400
            
            Value = Payload.lower() == 'true'
            Parts = Path.split('.')
            Current = MacroSystem.Config.Settings
            for Part in Parts[:-1]:
                Current = Current[Part]
            Current[Parts[-1]] = Value
            MacroSystem.Config.SaveToDisk()
            return jsonify({"status": "success", "value": Value})
        
        def HandleStringValue(Path):
            if Payload is None:
                return jsonify({"status": "error", "message": "Missing payload"}), 400
            
            Parts = Path.split('.')
            Current = MacroSystem.Config.Settings
            for Part in Parts[:-1]:
                Current = Current[Part]
            Current[Parts[-1]] = Payload
            MacroSystem.Config.SaveToDisk()
            return jsonify({"status": "success"})
        
        def HandleIntValue(Path):
            if Payload is None:
                return jsonify({"status": "error", "message": "Missing payload"}), 400
            
            try:
                Value = int(Payload)
                Parts = Path.split('.')
                Current = MacroSystem.Config.Settings
                for Part in Parts[:-1]:
                    Current = Current[Part]
                Current[Parts[-1]] = Value
                MacroSystem.Config.SaveToDisk()
                return jsonify({"status": "success"})
            except (ValueError, TypeError) as E:
                return jsonify({"status": "error", "message": f"Invalid integer: {str(E)}"}), 400
        
        def HandleFloatValue(Path):
            if Payload is None:
                return jsonify({"status": "error", "message": "Missing payload"}), 400
            
            try:
                Value = float(Payload)
                Parts = Path.split('.')
                Current = MacroSystem.Config.Settings
                for Part in Parts[:-1]:
                    Current = Current[Part]
                Current[Parts[-1]] = Value
                MacroSystem.Config.SaveToDisk()
                return jsonify({"status": "success"})
            except (ValueError, TypeError) as E:
                return jsonify({"status": "error", "message": f"Invalid float: {str(E)}"}), 400
        
        ActionMap = {
            'toggle_macro': lambda: HandleToggleMacro(),

            'set_water_point': lambda: HandlePointSelection('ClickPoints.Water'),
            'set_left_point': lambda: HandlePointSelection('ClickPoints.ShopLeft'),
            'set_middle_point': lambda: HandlePointSelection('ClickPoints.ShopCenter'),
            'set_right_point': lambda: HandlePointSelection('ClickPoints.ShopRight'),
            'set_store_fruit_point': lambda: HandlePointSelection('ClickPoints.StoreFruit'),
            'set_bait_point': lambda: HandlePointSelection('ClickPoints.Bait'),
            'set_loops_per_top_bait': lambda: HandleIntValue('AutomationFrequencies.LoopsPerTopBait'),

            'toggle_always_on_top': lambda: HandleBoolToggle('WindowSettings.AlwaysOnTop'),
            'toggle_debug_overlay': lambda: HandleBoolToggle('WindowSettings.ShowDebugOverlay'),
            'toggle_auto_buy_bait': lambda: HandleBoolToggle('AutomationFeatures.AutoBuyBait'),
            'toggle_auto_store_fruit': lambda: HandleBoolToggle('AutomationFeatures.AutoStoreFruit'),
            'toggle_auto_select_bait': lambda: HandleBoolToggle('AutomationFeatures.AutoSelectTopBait'),
            'toggle_smart_bait_select': lambda: HandleBoolToggle('AutomationFeatures.SmartBaitSelect'),
            'set_bait_tier_order': lambda: HandleBaitTierOrder(Payload),
            'test_bait_scan': lambda: HandleTestBaitScan(),
            'open_bait_region_selector': lambda: HandleBaitRegionSelector(),
            'toggle_store_to_backpack': lambda: HandleBoolToggle('DevilFruitStorage.StoreToBackpack'),
            'toggle_drop_unstorable_fruit': lambda: HandleBoolToggle('DevilFruitStorage.DropUnstorable'),
            'toggle_log_devil_fruit': lambda: HandleBoolToggle('LoggingOptions.LogDevilFruit'),
            'toggle_log_recast_timeouts': lambda: HandleBoolToggle('LoggingOptions.LogRecastTimeouts'),
            'toggle_log_periodic_stats': lambda: HandleBoolToggle('LoggingOptions.LogPeriodicStats'),
            'toggle_log_general_updates': lambda: HandleBoolToggle('LoggingOptions.LogGeneralUpdates'),
            'toggle_log_macro_state': lambda: HandleBoolToggle('LoggingOptions.LogMacroState'),
            'toggle_log_errors': lambda: HandleBoolToggle('LoggingOptions.LogErrors'),
            'toggle_ping_devil_fruit': lambda: HandleBoolToggle('LoggingOptions.PingDevilFruit'),
            'toggle_ping_recast_timeouts': lambda: HandleBoolToggle('LoggingOptions.PingRecastTimeouts'),
            'toggle_ping_periodic_stats': lambda: HandleBoolToggle('LoggingOptions.PingPeriodicStats'),
            'toggle_ping_general_updates': lambda: HandleBoolToggle('LoggingOptions.PingGeneralUpdates'),
            'toggle_ping_macro_state': lambda: HandleBoolToggle('LoggingOptions.PingMacroState'),
            'toggle_ping_errors': lambda: HandleBoolToggle('LoggingOptions.PingErrors'),
            'toggle_megalodon_sound': lambda: HandleBoolToggle('FishingModes.MegalodonSound'),

            'set_rod_hotkey': lambda: HandleStringValue('InventoryHotkeys.Rod'),
            'set_anything_else_hotkey': lambda: HandleStringValue('InventoryHotkeys.Alternate'),
            'set_webhook_url': lambda: HandleStringValue('DevilFruitStorage.WebhookUrl'),
            'set_discord_user_id': lambda: HandleStringValue('LoggingOptions.DiscordUserId'),

            'set_loops_per_store': lambda: HandleIntValue('AutomationFrequencies.LoopsPerStore'),
            'set_fruit_sweep_loops': lambda: HandleIntValue('AutomationFrequencies.FruitSweepLoops'),
            'toggle_store_only_when_detected': lambda: HandleBoolToggle('AutomationFeatures.StoreOnlyWhenDetected'),
            'set_loops_per_purchase': lambda: HandleIntValue('AutomationFrequencies.LoopsPerPurchase'),
            'set_periodic_stats_interval': lambda: HandleIntValue('LoggingOptions.PeriodicStatsIntervalMinutes'),

            'set_kp': lambda: HandleFloatValue('FishingControl.PdController.Kp'),
            'set_kd': lambda: HandleFloatValue('FishingControl.PdController.Kd'),
            'set_pd_clamp': lambda: HandleFloatValue('FishingControl.PdController.PdClamp'),
            'set_pd_approaching': lambda: HandleFloatValue('FishingControl.PdController.PdApproachingDamping'),
            'set_pd_chasing': lambda: HandleFloatValue('FishingControl.PdController.PdChasingDamping'),
            'set_gap_tolerance': lambda: HandleFloatValue('FishingControl.Detection.GapToleranceMultiplier'),
            'set_cast_hold': lambda: HandleFloatValue('FishingControl.Timing.CastHoldDuration'),
            'set_recast_timeout': lambda: HandleFloatValue('FishingControl.Timing.RecastTimeout'),
            'set_fish_end_delay': lambda: HandleFloatValue('FishingControl.Timing.FishEndDelay'),
            'set_catch_end_grace': lambda: HandleFloatValue('FishingControl.Timing.CatchEndGrace'),
            'set_state_resend': lambda: HandleFloatValue('FishingControl.Timing.StateResendInterval'),
            'set_focus_delay': lambda: HandleFloatValue('TimingDelays.RobloxWindow.RobloxFocusDelay'),
            'set_post_focus_delay': lambda: HandleFloatValue('TimingDelays.RobloxWindow.RobloxPostFocusDelay'),
            'set_precast_e_delay': lambda: HandleFloatValue('TimingDelays.PreCast.SetPrecastEDelay'),
            'set_precast_click_delay': lambda: HandleFloatValue('TimingDelays.PreCast.PreCastClickDelay'),
            'set_precast_type_delay': lambda: HandleFloatValue('TimingDelays.PreCast.PreCastTypeDelay'),
            'set_anti_detect_delay': lambda: HandleFloatValue('TimingDelays.PreCast.PreCastAntiDetectDelay'),
            'set_fruit_hotkey_delay': lambda: HandleFloatValue('TimingDelays.DevilFruitStorage.StoreFruitHotkeyDelay'),
            'set_fruit_click_delay': lambda: HandleFloatValue('TimingDelays.DevilFruitStorage.StoreFruitClickDelay'),
            'set_fruit_shift_delay': lambda: HandleFloatValue('TimingDelays.DevilFruitStorage.StoreFruitShiftDelay'),
            'set_fruit_backspace_delay': lambda: HandleFloatValue('TimingDelays.DevilFruitStorage.StoreFruitBackspaceDelay'),
            'set_rod_delay': lambda: HandleFloatValue('TimingDelays.Inventory.RodSelectDelay'),
            'set_bait_delay': lambda: HandleFloatValue('TimingDelays.Inventory.AutoSelectBaitDelay'),
            'set_cursor_delay': lambda: HandleFloatValue('TimingDelays.AntiDetection.CursorAntiDetectDelay'),
            'set_scan_delay': lambda: HandleFloatValue('FishingControl.Detection.ScanLoopDelay'),
            'set_black_threshold': lambda: HandleFloatValue('FishingControl.Detection.BlackScreenThreshold'),
            'set_spam_delay': lambda: HandleFloatValue('TimingDelays.AntiDetection.AntiMacroSpamDelay'),
            'set_sound_sensitivity': lambda: HandleFloatValue('FishingModes.SoundSensitivity'),

            'set_backpack_location_point': lambda: HandleBackpackLocationPoint(Payload),
            'test_webhook': lambda: HandleTestWebhook(),
            'open_ocr_area_selector': lambda: HandleOCRAreaSelector(),
            'open_area_selector': lambda: HandleAreaSelector(),
            'open_browser': lambda: HandleOpenBrowser(Payload),
            'export_settings': lambda: HandleExportSettings(),
            'import_settings': lambda: HandleImportSettings(),
            'reset_settings': lambda: HandleResetSettings(Payload),
            'open_config_folder': lambda: HandleOpenFolder(),
            'view_config': lambda: HandleViewConfig(),
            'reset_stats': lambda: HandleResetStats(),
        }
        
        if Action == 'rebind_hotkey':
            return HandleHotkeyRebind(Payload)
        
        if Action == 'set_devil_fruit_hotkeys':
            return HandleDevilFruitSlots(Payload)

        if Action == 'set_audio_device':
            if Payload is None:
                return jsonify({"status": "error", "message": "Missing payload"}), 400
            
            try:
                DeviceData = json.loads(Payload)
                MacroSystem.Config.Settings['AudioDevice']['SelectedDeviceIndex'] = DeviceData.get('index')
                MacroSystem.Config.Settings['AudioDevice']['DeviceName'] = DeviceData.get('name', '')
                MacroSystem.Config.SaveToDisk()
                return jsonify({"status": "success"})
            except Exception as E:
                return jsonify({"status": "error", "message": str(E)}), 500
        
        if Action in ActionMap:
            return ActionMap[Action]()
        else:
            return jsonify({"status": "error", "message": f"Unknown action: {Action}"}), 400
    
    except ValueError as E:
        return jsonify({"status": "error", "message": f"Invalid value: {str(E)}"}), 400
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleDevilFruitSlots(Payload):
    if Payload is None:
        return jsonify({"status": "error", "message": "Missing payload"}), 400
    
    try:
        Slots = [S.strip() for S in Payload.split(',') if S.strip()]
        MacroSystem.Config.Settings['InventoryHotkeys']['DevilFruits'] = Slots
        MacroSystem.Config.SaveToDisk()
        return jsonify({"status": "success", "slots": Slots})
    except Exception as E:
        return jsonify({"status": "error", "message": f"Invalid slots: {str(E)}"}), 400

def HandleBackpackLocationPoint(Payload):
    if Payload is None:
        return jsonify({"status": "error", "message": "Missing payload"}), 400
    try:
        Data = json.loads(Payload)
        SlotIndex = int(Data.get('slotIndex', 0))
        Locs = MacroSystem.Config.Settings['ClickPoints']['BackpackLocations']
        while len(Locs) <= SlotIndex:
            Locs.append(None)
        
        def OnPointSet(Name, Point):
            Locs[SlotIndex] = Point
            MacroSystem.Config.SaveToDisk()
        
        MacroSystem.PointSelector.StartSelection(f"BackpackLocation{SlotIndex}", OnPointSet)
        return jsonify({"status": "waiting_for_click"})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500

@OnMainThread
def HandleExportSettings():
    try:
        Root = tk.Tk()
        Root.withdraw()
        Root.attributes('-topmost', True)
        
        DefaultName = f"fishing_macro_settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        Path = filedialog.asksaveasfilename(
            title="Export Settings",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialfile=DefaultName
        )
        
        Root.destroy()
        
        if Path:
            shutil.copy(MacroSystem.Config.ConfigPath, Path)
            return jsonify({"status": "success", "path": Path})
        
        return jsonify({"status": "cancelled"})
    except Exception as E:
        messagebox.showerror("Export Failed", f"Failed to export settings:\n{str(E)}")
        return jsonify({"status": "error", "message": str(E)}), 500


@OnMainThread
def HandleImportSettings():
    try:
        Root = tk.Tk()
        Root.withdraw()
        Root.attributes('-topmost', True)
        
        Path = filedialog.askopenfilename(
            title="Import Settings",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        Root.destroy()
        
        if Path:
            BackupPath = MacroSystem.Config.ConfigPath + ".backup"
            shutil.copy(MacroSystem.Config.ConfigPath, BackupPath)
            
            try:
                shutil.copy(Path, MacroSystem.Config.ConfigPath)
                MacroSystem.Config.LoadFromDisk()
                return jsonify({"status": "success"})
            except Exception as E:
                shutil.copy(BackupPath, MacroSystem.Config.ConfigPath)
                raise E
        
        return jsonify({"status": "cancelled"})
    except Exception as E:
        messagebox.showerror("Import Failed", f"Failed to import settings:\n{str(E)}")
        return jsonify({"status": "error", "message": str(E)}), 500


@OnMainThread
def HandleResetSettings(Payload):
    if Payload != "confirm":
        return jsonify({"status": "error", "message": "Reset not confirmed"}), 400
    
    try:
        BackupPath = MacroSystem.Config.ConfigPath + f".backup_{int(time.time())}"
        if os.path.exists(MacroSystem.Config.ConfigPath):
            shutil.copy(MacroSystem.Config.ConfigPath, BackupPath)
        
        if os.path.exists(MacroSystem.Config.ConfigPath):
            os.remove(MacroSystem.Config.ConfigPath)
        
        MacroSystem.Config.Settings = MacroSystem.Config.InitializeDefaults()
        MacroSystem.Config.SaveToDisk()
        
        return jsonify({"status": "success"})
    except Exception as E:
        messagebox.showerror("Reset Failed", f"Failed to reset settings:\n{str(E)}")
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleOpenFolder():
    try:
        OpenInFileManager(os.path.dirname(MacroSystem.Config.ConfigPath))
        return jsonify({"status": "success"})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


@OnMainThread
def HandleViewConfig():
    try:
        if os.path.exists(MacroSystem.Config.ConfigPath):
            with open(MacroSystem.Config.ConfigPath, 'r') as F:
                Content = F.read()
            
            Root = tk.Tk()
            Root.title("Configuration File Viewer")
            Root.geometry("800x600")
            
            Text = tk.Text(Root, wrap=tk.WORD, font=("Consolas", 10))
            Text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            Text.insert(1.0, Content)
            Text.config(state=tk.DISABLED)
            
            Scroll = tk.Scrollbar(Text)
            Scroll.pack(side=tk.RIGHT, fill=tk.Y)
            Text.config(yscrollcommand=Scroll.set)
            Scroll.config(command=Text.yview)
            
            Root.mainloop()
            
            return jsonify({"status": "success"})
        else:
            messagebox.showwarning("File Not Found", "Configuration file does not exist yet.")
            return jsonify({"status": "error", "message": "Config file not found"}), 404
    except Exception as E:
        messagebox.showerror("View Failed", f"Failed to view config:\n{str(E)}")
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleToggleMacro():
    # ToggleMacro can wait on a finishing loop; don't hold the request (or the UI) for it
    threading.Thread(target=MacroSystem.ToggleMacro, daemon=True).start()
    return jsonify({"status": "success"})


def HandleResetStats():
    try:
        MacroSystem.State.BaitPurchaseCounter = 0
        MacroSystem.State.FruitStorageCounter = 0
        MacroSystem.State.TotalRecastTimeouts = 0
        MacroSystem.State.ConsecutiveRecastTimeouts = 0
        MacroSystem.State.TopBaitCounter = 0
        MacroSystem.State.BaitRescanNeeded = True

        MacroSystem.State.TotalFishCaught = 0
        MacroSystem.State.ResetDevilFruitCounts()
        MacroSystem.State.CumulativeUptime = 0
        MacroSystem.State.SessionStartTime = time.time() if MacroSystem.State.IsRunning else None
        MacroSystem.State.LastPeriodicStatsTime = time.time()
        MacroSystem.State.FishAtLastStats = 0
        
        return jsonify({"status": "success"})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleAreaSelector():
    MacroSystem.ModifyScanArea()
    return jsonify({"status": "opening_selector"})


def HandleOpenBrowser(Url):
    if not Url:
        return jsonify({"status": "error", "message": "Missing URL"}), 400
    
    try:
        webbrowser.open(Url)
        return jsonify({"status": "success"})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleHotkeyRebind(Payload):
    if Payload is None:
        return jsonify({"status": "error", "message": "Missing payload"}), 400
    
    MacroSystem.CurrentlyRebindingHotkey = Payload
    keyboard.unhook_all_hotkeys()
    
    def HandleKey(Event):
        if MacroSystem.CurrentlyRebindingHotkey == Payload:
            NewKey = Event.name.lower()
            MacroSystem.Config.Settings['Hotkeys'][Payload.title().replace('_', '')] = NewKey
            MacroSystem.Config.SaveToDisk()
            MacroSystem.CurrentlyRebindingHotkey = None
            keyboard.unhook_all()
            MacroSystem.RegisterHotkeys()
    
    keyboard.on_release(HandleKey, suppress=False)
    return jsonify({"status": "waiting_for_key"})


def HandleTestWebhook():
    if not MacroSystem.Config.Settings['DevilFruitStorage']['WebhookUrl']:
        return jsonify({"status": "error", "message": "No webhook URL configured"}), 400
    
    try:
        MacroSystem.Notifier.SendNotification(
            "Test webhook notification sent successfully! Your webhook is working correctly.",
            Color=0x3b82f6,
            Title="🎣 Webhook Test"
        )
        return jsonify({"status": "success"})
    except Exception as E:
        return jsonify({"status": "error", "message": str(E)}), 500


def HandleOCRAreaSelector():
    def OnOCRRegionComplete(Coords):
        MacroSystem.Config.Settings['OCRSettings'] = Coords
        MacroSystem.Config.SaveToDisk()
        MacroSystem.ActiveRegionSelector = None
        MacroSystem.RegionSelectorActive = False
    
    if MacroSystem.RegionSelectorActive:
        if MacroSystem.ActiveRegionSelector:
            try:
                MacroSystem.ActiveRegionSelector.RootWindow.after(10, MacroSystem.ActiveRegionSelector.CloseWindow)
            except:
                pass
        return jsonify({"status": "already_open"})
    
    MacroSystem.RegionSelectorActive = True
    
    def RunSelector():
        try:
            MacroSystem.ActiveRegionSelector = RunOnMainThread(lambda: RegionSelectionWindow(None, MacroSystem.Config.Settings['OCRSettings'], OnOCRRegionComplete))
        finally:
            MacroSystem.RegionSelectorActive = False
            MacroSystem.ActiveRegionSelector = None
    
    threading.Thread(target=RunSelector, daemon=True).start()
    return jsonify({"status": "opening_selector"})

def HandleBaitTierOrder(Payload):
    Tiers = [T.strip() for T in (Payload or '').split(',') if T.strip()]
    if not Tiers:
        return jsonify({"status": "error", "message": "Enter at least one bait name"}), 400

    MacroSystem.Config.Settings['BaitSelector']['TierOrder'] = Tiers
    MacroSystem.Config.SaveToDisk()
    MacroSystem.State.BaitRescanNeeded = True
    return jsonify({"status": "success"})


def HandleTestBaitScan():
    Baits = MacroSystem.BaitReader.ScanBaits()
    if Baits is None:
        return jsonify({"status": "error", "message": "OCR is unavailable"}), 500
    return jsonify({"status": "success", "baits": [{"name": B['Name'], "count": B['Count']} for B in Baits]})


def HandleBaitRegionSelector():
    def OnBaitRegionComplete(Coords):
        MacroSystem.Config.Settings['BaitSelector']['Region'] = Coords
        MacroSystem.Config.SaveToDisk()
        MacroSystem.State.BaitRescanNeeded = True
        MacroSystem.ActiveRegionSelector = None
        MacroSystem.RegionSelectorActive = False

    if MacroSystem.RegionSelectorActive:
        if MacroSystem.ActiveRegionSelector:
            try:
                MacroSystem.ActiveRegionSelector.RootWindow.after(10, MacroSystem.ActiveRegionSelector.CloseWindow)
            except:
                pass
        return jsonify({"status": "already_open"})

    MacroSystem.RegionSelectorActive = True

    def RunSelector():
        try:
            MacroSystem.ActiveRegionSelector = RunOnMainThread(lambda: RegionSelectionWindow(None, MacroSystem.Config.Settings['BaitSelector']['Region'], OnBaitRegionComplete))
        finally:
            MacroSystem.RegionSelectorActive = False
            MacroSystem.ActiveRegionSelector = None

    threading.Thread(target=RunSelector, daemon=True).start()
    return jsonify({"status": "opening_selector"})


def RunFlaskServer():
    # Loopback only: the API controls mouse/keyboard input, so it must not be reachable from the network
    FlaskApp.run(host='127.0.0.1', port=Port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    # One access-log line per request floods the console: the UI polls /state several times a second
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    ServerThread = threading.Thread(target=RunFlaskServer, daemon=True)
    ServerThread.start()
    # Load OCR (easyocr + torch, several seconds) after the server is up so it doesn't hold up the launcher
    threading.Timer(3.0, MacroSystem.OcrManager.Initialize).start()
    try:
        # The main thread idles here, running any tkinter work other threads hand it (RunOnMainThread)
        CocoaStarted = False
        while True:
            try:
                Task = MainThreadTasks.get(timeout=1) if not CocoaStarted else MainThreadTasks.get_nowait()
            except queue.Empty:
                if CocoaStarted:
                    PumpCocoaEvents(0.05)
                continue
            Task()
            CocoaStarted = IsMac
    except KeyboardInterrupt:
        os._exit(0)