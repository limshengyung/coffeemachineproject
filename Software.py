import wx
import wx.grid
import os
import csv
import json
import datetime
import serial
import traceback
import struct
import time
import threading
import tempfile
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import wx.lib.scrolledpanel as scrolled

# ─── GOOGLE GMAIL API IMPORTS ─────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── FILE PATHS ───────────────────────────────────────────────────────────────
GUI_DIR = '/home/coffeemachineproject/Desktop/GUI'
CLIENT_SECRET_FILE = os.path.join(GUI_DIR, 'credentials.json')
TOKEN_FILE = os.path.join(GUI_DIR, 'token.json') # Saves the OAuth2 session
EMAILS_FILE = os.path.join(GUI_DIR, 'saved_emails.json') # Saves the RECIPIENTS

# Added readonly scope to allow the app to fetch your email address for the UI chip
SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly'
]

# ─── CLOUD SYNC IMPORTS (FIRESTORE) ───────────────────────────────────────────
import firebase_admin
from firebase_admin import credentials as fb_credentials, firestore

if not firebase_admin._apps:
    firebase_cred_path = os.path.join(GUI_DIR, 'firebase_credentials.json')
    cred = fb_credentials.Certificate(firebase_cred_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ─── HARDWARE SETTINGS ────────────────────────────────────────────────────────
USB_PORT = "/dev/ttyACM0" 
BAUD_RATE = 115200

# ─── DESIGN TOKENS ────────────────────────────────────────────────────────────
NAVY        = "#0D1B2A"
NAVY_LIGHT  = "#1A2E45"
NAVY_MID    = "#243B55"
AMBER       = "#F5A623"
CREAM       = "#F5F0E8"
WHITE       = "#FFFFFF"
MUTED       = "#8899AA"
SUCCESS     = "#2ECC71"
DANGER      = "#E74C3C"
GRID_LINE   = "#2A3F58"
BLUE_ACCENT = "#4A90D9"

CORNER_RADIUS       = 14
BUTTON_BG_COLOR     = NAVY_LIGHT
BUTTON_BORDER_COLOR = AMBER

def hex_col(h):
    h = h.lstrip("#")
    return wx.Colour(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

# ─── GLOBAL HELPER FOR CUSTOM POPUPS ──────────────────────────────────────────
def show_custom_message(parent, title, message, is_error=False):
    dlg = StyledMessageDialog(parent, title, message, is_error=is_error)
    dlg.ShowModal()
    dlg.Destroy()

# ─── OAUTH2 AUTHENTICATION LOGIC ──────────────────────────────────────────────
def authenticate_google():
    """Triggers the browser login and saves token.json"""
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise FileNotFoundError(f"Missing {CLIENT_SECRET_FILE}. Please download it from Google Cloud Console.")
        
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception:
            creds = None
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        
        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            # Add a timeout to prevent infinite freezing if the user closes the browser
            creds = flow.run_local_server(port=0, timeout_seconds=90)
            
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return creds

def get_logged_in_email():
    """Helper to fetch the email using the saved OAuth token."""
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if creds:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                
                # Fetch user profile using the token (directly try the API call)
                service = build('gmail', 'v1', credentials=creds)
                profile = service.users().getProfile(userId='me').execute()
                return profile.get('emailAddress')
        except Exception as e:
            print(f"Error fetching Google profile: {e}")
            # Self-healing: If fetching fails, the token permissions are outdated.
            # Delete it silently so the app isn't permanently stuck.
            try:
                os.remove(TOKEN_FILE)
            except OSError:
                pass
    return None

def threaded_google_login(parent_ui, success_callback):
    """Runs Google login in a background thread and temporarily disables fullscreen."""
    top_frame = parent_ui.GetTopLevelParent()
    
    # Temporarily drop fullscreen AND force minimize so the browser is definitely visible
    if hasattr(top_frame, 'ShowFullScreen'):
        top_frame.ShowFullScreen(False)
    top_frame.Iconize(True) 

    def _auth_task():
        try:
            creds = authenticate_google()
            wx.CallAfter(_on_complete, creds, None)
        except Exception as e:
            wx.CallAfter(_on_complete, None, str(e))

    def _on_complete(creds, error):
        # Restore the application window once the browser flow is done or timed out
        top_frame.Iconize(False)
        top_frame.Raise()
        
        if hasattr(top_frame, 'ShowFullScreen'):
            top_frame.ShowFullScreen(True, wx.FULLSCREEN_ALL)
            
        if error:
            # If the user closes the browser, the timeout will trigger and safely hit this error block
            show_custom_message(parent_ui, "Login Failed", f"Could not log in or timed out:\n{error}", is_error=True)
        else:
            show_custom_message(parent_ui, "Login Successful", "Successfully logged into Google!")
            if success_callback:
                success_callback()

    # Start the thread so the GUI doesn't freeze
    threading.Thread(target=_auth_task, daemon=True).start()


# ─── BINARY SENSOR READING LOGIC ──────────────────────────────────────────────
def poll_sensor_binary(ser):
    if not ser or not ser.is_open:
        return None
    try:
        ser.reset_input_buffer()
        ser.write(b'\xAA') 
        
        sync_state = 0
        start_time = time.time()
        
        while (time.time() - start_time) < 0.5: 
            if ser.in_waiting > 0:
                byte = ser.read(1)[0]
                if sync_state == 0 and byte == 0xFF:
                    sync_state = 1
                elif sync_state == 1 and byte == 0xAA:
                    sync_state = 2
                    break
                else:
                    sync_state = 0
                    
        if sync_state == 2:
            packet = ser.read(32)
            if len(packet) == 32:
                calc_sum = sum(packet[:20]) & 0xFF
                recv_sum = packet[20]
                
                if calc_sum == recv_sum:
                    unpacked_data = struct.unpack('<ffffI', packet[:20])
                    temp = unpacked_data[0]
                    ph = unpacked_data[1]
                    turb_raw = unpacked_data[2]
                    return (temp, ph, turb_raw)
                else:
                    print(f"Checksum mismatch! Calc: {calc_sum}, Recv: {recv_sum}")
            else:
                print(f"Incomplete packet received. Got {len(packet)} bytes.")
        return None
    except Exception as e:
        print(f"Binary read error: {e}")
        return None

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def select_existing_file(parent):
    with TouchFileNavigatorDialog(parent) as dialog:
        if dialog.ShowModal() == wx.ID_OK:
            return dialog.GetPath()
    return None
            
def create_new_file(parent):
    with TouchKeyboardDialog(parent, "New Project Name", "Enter the project/folder name:") as dialog:
        result = dialog.ShowModal()
        val = dialog.GetValue().strip()
    
    if result == wx.ID_OK:
        if not val:
            return None
        if val.lower().endswith('.csv'):
            val = val[:-4]
        if not os.path.exists(val):
            os.makedirs(val)
        filepath = os.path.join(val, f"{val}.csv")
        return filepath
    return None

def draw_rounded_panel_bg(event):
    panel = event.GetEventObject()
    dc    = wx.PaintDC(panel)
    gc    = wx.GraphicsContext.Create(dc)
    if gc:
        rect = panel.GetClientRect()
        rect.Deflate(1, 1)
        hovered = getattr(panel, '_hovered', False)
        bg_col  = getattr(panel, '_custom_bg_color', BUTTON_BG_COLOR)
        border  = AMBER if hovered else BUTTON_BORDER_COLOR
        gc.SetPen(wx.Pen(hex_col(border), 2))
        gc.SetBrush(wx.Brush(hex_col(bg_col)))
        gc.DrawRoundedRectangle(rect.GetX(), rect.GetY(), rect.GetWidth(), rect.GetHeight(), CORNER_RADIUS)

def make_divider(parent):
    d = wx.Panel(parent, size=(-1, 1))
    d.SetBackgroundColour(hex_col(GRID_LINE))
    d.SetMinSize((-1, 1))
    return d

def scale_button(btn, w, h, is_icon_only=False):
    if not btn: return
    if is_icon_only:
        b_w = max(50, int(w * 0.06))
        b_h = max(40, int(h * 0.055))
        f_sz = max(22, int(h * 0.035))
    else:
        b_w = max(160, int(w * 0.18))
        b_h = max(45, int(h * 0.055))   
        f_sz = max(11, int(h * 0.022))  
        
    btn.SetMinSize((b_w, b_h))
    btn.SetFont(wx.Font(f_sz, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))

def styled_back_button(parent, handler):
    btn = wx.Button(parent, label="←")
    btn.SetBackgroundColour(hex_col(NAVY_MID))
    btn.SetForegroundColour(hex_col(CREAM))
    btn.Bind(wx.EVT_BUTTON, handler)
    return btn

def styled_primary_button(parent, label, handler, size=(180, 40)):
    btn = wx.Button(parent, label=label, size=size)
    btn.SetBackgroundColour(hex_col(AMBER))
    btn.SetForegroundColour(hex_col(NAVY))
    f = wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
    btn.SetFont(f)
    btn.Bind(wx.EVT_BUTTON, handler)
    return btn


# ─── GOOGLE STYLE PROFILE CHIP (AVATAR & SIGN IN) ─────────────────────────────
class GoogleProfileChip(wx.Panel):
    def __init__(self, parent, size=(220, 40)):
        super().__init__(parent)
        self.default_width = size[0]
        self.default_height = size[1]
        
        # Start dynamically as just a small circular icon
        self.SetMinSize((self.default_height, self.default_height))
        
        self.email = None
        self.is_hovered = False
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)

        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self.on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_click)
        self.click_handler = None

    def set_email(self, email):
        self.email = email
        if self.email:
            # Expand to full pill shape when logged in
            self.SetMinSize((self.default_width, self.default_height))
        else:
            # Shrink back to circular icon when logged out
            self.SetMinSize((self.default_height, self.default_height))
            
        if self.GetParent():
            self.GetParent().Layout()
        self.Refresh()
        self.Update() # Forces an immediate visual redraw before any pop-ups appear!

    def bind_click(self, handler):
        self.click_handler = handler

    def on_click(self, event):
        if self.click_handler:
            self.click_handler(event)

    def on_enter(self, event):
        self.is_hovered = True
        self.Refresh()

    def on_leave(self, event):
        self.is_hovered = False
        self.Refresh()

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        
        gc = wx.GraphicsContext.Create(dc)
        if not gc: return
        
        w, h = self.GetClientSize()
        
        if not self.email:
            # Logged Out: Clean Circular User Icon silhouette
            bg_col = wx.Colour(50, 65, 85) if self.is_hovered else hex_col(NAVY_MID)
            gc.SetBrush(wx.Brush(bg_col))
            gc.SetPen(wx.Pen(wx.TRANSPARENT_PEN))
            
            # Draw circular background
            circle_size = min(w, h)
            gc.DrawEllipse(0, 0, circle_size, circle_size)
            
            # Draw user silhouette
            gc.SetBrush(wx.Brush(hex_col(CREAM)))
            
            # Head
            head_r = circle_size * 0.18
            gc.DrawEllipse(circle_size/2 - head_r, circle_size/2 - head_r - circle_size*0.12, head_r*2, head_r*2)
            
            # Shoulders/Body
            path = gc.CreatePath()
            path.MoveToPoint(circle_size/2 - head_r*1.9, circle_size * 0.82)
            path.AddQuadCurveToPoint(circle_size/2, circle_size * 0.45, circle_size/2 + head_r*1.9, circle_size * 0.82)
            path.CloseSubpath()
            gc.FillPath(path)
            
        else:
            # Logged In: Account Chip with Initial Avatar & Email
            bg_col = wx.Colour(50, 65, 85) if self.is_hovered else hex_col(NAVY_MID)
            gc.SetBrush(wx.Brush(bg_col))
            gc.SetPen(wx.Pen(wx.TRANSPARENT_PEN))
            gc.DrawRoundedRectangle(0, 0, w, h, h/2)
            
            avatar_padding = 4
            avatar_size = h - (avatar_padding * 2)
            avatar_x = avatar_padding
            avatar_y = avatar_padding
            
            # Dynamically select a Google material color based on their first letter
            initial = self.email[0].upper() if self.email else "?"
            colors = ["#DB4437", "#0F9D58", "#4285F4", "#F4B400", "#673AB7", "#FF9800", "#009688", "#E91E63"]
            color_idx = ord(initial) % len(colors)
            gc.SetBrush(wx.Brush(hex_col(colors[color_idx])))
            gc.DrawEllipse(avatar_x, avatar_y, avatar_size, avatar_size)
            
            # Draw initial inside the circle
            font_avatar = wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
            gc.SetFont(font_avatar, wx.WHITE)
            tw, th, _, _ = gc.GetFullTextExtent(initial)
            gc.DrawText(initial, avatar_x + (avatar_size - tw)/2, avatar_y + (avatar_size - th)/2)
            
            # Draw email address next to it
            display_email = self.email if len(self.email) <= 17 else self.email[:14] + "..."
            font_email = wx.Font(11, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
            gc.SetFont(font_email, hex_col(CREAM))
            tw, th, _, _ = gc.GetFullTextExtent(display_email)
            text_x = avatar_x + avatar_size + 12
            gc.DrawText(display_email, text_x, (h - th)/2)

# ─── CUSTOM SIGN OUT PILL WIDGET ──────────────────────────────────────────────
class SignOutPopup(wx.PopupTransientWindow):
    def __init__(self, parent, signout_callback):
        super().__init__(parent, wx.BORDER_NONE)
        self.signout_callback = signout_callback
        
        self.panel = wx.Panel(self, size=(110, 36))
        # Match the app's navy background to blend the corners seamlessly
        self.panel.SetBackgroundColour(hex_col(NAVY))
        
        self.panel.Bind(wx.EVT_PAINT, self.on_paint)
        self.panel.Bind(wx.EVT_LEFT_DOWN, self.on_click)
        self.panel.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        
        self.SetClientSize((110, 36))
        
    def on_paint(self, event):
        dc = wx.PaintDC(self.panel)
        gc = wx.GraphicsContext.Create(dc)
        if not gc: return
        
        w, h = self.panel.GetClientSize()
        
        # Dark grey fill with a subtle lighter border to match the UI aesthetic
        gc.SetBrush(wx.Brush(wx.Colour(85, 85, 85))) 
        gc.SetPen(wx.Pen(wx.Colour(150, 150, 150), 1))
        gc.DrawRoundedRectangle(2, 2, w-4, h-4, 8) 
        
        # Center the text
        font = wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        gc.SetFont(font, hex_col(WHITE))
        tw, th, _, _ = gc.GetFullTextExtent("Sign Out")
        gc.DrawText("Sign Out", (w - tw) / 2, (h - th) / 2)
        
    def on_click(self, event):
        self.Dismiss()
        self.signout_callback(event)

# ─── TOUCHSCREEN FILE NAVIGATOR DIALOG ────────────────────────────────────────
class TouchFileNavigatorDialog(wx.Dialog):
    def __init__(self, parent, start_dir=None):
        super().__init__(parent, title="Select File", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        self.selected_file = None
        
        if start_dir is None:
            start_dir = os.getcwd()
        self.current_dir = os.path.abspath(start_dir)
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.title_lbl = wx.StaticText(self.panel, label="Select a Previous File")
        self.title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
        self.title_lbl.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(self.title_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 10)
        
        self.path_lbl = wx.StaticText(self.panel, label=self.current_dir)
        self.path_lbl.SetForegroundColour(hex_col(MUTED))
        self.path_lbl.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        main_sizer.Add(self.path_lbl, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 5)
        
        search_sizer = wx.BoxSizer(wx.HORIZONTAL)
        search_lbl = wx.StaticText(self.panel, label="Search:")
        search_lbl.SetForegroundColour(hex_col(CREAM))
        search_lbl.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        self.search_input = wx.TextCtrl(self.panel, value="", size=(250, 40), style=wx.TE_READONLY)
        self.search_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.search_input.SetForegroundColour(hex_col(CREAM))
        self.search_input.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.search_input.Bind(wx.EVT_LEFT_DOWN, self.on_search_click)
        
        btn_clear = wx.Button(self.panel, label="Clear", size=(80, 40))
        btn_clear.SetBackgroundColour(hex_col(DANGER))
        btn_clear.SetForegroundColour(hex_col(WHITE))
        btn_clear.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        btn_clear.Bind(wx.EVT_BUTTON, self.on_search_clear)

        search_sizer.Add(search_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        search_sizer.Add(self.search_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        search_sizer.Add(btn_clear, 0, wx.ALIGN_CENTER_VERTICAL)
        main_sizer.Add(search_sizer, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 10)
        main_sizer.Add(make_divider(self.panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 15)
        
        self.scroll_panel = scrolled.ScrolledPanel(self.panel, -1, size=(600, 300))
        self.scroll_panel.SetBackgroundColour(hex_col(NAVY))
        self.scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scroll_panel.SetSizer(self.scroll_sizer)
        
        main_sizer.Add(self.scroll_panel, 1, wx.EXPAND | wx.ALL, 10)
        
        btn_cancel = wx.Button(self.panel, label="Cancel", size=(150, 45))
        btn_cancel.SetBackgroundColour(hex_col(NAVY_MID))
        btn_cancel.SetForegroundColour(hex_col(WHITE))
        btn_cancel.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        btn_cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
        
        main_sizer.Add(btn_cancel, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 15)
        
        self.panel.SetSizer(main_sizer)
        
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent()
        self.load_directory(self.current_dir)

    def on_search_click(self, event):
        with TouchKeyboardDialog(self, "Search Files", "Enter file or folder name:", default_val=self.search_input.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                query = dialog.GetValue().strip()
                self.search_input.SetValue(query)
                self.load_directory(self.current_dir, search_query=query)

    def on_search_clear(self, event):
        self.search_input.SetValue("")
        self.load_directory(self.current_dir, search_query="")
        
    def load_directory(self, path, search_query=None):
        new_path = os.path.abspath(path)
        if new_path != self.current_dir and search_query is None:
            self.search_input.SetValue("")
            search_query = ""
            
        self.current_dir = new_path
        self.path_lbl.SetLabel(self.current_dir)
        
        if search_query is None:
            search_query = self.search_input.GetValue()
        search_query = search_query.lower()
        
        self.scroll_sizer.Clear(True)
        self.scroll_sizer.AddStretchSpacer(1)
        btn_font = wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        
        parent_dir = os.path.dirname(self.current_dir)
        if parent_dir != self.current_dir:
            btn_up = wx.Button(self.scroll_panel, label="⬆  Go Back (Up One Folder)", size=(550, 55))
            btn_up.SetBackgroundColour(hex_col(NAVY_MID))
            btn_up.SetForegroundColour(hex_col(AMBER))
            btn_up.SetFont(btn_font)
            btn_up.Bind(wx.EVT_BUTTON, lambda e: self.load_directory(parent_dir))
            self.scroll_sizer.Add(btn_up, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 4)
        
        try:
            items = os.listdir(self.current_dir)
        except PermissionError:
            items = []
            
        folders = []
        files = []
        for item in items:
            if item.startswith('.'): continue 
            if search_query and search_query not in item.lower(): continue
            full_path = os.path.join(self.current_dir, item)
            if os.path.isdir(full_path):
                folders.append(item)
            elif item.lower().endswith('.csv'):
                files.append(item)
                
        folders.sort()
        files.sort()
        
        for folder in folders:
            btn = wx.Button(self.scroll_panel, label=f"📁  {folder}", size=(550, 55))
            btn.SetBackgroundColour(hex_col(NAVY_LIGHT))
            btn.SetForegroundColour(hex_col(CREAM))
            btn.SetFont(btn_font)
            full_path = os.path.join(self.current_dir, folder)
            btn.Bind(wx.EVT_BUTTON, lambda e, p=full_path: self.load_directory(p))
            self.scroll_sizer.Add(btn, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 4)
            
        for file in files:
            btn = wx.Button(self.scroll_panel, label=f"📄  {file}", size=(550, 55))
            btn.SetBackgroundColour(hex_col(NAVY_MID))
            btn.SetForegroundColour(hex_col(SUCCESS))
            btn.SetFont(btn_font)
            full_path = os.path.join(self.current_dir, file)
            btn.Bind(wx.EVT_BUTTON, lambda e, p=full_path: self.on_file_select(p))
            self.scroll_sizer.Add(btn, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 4)
            
        if not folders and not files:
            empty_msg = "No matches found." if search_query else "No folders or CSV files here."
            empty = wx.StaticText(self.scroll_panel, label=empty_msg)
            empty.SetForegroundColour(hex_col(MUTED))
            empty.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
            self.scroll_sizer.Add(empty, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 20)
            
        self.scroll_sizer.AddStretchSpacer(1)
        self.scroll_panel.Layout()
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True)
        self.panel.Layout()
        self.Refresh()
        
    def on_file_select(self, path):
        self.selected_file = path
        self.EndModal(wx.ID_OK)
        
    def on_cancel(self, event):
        self.EndModal(wx.ID_CANCEL)
        
    def GetPath(self):
        return self.selected_file

# ─── GMAIL STYLE BUBBLE WIDGET ────────────────────────────────────────────────
class EmailBubble(wx.Panel):
    def __init__(self, parent, email, delete_callback):
        super().__init__(parent, size=(-1, 36))
        self.email = email
        self.SetBackgroundColour(hex_col(NAVY_MID))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(self, label=email)
        lbl.SetForegroundColour(hex_col(CREAM))
        lbl.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        
        self.del_btn = wx.StaticText(self, label="✖")
        self.del_btn.SetForegroundColour(hex_col(DANGER))
        self.del_btn.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.del_btn.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.del_btn.Bind(wx.EVT_LEFT_DOWN, lambda e: delete_callback(self))
        
        sizer.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(self.del_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        
        self.SetSizer(sizer)
        self.Layout()
        
    def on_paint(self, event):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(hex_col(NAVY_MID)))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc:
            rect = self.GetClientRect()
            rect.Deflate(1, 1)
            gc.SetPen(wx.Pen(hex_col(BLUE_ACCENT), 1))
            gc.SetBrush(wx.Brush(hex_col(NAVY_LIGHT)))
            gc.DrawRoundedRectangle(rect.GetX(), rect.GetY(), rect.GetWidth(), rect.GetHeight(), 18)


# ─── TOUCHSCREEN KEYBOARD DIALOG ──────────────────────────────────────────────
class TouchKeyboardDialog(wx.Dialog):
    def __init__(self, parent, title, message, default_val="", is_email_mode=False, is_password=False):
        super().__init__(parent, title="Keyboard", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        
        self.caps_lock = False
        self.letter_btns = []  
        self.caps_btn = None   
        self.is_email_mode = is_email_mode
        self.is_password = is_password
        self.has_password_toggle = is_password
        self.default_val = default_val
        self.email_list = []
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_lbl = wx.StaticText(self.panel, label=title)
        title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
        title_lbl.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 15)
        
        if message:
            msg_lbl = wx.StaticText(self.panel, label=message)
            msg_lbl.SetForegroundColour(hex_col(MUTED))
            msg_lbl.SetFont(wx.Font(11, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            main_sizer.Add(msg_lbl, 0, wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, 10)
        
        self.input_row = wx.BoxSizer(wx.HORIZONTAL)
        
        if self.is_email_mode:
            self.bubble_scroll = scrolled.ScrolledPanel(self.panel, size=(500, 80))
            self.bubble_scroll.SetBackgroundColour(hex_col(NAVY_MID))
            self.bubble_sizer = wx.WrapSizer(wx.HORIZONTAL)
            self.bubble_scroll.SetSizer(self.bubble_sizer)
            self.bubble_scroll.SetupScrolling(scroll_x=False, scroll_y=True)
            main_sizer.Add(self.bubble_scroll, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
            
            self.text_ctrl = wx.TextCtrl(self.panel, size=(500, 45), style=0)
            self.text_ctrl.SetBackgroundColour(hex_col(NAVY_MID))
            self.text_ctrl.SetForegroundColour(hex_col(CREAM))
            self.text_ctrl.SetFont(wx.Font(18, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            self.text_ctrl.SetValue(default_val)
            self.text_ctrl.SetInsertionPointEnd()
            self.input_row.Add(self.text_ctrl, 1, wx.EXPAND)
        else:
            self.tc_sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.recreate_text_ctrl()
            self.input_row.Add(self.tc_sizer, 1, wx.EXPAND)
            
            if self.has_password_toggle:
                self.btn_show = wx.Button(self.panel, label="👁", size=(60, 50))
                self.btn_show.SetBackgroundColour(hex_col(NAVY_LIGHT))
                self.btn_show.SetForegroundColour(hex_col(MUTED) if self.is_password else hex_col(SUCCESS))
                self.btn_show.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                self.btn_show.Bind(wx.EVT_BUTTON, self.on_toggle_show)
                self.input_row.Add(self.btn_show, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 0)
                
        main_sizer.Add(self.input_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)
        
        kb_sizer = wx.BoxSizer(wx.VERTICAL)
        keys = [
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'Bksp'],
            ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p', '-'],
            ['Caps', 'a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l', '_'],
            ['z', 'x', 'c', 'v', 'b', 'n', 'm', '.', ',', '@']
        ]
        btn_font = wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        
        for row in keys:
            row_sizer = wx.BoxSizer(wx.HORIZONTAL)
            row_sizer.AddStretchSpacer(1)
            for key in row:
                width = 90 if key in ('Bksp', 'Caps') else 50
                btn = wx.Button(self.panel, label=key, size=(width, 50))
                btn.SetBackgroundColour(hex_col(NAVY_LIGHT))
                btn.SetForegroundColour(hex_col(CREAM))
                btn.SetFont(btn_font)
                btn.Bind(wx.EVT_BUTTON, self.on_key_press)
                if key.isalpha() and len(key) == 1:
                    self.letter_btns.append(btn)
                elif key == 'Caps':
                    self.caps_btn = btn
                row_sizer.Add(btn, 0, wx.ALL, 3)
            row_sizer.AddStretchSpacer(1)
            kb_sizer.Add(row_sizer, 0, wx.EXPAND)
            
        ctrl_row = wx.BoxSizer(wx.HORIZONTAL)
        ctrl_row.AddStretchSpacer(1)
        
        btn_cancel = wx.Button(self.panel, label="Cancel", size=(120, 50))
        btn_cancel.SetBackgroundColour(hex_col(DANGER))
        btn_cancel.SetForegroundColour(hex_col(WHITE))
        btn_cancel.SetFont(btn_font)
        btn_cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
        
        space_width = 130 if self.is_email_mode else 250
        btn_space = wx.Button(self.panel, label="Space", size=(space_width, 50))
        btn_space.SetBackgroundColour(hex_col(NAVY_LIGHT))
        btn_space.SetForegroundColour(hex_col(CREAM))
        btn_space.SetFont(btn_font)
        btn_space.Bind(wx.EVT_BUTTON, self.on_key_press)
        
        btn_done = styled_primary_button(self.panel, "Done", self.on_ok, size=(160, 50))
        
        ctrl_row.Add(btn_cancel, 0, wx.ALL, 5)
        ctrl_row.Add(btn_space, 0, wx.ALL, 5)
        
        if self.is_email_mode:
            btn_enter = wx.Button(self.panel, label="Enter ↵", size=(130, 50))
            btn_enter.SetBackgroundColour(hex_col(NAVY_LIGHT))
            btn_enter.SetForegroundColour(hex_col(CREAM))
            btn_enter.SetFont(btn_font)
            btn_enter.Bind(wx.EVT_BUTTON, self.on_key_press)
            ctrl_row.Add(btn_enter, 0, wx.ALL, 5)
            
        ctrl_row.Add(btn_done, 0, wx.ALL, 5)
        ctrl_row.AddStretchSpacer(1)
        
        kb_sizer.Add(ctrl_row, 0, wx.EXPAND | wx.TOP, 10)
        main_sizer.Add(kb_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent()

    def recreate_text_ctrl(self):
        val = self.text_ctrl.GetValue() if hasattr(self, 'text_ctrl') else self.default_val
        if hasattr(self, 'text_ctrl'):
            self.tc_sizer.Detach(self.text_ctrl)
            self.text_ctrl.Destroy()
            
        style = wx.TE_PASSWORD if self.is_password else 0
        tw = 440 if self.has_password_toggle else 500
        
        self.text_ctrl = wx.TextCtrl(self.panel, size=(tw, 50), style=style)
        self.text_ctrl.SetBackgroundColour(hex_col(NAVY_MID))
        self.text_ctrl.SetForegroundColour(hex_col(CREAM))
        self.text_ctrl.SetFont(wx.Font(18, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.text_ctrl.SetValue(val)
        self.text_ctrl.SetInsertionPointEnd()
        
        self.tc_sizer.Add(self.text_ctrl, 1, wx.EXPAND)
        self.panel.Layout()

    def on_toggle_show(self, event):
        self.is_password = not self.is_password
        if self.is_password:
            self.btn_show.SetForegroundColour(hex_col(MUTED))
        else:
            self.btn_show.SetForegroundColour(hex_col(SUCCESS))
        self.recreate_text_ctrl()
        
    def add_email_bubble(self, email):
        if email not in self.email_list:
            self.email_list.append(email)
            bubble = EmailBubble(self.bubble_scroll, email, self.remove_email_bubble)
            self.bubble_sizer.Add(bubble, 0, wx.ALL, 3)
            self.bubble_scroll.Layout()
            self.bubble_scroll.SetupScrolling(scroll_x=False, scroll_y=True)
            self.bubble_scroll.Scroll(-1, 99999) 

    def remove_email_bubble(self, bubble):
        if bubble.email in self.email_list:
            self.email_list.remove(bubble.email)
        bubble.Destroy()
        self.bubble_scroll.Layout()
        
    def on_key_press(self, event):
        btn = event.GetEventObject()
        label = btn.GetLabel()
        current_text = self.text_ctrl.GetValue()
        
        if self.is_email_mode and label in ('Space', 'Enter ↵', ','):
            text_to_process = current_text.strip()
            if text_to_process:
                if "@" in text_to_process:
                    self.add_email_bubble(text_to_process)
                    self.text_ctrl.SetValue("")
                else:
                    show_custom_message(self, "Invalid Email", "Email address must contain an '@' symbol.", is_error=True)
            return

        if label == 'Bksp':
            self.text_ctrl.SetValue(current_text[:-1])
        elif label == 'Space':
            self.text_ctrl.SetValue(current_text + ' ')
        elif label == 'Caps':
            self.caps_lock = not self.caps_lock
            if self.caps_lock:
                self.caps_btn.SetBackgroundColour(hex_col(AMBER))
                self.caps_btn.SetForegroundColour(hex_col(NAVY))
            else:
                self.caps_btn.SetBackgroundColour(hex_col(NAVY_LIGHT))
                self.caps_btn.SetForegroundColour(hex_col(CREAM))
            for l_btn in self.letter_btns:
                char = l_btn.GetLabel()
                l_btn.SetLabel(char.upper() if self.caps_lock else char.lower())
            self.panel.Refresh() 
        else:
            self.text_ctrl.SetValue(current_text + label)
            
        self.text_ctrl.SetInsertionPointEnd() 
        
    def on_ok(self, event):
        self.EndModal(wx.ID_OK)
        
    def on_cancel(self, event):
        self.EndModal(wx.ID_CANCEL)
        
    def GetValue(self):
        if self.is_email_mode:
            lingering_text = self.text_ctrl.GetValue().strip()
            if lingering_text and "@" in lingering_text:
                self.add_email_bubble(lingering_text)
            return ", ".join(self.email_list)
        return self.text_ctrl.GetValue()


# ─── CUSTOM INFO DIALOG ───────────────────────────────────────────────────────
class InfoDialog(wx.Dialog):
    def __init__(self, parent, title, content):
        super().__init__(parent, title="Info", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_lbl = wx.StaticText(self.panel, label=title)
        title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
        title_lbl.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title_lbl, 0, wx.ALL, 18)
        
        div = wx.Panel(self.panel, size=(-1, 1))
        div.SetBackgroundColour(hex_col(GRID_LINE))
        main_sizer.Add(div, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 18)
        
        content_text = wx.TextCtrl(self.panel, value=content, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_NONE)
        content_text.SetBackgroundColour(hex_col(NAVY))
        content_text.SetForegroundColour(hex_col(CREAM))
        content_text.SetFont(wx.Font(11, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        content_text.SetMinSize((400, 250))
        main_sizer.Add(content_text, 1, wx.EXPAND | wx.ALL, 18)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = styled_primary_button(self.panel, "Got it", self.on_ok, size=(120, 36))
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(ok_btn, 0, wx.BOTTOM | wx.RIGHT, 18)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent() 
        
    def on_ok(self, event):
        self.EndModal(wx.ID_OK)

# ─── CUSTOM CONFIRMATION DIALOG ───────────────────────────────────────────────
class ConfirmDialog(wx.Dialog):
    def __init__(self, parent, title, message, yes_label="Yes", no_label="No", show_close=False):
        super().__init__(parent, title="Confirm", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        if show_close:
            top_row = wx.BoxSizer(wx.HORIZONTAL)
            
            # Spacer on the left to keep the title perfectly centered
            top_row.Add((30, 30), 0, wx.LEFT, 15)
            top_row.AddStretchSpacer(1)
            
            title_lbl = wx.StaticText(self.panel, label=title)
            title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
            title_lbl.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            top_row.Add(title_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            
            top_row.AddStretchSpacer(1)
            
            # Use StaticText for a clean, flat icon look
            self.btn_close_top = wx.StaticText(self.panel, label="✖")
            self.btn_close_top.SetForegroundColour(hex_col(MUTED))
            self.btn_close_top.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            self.btn_close_top.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            self.btn_close_top.Bind(wx.EVT_LEFT_DOWN, self.on_close)
            
            top_row.Add(self.btn_close_top, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
            main_sizer.Add(top_row, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 12)
        else:
            title_lbl = wx.StaticText(self.panel, label=title)
            title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
            title_lbl.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            main_sizer.Add(title_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 15)
        
        div = wx.Panel(self.panel, size=(-1, 1))
        div.SetBackgroundColour(hex_col(GRID_LINE))
        main_sizer.Add(div, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 18)
        
        msg_lbl = wx.StaticText(self.panel, label=message)
        msg_lbl.SetForegroundColour(hex_col(CREAM))
        msg_lbl.SetFont(wx.Font(11, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        msg_lbl.Wrap(400) 
        main_sizer.Add(msg_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 20)
        
        main_sizer.AddStretchSpacer(1)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_no = wx.Button(self.panel, label=no_label, size=(130, 36))
        self.btn_no.SetBackgroundColour(hex_col(NAVY_MID))
        self.btn_no.SetForegroundColour(hex_col(CREAM))
        self.btn_no.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.btn_no.Bind(wx.EVT_BUTTON, self.on_no)
        
        self.btn_yes = styled_primary_button(self.panel, yes_label, self.on_yes, size=(130, 36))
        if title == "Sign Out":
            self.btn_yes.SetBackgroundColour(hex_col(DANGER))
            self.btn_yes.SetForegroundColour(hex_col(WHITE))
            
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(self.btn_no, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.btn_yes, 0, wx.LEFT, 10)
        btn_sizer.AddStretchSpacer(1)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 18)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent() 
        
    def on_yes(self, event):
        self.EndModal(wx.ID_YES)
        
    def on_no(self, event):
        self.EndModal(wx.ID_NO)

    def on_close(self, event):
        self.EndModal(wx.ID_CANCEL)

# ─── CUSTOM MESSAGE DIALOG ────────────────────────────────────────────────────
class StyledMessageDialog(wx.Dialog):
    def __init__(self, parent, title, message, is_error=False):
        super().__init__(parent, title="Message", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_lbl = wx.StaticText(self.panel, label=title)
        if title in ("Success", "Login Successful", "Sync Complete"):
            title_color = SUCCESS
        elif is_error:
            title_color = DANGER
        else:
            title_color = BLUE_ACCENT
            
        title_lbl.SetForegroundColour(hex_col(title_color))
        title_lbl.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 15)
        
        div = wx.Panel(self.panel, size=(-1, 1))
        div.SetBackgroundColour(hex_col(GRID_LINE))
        main_sizer.Add(div, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 18)
        
        msg_lbl = wx.StaticText(self.panel, label=message)
        msg_lbl.SetForegroundColour(hex_col(CREAM))
        msg_lbl.SetFont(wx.Font(11, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        msg_lbl.Wrap(350) 
        main_sizer.Add(msg_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 20)
        
        main_sizer.AddStretchSpacer(1)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_ok = styled_primary_button(self.panel, "OK", self.on_ok, size=(130, 36))
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(self.btn_ok, 0, wx.ALL, 10)
        btn_sizer.AddStretchSpacer(1)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 18)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent() 
        
    def on_ok(self, event):
        self.EndModal(wx.ID_OK)


# ─── CUSTOM LISTCTRL WITH NATIVE CHECKBOXES ───────────────────────────────────
class CloudCheckListCtrl(wx.ListCtrl):
    def __init__(self, parent, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.EnableCheckBoxes(True) 
        self.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_row_selected)
        
    def on_row_selected(self, event):
        idx = event.GetIndex()
        current_state = self.IsItemChecked(idx)
        self.CheckItem(idx, not current_state)
        wx.CallAfter(self.Select, idx, 0)

# ─── EMAIL ADDRESS BOOK HELPERS (RECIPIENTS) ──────────────────────────────────
def load_saved_emails():
    if os.path.exists(EMAILS_FILE):
        try:
            with open(EMAILS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading emails: {e}")
    return []

def save_emails(emails):
    try:
        with open(EMAILS_FILE, 'w', encoding='utf-8') as f:
            json.dump(emails, f)
    except Exception as e:
        print(f"Failed to save emails: {e}")

class EmailSelectionDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title="Recipients", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        self.saved_emails = load_saved_emails()
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_lbl = wx.StaticText(self.panel, label="Who are you sending this to?")
        title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
        title_lbl.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(title_lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 15)
        
        main_sizer.Add(make_divider(self.panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 15)
        
        self.list_ctrl = CloudCheckListCtrl(self.panel, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES | wx.BORDER_NONE, size=(600, 300))
        self.list_ctrl.SetBackgroundColour(hex_col(NAVY_MID))
        self.list_ctrl.SetTextColour(hex_col(CREAM))
        self.list_ctrl.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.list_ctrl.InsertColumn(0, "Saved Recipient Addresses", width=600)
        
        self.populate_list()
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 15)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        btn_add = styled_primary_button(self.panel, "➕ Add New Email", self.on_add, size=(180, 40))
        btn_add.SetBackgroundColour(hex_col(NAVY_LIGHT))
        btn_add.SetForegroundColour(hex_col(CREAM))
        
        btn_send = styled_primary_button(self.panel, "📤 Send", self.on_send, size=(150, 40))
        btn_send.SetBackgroundColour(hex_col(SUCCESS))
        btn_send.SetForegroundColour(hex_col(WHITE))
        
        btn_cancel = styled_primary_button(self.panel, "✖ Cancel", self.on_cancel, size=(120, 40))
        btn_cancel.SetBackgroundColour(hex_col(DANGER))
        btn_cancel.SetForegroundColour(hex_col(WHITE))
        
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(btn_add, 0, wx.RIGHT, 10)
        btn_sizer.Add(btn_send, 0, wx.RIGHT, 10)
        btn_sizer.Add(btn_cancel, 0, wx.LEFT, 10)
        btn_sizer.AddStretchSpacer(1)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 15)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent()

    def populate_list(self):
        self.list_ctrl.DeleteAllItems()
        for i, email in enumerate(self.saved_emails):
            self.list_ctrl.InsertItem(i, email)

    def on_add(self, event):
        with TouchKeyboardDialog(self, "Add Email", "Type email, then press Space/Enter:", is_email_mode=True) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                raw_emails = dialog.GetValue().strip()
                if raw_emails:
                    new_emails = [e.strip() for e in raw_emails.split(',') if e.strip()]
                    added = False
                    for ne in new_emails:
                        if ne not in self.saved_emails:
                            self.saved_emails.append(ne)
                            added = True
                    if added:
                        save_emails(self.saved_emails)
                        self.populate_list()

    def on_send(self, event):
        if not self.get_selected_emails():
            show_custom_message(self, "No Recipients", "Please select at least one email address by checking the box.", is_error=True)
            return
        self.EndModal(wx.ID_OK)

    def on_cancel(self, event):
        self.EndModal(wx.ID_CANCEL)

    def get_selected_emails(self):
        selected = []
        for i in range(self.list_ctrl.GetItemCount()):
            if self.list_ctrl.IsItemChecked(i):
                selected.append(self.list_ctrl.GetItemText(i, 0))
        return ", ".join(selected)


# ─── CLOUD DATA VIEWER DIALOG (OAUTH INTEGRATED) ──────────────────────────────
class CloudDataViewerDialog(wx.Dialog):
    def __init__(self, parent, collection_name):
        super().__init__(parent, title="Cloud Data", style=wx.STAY_ON_TOP | wx.BORDER_NONE)
        self.panel = wx.Panel(self)
        self.panel.SetBackgroundColour(hex_col(NAVY))
        
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 1. TOP ROW: Title and Close Button
        top_row = wx.BoxSizer(wx.HORIZONTAL)
        
        # Spacer to balance out the close button so the title stays perfectly centered
        top_row.Add((40, 40), 0, wx.LEFT, 15)
        top_row.AddStretchSpacer(1)
        
        title_lbl = wx.StaticText(self.panel, label=f"☁ Live Cloud Data: {collection_name}")
        title_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
        title_lbl.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        top_row.Add(title_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        
        top_row.AddStretchSpacer(1)
        
        # Move Close button to top right
        btn_close_top = wx.Button(self.panel, label="✖", size=(40, 40))
        btn_close_top.SetBackgroundColour(hex_col(DANGER))
        btn_close_top.SetForegroundColour(hex_col(WHITE))
        btn_close_top.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        btn_close_top.Bind(wx.EVT_BUTTON, self.on_close)
        top_row.Add(btn_close_top, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
        
        main_sizer.Add(top_row, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 10)
        
        main_sizer.Add(make_divider(self.panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 15)
        
        # List Ctrl
        self.list_ctrl = CloudCheckListCtrl(self.panel, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES | wx.BORDER_NONE, size=(1000, 400))
        self.list_ctrl.SetBackgroundColour(hex_col(NAVY_MID))
        self.list_ctrl.SetTextColour(hex_col(CREAM))
        self.list_ctrl.SetFont(wx.Font(10, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 15)
        
        # 2. BOTTOM ROW: Select All (Left) & Email Selected (Right)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_select_all = styled_primary_button(self.panel, "☑ Select All", self.on_select_all, size=(160, 40))
        self.btn_select_all.SetBackgroundColour(hex_col(NAVY_LIGHT))
        self.btn_select_all.SetForegroundColour(hex_col(CREAM))
        
        btn_email = styled_primary_button(self.panel, "📧 Email Selected", self.on_email_click, size=(180, 40))
        btn_email.SetBackgroundColour(hex_col(SUCCESS))
        btn_email.SetForegroundColour(hex_col(WHITE))
        
        btn_sizer.Add(self.btn_select_all, 0, wx.LEFT, 15)
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(btn_email, 0, wx.RIGHT, 15)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM, 15)
        
        self.panel.SetSizer(main_sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(self.panel, 1, wx.EXPAND)
        self.SetSizerAndFit(dlg_sizer)
        self.CenterOnParent()
        
        self.collection_name = collection_name
        self.load_cloud_data()
        
    def load_cloud_data(self):
        self.headers = [
            "Batch Name", "Cup", "TDS(ppm)", "Brew Time (s)", "Time", 
            "Overall", "Taste", "Bitterness", "Acidity", "Mouthfeel", 
            "Temp (°C)", "pH", "Turbidity (NTU)"
        ]
        
        col_widths = [100, 50, 75, 95, 150, 65, 65, 80, 65, 80, 80, 60, 110]
        for col_idx, header in enumerate(self.headers):
            self.list_ctrl.InsertColumn(col_idx, header, width=col_widths[col_idx])
            
        try:
            docs = db.collection(self.collection_name).stream()
            for row_idx, doc in enumerate(docs):
                data = doc.to_dict()
                self.list_ctrl.InsertItem(row_idx, str(data.get(self.headers[0], "--")))
                for col_idx in range(1, len(self.headers)):
                    col_name = self.headers[col_idx]
                    self.list_ctrl.SetItem(row_idx, col_idx, str(data.get(col_name, "--")))
        except Exception as e:
            show_custom_message(self, "Network Error", f"Could not load data from Firebase.\n\nError: {e}", is_error=True)

    def on_select_all(self, event):
        num_items = self.list_ctrl.GetItemCount()
        if num_items == 0: return
        all_checked = all(self.list_ctrl.IsItemChecked(i) for i in range(num_items))
        for i in range(num_items):
            self.list_ctrl.CheckItem(i, not all_checked)
        if all_checked:
            self.btn_select_all.SetLabel("☑ Select All")
        else:
            self.btn_select_all.SetLabel("☐ Unselect All")

    def on_email_click(self, event):
        selected_rows = []
        num_items = self.list_ctrl.GetItemCount()
        
        for i in range(num_items):
            if self.list_ctrl.IsItemChecked(i):
                row_data = [self.list_ctrl.GetItemText(i, col) for col in range(self.list_ctrl.GetColumnCount())]
                selected_rows.append(row_data)
                
        if not selected_rows:
            show_custom_message(self, "No Data Selected", "Please tap on at least one row to select it for email.", is_error=True)
            return

        def _proceed_to_email():
            with EmailSelectionDialog(self) as dialog:
                if dialog.ShowModal() == wx.ID_OK:
                    formatted_emails = dialog.get_selected_emails()
                    if formatted_emails:
                        self.prepare_and_send_email(formatted_emails, selected_rows)

        # Ensure user is logged in before opening address book
        if not os.path.exists(TOKEN_FILE):
            dlg = ConfirmDialog(
                self, 
                "Login Required", 
                "You need to log into Google to send emails. We will open a secure browser window for you to authenticate.", 
                yes_label="Open Browser", 
                no_label="Cancel"
            )
            result = dlg.ShowModal()
            dlg.Destroy()
            
            if result == wx.ID_YES:
                threaded_google_login(self, _proceed_to_email)
            return # Stop execution here, the callback will resume the flow if successful
        
        _proceed_to_email()

    def prepare_and_send_email(self, recipient_emails, selected_rows):
        self.Disable() 
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(tmp_fd, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                headers = [self.list_ctrl.GetColumn(i).GetText() for i in range(self.list_ctrl.GetColumnCount())]
                writer.writerow(headers)
                writer.writerows(selected_rows)
                
            threading.Thread(target=self._send_email_thread, args=(recipient_emails, tmp_path, len(selected_rows)), daemon=True).start()
            
        except Exception as e:
            show_custom_message(self, "Error", f"Failed to create CSV:\n{e}", is_error=True)
            self.Enable()

    def _send_email_thread(self, recipient_emails, tmp_path, row_count):
        try:
            # Rebuild the credentials and fetch the sender's actual email address
            creds = authenticate_google()
            service = build('gmail', 'v1', credentials=creds)
            profile = service.users().getProfile(userId='me').execute()
            sender_email = profile.get('emailAddress')

            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            date_file_tag = datetime.datetime.now().strftime("%Y%m%d")
            batch_filename = f"{self.collection_name}.csv"
            attachment_name = f"{self.collection_name}_data_{date_file_tag}.csv"

            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = recipient_emails
            msg['Subject'] = f"Coffee Data Export | File: {self.collection_name} | {now_str}"

            email_body = (
                f"Coffee Machine Monitoring System - Data Export\n"
                f"{'=' * 50}\n\n"
                f"• Source File / Batch : {batch_filename}\n"
                f"• Export Date & Time  : {now_str}\n"
                f"• Total Cups Exported : {row_count} row(s)\n\n"
                f"The exported dataset contains sensor readings (Temperature, pH, Turbidity, TDS, Brew Time)\n"
                f"and cupping sensory ratings (Overall, Taste, Bitterness, Acidity, Mouthfeel).\n\n"
                f"Please find the full CSV file attached below.\n\n"
                f"---\n"
                f"Coffee Machine Field Monitor (Raspberry Pi)"
            )
            msg.attach(MIMEText(email_body, 'plain'))

            with open(tmp_path, 'rb') as f:
                part = MIMEApplication(f.read(), Name=attachment_name)
            part['Content-Disposition'] = f'attachment; filename="{attachment_name}"'
            msg.attach(part)

            # Send via Gmail API directly!
            raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
            create_message = {'raw': raw_msg}
            service.users().messages().send(userId='me', body=create_message).execute()

            wx.CallAfter(show_custom_message, self, "Success", "CSV sent successfully via your Google account!")
            
        except Exception as e:
            wx.CallAfter(show_custom_message, self, "Email Error", f"Failed to send email:\n{str(e)}", True)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            wx.CallAfter(self.Enable)

    def on_close(self, event):
        self.EndModal(wx.ID_OK)


# ═════════════════════════════════════════════════════════════════════════════
#  RESPONSIVE UI CLASSES
# ═════════════════════════════════════════════════════════════════════════════
class ResponsivePanel(wx.Panel):
    def __init__(self, parent, label_text, image_path=None, click_handler=None, default_sys_art=wx.ART_HELP):
        super().__init__(parent)
        self._custom_bg_color = NAVY_LIGHT
        self._hovered = False

        self.original_image = None
        if image_path and os.path.exists(image_path):
            self.original_image = wx.Image(image_path, wx.BITMAP_TYPE_ANY)
        else:
            bmp = wx.ArtProvider.GetBitmap(default_sys_art, wx.ART_BUTTON, (128, 128))
            self.original_image = bmp.ConvertToImage()

        self.icon_ctrl = wx.StaticBitmap(self, -1, wx.Bitmap(1, 1))
        self.text_ctrl = wx.StaticText(self, label=label_text)
        self.text_ctrl.SetForegroundColour(hex_col(CREAM))

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.AddStretchSpacer(1)
        self.sizer.Add(self.icon_ctrl, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 6)
        self.sizer.Add(self.text_ctrl, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 12)
        self.sizer.AddStretchSpacer(1)
        self.SetSizer(self.sizer)

        self.Bind(wx.EVT_SIZE, self.on_resize)
        self.Bind(wx.EVT_PAINT, draw_rounded_panel_bg)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set_hover(True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(False))
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))

        if click_handler:
            self.Bind(wx.EVT_LEFT_DOWN, click_handler)
            self.icon_ctrl.Bind(wx.EVT_LEFT_DOWN, click_handler)
            self.text_ctrl.Bind(wx.EVT_LEFT_DOWN, click_handler)

    def _set_hover(self, state):
        self._hovered = state
        self.Refresh()

    def on_resize(self, event):
        size = self.GetClientSize()
        w, h = size.width, size.height

        if w < 20 or h < 20: 
            return

        icon_dim = int(min(w, h) * 0.35)
        font_size = max(8, int(h * 0.045))

        if self.original_image:
            scaled_img = self.original_image.Scale(icon_dim, icon_dim, wx.IMAGE_QUALITY_HIGH)
            self.icon_ctrl.SetBitmap(wx.Bitmap(scaled_img))

        font = wx.Font(font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.text_ctrl.SetFont(font)
        
        # --- BULLETPROOF GTK FIX ---
        # 1. Measure the exact pixel dimensions the new font requires
        dc = wx.ClientDC(self)
        dc.SetFont(font)
        text_w, text_h = dc.GetTextExtent(self.text_ctrl.GetLabel())
        
        # 2. Force the bounding box to accommodate the exact text size + 10px padding 
        # (Padding prevents GTK from slicing off the bottom of letters like 'g')
        self.text_ctrl.SetMinSize((text_w + 10, text_h + 10))

        self.Layout()
        self.Refresh() 
        event.Skip()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGES
# ═════════════════════════════════════════════════════════════════════════════
class LauncherPage(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(hex_col(NAVY))
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 1. PROFESSIONAL TOP HEADER (Exit Button Top-Right)
        top_bar = wx.BoxSizer(wx.HORIZONTAL)
        top_bar.AddStretchSpacer(1) # Pushes everything to the right
        
        exit_btn = styled_primary_button(self, "✖ Exit App", self.on_exit_click, size=(180, 50))
        exit_btn.SetBackgroundColour(hex_col(DANGER))
        exit_btn.SetForegroundColour(hex_col(WHITE))
        exit_btn.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        top_bar.Add(exit_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 15)
        
        frame_sizer.Add(top_bar, 0, wx.EXPAND)
        
        # 2. MAIN GRID
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        box1 = ResponsivePanel(self, label_text="Training", image_path=os.path.join(GUI_DIR, "sand-clock.png"), click_handler=self.on_training_click)
        box2 = ResponsivePanel(self, label_text="hello")
        
        sizer.Add(box1, proportion=1, flag=wx.EXPAND | wx.ALL, border=22)
        sizer.Add(box2, proportion=1, flag=wx.EXPAND | wx.ALL, border=22)
        frame_sizer.Add(sizer, 1, wx.EXPAND)

        # 3. BOTTOM STATUS BAR (Cleaned up)
        div2 = wx.Panel(self, size=(-1, 1))
        div2.SetBackgroundColour(hex_col(GRID_LINE))
        div2.SetMinSize((-1, 1))
        frame_sizer.Add(div2, 0, wx.EXPAND)
        
        status = wx.Panel(self)
        status.SetBackgroundColour(hex_col(NAVY_LIGHT))
        status.SetMinSize((-1, 50))
        st_s = wx.BoxSizer(wx.HORIZONTAL)
        
        df = wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        
        dot = wx.StaticText(status, label="●  SYSTEM READY")
        dot.SetFont(df)
        dot.SetForegroundColour(hex_col(SUCCESS))
        st_s.Add(dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 20)
        
        st_s.AddStretchSpacer(1)
        
        ts = wx.StaticText(status, label=datetime.datetime.now().strftime("%Y-%m-%d  %H:%M"))
        ts.SetFont(df)
        ts.SetForegroundColour(hex_col(MUTED))
        st_s.Add(ts, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        status.SetSizer(st_s)
        frame_sizer.Add(status, 0, wx.EXPAND)
        
        self.SetSizer(frame_sizer)
        
    def on_training_click(self, event):
        self.GetTopLevelParent().show_page(TrainingPage)
        
    def on_exit_click(self, event):
        self.GetTopLevelParent().Close()


class TrainingPage(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(hex_col(NAVY))
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        back_row = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = styled_back_button(self, self.on_back)
        back_row.Add(self.back_btn, 0, wx.ALL, 10)
        back_row.AddStretchSpacer(1)
        main_sizer.Add(back_row, 0, wx.EXPAND)
        
        file_sizer = wx.BoxSizer(wx.VERTICAL)
        file_sizer.AddStretchSpacer(1)

        self.instructions = wx.StaticText(self, label="Choose a file option for training:")
        self.instructions.SetForegroundColour(hex_col(CREAM))
        file_sizer.Add(self.instructions, 0, wx.ALIGN_CENTER | wx.BOTTOM, 24)

        button_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_prev_file = ResponsivePanel(
            self, label_text="Select Previous File", image_path=os.path.join(GUI_DIR, "open-file.png"),
            click_handler=self.on_select_previous, default_sys_art=wx.ART_FILE_OPEN
        )
        self.btn_new_file = ResponsivePanel(
            self, label_text="Create New File", image_path=os.path.join(GUI_DIR, "add-file.png"),
            click_handler=self.on_create_new, default_sys_art=wx.ART_NORMAL_FILE
        )
        
        button_sizer.Add(self.btn_prev_file, 1, wx.EXPAND | wx.ALL, 14)
        button_sizer.Add(self.btn_new_file, 1, wx.EXPAND | wx.ALL, 14)
        file_sizer.Add(button_sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 40)

        self.file_path_label = wx.StaticText(self, label="No file selected yet.")
        self.file_path_label.SetForegroundColour(hex_col(MUTED))
        file_sizer.Add(self.file_path_label, 0, wx.ALIGN_CENTER | wx.TOP, 18)

        file_sizer.AddStretchSpacer(1)
        main_sizer.Add(file_sizer, 1, wx.EXPAND)

        self.SetSizer(main_sizer)
        self.Bind(wx.EVT_SIZE, self.on_resize)

    def on_resize(self, event):
        size = self.GetClientSize()
        w, h = size.width, size.height
        
        scale_button(self.back_btn, w, h, is_icon_only=True)
        
        title_size = max(10, int(h * 0.04))
        self.instructions.SetFont(wx.Font(title_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.instructions.InvalidateBestSize()
        
        path_size = max(8, int(h * 0.025))
        self.file_path_label.SetFont(wx.Font(path_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.file_path_label.InvalidateBestSize()
        
        self.Layout()
        event.Skip()

    def on_select_previous(self, event):
        filepath = select_existing_file(self)
        if filepath:
            self.file_path_label.SetLabel(f"Using existing file:\n{filepath}")
            self.process_training_file(filepath, is_new=False)

    def on_create_new(self, event):
        filepath = create_new_file(self)
        if filepath:
            self.file_path_label.SetLabel(f"Using new file:\n{filepath}")
            self.process_training_file(filepath, is_new=True)

    def process_training_file(self, filepath, is_new):
        self.GetTopLevelParent().show_page(PageRatingPage, filepath=filepath)

    def on_back(self, event):
        self.GetTopLevelParent().show_page(LauncherPage)


# -----------------------------------------------------------------------------
# Helper Class for 5-Star Category Row
# -----------------------------------------------------------------------------
class CategoryStarRating(wx.Panel):
    def __init__(self, parent, label_text, descriptions=None, info_text=""):
        super().__init__(parent)
        self.rating = 0.0
        self.stars = []
        self.desc_labels = [] 
        self.descriptions = descriptions 
        self.info_text = info_text  
        self.info_img = None 
        self.SetBackgroundColour(hex_col(NAVY))
        
        main_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.label_container = wx.BoxSizer(wx.HORIZONTAL)
        
        self.category_label = wx.StaticText(self, label=label_text)
        self.category_label.SetForegroundColour(hex_col(CREAM))
        font = wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.category_label.SetFont(font)
        self.label_container.Add(self.category_label, 0, wx.ALIGN_CENTER_VERTICAL)
        
        if self.info_text:
            icon_path = os.path.join(GUI_DIR, "info.png")
            if os.path.exists(icon_path):
                self.info_img = wx.Image(icon_path, wx.BITMAP_TYPE_ANY)
                temp_scaled = self.info_img.Scale(18, 18, wx.IMAGE_QUALITY_HIGH)
                bmp = wx.Bitmap(temp_scaled)
                self.info_icon = wx.StaticBitmap(self, -1, bmp)
                self.info_icon.SetCursor(wx.Cursor(wx.CURSOR_HAND))
                self.info_icon.Bind(wx.EVT_LEFT_DOWN, self.on_info_click)
                self.label_container.Add(self.info_icon, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            else:
                self.info_icon = wx.StaticText(self, label=" [?]")
                self.info_icon.SetForegroundColour(hex_col(BLUE_ACCENT))
                self.info_icon.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                self.info_icon.SetCursor(wx.Cursor(wx.CURSOR_HAND))
                self.info_icon.Bind(wx.EVT_LEFT_DOWN, self.on_info_click)
                self.label_container.Add(self.info_icon, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)

        main_sizer.Add(self.label_container, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        
        right_hbox = wx.BoxSizer(wx.HORIZONTAL)
        stars_hbox = wx.BoxSizer(wx.HORIZONTAL)
        
        for i in range(1, 6):
            star_vbox = wx.BoxSizer(wx.VERTICAL)
            
            star_panel = wx.Panel(self)
            star_panel.SetMinSize((40, 40)) 
            star_panel.SetBackgroundColour(hex_col(NAVY))
            star_panel.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            star_panel.SetName(f"star_{i}")
            star_panel.star_state = "empty"
            
            star_panel.Bind(wx.EVT_PAINT, self.on_paint_star)
            star_panel.Bind(wx.EVT_LEFT_DOWN, self.on_star_click)
            
            self.stars.append(star_panel)
            star_vbox.Add(star_panel, 0, wx.ALIGN_CENTER_HORIZONTAL)
            
            desc_word = ""
            if self.descriptions and float(i) in self.descriptions:
                desc_word = self.descriptions[float(i)]
                
            desc_lbl = wx.StaticText(self, label=desc_word, style=wx.ALIGN_CENTER)
            desc_lbl.SetForegroundColour(hex_col(MUTED)) 
            font_desc = wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL)
            desc_lbl.SetFont(font_desc)
            
            self.desc_labels.append(desc_lbl)
            star_vbox.Add(desc_lbl, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 2)
            stars_hbox.Add(star_vbox, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            
        right_hbox.Add(stars_hbox, 0, wx.ALIGN_CENTER_VERTICAL)
        
        self.rating_text = wx.StaticText(self, label="0.0 ★")
        self.rating_text.SetForegroundColour(hex_col(MUTED))
        font_rating = wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.rating_text.SetFont(font_rating)
        
        right_hbox.Add(self.rating_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 15)
        main_sizer.Add(right_hbox, 1, wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(main_sizer)

    def on_info_click(self, event):
        dlg = InfoDialog(self, f"{self.category_label.GetLabel()} Guide", self.info_text)
        dlg.ShowModal()
        dlg.Destroy()

    def apply_scale(self, label_font_size, star_box_size, rating_font_size, label_width):
        font = wx.Font(label_font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.category_label.SetFont(font)
        self.category_label.InvalidateBestSize()
        
        if hasattr(self, 'info_icon'):
            if self.info_img:
                icon_dim = max(16, int(label_font_size * 1.8))
                scaled_img = self.info_img.Scale(icon_dim, icon_dim, wx.IMAGE_QUALITY_HIGH)
                self.info_icon.SetBitmap(wx.Bitmap(scaled_img))
            else:
                fallback_font_size = max(12, int(label_font_size * 1.2))
                self.info_icon.SetFont(wx.Font(fallback_font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                self.info_icon.InvalidateBestSize()

        self.label_container.SetMinSize((label_width, -1))
        for star_panel in self.stars:
            star_panel.SetMinSize((star_box_size, star_box_size))
            
        rating_font = wx.Font(rating_font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.rating_text.SetFont(rating_font)
        self.rating_text.InvalidateBestSize()
        
        desc_font_size = max(8, int(rating_font_size * 0.8))
        desc_font = wx.Font(desc_font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL)
        for desc_lbl in self.desc_labels:
            desc_lbl.SetFont(desc_font)
            desc_lbl.InvalidateBestSize()
        
        self.Layout()
        self.Refresh()

    def on_paint_star(self, event):
        panel = event.GetEventObject()
        dc    = wx.PaintDC(panel)
        dc.SetBackground(wx.Brush(hex_col(NAVY)))
        dc.Clear()
        
        w, h = panel.GetClientSize()
        if w == 0 or h == 0: return
        
        star_size = int(min(w, h) * 0.85)
        font = wx.Font(star_size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        dc.SetFont(font)

        state = getattr(panel, 'star_state', 'empty')
        empty_char, full_char  = "☆", "★"
        tw, th = dc.GetTextExtent(empty_char)
        
        x = max(0, (w - tw) // 2)
        y = max(0, (h - th) // 2) - int(star_size * 0.1)

        if state == "empty":
            dc.SetTextForeground(hex_col(GRID_LINE))
            dc.DrawText(empty_char, x, y)
        elif state == "full":
            dc.SetTextForeground(hex_col(AMBER))
            dc.DrawText(full_char, x, y)
        elif state == "half":
            dc.SetTextForeground(hex_col(GRID_LINE))
            dc.DrawText(empty_char, x, y)
            dc.SetTextForeground(hex_col(AMBER))
            dc.SetClippingRegion(0, y, w // 2, h)
            dc.DrawText(full_char, x, y)
            dc.DestroyClippingRegion()

    def on_star_click(self, event):
        star_widget = event.GetEventObject()
        name_parts  = star_widget.GetName().split('_')
        star_index  = int(name_parts[1])
        click_x     = event.GetPosition().x
        star_width  = star_widget.GetClientSize().width

        if star_index == 1:
            if click_x < (star_width / 3): new_rating = 0.0
            elif click_x < (2 * star_width / 3): new_rating = 0.5
            else: new_rating = 1.0
        else:
            if click_x < (star_width / 2): new_rating = star_index - 0.5
            else: new_rating = float(star_index)

        if self.rating == new_rating and new_rating != 0.0:
            self.rating = 0.0
        else:
            self.rating = new_rating

        self.update_stars()
        
        self.rating_text.SetLabel(f"{self.rating} ★")
        self.rating_text.SetForegroundColour(hex_col(AMBER))
        
        for i, desc_lbl in enumerate(self.desc_labels):
            star_val = i + 1
            if self.rating == float(star_val) or self.rating == star_val - 0.5:
                if self.rating <= 2.0: desc_lbl.SetForegroundColour(hex_col(DANGER))
                elif self.rating == 5.0: desc_lbl.SetForegroundColour(hex_col(SUCCESS))
                else: desc_lbl.SetForegroundColour(hex_col(BLUE_ACCENT))
            else:
                desc_lbl.SetForegroundColour(hex_col(MUTED))
        self.Layout()

    def update_stars(self):
        for idx, star_panel in enumerate(self.stars):
            star_val = idx + 1
            if self.rating >= star_val: star_panel.star_state = "full"
            elif self.rating >= (star_val - 0.5): star_panel.star_state = "half"
            else: star_panel.star_state = "empty"
            star_panel.Refresh()

    def get_rating(self):
        return self.rating


class PageRatingPage(wx.Panel):
    def __init__(self, parent, filepath):
        super().__init__(parent)
        self.filepath     = filepath
        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(hex_col(NAVY)) 
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        back_row = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = styled_back_button(self, self.on_back)
        back_row.Add(self.back_btn, 0, wx.ALL, 10)
        back_row.AddStretchSpacer(1)
        main_sizer.Add(back_row, 0, wx.EXPAND)

        self.title_lbl = wx.StaticText(self, label="Rate the Following Categories")
        self.title_lbl.SetForegroundColour(hex_col(CREAM))
        self.title_lbl.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(self.title_lbl, 0, wx.ALIGN_CENTER | wx.BOTTOM | wx.TOP, 10)

        bn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.bn_lbl = wx.StaticText(self, label="Batch Name:")
        self.bn_lbl.SetForegroundColour(hex_col(MUTED))
        
        default_batch = os.path.basename(filepath).replace('.csv', '')
        self.batch_input = wx.TextCtrl(self, value=default_batch, size=(200, -1), style=wx.TE_READONLY)
        self.batch_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.batch_input.SetForegroundColour(hex_col(CREAM))
        self.batch_input.Bind(wx.EVT_LEFT_DOWN, self.on_batch_click)
        
        bn_sizer.Add(self.bn_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bn_sizer.Add(self.batch_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 30)

        self.cup_lbl = wx.StaticText(self, label="Cup:")
        self.cup_lbl.SetForegroundColour(hex_col(MUTED))

        self.btn_minus = wx.Button(self, label="-", size=(40, -1))
        self.btn_minus.SetBackgroundColour(hex_col(NAVY_LIGHT))
        self.btn_minus.SetForegroundColour(hex_col(CREAM))
        
        self.cup_input = wx.TextCtrl(self, value="1", size=(50, -1), style=wx.TE_CENTER | wx.TE_READONLY)
        self.cup_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.cup_input.SetForegroundColour(hex_col(CREAM))

        self.btn_plus = wx.Button(self, label="+", size=(40, -1))
        self.btn_plus.SetBackgroundColour(hex_col(NAVY_LIGHT))
        self.btn_plus.SetForegroundColour(hex_col(CREAM))

        self.btn_minus.Bind(wx.EVT_BUTTON, self.on_cup_minus)
        self.btn_plus.Bind(wx.EVT_BUTTON, self.on_cup_plus)

        bn_sizer.Add(self.cup_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bn_sizer.Add(self.btn_minus, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        bn_sizer.Add(self.cup_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        bn_sizer.Add(self.btn_plus, 0, wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(bn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 24)
        main_sizer.Add(make_divider(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 60)
        
        self.scroll_panel = scrolled.ScrolledPanel(self, -1)
        self.scroll_panel.SetBackgroundColour(hex_col(NAVY))
        
        scroll_sizer = wx.BoxSizer(wx.VERTICAL)
        scroll_sizer.AddStretchSpacer(1)
        
        overall_descriptions = {0.5: "", 1.0: "Very Poor", 1.5: "", 2.0: "", 2.5: "", 3.0: "Average", 3.5: "", 4.0: "", 4.5: "", 5.0: "Excellent"}
        taste_descriptions = {0.5: "", 1.0: "Unbalanced", 1.5: "", 2.0: "", 2.5: "", 3.0: "Imbalance", 3.5: "", 4.0: "", 4.5: "", 5.0: "Well-balanced"}
        bitterness_descriptions = {0.5: "", 1.0: "Harsh", 1.5: "", 2.0: "", 2.5: "", 3.0: "Noticeable", 3.5: "", 4.0: "", 4.5: "", 5.0: "Smooth"}
        acidity_descriptions = {0.5: "", 1.0: "Sour", 1.5: "", 2.0: "", 2.5: "", 3.0: "Mild", 3.5: "", 4.0: "", 4.5: "", 5.0: "Bright"}
        mouthfeel_descriptions = {0.5: "", 1.0: "Thin", 1.5: "", 2.0: "", 2.5: "", 3.0: "Medium", 3.5: "", 4.0: "", 4.5: "", 5.0: "Full"}
        
        info_texts = {
            "Overall Quality": (
                "What it means:\n"
                "The overall impression of the coffee, considering taste, balance, and drinkability.\n\n"
                "How to rate:\n"
                "1: Very poor, unpleasant, not drinkable\n"
                "3: Acceptable, average quality\n"
                "5: Excellent, well-executed and enjoyable"
            ),
            "Taste Balance": (
                "What it means:\n"
                "How well acidity, sweetness, and bitterness work together without one dominating.\n\n"
                "How to rate:\n"
                "1: Very unbalanced (one aspect overpowering)\n"
                "3: Some imbalance but acceptable\n"
                "5: Well-balanced and harmonious"
            ),
            "Bitterness": (
                "What it means:\n"
                "The intensity and quality of bitterness in the coffee.\n\n"
                "How to rate:\n"
                "1: Harsh, unpleasant, over-extracted\n"
                "3: Noticeable but acceptable\n"
                "5: Smooth, controlled, and pleasant"
            ),
            "Acidity": (
                "What it means:\n"
                "The brightness or sharpness of the coffee, and whether it is pleasant or sour.\n\n"
                "How to rate:\n"
                "1: Sharp, sour, unpleasant\n"
                "3: Mild or slightly unbalanced\n"
                "5: Bright, lively, and pleasant"
            ),
            "Mouthfeel": (
                "What it means:\n"
                "The texture and weight of the coffee in the mouth.\n\n"
                "How to rate:\n"
                "1: Thin, watery\n"
                "3: Medium body\n"
                "5: Full, smooth, and rich"
            )
        }

        categories = ["Overall Quality", "Taste Balance", "Bitterness", "Acidity", "Mouthfeel"]
        self.rating_panels = {}
        
        cat_sizer = wx.BoxSizer(wx.VERTICAL)
        for cat in categories:
            if cat == "Overall Quality": custom_desc = overall_descriptions
            elif cat == "Taste Balance": custom_desc = taste_descriptions
            elif cat == "Bitterness": custom_desc = bitterness_descriptions
            elif cat == "Acidity": custom_desc = acidity_descriptions
            elif cat == "Mouthfeel": custom_desc = mouthfeel_descriptions
            else: custom_desc = None 
                
            cat_panel = CategoryStarRating(self.scroll_panel, cat, descriptions=custom_desc, info_text=info_texts.get(cat, ""))
            self.rating_panels[cat] = cat_panel
            cat_sizer.Add(cat_panel, 0, wx.ALIGN_CENTER | wx.BOTTOM, 15)
            
        scroll_sizer.Add(cat_sizer, 0, wx.ALIGN_CENTER)
        scroll_sizer.AddStretchSpacer(2)
        
        self.scroll_panel.SetSizer(scroll_sizer)
        self.scroll_panel.SetupScrolling(scroll_x=False, scroll_y=True)
        main_sizer.Add(self.scroll_panel, 1, wx.EXPAND | wx.ALL, 0)
        
        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(hex_col(NAVY_LIGHT))
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer(1)
        
        self.submit_btn = styled_primary_button(self.footer, "💾 Save Ratings", self.on_submit)
        btn_sizer.Add(self.submit_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        self.footer.SetSizer(btn_sizer)
        
        main_sizer.Add(self.footer, 0, wx.EXPAND)
        self.SetSizer(main_sizer)
        self.Bind(wx.EVT_SIZE, self.on_resize)

    def on_batch_click(self, event):
        with TouchKeyboardDialog(self, "Edit Batch Name", "Enter the new batch name:", default_val=self.batch_input.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.batch_input.SetValue(dialog.GetValue().strip())

    def on_cup_minus(self, event):
        try:
            val = int(self.cup_input.GetValue())
            if val > 1: self.cup_input.SetValue(str(val - 1))
        except ValueError: self.cup_input.SetValue("1")

    def on_cup_plus(self, event):
        try:
            val = int(self.cup_input.GetValue())
            self.cup_input.SetValue(str(val + 1))
        except ValueError: self.cup_input.SetValue("1")

    def on_resize(self, event):
        size = self.GetClientSize()
        h, w = size.height, size.width
        if h < 50 or w < 50: return

        scale_button(self.back_btn, w, h, is_icon_only=True)
        scale_button(self.submit_btn, w, h, is_icon_only=False)

        footer_h = max(60, int(h * 0.08))
        self.footer.SetMinSize((-1, footer_h))

        title_font_size  = max(14, int(h * 0.045))
        label_font_size  = max(10, int(h * 0.025))
        rating_font_size = max(10, int(h * 0.025))
        star_box_size    = max(30, int(h * 0.070)) 
        label_width      = max(120, int(w * 0.22))

        lbl_font  = max(11, int(h * 0.025))
        val_font  = max(14, int(h * 0.035))
        font_lbl = wx.Font(lbl_font, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.bn_lbl.SetFont(font_lbl)
        self.bn_lbl.InvalidateBestSize()
        self.cup_lbl.SetFont(font_lbl)
        self.cup_lbl.InvalidateBestSize()
        
        btn_font = wx.Font(val_font, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.cup_input.SetFont(btn_font)
        self.btn_minus.SetFont(btn_font)
        self.btn_plus.SetFont(btn_font)
        self.batch_input.SetFont(btn_font)

        batch_w = max(150, int(w * 0.25))
        cup_w   = max(50, int(w * 0.06))
        btn_w   = max(40, int(w * 0.04))
        input_h = max(36, int(h * 0.065)) 

        self.batch_input.SetMinSize((batch_w, input_h))
        self.cup_input.SetMinSize((cup_w, input_h))
        self.btn_minus.SetMinSize((btn_w, input_h))
        self.btn_plus.SetMinSize((btn_w, input_h))

        self.title_lbl.SetFont(wx.Font(title_font_size, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.title_lbl.InvalidateBestSize()
        
        for cat_panel in self.rating_panels.values():
            cat_panel.apply_scale(label_font_size, star_box_size, rating_font_size, label_width)

        self.Layout()
        event.Skip()

    def on_submit(self, event):
        custom_batch = self.batch_input.GetValue()
        custom_cup = self.cup_input.GetValue()

        cat_ratings = {}
        for cat_name, panel in self.rating_panels.items():
            cat_ratings[cat_name] = panel.get_rating()

        next_page = self.GetTopLevelParent().show_page(
            DataRatingPage, 
            filepath=self.filepath, 
            category_ratings=cat_ratings 
        )
        
        next_page.batch_input.SetValue(custom_batch)
        next_page.cup_input.SetValue(custom_cup)

    def on_back(self, event):
        self.GetTopLevelParent().show_page(TrainingPage)


class DataRatingPage(wx.Panel):
    def __init__(self, parent, filepath, category_ratings=None):
        super().__init__(parent)
        self.filepath       = filepath
        self.category_ratings = category_ratings if category_ratings else {} 

        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(hex_col(NAVY))
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        back_row = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = styled_back_button(self, self.on_back)
        back_row.Add(self.back_btn, 0, wx.ALL, 10)
        back_row.AddStretchSpacer(1)
        main_sizer.Add(back_row, 0, wx.EXPAND)

        main_sizer.AddStretchSpacer(1) 

        self.title_lbl = wx.StaticText(self, label="Sensed Data")
        self.title_lbl.SetForegroundColour(hex_col(CREAM))
        main_sizer.Add(self.title_lbl, 0, wx.ALIGN_CENTER | wx.BOTTOM, 12)

        bn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.bn_lbl = wx.StaticText(self, label="Batch Name:")
        self.bn_lbl.SetForegroundColour(hex_col(MUTED))
        
        default_batch = os.path.basename(filepath).replace('.csv', '')
        
        self.batch_input = wx.TextCtrl(self, value=default_batch, size=(200, -1), style=wx.TE_READONLY)
        self.batch_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.batch_input.SetForegroundColour(hex_col(CREAM))
        self.batch_input.Bind(wx.EVT_LEFT_DOWN, self.on_batch_click)
        
        bn_sizer.Add(self.bn_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bn_sizer.Add(self.batch_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 30)

        self.cup_lbl = wx.StaticText(self, label="Cup:")
        self.cup_lbl.SetForegroundColour(hex_col(MUTED))

        self.btn_minus = wx.Button(self, label="-", size=(40, -1))
        self.btn_minus.SetBackgroundColour(hex_col(NAVY_LIGHT))
        self.btn_minus.SetForegroundColour(hex_col(CREAM))
        
        self.cup_input = wx.TextCtrl(self, value="1", size=(50, -1), style=wx.TE_CENTER | wx.TE_READONLY)
        self.cup_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.cup_input.SetForegroundColour(hex_col(CREAM))

        self.btn_plus = wx.Button(self, label="+", size=(40, -1))
        self.btn_plus.SetBackgroundColour(hex_col(NAVY_LIGHT))
        self.btn_plus.SetForegroundColour(hex_col(CREAM))

        self.btn_minus.Bind(wx.EVT_BUTTON, self.on_cup_minus)
        self.btn_plus.Bind(wx.EVT_BUTTON, self.on_cup_plus)

        bn_sizer.Add(self.cup_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bn_sizer.Add(self.btn_minus, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        bn_sizer.Add(self.cup_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        bn_sizer.Add(self.btn_plus, 0, wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(bn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 24)

        main_sizer.Add(make_divider(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 60)
        main_sizer.AddStretchSpacer(1)

        self.param_sizer = wx.FlexGridSizer(rows=5, cols=2, vgap=40, hgap=60)

        self.t_lbl = wx.StaticText(self, label="Temperature")
        self.t_lbl.SetForegroundColour(hex_col(MUTED))
        self.temp_val = wx.StaticText(self, label="-- °C")
        self.temp_val.SetForegroundColour(hex_col(DANGER))
        self.param_sizer.Add(self.t_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.param_sizer.Add(self.temp_val, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        self.p_lbl = wx.StaticText(self, label="pH Level")
        self.p_lbl.SetForegroundColour(hex_col(MUTED))
        self.ph_val = wx.StaticText(self, label="--")
        self.ph_val.SetForegroundColour(hex_col(BLUE_ACCENT))
        self.param_sizer.Add(self.p_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.param_sizer.Add(self.ph_val, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        self.tb_lbl = wx.StaticText(self, label="Turbidity")
        self.tb_lbl.SetForegroundColour(hex_col(MUTED))
        self.turb_val = wx.StaticText(self, label="-- NTU")
        self.turb_val.SetForegroundColour(hex_col(SUCCESS))
        self.param_sizer.Add(self.tb_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.param_sizer.Add(self.turb_val, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        self.tds_lbl = wx.StaticText(self, label="TDS (ppm)")
        self.tds_lbl.SetForegroundColour(hex_col(MUTED))
        
        self.tds_input = wx.TextCtrl(self, value="", size=(100, -1), style=wx.TE_CENTER | wx.TE_READONLY)
        self.tds_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.tds_input.SetForegroundColour(hex_col(CREAM))
        self.tds_input.Bind(wx.EVT_LEFT_DOWN, self.on_tds_click)

        self.param_sizer.Add(self.tds_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.param_sizer.Add(self.tds_input, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        self.brew_time_lbl = wx.StaticText(self, label="Brew Time (s)")
        self.brew_time_lbl.SetForegroundColour(hex_col(MUTED))
        
        self.brew_time_input = wx.TextCtrl(self, value="", size=(100, -1), style=wx.TE_CENTER | wx.TE_READONLY)
        self.brew_time_input.SetBackgroundColour(hex_col(NAVY_MID))
        self.brew_time_input.SetForegroundColour(hex_col(CREAM))
        self.brew_time_input.Bind(wx.EVT_LEFT_DOWN, self.on_brew_time_click)

        self.param_sizer.Add(self.brew_time_lbl, 0, wx.ALIGN_RIGHT | wx.ALIGN_CENTER_VERTICAL)
        self.param_sizer.Add(self.brew_time_input, 0, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)

        main_sizer.Add(self.param_sizer, 0, wx.ALIGN_CENTER)
        main_sizer.AddStretchSpacer(2)

        main_sizer.Add(make_divider(self), 0, wx.EXPAND)
        
        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(hex_col(NAVY_LIGHT))
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer(1)
        
        self.submit_btn = styled_primary_button(self.footer, "💾 Save Data", self.on_submit)
        btn_sizer.Add(self.submit_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        self.footer.SetSizer(btn_sizer)
        
        main_sizer.Add(self.footer, 0, wx.EXPAND)
        self.SetSizer(main_sizer)
        
        self.Bind(wx.EVT_SIZE, self.on_resize)

        self.ser = None
        try:
            self.ser = serial.Serial(USB_PORT, BAUD_RATE, timeout=0.5)
        except Exception as e:
            print(f"Warning: Could not open USB port on DataRatingFrame. {e}")

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.fetch_live_sensor_data, self.timer)
        self.timer.Start(1500)
        
    def on_batch_click(self, event):
        with TouchKeyboardDialog(self, "Edit Batch Name", "Enter the new batch name:", default_val=self.batch_input.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.batch_input.SetValue(dialog.GetValue().strip())

    def on_tds_click(self, event):
        with TouchKeyboardDialog(self, "TDS Value", "Enter TDS (ppm):", default_val=self.tds_input.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.tds_input.SetValue(dialog.GetValue().strip())

    def on_brew_time_click(self, event):
        with TouchKeyboardDialog(self, "Brew Time", "Enter Brew Time in seconds:", default_val=self.brew_time_input.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                val = dialog.GetValue().strip()
                if val.isdigit() or val == "":
                    self.brew_time_input.SetValue(val)
                else:
                    show_custom_message(self, "Invalid Input", "Please enter a valid whole number for seconds.", is_error=True)

    def on_cup_minus(self, event):
        try:
            val = int(self.cup_input.GetValue())
            if val > 1: self.cup_input.SetValue(str(val - 1))
        except ValueError: self.cup_input.SetValue("1")

    def on_cup_plus(self, event):
        try:
            val = int(self.cup_input.GetValue())
            self.cup_input.SetValue(str(val + 1))
        except ValueError: self.cup_input.SetValue("1")

    def on_resize(self, event):
        size = self.GetClientSize()
        h, w = size.height, size.width
        if h < 50: return

        scale_button(self.back_btn, w, h, is_icon_only=True)
        scale_button(self.submit_btn, w, h, is_icon_only=False)

        footer_h = max(60, int(h * 0.08))
        self.footer.SetMinSize((-1, footer_h))

        title_font  = max(16, int(h * 0.045))
        lbl_font    = max(11, int(h * 0.025))
        val_font    = max(14, int(h * 0.035))

        self.title_lbl.SetFont(wx.Font(title_font, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.title_lbl.InvalidateBestSize()
        
        font_lbl = wx.Font(lbl_font, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.bn_lbl.SetFont(font_lbl)
        self.bn_lbl.InvalidateBestSize()
        self.cup_lbl.SetFont(font_lbl)
        self.cup_lbl.InvalidateBestSize()
        
        btn_font = wx.Font(val_font, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.cup_input.SetFont(btn_font)
        self.btn_minus.SetFont(btn_font)
        self.btn_plus.SetFont(btn_font)
        self.batch_input.SetFont(btn_font)

        batch_w = max(150, int(w * 0.25))
        cup_w   = max(50, int(w * 0.06))
        btn_w   = max(40, int(w * 0.04))
        input_h = max(36, int(h * 0.065)) 

        self.batch_input.SetMinSize((batch_w, input_h))
        self.cup_input.SetMinSize((cup_w, input_h))
        self.btn_minus.SetMinSize((btn_w, input_h))
        self.btn_plus.SetMinSize((btn_w, input_h))

        font_val = wx.Font(val_font, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)

        self.t_lbl.SetFont(font_lbl)
        self.t_lbl.InvalidateBestSize()
        self.p_lbl.SetFont(font_lbl)
        self.p_lbl.InvalidateBestSize()
        self.tb_lbl.SetFont(font_lbl)
        self.tb_lbl.InvalidateBestSize()
        
        self.tds_lbl.SetFont(font_lbl)
        self.tds_lbl.InvalidateBestSize()
        self.tds_input.SetFont(font_val)
        tds_w = max(100, int(w * 0.15))
        self.tds_input.SetMinSize((tds_w, input_h))

        self.brew_time_lbl.SetFont(font_lbl)
        self.brew_time_lbl.InvalidateBestSize()
        self.brew_time_input.SetFont(font_val)
        self.brew_time_input.SetMinSize((tds_w, input_h))
        
        self.temp_val.SetFont(font_val)
        self.temp_val.InvalidateBestSize()
        self.ph_val.SetFont(font_val)
        self.ph_val.InvalidateBestSize()
        self.turb_val.SetFont(font_val)
        self.turb_val.InvalidateBestSize()

        v_gap = max(26, int(h * 0.055))
        h_gap = max(40, int(w * 0.08))
        self.param_sizer.SetVGap(v_gap)
        self.param_sizer.SetHGap(h_gap)

        self.Layout()
        event.Skip()

    def fetch_live_sensor_data(self, event):
        data = poll_sensor_binary(self.ser)
        if data:
            temp, ph, turb = data
            self.temp_val.SetLabel(f"{temp:.2f} °C")
            self.ph_val.SetLabel(f"{ph:.2f}")
            self.turb_val.SetLabel(f"{turb:.0f} NTU")
            self.Layout()

    def cleanup_hardware(self):
        if hasattr(self, 'timer'): self.timer.Stop()
        if hasattr(self, 'ser') and self.ser and self.ser.is_open:
            self.ser.close()

    def on_back(self, event):
        self.cleanup_hardware()
        self.GetTopLevelParent().show_page(PageRatingPage, filepath=self.filepath)

    def on_submit(self, event):
        self.cleanup_hardware()
        
        custom_batch_name = self.batch_input.GetValue().strip()
        if not custom_batch_name: custom_batch_name = "Unknown_Batch"
            
        cup_val = self.cup_input.GetValue().strip()
        if not cup_val.isdigit(): cup_val = "1"
            
        tds_val = self.tds_input.GetValue().strip()
        brew_time_val = self.brew_time_input.GetValue().strip()
            
        self.GetTopLevelParent().show_page(
            ResultsPage, 
            filepath=self.filepath, 
            category_ratings=self.category_ratings, 
            batch_name=custom_batch_name, 
            cup_val=cup_val,
            tds_val=tds_val,
            brew_time_val=brew_time_val
        )


class ResultsPage(wx.Panel):
    def __init__(self, parent, filepath, category_ratings, batch_name, cup_val, tds_val, brew_time_val):
        super().__init__(parent) 
        self.filepath       = filepath
        self.category_ratings = category_ratings 
        self.batch_name   = batch_name
        self.cup_val      = cup_val 
        self.tds_val      = tds_val 
        self.brew_time_val = brew_time_val

        self.current_temp = "--"
        self.current_ph   = "--"
        self.current_turb = "--"
        
        self.is_logging = False

        self.SetDoubleBuffered(True)
        self.SetBackgroundColour(hex_col(NAVY))
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        back_row = wx.BoxSizer(wx.HORIZONTAL)
        self.back_btn = styled_back_button(self, self.on_back)
        back_row.Add(self.back_btn, 0, wx.ALL, 10)
        back_row.AddStretchSpacer(1)
        
        self.btn_user_profile = GoogleProfileChip(self, size=(220, 36))
        self.btn_user_profile.bind_click(self.on_user_profile_click)
        back_row.Add(self.btn_user_profile, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
        
        self.btn_view_cloud = styled_primary_button(self, "🔍 View Cloud Database", self.on_view_cloud_click, size=(220, 36))
        back_row.Add(self.btn_view_cloud, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 15)
        
        self.cloud_status = wx.StaticText(self, label="☁ ✖ Unsynced")
        self.cloud_status.SetFont(wx.Font(14, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.cloud_status.SetForegroundColour(hex_col(MUTED)) 
        self.cloud_status.SetCursor(wx.Cursor(wx.CURSOR_HAND)) 
        self.cloud_status.Bind(wx.EVT_LEFT_DOWN, self.on_cloud_sync)
        
        back_row.Add(self.cloud_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        main_sizer.Add(back_row, 0, wx.EXPAND)

        title_lbl  = wx.StaticText(self, label="Measured Data Log")
        title_font = wx.Font(17, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        title_lbl.SetFont(title_font)
        title_lbl.SetForegroundColour(hex_col(CREAM))
        main_sizer.Add(title_lbl, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 4)

        self.table_headers = [
            "Batch Name", "Cup", "TDS(ppm)", "Brew Time (s)", "Time", 
            "Overall", "Taste", "Bitterness","Acidity", "Mouthfeel", 
            "Temp (°C)", "pH", "Turbidity (NTU)"
        ]
        
        self.grid = wx.grid.Grid(self)
        self.grid.CreateGrid(0, len(self.table_headers))
        self.grid.SetRowLabelSize(0)
        self.grid.SetMargins(0, 0)
        self.grid.SetGridLineColour(hex_col(GRID_LINE))
        self.grid.SetLabelBackgroundColour(hex_col(NAVY_MID))
        self.grid.SetLabelTextColour(hex_col(MUTED))

        for i, header in enumerate(self.table_headers):
            self.grid.SetColLabelValue(i, header)
            
        self.load_existing_csv_data()
            
        self.grid.AutoSizeColumns()
        self.grid.Bind(wx.EVT_SIZE, self.on_grid_resize)
        main_sizer.Add(self.grid, 1, wx.EXPAND | wx.ALL, 18)

        self.live_banner = wx.StaticText(self, label="Live Sensor Data: Waiting for hardware...")
        banner_font = wx.Font(11, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.live_banner.SetFont(banner_font)
        self.live_banner.SetForegroundColour(hex_col(SUCCESS))
        
        banner_sizer = wx.BoxSizer(wx.HORIZONTAL)
        banner_sizer.AddStretchSpacer(1)                                     
        banner_sizer.Add(self.live_banner, 0, wx.ALIGN_CENTER_VERTICAL)  
        banner_sizer.AddStretchSpacer(1)                                     
        main_sizer.Add(banner_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        log_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.toggle_log_btn = styled_primary_button(self, "Start Auto-Log", self.on_toggle_log, size=(160, 36))
        log_sizer.AddStretchSpacer(1)
        log_sizer.Add(self.toggle_log_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        log_sizer.AddStretchSpacer(1)
        
        main_sizer.Add(log_sizer, 0, wx.EXPAND | wx.BOTTOM, 16)
        main_sizer.Add(make_divider(self), 0, wx.EXPAND)

        self.footer = wx.Panel(self)
        self.footer.SetBackgroundColour(hex_col(NAVY_LIGHT))
        
        f_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.live_indicator = wx.StaticText(self.footer, label="● LIVE")
        self.live_indicator.SetFont(wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.live_indicator.SetForegroundColour(hex_col(SUCCESS))
        f_sizer.Add(self.live_indicator, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 20)
        
        f_sizer.AddStretchSpacer(1)
        
        self.finish_btn = styled_primary_button(self.footer, "💾 Save", self.on_finish, size=(160, 38))
        self.finish_btn.SetBackgroundColour(hex_col(NAVY_MID))
        self.finish_btn.SetForegroundColour(hex_col(CREAM))
        f_sizer.Add(self.finish_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        self.footer.SetSizer(f_sizer)
        main_sizer.Add(self.footer, 0, wx.EXPAND)
        self.SetSizer(main_sizer)

        self.Bind(wx.EVT_SIZE, self.on_resize)

        self.ser = None
        try:
            self.ser = serial.Serial(USB_PORT, BAUD_RATE, timeout=0.5)
        except Exception as e:
            print(f"Warning: Could not open USB port on ResultsFrame. {e}")

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.update_live_data, self.timer)
        self.timer.Start(1500)

        wx.CallAfter(self.resize_grid_columns)
        wx.CallAfter(self.on_cloud_sync, None)
        self.update_user_profile_button()

    def update_user_profile_button(self):
        email = get_logged_in_email()
        self.btn_user_profile.set_email(email)

    def on_user_profile_click(self, event):
        email = get_logged_in_email()
        if not email:
            dlg = ConfirmDialog(
                self, 
                "Secure Sign-In", 
                "We are going to open a secure browser window for you to log into your Google account.\n\nReady to continue?", 
                yes_label="Open Browser", 
                no_label="Cancel"
            )
            result = dlg.ShowModal()
            dlg.Destroy()
            
            if result == wx.ID_YES:
                threaded_google_login(self, self.update_user_profile_button)
        else:
            # Replaces the native wx.Menu with the custom fused pill dropdown
            popup = SignOutPopup(self, self.on_signout)
            
            btn = self.btn_user_profile
            screen_pos = btn.ClientToScreen((0, 0))
            btn_size = btn.GetSize()
            popup_size = popup.GetSize()
            
            # Center the pill horizontally beneath the profile chip
            screen_pos.x += (btn_size.width - popup_size.width) // 2
            screen_pos.y += btn_size.height + 4
            
            popup.Position(screen_pos, (0, 0))
            popup.Popup()

    def on_signout(self, event):
        if os.path.exists(TOKEN_FILE):
            dlg = ConfirmDialog(self, "Sign Out", "Are you sure you want to securely sign out the currently logged-in Google account?", yes_label="Sign Out", no_label="Cancel")
            result = dlg.ShowModal()
            dlg.Destroy()
            if result == wx.ID_YES:
                try:
                    os.remove(TOKEN_FILE)
                    self.update_user_profile_button()
                    show_custom_message(self, "Signed Out", "Your Google account has been securely signed out.\n\nThe next user will be prompted to log in before sending emails.", is_error=False)
                except Exception as e:
                    show_custom_message(self, "Error", f"Could not sign out: {e}", is_error=True)

    def on_resize(self, event):
        size = self.GetClientSize()
        w, h = size.width, size.height
        if h < 50 or w < 50: return
        
        scale_button(self.back_btn, w, h, is_icon_only=True)
        scale_button(self.finish_btn, w, h, is_icon_only=False)

        footer_h = max(60, int(h * 0.08))
        self.footer.SetMinSize((-1, footer_h))
        
        self.Layout()
        event.Skip()

    def load_existing_csv_data(self):
        if not os.path.exists(self.filepath): return
            
        try:
            with open(self.filepath, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None) 
                
                is_old_format = False
                if header and len(header) < 13:
                    is_old_format = True
                
                val_font = wx.Font(10, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
                
                for row in reader:
                    if not row: continue 
                    row_idx = self.grid.GetNumberRows()
                    self.grid.AppendRows(1)
                    
                    if is_old_format and len(row) == 10:
                        mapped_row = [
                            row[0], row[1], row[2], "--", row[3],  
                            "--", "--", "--", "--", "--",    
                            row[7], row[8], row[9]             
                        ]
                    elif is_old_format and len(row) >= 15:
                        mapped_row = row[:9] + ["--"] + row[12:15]
                    else:
                        mapped_row = row 
                    
                    for col_idx, val in enumerate(mapped_row):
                        if col_idx < self.grid.GetNumberCols():
                            self.grid.SetCellValue(row_idx, col_idx, str(val))
                            self.grid.SetReadOnly(row_idx, col_idx)
                            self.grid.SetCellFont(row_idx, col_idx, val_font)
                            self.grid.SetCellAlignment(row_idx, col_idx, wx.ALIGN_CENTER, wx.ALIGN_CENTER)
                            self.grid.SetCellBackgroundColour(row_idx, col_idx, hex_col(NAVY_LIGHT))
                            
                            if 5 <= col_idx <= 9:
                                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(AMBER))
                            elif 10 <= col_idx <= 12:
                                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(SUCCESS))
                            else:
                                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(CREAM))
        except Exception as e:
            print(f"Failed to load existing CSV: {e}")

    def on_grid_resize(self, event):
        self.resize_grid_columns()
        event.Skip()

    def resize_grid_columns(self):
        if not hasattr(self, 'grid') or not self.grid: return
        self.grid.AutoSizeColumns()
        grid_width = self.grid.GetClientSize().width
        num_cols = self.grid.GetNumberCols()
        total_cols_width = sum(self.grid.GetColSize(i) for i in range(num_cols))
        if grid_width > total_cols_width and num_cols > 0:
            extra = (grid_width - total_cols_width) // num_cols
            for i in range(num_cols):
                self.grid.SetColSize(i, self.grid.GetColSize(i) + extra)

    def update_live_data(self, event):
        data = poll_sensor_binary(self.ser)
        if data:
            temp, ph, turb = data
            self.current_temp = f"{temp:.2f}"
            self.current_ph   = f"{ph:.2f}"
            self.current_turb = f"{turb:.0f}"
            
            banner_text = f"Live Data ➔ Temp: {self.current_temp} °C    |    pH: {self.current_ph}    |    Turb: {self.current_turb} NTU"
            self.live_banner.SetLabel(banner_text)
            self.Layout()
            
            current_label = self.live_indicator.GetLabel()
            self.live_indicator.SetLabel("○ LIVE" if current_label == "● LIVE" else "● LIVE")

            if self.is_logging: self.record_data_row()

    def on_toggle_log(self, event):
        if not self.is_logging:
            if not self.tds_val:
                show_custom_message(self, "Missing Data", "You didn't enter a TDS value on the previous page. Auto-Log will record a blank TDS.", is_error=True)

            self.is_logging = True
            self.toggle_log_btn.SetLabel("Stop Auto-Log")
            self.toggle_log_btn.SetBackgroundColour(hex_col(DANGER)) 
            self.toggle_log_btn.SetForegroundColour(hex_col(WHITE))
        else:
            self.is_logging = False
            self.toggle_log_btn.SetLabel("Start Auto-Log")
            self.toggle_log_btn.SetBackgroundColour(hex_col(AMBER)) 
            self.toggle_log_btn.SetForegroundColour(hex_col(NAVY))
        
        self.Layout()

    def record_data_row(self):
        row_idx = self.grid.GetNumberRows()
        self.grid.AppendRows(1)
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        values = [
            self.batch_name, 
            self.cup_val,
            self.tds_val, 
            self.brew_time_val,
            current_time, 
            self.category_ratings.get("Overall Quality", 0.0), 
            self.category_ratings.get("Taste Balance", 0.0),    
            self.category_ratings.get("Bitterness", 0.0),      
            self.category_ratings.get("Acidity", 0.0),         
            self.category_ratings.get("Mouthfeel", 0.0),        
            self.current_temp, 
            self.current_ph, 
            self.current_turb
        ]
        
        val_font = wx.Font(10, wx.FONTFAMILY_MODERN, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        
        for col_idx, val in enumerate(values):
            self.grid.SetCellValue(row_idx, col_idx, str(val))
            self.grid.SetReadOnly(row_idx, col_idx)
            self.grid.SetCellFont(row_idx, col_idx, val_font)
            self.grid.SetCellAlignment(row_idx, col_idx, wx.ALIGN_CENTER, wx.ALIGN_CENTER)
            self.grid.SetCellBackgroundColour(row_idx, col_idx, hex_col(NAVY_LIGHT))
            
            if 5 <= col_idx <= 9:
                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(AMBER))
            elif 10 <= col_idx <= 12:
                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(SUCCESS))
            else:
                self.grid.SetCellTextColour(row_idx, col_idx, hex_col(CREAM))
        
        self.resize_grid_columns()
        self.grid.MakeCellVisible(row_idx, 0)
        self.grid.ForceRefresh()
        self.append_to_csv(values)

        if self.cloud_status.GetLabel() == "☁ ✔ Synced":
            self.cloud_status.SetLabel("☁ ⏳ Syncing...")
            self.cloud_status.SetForegroundColour(hex_col(AMBER))
            self.Layout()
            
        row_dict = {}
        for col_idx, col_name in enumerate(self.table_headers):
            row_dict[col_name] = str(values[col_idx])
            
        threading.Thread(target=self.upload_single_row_background, args=(row_dict,), daemon=True).start()

    def append_to_csv(self, values):
        try:
            file_exists = os.path.isfile(self.filepath)
            with open(self.filepath, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(self.table_headers)
                writer.writerow(values)
        except Exception as e:
            print(f"Failed to save to CSV: {e}")

    def upload_single_row_background(self, row_dict):
        try:
            collection_name = os.path.splitext(os.path.basename(self.filepath))[0]
            collection_ref = db.collection(collection_name)
            
            doc_id = str(row_dict.get("Time", ""))
            if not doc_id:
                doc_id = f"auto_{time.time()}"
                
            collection_ref.document(doc_id).set(row_dict, merge=True)
            
            wx.CallAfter(self.update_cloud_status_success)
            
        except Exception as e:
            print(f"Background sync error: {e}")
            wx.CallAfter(self.update_cloud_status_fail)

    def update_cloud_status_success(self):
        self.cloud_status.SetLabel("☁ ✔ Synced")
        self.cloud_status.SetForegroundColour(hex_col(SUCCESS))
        self.Layout()

    def update_cloud_status_fail(self):
        self.cloud_status.SetLabel("☁ ✖ Failed")
        self.cloud_status.SetForegroundColour(hex_col(DANGER))
        self.Layout()

    def on_cloud_sync(self, event=None):
        self.cloud_status.SetLabel("☁ ⏳ Syncing...")
        self.cloud_status.SetForegroundColour(hex_col(AMBER)) 
        self.cloud_status.Unbind(wx.EVT_LEFT_DOWN) 
        self.Layout()

        data_to_upload = []
        num_rows = self.grid.GetNumberRows()
        num_cols = self.grid.GetNumberCols()
        
        for r in range(num_rows):
            row_dict = {}
            for c in range(num_cols):
                col_name = self.table_headers[c]
                val = self.grid.GetCellValue(r, c)
                row_dict[col_name] = str(val)
            data_to_upload.append(row_dict)

        collection_name = os.path.splitext(os.path.basename(self.filepath))[0]
        
        is_manual_click = (event is not None)
        threading.Thread(
            target=self._background_sync_task, 
            args=(collection_name, data_to_upload, is_manual_click), 
            daemon=True
        ).start()

    def _background_sync_task(self, collection_name, data_to_upload, is_manual_click):
        try:
            collection_ref = db.collection(collection_name)
            for index, data_dict in enumerate(data_to_upload):
                doc_id = str(data_dict.get("Time", ""))
                if not doc_id:
                    doc_id = f"row_{index}"
                    
                collection_ref.document(doc_id).set(data_dict, merge=True)

            wx.CallAfter(self._finalize_sync, True, collection_name, is_manual_click, None)

        except Exception as e:
            wx.CallAfter(self._finalize_sync, False, collection_name, is_manual_click, str(e))

    def _finalize_sync(self, success, collection_name, is_manual_click, error_msg):
        if success:
            self.cloud_status.SetLabel("☁ ✔ Synced")
            self.cloud_status.SetForegroundColour(hex_col(SUCCESS)) 
            if is_manual_click: 
                show_custom_message(self, "Sync Complete", f"Successfully synced '{collection_name}' to Firestore!")
        else:
            self.cloud_status.SetLabel("☁ ✖ Failed")
            self.cloud_status.SetForegroundColour(hex_col(DANGER)) 
            if is_manual_click:
                show_custom_message(self, "Sync Error", f"Failed to sync to Firestore.\nError: {error_msg}", is_error=True)

        self.cloud_status.Bind(wx.EVT_LEFT_DOWN, self.on_cloud_sync)
        self.Layout()

    def on_view_cloud_click(self, event):
        collection_name = os.path.splitext(os.path.basename(self.filepath))[0]
        dlg = CloudDataViewerDialog(self, collection_name)
        dlg.ShowModal()
        dlg.Destroy()

    def cleanup_hardware(self):
        self.is_logging = False 
        if hasattr(self, 'timer'): self.timer.Stop()
        if hasattr(self, 'ser') and self.ser and self.ser.is_open:
            self.ser.close()

    def on_back(self, event):
        self.cleanup_hardware()
        
        prev_page = self.GetTopLevelParent().show_page(
            DataRatingPage, 
            filepath=self.filepath, 
            category_ratings=self.category_ratings
        )
        
        prev_page.batch_input.SetValue(self.batch_name)
        prev_page.cup_input.SetValue(str(self.cup_val))
        prev_page.tds_input.SetValue(str(self.tds_val))
        prev_page.brew_time_input.SetValue(str(self.brew_time_val))

    def on_finish(self, event):
        dlg = ConfirmDialog(
            self, 
            "Next Cup?", 
            "Do you want to record another cup for this batch?", 
            yes_label="Yes, Next Cup", 
            no_label="No, I'm Done",
            show_close=True
        )
        result = dlg.ShowModal()
        dlg.Destroy()
        
        if result == wx.ID_YES:
            self.cleanup_hardware()
            rating_page = self.GetTopLevelParent().show_page(PageRatingPage, filepath=self.filepath)
            rating_page.batch_input.SetValue(self.batch_name)
            
            try:
                next_cup = int(self.cup_val) + 1
                rating_page.cup_input.SetValue(str(next_cup))
            except Exception:
                pass
            
        elif result == wx.ID_NO:
            self.cleanup_hardware()
            show_custom_message(self, "Done", "Session finished and all lines saved to CSV!")
            self.GetTopLevelParent().show_page(LauncherPage)


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN APP FRAME (Single Window Architecture)
# ═════════════════════════════════════════════════════════════════════════════
class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Coffee Machine — Field Monitor", size=(820, 600))
        self.SetMinSize((640, 420))
        self.SetBackgroundColour(hex_col(NAVY))
        
        # --- ENABLE KIOSK MODE ---
        self.ShowFullScreen(True, wx.FULLSCREEN_ALL)
        
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.sizer)
        
        self.current_page = None
        self.show_page(LauncherPage)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def show_page(self, page_class, **kwargs):
        self.Freeze() 
        
        if self.current_page:
            if hasattr(self.current_page, 'cleanup_hardware'):
                self.current_page.cleanup_hardware()
            self.current_page.Destroy()
            
        self.current_page = page_class(self, **kwargs)
        self.sizer.Add(self.current_page, 1, wx.EXPAND)
        
        self.Layout()
        self.Thaw() 
        
        return self.current_page

    def on_close(self, event):
        if self.current_page and hasattr(self.current_page, 'cleanup_hardware'):
            self.current_page.cleanup_hardware()
        event.Skip()


if __name__ == "__main__":
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
