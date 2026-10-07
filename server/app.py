# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Glanceboard — Firebase Cloud Functions (Multi-User, Multi-Device).

Serverless image generation pipeline with subscription tiers:
  - Free (self-hosted): User runs their own Firebase project
  - Hosted ($3/mo): User provides their own API key, we host the app
  - Plus ($9/mo): Fully managed, server-side API key
  - Additional devices: $10/mo per extra display (paid tiers only)

Display hardware: Waveshare ESP32-S3 PhotoPainter (all-in-one e-ink frame).
Legacy Raspberry Pi + separate display is still supported but no longer primary.

Pipeline (per device):
  1. Fetch calendar events via Google Calendar API (OAuth refresh token)
  2. Fetch weather via Open-Meteo API
  3. Load character config from user's Firestore subcollection
  4. Build an adventure prompt (with weather context)
  5. Generate image via appropriate API key (user's or server's)
  6. Resize & dither for the 6-color e-ink display
  7. Save to device's Firebase Storage path (publicly accessible for the Pi)

Data model:
  User-level (shared across devices):
    Firestore: users/{uid}/settings/account  (API key, timezone, location)
    Firestore: users/{uid}/settings/subscription
    Firestore: users/{uid}/settings/google_tokens
    Firestore: users/{uid}/characters/{id}

  Device-level (per display):
    Firestore: users/{uid}/devices/{deviceId}  (name, aesthetic, model, calendar)
    Firestore: users/{uid}/devices/{deviceId}/prompt/prompt
    Firestore: users/{uid}/devices/{deviceId}/status/status
    Storage:   users/{uid}/devices/{deviceId}/display/*
    Storage:   users/{uid}/devices/{deviceId}/archive/*

  Legacy (pre-migration, still supported with fallback reads):
    Firestore: users/{uid}/settings/config
    Firestore: users/{uid}/settings/prompt
    Storage:   users/{uid}/display/*
"""
import base64
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image

# ─── FastAPI Init ──────────────────────────────────────────────

app = FastAPI(title="Glanceboard Local Server")

# We will mount static files later

# ─── Constants ──────────────────────────────────────────────────

DISPLAY_WIDTH = 800
DISPLAY_HEIGHT = 480
DEFAULT_TIMEZONE = "Australia/Sydney"

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

DEFAULT_TEXT_MODEL = "gemini-flash-latest"

# ─── Optional Gmail Integration ────────────────────────────────
# These dependencies are optional — install via: pip install -r requirements-email.txt
# See EMAIL_SETUP.md for full setup instructions.
GMAIL_AVAILABLE = False
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow, Flow
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build as build_gmail_service
    GMAIL_AVAILABLE = True
except ImportError:
    pass

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = "data/gmail_credentials.json"
GMAIL_TOKEN_FILE = "data/gmail_token.json"

# E-Ink Spectra 6 color palette (RGB)
EINK_PALETTE = np.array([
    [0,   0,   0],      # Black
    [255, 255, 255],    # White
    [200, 30,  30],     # Red
    [0,   128, 0],      # Green
    [0,   50,  180],    # Blue
    [230, 200, 0],      # Yellow
], dtype=np.float64)

# Colour codes the PhotoPainter firmware expects, in the same row order as
# EINK_PALETTE above. The firmware orders its palette differently
# (display.h: BLACK=0, WHITE=1, GREEN=2, BLUE=3, RED=4, YELLOW=5), so this
# maps our palette index to the firmware's code rather than reusing the index.
EINK_PALETTE_NIBBLES = np.array([0x0, 0x1, 0x4, 0x2, 0x3, 0x5], dtype=np.uint8)

BACKUP_QUOTES = [
    "Be kind to everyone you meet today!",
    "Today is a great day to learn something new!",
    "You are brave and wonderful!",
    "What adventure will you find today?",
    "Read a book, change the world! 📚",
    "Smile at someone today — it's contagious!",
    "Try something you've never done before!",
    "Be a good friend today!",
    "The world is better because you're in it!",
    "Every day is a chance to be awesome!",
    "Kindness is a superpower — use it!",
    "Dream big, start small, act now!",
]

# Weather condition code mapping (WMO codes from Open-Meteo)
WMO_WEATHER_CODES = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌧️"),
    56: ("Freezing drizzle", "🌧️"),
    57: ("Heavy freezing drizzle", "🌧️"),
    61: ("Light rain", "🌦️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "🌨️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"),
    81: ("Showers", "🌧️"),
    82: ("Heavy showers", "⛈️"),
    85: ("Light snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with hail", "⛈️"),
    99: ("Heavy thunderstorm with hail", "⛈️"),
}

DEFAULT_PROMPT_TEMPLATE = """Create a children's illustrated daily planner in pen-and-ink style on a clean white paper background with crosshatching. The output image MUST be EXACTLY 800×480 pixels — a wide landscape format (5:3 aspect ratio). The image MUST be significantly wider than it is tall.

CRITICAL FRAMING: Leave generous margins — at least 40 pixels of padding on ALL sides (top, bottom, left, right). Do NOT place any text, characters, or important elements near the edges. Everything must be well within the safe zone to avoid clipping on the e-ink display.

LAYOUT — FULL-WIDTH SCENE WITH OVERLAID TEXT:

The ENTIRE image is a single charming pen-and-ink illustration. {{SCENE_DESCRIPTION}} The scene fills the whole canvas.

TOP: A ribbon banner reads: '{{BANNER_TEXT}}' in bold hand-drawn block letters. Keep it well below the top edge.

{{TEXT_LAYOUT}}
{{EVENT_LIST}}

RIGHT SIDE ({{RIGHT_WIDTH}}) — MAIN SCENE:
This is where the main action and characters are. The illustration flows naturally from the left side but the main focal point (characters, action) is on the right so it doesn't compete with the text.
{{CHARACTERS}}

{{WEATHER}}

{{COUNTDOWN}}

STYLE RULES: Pen-and-ink illustration, clean WHITE background, hand-drawn crosshatching, charming and whimsical.
Use ONLY these colors: black ink on pure white paper, plus limited accents of red, green, blue, and yellow. The background MUST be plain white (#FFFFFF) — no cream, beige, parchment, or off-white tones.
Kid-friendly, warm, joyful. No scary elements.
The text on the left must be CLEARLY READABLE — high contrast against the background.
Remember: 800×480 pixels, wide landscape, generous margins on all sides.

{{REGION_GUIDANCE}}"""


FASHION_PROMPT_TEMPLATE = """Create a stylish fashion-illustration daily planner in high-end editorial sketch style. The output image MUST be EXACTLY 800×480 pixels — a wide landscape format (5:3 aspect ratio). The image MUST be significantly wider than it is tall.

CRITICAL FRAMING: Leave generous margins — at least 40 pixels of padding on ALL sides (top, bottom, left, right). Do NOT place any text, characters, or important elements near the edges. Everything must be well within the safe zone to avoid clipping on the e-ink display.

LAYOUT — FULL-WIDTH SCENE WITH OVERLAID TEXT:

The ENTIRE image is a single elegant fashion illustration. {{SCENE_DESCRIPTION}} Think high-fashion editorial meets daily planner — loose, confident brush strokes and fine ink lines on a clean white background.

TOP: An elegant hand-lettered header reads: '{{BANNER_TEXT}}' in stylish calligraphic or modern serif letters. Keep it well below the top edge.

{{TEXT_LAYOUT}}
{{EVENT_LIST}}

RIGHT SIDE ({{RIGHT_WIDTH}}) — MAIN SCENE:
This is the focal point. Show the characters in a scene related to the day's events, rendered in fashion illustration style — elongated proportions, confident ink lines, watercolor washes in muted tones, editorial poses. Think Garance Doré, Inslee Haynes, or Jason Brooks style illustration.
{{CHARACTERS}}

{{WEATHER}}

{{COUNTDOWN}}

STYLE RULES: Fashion illustration / editorial sketch style. Confident loose ink lines, watercolor washes, muted sophisticated color palette.
Use ONLY these colors: black ink on pure white paper, plus limited accents of muted red, sage green, dusty blue, and ochre yellow. The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white tones.
Sophisticated, modern, editorial. Loose and artistic, not tight or cartoonish.
The text on the left must be CLEARLY READABLE — elegant but legible.
Remember: 800×480 pixels, wide landscape, generous margins on all sides.

{{REGION_GUIDANCE}}"""


# ─── Google OAuth Helpers ───────────────────────────────────────

# ─── Calendar Fetching (iCal) ───────────────────────────────────

def _fetch_events_ical(ical_url, range_start, range_end, timezone):
    """Fetch events from iCal feed (fallback if no Google OAuth)."""
    from icalendar import Calendar as ICalendar
    import recurring_ical_events

    try:
        response = requests.get(ical_url, timeout=15)
        if response.status_code != 200:
            return []

        cal = ICalendar.from_ical(response.content)
        tz = ZoneInfo(timezone)
        events_in_range = recurring_ical_events.of(cal).between(range_start, range_end)

        events = []
        for component in events_in_range:
            summary = str(component.get("summary", "Untitled event"))
            dtstart = component.get("dtstart")
            dtend = component.get("dtend")
            start_str = ""
            start_iso = None
            end_iso = None

            if dtstart:
                dt = dtstart.dt
                if hasattr(dt, "hour"):
                    if dt.tzinfo:
                        dt = dt.astimezone(tz)
                    start_str = dt.strftime("%H:%M")
                    start_iso = dt.isoformat()
                else:
                    start_str = "All day"

            if dtend:
                dt_end = dtend.dt
                if hasattr(dt_end, "hour"):
                    if dt_end.tzinfo:
                        dt_end = dt_end.astimezone(tz)
                    end_iso = dt_end.isoformat()

            location = str(component.get("location", "") or "")
            events.append({
                "summary": summary,
                "start": start_str,
                "start_iso": start_iso,
                "end_time": end_iso,
                "location": location,
            })

        events.sort(key=lambda e: e.get("start", ""))
        return events
    except Exception as e:
        print(f"iCal fetch error: {e}")
        return []


def fetch_events_ical(ical_url, timezone=DEFAULT_TIMEZONE, target_date=None):
    """Fetch events from iCal feed for a specific date."""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    if target_date is None:
        target_date = now.date()
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=tz)
    end = start + timedelta(days=1)
    return _fetch_events_ical(ical_url, start, end, timezone)


# ─── Weather Fetching ───────────────────────────────────────────

def fetch_weather(latitude, longitude, temp_unit="celsius"):
    """
    Fetch current weather from Open-Meteo API (free, no API key needed).

    Returns a dict with: temp, temp_unit_symbol, condition, condition_emoji,
    high, low, clothing_hint.
    """
    try:
        temp_param = "celsius" if temp_unit == "celsius" else "fahrenheit"
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={latitude}&longitude={longitude}"
            f"&current=temperature_2m,weather_code"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&temperature_unit={temp_param}"
            f"&forecast_days=1"
            f"&timezone=auto"
        )

        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"Weather API error: HTTP {response.status_code}")
            return None

        data = response.json()
        current = data.get("current", {})
        daily = data.get("daily", {})

        current_temp = current.get("temperature_2m")
        weather_code = current.get("weather_code", 0)
        condition, emoji = WMO_WEATHER_CODES.get(weather_code, ("Unknown", "🌡️"))

        high = daily.get("temperature_2m_max", [None])[0]
        low = daily.get("temperature_2m_min", [None])[0]

        unit_symbol = "°C" if temp_unit == "celsius" else "°F"

        # Use the day's HIGH temperature for the main display and clothing.
        # Early-morning current temps are misleadingly cold and cause the AI
        # to overdress the characters. The high is what matters for the day.
        display_temp = high if high is not None else current_temp

        # Generate clothing hint based on the day's HIGH temperature (in celsius)
        if display_temp is not None:
            temp_c = display_temp if temp_unit == "celsius" else (display_temp - 32) * 5 / 9
        else:
            temp_c = None

        if temp_c is None:
            clothing_hint = ""
        elif temp_c >= 30:
            clothing_hint = "light summer clothes (shorts, t-shirts, sun hats)"
        elif temp_c >= 22:
            clothing_hint = "casual warm-weather clothes (t-shirts, light pants)"
        elif temp_c >= 15:
            clothing_hint = "layers or light jumpers"
        elif temp_c >= 8:
            clothing_hint = "warm clothes (jackets, long pants)"
        elif temp_c >= 0:
            clothing_hint = "heavy winter clothes (coats, scarves, beanies)"
        else:
            clothing_hint = "very heavy winter gear (thick coats, gloves, boots)"

        # Add rain gear if wet
        if weather_code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
            clothing_hint += ", and rain gear (umbrellas, rain jackets)"
        elif weather_code in (71, 73, 75, 85, 86):
            clothing_hint += ", and snow-appropriate gear"

        return {
            "temp": display_temp,  # Day's high (used for display + clothing)
            "current_temp": current_temp,  # Actual current reading
            "unit_symbol": unit_symbol,
            "condition": condition,
            "emoji": emoji,
            "high": high,
            "low": low,
            "clothing_hint": clothing_hint,
        }

    except Exception as e:
        print(f"Weather fetch error: {e}")
        return None


def _reverse_geocode_location(latitude, longitude):
    """Get a rough location name from lat/long using Open-Meteo's geocoding.

    Returns a string like 'Sydney, Australia' or 'London, UK'.
    Falls back gracefully to empty string if the API fails.
    """
    try:
        resp = requests.get(
            f"https://nominatim.openstreetmap.org/reverse"
            f"?lat={latitude}&lon={longitude}&format=json&zoom=10",
            headers={"User-Agent": "Glanceboard/1.0"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            address = data.get("address", {})
            city = address.get("city") or address.get("town") or address.get("suburb", "")
            country = address.get("country", "")
            if city and country:
                return f"{city}, {country}"
            elif country:
                return country
    except Exception as e:
        print(f"  Reverse geocode failed: {e}")
    return ""


def describe_scene_weather_via_gemini(weather, season, timezone, api_key,
                                       api_provider="google", location_name="",
                                       events=None, text_model=None):
    """Use the configured text model to generate a realistic, location-aware scene
    description based on actual weather conditions.

    Instead of just saying 'winter scene' (which causes the image model to draw
    snow everywhere), this produces something like 'a cool, overcast Sydney
    winter morning with green trees and grey skies — characters wear light
    jackets'.

    Returns a 1-2 sentence scene description string, or a sensible fallback.
    """
    if not api_key or not weather:
        return f"The setting is a {season} day."

    temp = weather.get("temp")
    unit = weather.get("unit_symbol", "°C")
    condition = weather.get("condition", "").lower()
    clothing = weather.get("clothing_hint", "")

    # Build event context so the scene relates to the day's activities
    event_context = ""
    if events:
        summaries = [e.get("humanized") or e.get("summary", "") for e in events[:5]]
        if summaries:
            event_context = f"\nToday's activities include: {', '.join(summaries)}."

    location_ctx = f" in {location_name}" if location_name else ""

    prompt = f"""You are writing a scene description for a children's illustrated daily planner.

Describe the outdoor setting for an illustration given these REAL weather conditions:
- Temperature: {temp}{unit}
- Condition: {condition}
- Season: {season}
- Location: {location_name or 'unspecified'}{event_context}

Rules:
- Be specific and REALISTIC for the actual location and temperature
- Do NOT mention snow, frost, or ice unless the temperature is below 2°C
- Do NOT include animals that don't exist in the location's region (e.g. no badgers, foxes, deer, or raccoons in Australia; no kangaroos in Europe)
- Focus on the sky, light, trees, atmosphere, and what people would wear
- If the location is known, use regionally appropriate vegetation (e.g. eucalyptus and gum trees for Australia, not oak and pine)
- Keep it to 1-2 SHORT sentences describing just the atmosphere and setting
- Do NOT mention specific people or characters

Example outputs:
- "A mild, cool winter morning{location_ctx} with grey overcast skies and green trees. The light is soft and gentle."
- "A bright, warm summer afternoon with clear blue skies and dappled sunlight through leafy trees."
- "A crisp autumn day with golden leaves and a gentle breeze under partly cloudy skies."

Now write the scene description:"""

    try:
        if api_provider == "openrouter":
            tm = text_model or DEFAULT_TEXT_MODEL
            # OpenRouter needs google/ prefix
            or_model = f"google/{tm}" if not tm.startswith("google/") else tm
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": or_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        else:
            tm = text_model or DEFAULT_TEXT_MODEL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tm}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Clean up — remove quotes if Gemini wraps the response
        text = text.strip('"').strip("'")
        print(f"  🌤️ Scene description: {text[:100]}...")
        return text

    except Exception as e:
        print(f"  ⚠️ Scene description failed ({e}), using fallback")
        return f"The setting is a {season} day, {temp}{unit} and {condition}."

def _fetch_sports_real(team_name):
    """Fetch the last match result for a team from TheSportsDB (free, no API key)."""
    try:
        # Search for the team
        resp = requests.get(
            "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
            params={"t": team_name}, timeout=10,
        )
        resp.raise_for_status()
        teams = resp.json().get("teams", [])
        if not teams:
            print(f"  ⚠️ Sports: Team '{team_name}' not found on TheSportsDB")
            return None
        
        team_id = teams[0]["idTeam"]
        team_full = teams[0].get("strTeam", team_name)
        
        # Get last events
        resp2 = requests.get(
            "https://www.thesportsdb.com/api/v1/json/3/eventslast.php",
            params={"id": team_id}, timeout=10,
        )
        resp2.raise_for_status()
        events = resp2.json().get("results", [])
        if not events:
            return f"{team_full}: No recent results found."
        
        ev = events[0]
        home = ev.get("strHomeTeam", "?")
        away = ev.get("strAwayTeam", "?")
        home_score = ev.get("intHomeScore", "?")
        away_score = ev.get("intAwayScore", "?")
        date = ev.get("dateEvent", "")
        
        # Determine win/loss/draw from perspective of team_name
        is_home = (home.lower() in team_name.lower() or team_name.lower() in home.lower())
        try:
            h = int(home_score)
            a = int(away_score)
            if is_home:
                if h > a:
                    result_word = "defeated"
                elif h < a:
                    result_word = "lost to"
                else:
                    result_word = "drew with"
                opponent = away
                score_str = f"{h}-{a}"
            else:
                if a > h:
                    result_word = "defeated"
                elif a < h:
                    result_word = "lost to"
                else:
                    result_word = "drew with"
                opponent = home
                score_str = f"{a}-{h}"
        except (ValueError, TypeError):
            result_word = "played"
            opponent = away if is_home else home
            score_str = f"{home_score}-{away_score}"
        
        date_str = f" on {date}" if date else ""
        return f"{team_full} {result_word} {opponent} {score_str}{date_str}."
    except Exception as e:
        print(f"  ⚠️ Sports API failed: {e}")
        return None


def _fetch_stock_real(symbol):
    """Fetch real stock price from Yahoo Finance (free, no API key)."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, params={"interval": "1d", "range": "2d"}, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose", meta.get("previousClose"))
        
        if price is not None and prev_close and prev_close > 0:
            change_pct = ((price - prev_close) / prev_close) * 100
            direction = "▲" if change_pct >= 0 else "▼"
            return f"{symbol} {direction} ${price:.2f} ({change_pct:+.1f}%)"
        elif price is not None:
            return f"{symbol} ${price:.2f}"
        return None
    except Exception as e:
        print(f"  ⚠️ Stock API failed for {symbol}: {e}")
        return None


def _fetch_history_and_news_via_gemini(api_key, api_provider="google", text_model=None):
    """Use Gemini to generate a historical fact and news headlines (general knowledge, not real-time)."""
    prompt = """You are a helpful assistant for a daily e-ink display.
Based on today's date, provide:
1. "history": An interesting, SPECIFIC historical fact for today's date. Include the year and what happened. Max 20 words.
2. "news": 2 very short, interesting, family-friendly news headlines about recent world events or discoveries.

Output strictly as a valid JSON object:
{
  "history": "On this day in 1969, Apollo 11 astronauts walked on the Moon.",
  "news": [
    "Scientists discover high water content in Mars rock samples",
    "Record-breaking coral reef recovery observed in the Great Barrier Reef"
  ]
}
Do NOT wrap in markdown code blocks. Output raw JSON only."""
    
    try:
        tm = text_model or DEFAULT_TEXT_MODEL
        if api_provider == "openrouter":
            or_model = f"google/{tm}" if not tm.startswith("google/") else tm
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": or_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                "response_format": {"type": "json_object"}
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=15,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tm}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 2048,
                    "temperature": 0.5
                },
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Parse JSON robustly
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            json_text = text[first_brace:last_brace+1]
        else:
            json_text = text
        return json.loads(json_text.strip())
    except Exception as e:
        print(f"  ⚠️ History/news Gemini call failed: {e}")
        return {}


# ─── Email Widget Helpers ───────────────────────────────────────

def _get_gmail_credentials():
    """Load stored Gmail OAuth credentials, refreshing if needed.
    Returns Credentials object or None."""
    if not GMAIL_AVAILABLE:
        return None
    if not os.path.exists(GMAIL_TOKEN_FILE):
        return None
    try:
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            with open(GMAIL_TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        if creds and creds.valid:
            return creds
    except Exception as e:
        print(f"  ⚠️ Gmail token refresh failed: {e}")
    return None


def _fetch_email_summaries(max_results=5):
    """Fetch unread email subject lines and senders from Gmail.
    Returns list of {sender, subject} dicts, or None if not configured."""
    creds = _get_gmail_credentials()
    if not creds:
        return None
    try:
        service = build_gmail_service("gmail", "v1", credentials=creds)
        results = service.users().messages().list(
            userId="me", q="is:unread category:primary",
            maxResults=max_results
        ).execute()
        messages = results.get("messages", [])
        if not messages:
            return []

        email_list = []
        for msg_ref in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From", "Subject"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = headers.get("From", "Unknown")
            # Clean sender: "John Doe <john@example.com>" -> "John Doe"
            if "<" in sender:
                sender = sender.split("<")[0].strip().strip('"')
            subject = headers.get("Subject", "(no subject)")
            email_list.append({"sender": sender, "subject": subject})
        return email_list
    except Exception as e:
        print(f"  ⚠️ Gmail fetch failed: {e}")
        return None


def _summarize_emails_via_gemini(email_list, api_key, api_provider="google", text_model=None):
    """Use Gemini to create a short, friendly email digest from subject lines."""
    if not email_list or not api_key:
        return None
    
    email_lines = "\n".join([f"- From: {e['sender']} — Subject: {e['subject']}" for e in email_list])
    prompt = f"""Summarize these {len(email_list)} unread emails into a very short digest (2-3 lines max) suitable for an e-ink display.
Be concise and friendly. Group similar items. Use emoji sparingly.
Do NOT include email addresses. Just give the key info.

Emails:
{email_lines}

Respond with ONLY the digest text, no JSON, no markdown."""

    try:
        if api_provider == "openrouter":
            tm = text_model or DEFAULT_TEXT_MODEL
            or_model = f"google/{tm}" if not tm.startswith("google/") else tm
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": or_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            tm = text_model or DEFAULT_TEXT_MODEL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tm}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 512, "temperature": 0.5},
            }
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠️ Email summarization failed: {e}")
        return None


def fetch_widget_data_via_gemini(api_key, api_provider="google", text_model=None,
                                 stocks_symbols=None, sports_team=None):
    """Fetch real widget data using dedicated APIs (sports, stocks) and Gemini (history, news)."""
    result = {}
    
    # ── Sports: Real API (TheSportsDB, free) ──
    if sports_team:
        print(f"  🏈 Fetching real sports data for: {sports_team}")
        sports_text = _fetch_sports_real(sports_team)
        if sports_text:
            result["sports"] = sports_text
            print(f"  ✅ Sports: {sports_text}")
    
    # ── Stocks: Real API (Yahoo Finance, free) ──
    if stocks_symbols:
        print(f"  📈 Fetching real stock data for: {', '.join(stocks_symbols)}")
        stocks_data = {}
        for sym in stocks_symbols:
            stock_text = _fetch_stock_real(sym)
            if stock_text:
                stocks_data[sym] = stock_text
                print(f"  ✅ Stock {sym}: {stock_text}")
            else:
                stocks_data[sym] = f"{sym} — price unavailable"
        result["stocks"] = stocks_data
    
    # ── History & News: Gemini (general knowledge) ──
    if api_key:
        print(f"  📰 Fetching history & news via Gemini...")
        gemini_data = _fetch_history_and_news_via_gemini(api_key, api_provider, text_model)
        if gemini_data.get("history"):
            result["history"] = gemini_data["history"]
            print(f"  ✅ History: {result['history']}")
        if gemini_data.get("news"):
            result["news"] = gemini_data["news"]
            print(f"  ✅ News: {len(result['news'])} headlines")
    
    # ── Fallbacks for missing data ──
    if "history" not in result:
        from datetime import datetime as dt
        day_str = dt.now().strftime("%B %d")
        result["history"] = f"On {day_str} in 1969, Apollo 11 astronauts walked on the Moon."
    if "news" not in result:
        result["news"] = [
            "Scientists make breakthrough in renewable energy storage.",
            "New marine sanctuary protects high-biodiversity coral reef."
        ]
    if "sports" not in result and sports_team:
        result["sports"] = f"{sports_team}: No recent results available."
    if "stocks" not in result and stocks_symbols:
        result["stocks"] = {sym: f"{sym} — price unavailable" for sym in stocks_symbols}
    
    return result


def fetch_email_widget_data(api_key, api_provider="google", text_model=None, max_emails=5):
    """Fetch and summarise email data for the email widget.
    Returns dict with 'email_summary' and 'email_count', or empty dict."""
    if not GMAIL_AVAILABLE:
        print("  📧 Email: dependencies not installed (pip install -r requirements-email.txt)")
        return {}
    
    email_list = _fetch_email_summaries(max_results=max_emails)
    if email_list is None:
        print("  📧 Email: not configured or not authorised")
        return {}
    
    if len(email_list) == 0:
        return {"email_summary": "📭 No unread emails", "email_count": 0}
    
    print(f"  📧 Fetched {len(email_list)} unread emails, summarising...")
    summary = _summarize_emails_via_gemini(email_list, api_key, api_provider, text_model)
    if not summary:
        # Fallback: just list senders
        senders = ", ".join([e["sender"] for e in email_list[:3]])
        summary = f"{len(email_list)} unread: {senders}"
    
    return {"email_summary": summary, "email_count": len(email_list)}


def scan_important_events_via_gemini(events_14_days, api_key, api_provider="google",
                                      characters=None, text_model=None):
    """Use the configured text model to identify important upcoming events worth
    counting down to — birthdays, trips, holidays, celebrations.

    Args:
        events_14_days: List of event dicts for the next 14 days.
        api_key: API key for Gemini.
        api_provider: 'google' or 'openrouter'.
        characters: Optional character list for name context.

    Returns:
        List of dicts: [{"event": str, "date": str, "type": str, "days_away": int}]
    """
    if not events_14_days or not api_key:
        return []

    # Build people context
    people_context = ""
    if characters:
        names = [c.get("name", "") for c in characters if c.get("name")]
        if names:
            people_context = f"\nFamily members: {', '.join(names)}.\n"

    # Build the event list
    event_lines = []
    for ev in events_14_days:
        date_str = ev.get("date", ev.get("start", ""))
        summary = ev.get("summary", "")
        days = ev.get("days_away", "?")
        event_lines.append(f"- [{date_str}] (in {days} days) {summary}")

    events_text = "\n".join(event_lines)

    prompt = f"""Analyze this list of calendar events for the next 14 days and identify any IMPORTANT or SPECIAL events that a family would want to count down to.
{people_context}
IMPORTANT event types to look for:
- 🎂 Birthdays (any family member or friend)
- ✈️ Trips, holidays, or vacations
- 🏖️ School holidays or breaks
- 🎉 Celebrations, parties, or special occasions
- 👥 Visitors or guests coming
- 🎪 Concerts, shows, or special outings

Do NOT include:
- Routine events (swimming lessons, dentist, school, work meetings)
- Regular weekly activities
- Reminders for items (library bags, homework)

Calendar events:
{events_text}

Respond with ONLY a JSON array of important events. If none found, respond with []
Format: [{{"event": "Dad's birthday", "date": "2026-07-10", "type": "birthday", "days_away": 7}}]"""

    try:
        if api_provider == "openrouter":
            tm = text_model or DEFAULT_TEXT_MODEL
            or_model = f"google/{tm}" if not tm.startswith("google/") else tm
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": or_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=20,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        else:
            tm = text_model or DEFAULT_TEXT_MODEL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tm}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.3},
            }
            resp = requests.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            result = resp.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Parse JSON — handle markdown code blocks
        import json as json_module
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"```(?:json)?\s*", "", text)
            text = text.rstrip("`").strip()

        important = json_module.loads(text)
        print(f"  📅 Found {len(important)} important events via Gemini scan")
        return important if isinstance(important, list) else []

    except Exception as e:
        print(f"  ⚠️ Important events scan failed: {e}")
        return []


# ─── Prompt Building ────────────────────────────────────────────

TROPICS_LATITUDE = 23.5


def is_tropical(latitude):
    """True if the latitude sits between the tropics of Cancer and Capricorn."""
    return latitude is not None and abs(latitude) <= TROPICS_LATITUDE


def get_season(month, latitude=None):
    """Get the season for a given month, aware of the hemisphere.

    The table is Southern Hemisphere, which was the project's original
    assumption. When a latitude is supplied and the location sits north of
    the equator the season is flipped, so an August day in Berlin reads as
    summer rather than winter.

    Locations inside the tropics have no meaningful four-season cycle, so
    they return "tropical" instead and the scene is described by climate
    rather than by season.
    """
    if is_tropical(latitude):
        return "tropical"

    seasons = {
        12: "summer", 1: "summer", 2: "summer",
        3: "autumn", 4: "autumn", 5: "autumn",
        6: "winter", 7: "winter", 8: "winter",
        9: "spring", 10: "spring", 11: "spring",
    }
    season = seasons.get(month, "spring")
    if latitude is not None and latitude >= 0:
        opposite = {"summer": "winter", "winter": "summer",
                    "autumn": "spring", "spring": "autumn"}
        season = opposite[season]
    return season


def _determine_mode_and_events(hour, today_events, timezone):
    """Determine the display mode, banner text, and events.

    The board is always about today.  Every one of today's events is shown
    all day long, including ones that have already happened — the display
    never looks ahead to tomorrow.  When there are no events at all, fall
    back to a friendly time-of-day greeting.

    Returns:
        (mode, banner_text, events) tuple
    """
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    day_name = now.strftime("%A")

    if today_events:
        return "today", f"{day_name.upper()} ADVENTURE", today_events

    # No events at all — friendly greeting
    if hour < 12:
        banner = "GOOD MORNING!"
    elif hour < 17:
        banner = "GOOD AFTERNOON!"
    else:
        banner = "GOOD EVENING!"
    return "today", banner, []


def _compute_generation_hash(mode, banner_text, events, weather_summary="", weather=None):
    """Compute a hash of the generation inputs to detect changes.

    Only regenerate when this hash differs from the last generation.
    Weather is coarsened to prevent minor fluctuations triggering regeneration —
    temperature is rounded to the nearest 5 degrees and condition is bucketed.
    """
    event_keys = []
    for ev in (events or []):
        event_keys.append(f"{ev.get('start', '')}|{ev.get('summary', '')}")
    event_keys.sort()

    # Coarsen weather so minor changes (17°C → 18°C, "clear" → "few clouds") don't trigger regen
    coarse_weather = ""
    if weather:
        temp = weather.get("temp", 0)
        rounded_temp = round(temp / 5) * 5  # Round to nearest 5°
        condition = weather.get("condition", "").lower()
        # Bucket conditions into broad categories
        if any(w in condition for w in ["rain", "drizzle", "shower"]):
            bucket = "rain"
        elif any(w in condition for w in ["storm", "thunder"]):
            bucket = "storm"
        elif any(w in condition for w in ["snow", "sleet", "ice"]):
            bucket = "snow"
        elif any(w in condition for w in ["cloud", "overcast"]):
            bucket = "cloudy"
        elif any(w in condition for w in ["fog", "mist", "haze"]):
            bucket = "fog"
        else:
            bucket = "clear"
        coarse_weather = f"{rounded_temp}|{bucket}"

    hash_input = json.dumps({
        "mode": mode,
        "banner": banner_text,
        "events": event_keys,
        "weather": coarse_weather,
    }, sort_keys=True)

    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def humanize_events_via_gemini(events, api_key, api_provider="google", characters=None,
                                text_model=None):
    """Use the configured text model to rewrite raw calendar events into friendly, human language.

    Transforms entries like "9:00am Tavi Library Bag" into
    "📚 Tavi — remember Library bag!" with emojis and warmth.

    Args:
        events: List of event dicts with 'summary', 'start', 'end_time', 'location'.
        api_key: The user's API key (Google AI Studio or OpenRouter).
        api_provider: 'google' or 'openrouter'.
        characters: Optional list of character dicts to help Gemini recognize names.

    Returns:
        List of event dicts with an added 'humanized' key containing the
        friendly version. Falls back to original summary if anything fails.
    """
    if not events or not api_key:
        return events

    # Build context about known people so Gemini can personalise
    people_context = ""
    if characters:
        names = [c.get("name", "") for c in characters if c.get("name")]
        if names:
            people_context = f"\nThe family members / people you know about: {', '.join(names)}.\n"

    # Build the list of events for Gemini to rewrite
    event_lines = []
    for i, ev in enumerate(events):
        time_str = ev.get("start", "")
        summary = ev.get("summary", "")
        location = ev.get("location", "")
        line = f"{i+1}. [{time_str}] {summary}"
        if location:
            line += f" (at {location})"
        event_lines.append(line)

    events_text = "\n".join(event_lines)

    gemini_prompt = f"""You are a friendly family assistant writing for an e-ink daily display.

Rewrite each calendar event below into a SHORT, warm, human-friendly version.
Rules:
- Keep it brief — max ~8 words per event
- Add a relevant emoji at the start of each line
- If an event is a reminder (e.g. "Library Bag", "Homework Due"), phrase it as a friendly nudge like "remember your library bag!" or "homework is due today!"
- If a person's name is in the event, address or mention them directly (e.g. "Tavi — swimming today!")
- Keep the TIME as-is but convert 24h to 12h format with am/pm
- If it's an "All day" event, don't include a time
- Don't add quotation marks around the output
- Each line should start with the number, then the rewritten text
{people_context}
Calendar events to rewrite:
{events_text}

Respond with ONLY the numbered list, one per line. Example format:
1. 📚 9am — Tavi, remember library bag!
2. 🏊 3:30pm — Tavi has swimming
3. 🎂 All day — Grandma's birthday!"""

    try:
        if api_provider == "openrouter":
            tm = text_model or DEFAULT_TEXT_MODEL
            or_model = f"google/{tm}" if not tm.startswith("google/") else tm
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": or_model,
                "messages": [{"role": "user", "content": gemini_prompt}],
                "max_tokens": 1024,
            }
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
        else:
            # Google AI Studio
            tm = text_model or DEFAULT_TEXT_MODEL
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{tm}:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": gemini_prompt}]}],
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7},
            }
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            text = result["candidates"][0]["content"]["parts"][0]["text"].strip()

        # Parse the numbered responses back into the events
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for line in lines:
            # Match lines like "1. 📚 9am — Tavi, remember library bag!"
            # or "1) 📚 9am — ..." 
            match = re.match(r"^(\d+)[.)\s]+(.+)$", line)
            if match:
                idx = int(match.group(1)) - 1
                humanized = match.group(2).strip()
                if 0 <= idx < len(events):
                    events[idx]["humanized"] = humanized

        print(f"  ✨ Humanized {sum(1 for e in events if 'humanized' in e)}/{len(events)} events via Gemini")

    except Exception as e:
        print(f"  ⚠️  Event humanization failed (falling back to raw): {e}")

    return events


def get_upcoming_countdowns(characters, today, days_ahead=14):
    """Compute upcoming countdowns for character birthdays and major holidays.

    Returns a sorted list of dicts: [{name, days_away, type}, ...]
    Only includes items within `days_ahead` days.
    """
    countdowns = []

    # ─── Character birthdays ─────────────────────────────────────
    for char in characters:
        bday_str = char.get("birthday")
        if not bday_str:
            continue
        try:
            bday = datetime.strptime(bday_str, "%Y-%m-%d").date()
            # This year's birthday
            this_year_bday = bday.replace(year=today.year)
            if this_year_bday < today:
                this_year_bday = bday.replace(year=today.year + 1)
            days = (this_year_bday - today).days
            if 0 <= days <= days_ahead:
                countdowns.append({
                    "name": f"{char.get('name', 'Someone')}'s birthday",
                    "days_away": days,
                    "type": "birthday",
                })
        except (ValueError, TypeError):
            continue

    # ─── Major holidays (fixed dates) ────────────────────────────
    holidays = [
        (12, 25, "Christmas"),
        (1, 1, "New Year's Day"),
        (2, 14, "Valentine's Day"),
        (10, 31, "Halloween"),
    ]
    for month, day, name in holidays:
        try:
            this_year = date(today.year, month, day)
            if this_year < today:
                this_year = date(today.year + 1, month, day)
            days = (this_year - today).days
            if 0 <= days <= days_ahead:
                countdowns.append({
                    "name": name,
                    "days_away": days,
                    "type": "holiday",
                })
        except ValueError:
            continue

    # ─── Easter (computed) ───────────────────────────────────────
    for yr in [today.year, today.year + 1]:
        easter = _compute_easter(yr)
        days = (easter - today).days
        if 0 <= days <= days_ahead:
            countdowns.append({
                "name": "Easter",
                "days_away": days,
                "type": "holiday",
            })
            break

    countdowns.sort(key=lambda x: x["days_away"])
    return countdowns


def _compute_easter(year):
    """Anonymous Gregorian algorithm for Easter date."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


# ─── Aesthetic Style Definitions ────────────────────────────────

AESTHETIC_STYLES = {
    "watercolor": {
        "intro": "Create a soft watercolour daily planner painted in loose, flowing washes on textured watercolour paper.",
        "scene": "The ENTIRE image is a dreamy watercolour painting.",
        "style_rules": (
            "STYLE RULES: Soft watercolour washes, wet-on-wet blending. "
            "Loose, painterly brushwork — NOT digital or perfect. Think Beatrix Potter meets travel journal.\n"
            "Use soft, blended colours: muted blues, warm yellows, gentle greens, rosy pinks on a pure WHITE background.\n"
            "The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white. "
            "Dreamy, gentle, and inviting. The text must be CLEARLY READABLE."
        ),
    },
    "pixel": {
        "intro": "Create a retro 16-bit pixel art daily planner in classic video game style with chunky pixels and a limited colour palette.",
        "scene": "The ENTIRE image is a pixel art scene, like a classic SNES or GBA game.",
        "style_rules": (
            "STYLE RULES: Crisp pixel art, visible square pixels, limited 16-bit colour palette. "
            "Think classic video game sprite art — Stardew Valley, Earthbound, or early Final Fantasy.\n"
            "Use bold, saturated colours with clear outlines. Black outlines around shapes. "
            "The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white.\n"
            "Charming, nostalgic, retro. Text should be in a pixel font style but CLEARLY READABLE."
        ),
    },
    "comic": {
        "intro": "Create a bold comic book style daily planner with thick ink lines, halftone dot shading, and dynamic composition.",
        "scene": "The ENTIRE image is a comic book panel illustration.",
        "style_rules": (
            "STYLE RULES: Bold black ink outlines, halftone dot shading, comic book colouring. "
            "Think vintage newspaper comic strips or classic Marvel/DC illustration.\n"
            "Use strong primary colours: red, blue, yellow, with black outlines and Ben-Day dot patterns. "
            "The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white.\n"
            "Dynamic, energetic, fun. Text should be in comic book lettering style but CLEARLY READABLE."
        ),
    },
    "japanese": {
        "intro": "Create a serene sumi-e (Japanese ink wash) daily planner in traditional brush painting style on rice paper.",
        "scene": "The ENTIRE image is a sumi-e brush painting with elegant minimalism.",
        "style_rules": (
            "STYLE RULES: Traditional Japanese ink wash painting (sumi-e). Flowing brush strokes, "
            "varying ink density from deep black to pale grey washes.\n"
            "Use mostly black ink on pure white paper, with occasional subtle accents of "
            "muted red (vermillion seal style) and sage green. "
            "The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white.\n"
            "Serene, contemplative, elegant. Embrace empty space (ma). "
            "Text should be in elegant brush-style but CLEARLY READABLE."
        ),
    },
}


def _get_aesthetic_style(aesthetic):
    """Return style overrides for a given aesthetic, or None if it's the default."""
    if aesthetic in AESTHETIC_STYLES:
        return AESTHETIC_STYLES[aesthetic]
    if aesthetic not in ("whimsical", "fashion", ""):
        # Custom aesthetic — generate style rules from the description
        return {
            "intro": f"Create a daily planner illustration in the following style: {aesthetic}.",
            "scene": f"The ENTIRE image is rendered in this style: {aesthetic}.",
            "style_rules": (
                f"STYLE RULES: {aesthetic}.\n"
                "The background MUST be plain white (#FFFFFF) — no cream, beige, parchment, or off-white tones.\n"
                "The text on the left must be CLEARLY READABLE — high contrast against the background."
            ),
        }
    return None


def _apply_aesthetic_to_template(template, aesthetic, style_overrides):
    """Replace the intro line and STYLE RULES section in the default template."""
    lines = template.split("\n")
    # Replace the first line (intro)
    if lines:
        lines[0] = (
            style_overrides["intro"]
            + " The output image MUST be EXACTLY 800×480 pixels — a wide landscape format "
            "(5:3 aspect ratio). The image MUST be significantly wider than it is tall."
        )
    # Replace the scene description line
    for i, line in enumerate(lines):
        if "The ENTIRE image is a single charming pen-and-ink illustration." in line:
            lines[i] = line.replace(
                "The ENTIRE image is a single charming pen-and-ink illustration.",
                style_overrides["scene"],
            )
            break
    # Replace STYLE RULES block
    for i, line in enumerate(lines):
        if line.startswith("STYLE RULES:"):
            # Find and replace until the next section or blank line
            end = i + 1
            while end < len(lines) and lines[end].strip() and not lines[end].startswith("{{"):
                end += 1
            lines[i:end] = [style_overrides["style_rules"]]
            break
    return "\n".join(lines)


def _get_grid_position_desc(col, row, cols, rows):
    # col: 1..12, row: 1..8
    h_pos = ""
    if col <= 3:
        h_pos = "on the far left side"
    elif col <= 5:
        h_pos = "on the left side"
    elif col <= 8:
        if col + cols - 1 >= 9:
            h_pos = "in the center"
        else:
            h_pos = "in the center-left area"
    elif col >= 9:
        h_pos = "on the right side"
    else:
        h_pos = "in the center area"
        
    v_pos = ""
    if row <= 2:
        v_pos = "near the top"
    elif row <= 4:
        v_pos = "in the upper-middle area"
    elif row <= 6:
        v_pos = "in the lower-middle area"
    else:
        v_pos = "near the bottom"
        
    return f"{v_pos} {h_pos} (grid cells: columns {col} to {col+cols-1}, rows {row} to {row+rows-1})"


def _get_aesthetic_info(aesthetic):
    if aesthetic == "fashion":
        return {
            "intro": "stylish fashion-illustration daily planner in high-end editorial sketch",
            "style_rules": (
                "STYLE RULES: Fashion illustration / editorial sketch style. Confident loose ink lines, watercolor washes, muted sophisticated color palette.\n"
                "Use ONLY these colors: black ink on pure white paper, plus limited accents of muted red, sage green, dusty blue, and ochre yellow. The background MUST be plain white (#FFFFFF) — no cream, beige, or off-white tones.\n"
                "Sophisticated, modern, editorial. Loose and artistic, not tight or cartoonish.\n"
                "The text must be CLEARLY READABLE — elegant but legible."
            )
        }
    elif aesthetic in AESTHETIC_STYLES:
        info = AESTHETIC_STYLES[aesthetic]
        return {
            "intro": info["intro"].replace("Create a ", "").replace("daily planner ", "").strip("."),
            "style_rules": info["style_rules"]
        }
    else:
        # Default whimsical
        return {
            "intro": "children's illustrated daily planner in pen-and-ink",
            "style_rules": (
                "STYLE RULES: Pen-and-ink illustration, clean WHITE background, hand-drawn crosshatching, charming and whimsical.\n"
                "Use ONLY these colors: black ink on pure white paper, plus limited accents of red, green, blue, and yellow. The background MUST be plain white (#FFFFFF) — no cream, beige, parchment, or off-white tones.\n"
                "Kid-friendly, warm, joyful. No scary elements.\n"
                "The text must be CLEARLY READABLE — high contrast against the background."
            )
        }


# ─── Character Casting ─────────────────────────────────────────

# Ceiling on how many characters go into one illustration. Past about five the
# model starts blending faces and losing the scene, and every character with a
# photo also costs a reference image on the generation call.
MAX_SCENE_CHARACTERS = 5


def _character_inclusion(char):
    """How a character earns a place in the scene: 'always' or 'when_mentioned'.

    Records created before casting existed have no `inclusion` field. They
    default to 'always' so upgrading never silently empties someone's scene.
    """
    value = str(char.get("inclusion") or "").strip().lower()
    if value in ("always", "when_mentioned"):
        return value
    return "always"


def _character_match_terms(char):
    """Strings that pull a character into the scene: their name plus aliases.

    Aliases are what let "Trip with mom" resolve to Sarah — a calendar rarely
    uses the name we filed the character under.
    """
    terms = []
    for term in [char.get("name")] + list(char.get("aliases") or []):
        term = str(term or "").strip()
        if term:
            terms.append(term)
    return terms


def _event_search_text(event):
    """The event text we scan for character names.

    Deliberately the RAW summary rather than the humanized rewrite — the text
    model is free to reword or drop a name, and a name lost there would
    silently drop the character from the illustration.
    """
    parts = [event.get(key) for key in ("summary", "description", "location")]
    return " ".join(str(p) for p in parts if p)


def _mentions_term(text, term):
    """Case-insensitive whole-word match, so "Al" doesn't match "Always"."""
    pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _character_key(char):
    return char.get("id") or char.get("name", "")


def resolve_scene_characters(events, characters, max_characters=MAX_SCENE_CHARACTERS):
    """Pick which characters belong in today's illustration.

    'always' characters are the board's regulars — the people it is for — and
    appear every day. Everyone else waits in the library until an event names
    them, so "Playdate with Steve" casts Steve for the day and leaves the other
    forty characters out of the prompt entirely.

    Returns (cast, reasons): the character list, and a map of character key ->
    the event summaries that pulled them in (absent for regulars). Regulars are
    never dropped to satisfy max_characters — only guest slots are capped.
    """
    regulars = []
    candidates = []
    for char in characters:
        if _character_inclusion(char) == "always":
            regulars.append(char)
        else:
            candidates.append(char)

    reasons = {}
    guests = []
    if candidates and events:
        event_texts = [(ev, _event_search_text(ev)) for ev in events]
        for char in candidates:
            terms = _character_match_terms(char)
            if not terms:
                continue
            matched = [
                ev.get("summary", "")
                for ev, text in event_texts
                if any(_mentions_term(text, term) for term in terms)
            ]
            if matched:
                guests.append(char)
                reasons[_character_key(char)] = matched

    # Regulars always make the cut; guests fill whatever room is left over.
    room = max(0, max_characters - len(regulars))
    if len(guests) > room:
        print(
            f"  🎭 {len(guests) - room} mentioned character(s) left out — "
            f"scene cap is {max_characters}"
        )
        for char in guests[room:]:
            reasons.pop(_character_key(char), None)
        guests = guests[:room]

    return regulars + guests, reasons


def _describe_character(char, index, reasons):
    """One numbered CHARACTERS line for the image prompt."""
    name = char.get("name", "Person")

    if char.get("type") == "kid":
        gender = char.get("gender", "male")
        age = char.get("age")
        if gender == "male":
            gender_word = "man" if (age and age >= 18) else "boy"
        elif gender == "female":
            gender_word = "woman" if (age and age >= 18) else "girl"
        else:
            gender_word = "person"
        age_str = f", age {age}" if age else ""
        desc = f"{index}) A {gender_word} named {name}{age_str}. {char.get('description', '')}"
    else:
        desc = f"{index}) {name}. {char.get('description', '')}"

    # Telling the model WHY a guest is here lets it stage them doing the thing
    # rather than lining everyone up facing forward.
    matched = reasons.get(_character_key(char)) or []
    if matched:
        desc = f"{desc.rstrip()} — here today for: {'; '.join(matched[:2])}"

    return desc


# ─── Default Daily Routine ──────────────────────────────────────

# Shown on days with no calendar events, alongside an inspirational note.
# Configurable per display; an empty string means "no routine, just the note".
DEFAULT_ROUTINE_WEEKDAY = "📝 Homework & practice piano"
DEFAULT_ROUTINE_WEEKEND = "🎹 Practice piano & tidy your room"

INSPIRATION_MESSAGES = [
    "Take a moment to enjoy the weather today! 🌤️",
    "A great day for something new! ✨",
    "Make someone smile today! 😊",
    "Enjoy the little things today! 💛",
    "A perfect day to explore! 🌿",
    "Be curious, be kind! 🌈",
    "Fresh air and good vibes today! 🍃",
    "Today is full of possibilities! 🚀",
]


def empty_day_items(day, routine_weekday, routine_weekend, inspiration):
    """Bullet lines for a day with no events: the routine for that kind of
    day (if set), then the inspirational note."""
    routine = routine_weekend if day.weekday() >= 5 else routine_weekday
    routine = (routine or "").strip()
    items = [f"• {routine}"] if routine else []
    items.append(f"• {inspiration}")
    return items


def build_prompt(events, characters, prompt_template, timezone=DEFAULT_TIMEZONE,
                 mode="today", banner_text=None, characters_enabled=True,
                 weather=None, birthdays=None, aesthetic="whimsical",
                 scene_description="", important_events=None,
                 location_name="", layout_placements=None, widget_configs=None,
                 widget_data=None, latitude=None,
                 routine_weekday=DEFAULT_ROUTINE_WEEKDAY,
                 routine_weekend=DEFAULT_ROUTINE_WEEKEND):
    """
    Build the image generation prompt from events + characters + weather + countdowns.
    """
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    raw_season = get_season(now.month, latitude)

    # Use Gemini-generated scene description if available, otherwise fall back
    if not scene_description:
        if weather and weather.get("temp") is not None:
            temp = weather["temp"]
            unit = weather.get("unit_symbol", "°C")
            condition = weather.get("condition", "").lower()
            scene_description = f"The setting is a {raw_season} day ({temp}{unit}, {condition})."
        else:
            scene_description = f"The setting is a {raw_season} day."

    # Build region-aware negative guidance
    region_guidance_parts = [
        "IMPORTANT REALISM RULES:",
        "- Do NOT include snow, frost, ice, or winter precipitation unless the temperature is below 2°C.",
        "- Do NOT include animals that don't exist in the local region.",
    ]
    if location_name:
        lower_loc = location_name.lower()
        if "australia" in lower_loc:
            region_guidance_parts.append(
                "- This is set in Australia. Do NOT draw badgers, foxes, deer, raccoons, "
                "squirrels, robins, or other Northern Hemisphere animals. "
                "Do NOT draw stereotypical Australian animals (kangaroos, koalas) "
                "unless specifically requested. Use native birds (magpies, lorikeets, kookaburras) "
                "sparingly and only if they fit the scene naturally. "
                "Use Australian vegetation (eucalyptus, gum trees, bottlebrush) not oak, maple, or pine."
            )
        elif any(x in lower_loc for x in ["united kingdom", "england", "scotland", "wales"]):
            region_guidance_parts.append(
                "- This is set in the UK. Use regionally appropriate flora and fauna."
            )
        else:
            region_guidance_parts.append(
                f"- This is set in {location_name}. Use flora, fauna, architecture and "
                f"clothing appropriate to that region. Do not draw wildlife or plants "
                f"that do not occur there."
            )
    if is_tropical(latitude):
        region_guidance_parts.append(
            "- This location is in the tropics, which has no seasons. Do NOT depict "
            "seasonal cues of any kind: no autumn leaves, no bare winter branches, no "
            "spring blossom, and never snow, frost or ice. Draw a warm tropical setting "
            "instead — strong sunshine, bright clear light, palms and lush green foliage, "
            "and characters in light, airy clothing."
        )
    region_guidance = "\n".join(region_guidance_parts)

    # The board is always about today
    day_name = now.strftime("%A")

    # Use provided banner_text, or fall back to default
    if not banner_text:
        banner_text = f"{day_name.upper()} ADVENTURE"

    # Build event list — prefer Gemini-humanized text when available
    event_list_items = []
    if events:
        for ev in events:
            if ev.get("humanized"):
                # Gemini already formatted this with emoji, time, and friendly text
                item = f"• {ev['humanized']}"
            else:
                # Fallback: format the raw event as before
                time_str = ev.get("start", "")
                if time_str and ":" in str(time_str):
                    try:
                        h, m = str(time_str).split(":")[:2]
                        hour = int(h)
                        ampm = "AM" if hour < 12 else "PM"
                        h12 = hour if hour <= 12 else hour - 12
                        if h12 == 0:
                            h12 = 12
                        time_str = f"{h12}:{m} {ampm}"
                    except (ValueError, IndexError):
                        pass
                item = f"• {time_str} — {ev['summary']}"
                if ev.get("location"):
                    item += f" ({ev['location']})"
            event_list_items.append(item)
        event_list_str = (
            "Each event on its own line with a bullet.\n"
            "Events to show:\n" + "\n".join(event_list_items)
        )
    else:
        # No events today — show the standing daily routine (if any) plus an
        # inspirational note. Spell out that there is no schedule, otherwise
        # the image model pads the list with invented events.
        event_list_items = empty_day_items(
            now, routine_weekday, routine_weekend,
            random.choice(INSPIRATION_MESSAGES),
        )
        line_word = "this bulleted line" if len(event_list_items) == 1 else "these bulleted lines"
        event_list_str = (
            f"There are NO calendar events today. Write {line_word}. "
            "Do NOT invent, add, or imply any other schedule items, times, "
            "or activities.\n" + "\n".join(event_list_items)
        )

    event_count = len(event_list_items)

    # ─── Countdowns (birthdays + holidays) ───────────────────────
    today = now.date()
    countdowns = get_upcoming_countdowns(characters, today)

    # Also include Google Calendar birthdays if provided
    if birthdays:
        for bday in birthdays:
            days = bday.get("days_away", 999)
            name = bday.get("name", "Someone")
            if days <= 14 and not any(c["name"].startswith(name) for c in countdowns):
                countdowns.append({
                    "name": f"{name}'s birthday",
                    "days_away": days,
                    "type": "birthday",
                })
        countdowns.sort(key=lambda x: x["days_away"])

    # Also include Gemini-identified important events (trips, celebrations, etc.)
    if important_events:
        for ie in important_events:
            event_name = ie.get("event", "")
            days = ie.get("days_away", 999)
            event_type = ie.get("type", "event")
            # Avoid duplicates
            if days <= 14 and not any(event_name.lower() in c["name"].lower() for c in countdowns):
                countdowns.append({
                    "name": event_name,
                    "days_away": days,
                    "type": event_type,
                })
        countdowns.sort(key=lambda x: x["days_away"])

    # Build countdown text for the prompt
    countdown_text = ""
    countdown_items = []
    for cd in countdowns[:3]:  # Max 3 countdowns
        # Choose emoji based on event type
        type_emoji = {
            "birthday": "🎂",
            "holiday": "🎉",
            "trip": "✈️",
            "celebration": "🎉",
            "event": "⭐",
        }.get(cd.get("type", "event"), "📅")

        if cd["days_away"] == 0:
            if cd["type"] == "birthday":
                countdown_items.append(f"🎂 It's {cd['name']} TODAY!")
            else:
                countdown_items.append(f"{type_emoji} {cd['name']} is TODAY!")
        elif cd["days_away"] == 1:
            countdown_items.append(f"⏰ {cd['name']} is TOMORROW!")
        else:
            countdown_items.append(f"{type_emoji} {cd['days_away']} days until {cd['name']}!")

    if countdown_items:
        countdown_text = (
            "BOTTOM RIGHT CORNER — COUNTDOWN:\n"
            "In the BOTTOM RIGHT corner, draw a small countdown note in hand-drawn style. "
            "It should read:\n" + "\n".join(countdown_items)
        )
    else:
        # Explicitly tell the AI NOT to draw anything in the countdown area
        countdown_text = ""

    # Also add birthday text to event list for backwards compatibility
    birthday_text = ""
    for cd in countdowns:
        if cd["type"] == "birthday" and cd["days_away"] <= 7:
            if cd["days_away"] == 0:
                birthday_text = f"🎂 It's {cd['name']} today!"
            elif cd["days_away"] == 1:
                birthday_text = f"🎂 {cd['name']} is TOMORROW!"
            else:
                birthday_text = f"🎂 {cd['days_away']} days until {cd['name']}!"
            break

    if birthday_text:
        event_list_str += f"\n\n{birthday_text}"

    # Characters — only the day's cast, not the whole library
    char_section = ""
    cast, cast_reasons = ([], {})
    if characters_enabled and characters:
        cast, cast_reasons = resolve_scene_characters(events, characters)

    if cast:
        # People first, then pets/toys/objects, matching the old prompt shape.
        people = [c for c in cast if c.get("type") == "kid"]
        extras = [c for c in cast if c.get("type") != "kid"]

        char_descs = [
            _describe_character(char, i + 1, cast_reasons)
            for i, char in enumerate(people + extras)
        ]

        all_chars = "\n".join(char_descs)

        # Add clothing guidance from weather — include actual temperature
        # so the AI doesn't draw snow gear on a mild 20°C winter day
        clothing_note = ""
        if weather and weather.get("clothing_hint"):
            temp = weather.get("temp")
            unit = weather.get("unit_symbol", "°C")
            condition = weather.get("condition", "")
            clothing_note = (
                f"\nIMPORTANT: Today's forecast high is {temp}{unit} ({condition.lower()}). "
                f"The characters should be dressed appropriately for {temp}{unit} "
                f"{raw_season} weather — wearing {weather['clothing_hint']}. "
                f"Do NOT draw snow, ice, or heavy frost unless the temperature is below 2°C."
            )

        char_area = "on the right side"
        if layout_placements:
            left_occupied = False
            center_occupied = False
            right_occupied = False
            for widget_key, p in layout_placements.items():
                col = p.get("col", 1)
                cols = p.get("cols", 2)
                end_col = col + cols - 1
                if col <= 4:
                    left_occupied = True
                if (col <= 8 and end_col >= 5) or (col <= 5 and end_col >= 8):
                    center_occupied = True
                if end_col >= 9:
                    right_occupied = True
            
            if not right_occupied:
                char_area = "on the right side"
            elif not left_occupied:
                char_area = "on the left side"
            elif not center_occupied:
                char_area = "in the center area"
            else:
                # Count cell coverages to find least occupied
                left_cells = 0
                center_cells = 0
                right_cells = 0
                for widget_key, p in layout_placements.items():
                    col = p.get("col", 1)
                    cols = p.get("cols", 2)
                    rows = p.get("rows", 2)
                    cells = cols * rows
                    end_col = col + cols - 1
                    if col <= 4:
                        left_cells += cells
                    if (col <= 8 and end_col >= 5):
                        center_cells += cells
                    if end_col >= 9:
                        right_cells += cells
                min_cells = min(left_cells, center_cells, right_cells)
                if min_cells == right_cells:
                    char_area = "on the right side"
                elif min_cells == left_cells:
                    char_area = "on the left side"
                else:
                    char_area = "in the center area"

        char_section = (
            f"\n\nCHARACTERS (in the scene {char_area}): "
            f"Show these characters in the scene. Incorporate the day's activities "
            f"into the illustration when relevant and appropriate. "
            f"Draw ONLY the characters listed below — do not add any other people."
            f"{clothing_note}"
            f"\nCHARACTERS:\n{all_chars}"
        )

    # Weather section
    weather_section = ""
    if weather:
        temp = weather.get("temp")
        unit = weather.get("unit_symbol", "°C")
        condition = weather.get("condition", "")
        emoji = weather.get("emoji", "")

        weather_badge = f"{emoji} {temp}{unit} {condition}"

        weather_section = (
            f"BOTTOM LEFT CORNER — WEATHER:\n"
            f"In the BOTTOM LEFT corner of the image, draw a small weather badge or "
            f"banner in a clear, readable hand-drawn style. It should read: "
            f"'{weather_badge}'. Make it small but legible — like a little weather "
            f"stamp on the illustration."
        )

    # ─── Dynamic text layout based on event count ─────────────────
    # Adapt the left-side text area so a few events don't leave a huge
    # blank column. With many events the text panel is wider; with few
    # it's compact and vertically centred so the illustration fills more
    # of the canvas.
    if event_count <= 2:
        text_layout = (
            "LEFT SIDE — COMPACT TEXT OVERLAY (roughly 25% width, vertically centred):\n"
            "Because there are only a few items, keep the text block SHORT and vertically centred "
            "on the left side. The text area should be a compact, tidy cluster — NOT a tall column "
            "stretching the full height. Let the illustration fill most of the canvas. "
            "The text sits in the FOREGROUND — the scene continues behind and around it."
        )
        right_width = "roughly 75% width"
    elif event_count <= 5:
        text_layout = (
            "LEFT SIDE — TEXT OVERLAY (roughly 35% width, vertically centred):\n"
            "Place the schedule list on the left portion, vertically centred. The text block "
            "should be compact — only as tall as needed for the items. Don't stretch it to "
            "fill the full height. The text sits in the FOREGROUND on top of the illustration, "
            "but the scene continues behind and around it."
        )
        right_width = "roughly 65% width"
    else:
        text_layout = (
            "LEFT SIDE (roughly 40% width) — TEXT OVERLAY:\n"
            "Overlaid on top of the left portion of the scene, write a clear readable "
            "handwritten-style list of the day's schedule. The text sits in the FOREGROUND "
            "on top of the illustration, but the scene continues behind and around it — you "
            "might see trees, sky, a wall, or background details peeking around the edges. "
            "Keep the area behind the text relatively uncluttered so it stays legible."
        )
        right_width = "roughly 60% width"

    # Build final prompt — choose template based on aesthetic
    if prompt_template and prompt_template.strip():
        template = prompt_template
    elif aesthetic == "fashion":
        template = FASHION_PROMPT_TEMPLATE
    else:
        template = DEFAULT_PROMPT_TEMPLATE

    # Override style rules based on aesthetic (unless user has a custom prompt template)
    if not (prompt_template and prompt_template.strip()):
        style_overrides = _get_aesthetic_style(aesthetic)
        if style_overrides:
            template = _apply_aesthetic_to_template(template, aesthetic, style_overrides)

    # ─── Dynamic Layout Prompt Construction ────────────────────────
    if layout_placements and not (prompt_template and prompt_template.strip()):
        aes_info = _get_aesthetic_info(aesthetic)
        
        layout_desc_parts = [
            "LAYOUT & SPATIAL STRUCTURE (Grid coordinates: 12 columns × 8 rows):\n"
            f"The image is a single cohesive illustration. {scene_description} It must fill the entire 800×480 screen. "
            "It must seamlessly integrate the following textual widgets directly into the illustration at their specific grid positions, "
            "drawing them inside charming hand-drawn panels, signs, speech bubbles, or clean background spaces. "
            "Ensure the background behind all text elements is plain white (#FFFFFF) for absolute legibility."
        ]
        
        # Header banner at top
        layout_desc_parts.append(
            f"- HEADER BANNER: Near the very top, draw a neat handwritten banner reading: '{banner_text}'."
        )

        for widget_key, p in layout_placements.items():
            col, row = p.get("col", 1), p.get("row", 1)
            cols, rows = p.get("cols", 2), p.get("rows", 2)
            pos_desc = _get_grid_position_desc(col, row, cols, rows)
            
            if widget_key == "calendar":
                if event_list_str.strip():
                    layout_desc_parts.append(
                        f"- CALENDAR / EVENTS (placed {pos_desc}): Draw a neat, clean handwritten list "
                        f"of today's events:\n{event_list_str}"
                    )
            elif widget_key == "weather":
                if weather_section:
                    layout_desc_parts.append(
                        f"- WEATHER INFO (placed {pos_desc}): Draw a small weather banner or stamp reading: "
                        f"'{weather_badge}'"
                    )
            elif widget_key == "quote":
                quote_text = random.choice(BACKUP_QUOTES)
                layout_desc_parts.append(
                    f"- DAILY QUOTE (placed {pos_desc}): Draw this quote in a charming speech bubble or quote card: "
                    f"\"{quote_text}\""
                )
            elif widget_key == "stocks":
                stocks_info = widget_data.get("stocks", {}) if widget_data else {}
                stocks_lines = []
                symbols = widget_configs.get("stocks", {}).get("symbols", ["GOOG"]) if widget_configs else ["GOOG"]
                for sym in symbols:
                    val = stocks_info.get(sym) or f"{sym} ▲ $182.45 (+1.2%)"
                    stocks_lines.append(f"• {val}")
                stocks_text = "\n".join(stocks_lines)
                layout_desc_parts.append(
                    f"- STOCK TICKERS (placed {pos_desc}): Draw these stock prices neatly in a small financial widget card:\n{stocks_text}"
                )
            elif widget_key == "sports":
                sports_text = widget_data.get("sports") if widget_data else None
                if not sports_text:
                    team = widget_configs.get("sports", {}).get("team", "Sydney Swans") if widget_configs else "Sydney Swans"
                    sports_text = f"{team} won recent match! 🏆"
                layout_desc_parts.append(
                    f"- SPORTS SCORE (placed {pos_desc}): Draw this sports score/status in a sporty badge or banner:\n• {sports_text}"
                )
            elif widget_key == "news":
                news_list = widget_data.get("news") if widget_data else None
                if not news_list:
                    news_list = [
                        "Local park opens new community garden",
                        "New solar power records set today"
                    ]
                news_lines = "\n".join([f"• {n}" for n in news_list])
                layout_desc_parts.append(
                    f"- NEWS HEADLINES (placed {pos_desc}): Draw a mini-newspaper snippet with these headlines:\n{news_lines}"
                )
            elif widget_key == "history":
                history_text = widget_data.get("history") if widget_data else None
                if not history_text:
                    history_text = "On this day, an extraordinary event happened!"
                layout_desc_parts.append(
                    f"- THIS DAY IN HISTORY (placed {pos_desc}): Draw this historical fact in a small scroll or vintage stamp:\n• {history_text}"
                )
            elif widget_key == "email":
                email_summary = widget_data.get("email_summary") if widget_data else None
                if email_summary:
                    layout_desc_parts.append(
                        f"- EMAIL DIGEST (placed {pos_desc}): Draw a small envelope/mail icon with this email summary in a compact card:\n• {email_summary}"
                    )
            elif widget_key == "countdown":
                if countdown_text:
                    layout_desc_parts.append(
                        f"- COUNTDOWN NOTE (placed {pos_desc}): Draw a small reminder tab reading:\n{countdown_text}"
                    )
        
        # Characters placement
        if char_section:
            layout_desc_parts.append(
                f"- CHARACTERS (placed in open areas): Draw the characters in the remaining free areas of the illustration. "
                "They should not overlap or block any of the textual widgets described above. "
                f"{char_section}"
            )
            
        dynamic_layout_desc = "\n\n".join(layout_desc_parts)
        
        prompt = f"""Create a daily planner in {aes_info['intro']} style on a clean WHITE background. The output image MUST be EXACTLY 800×480 pixels — a wide landscape format (5:3 aspect ratio). The image MUST be significantly wider than it is tall.

CRITICAL FRAMING: Leave generous margins — at least 40 pixels of padding on ALL sides (top, bottom, left, right). Do NOT place any text, characters, or important elements near the edges. Everything must be well within the safe zone to avoid clipping on the e-ink display.

{dynamic_layout_desc}

{aes_info['style_rules']}

{region_guidance}

Remember: 800×480 pixels, wide landscape, generous margins on all sides, white background.
"""
    else:
        # Standard replacement
        prompt = template
        prompt = prompt.replace("{{DAY_NAME}}", day_name)
        prompt = prompt.replace("{{BANNER_TEXT}}", banner_text)
        prompt = prompt.replace("{{SCENE_DESCRIPTION}}", scene_description)
        prompt = prompt.replace("{{SEASON}}", raw_season)
        prompt = prompt.replace("{{TEXT_LAYOUT}}", text_layout)
        prompt = prompt.replace("{{RIGHT_WIDTH}}", right_width)
        prompt = prompt.replace("{{EVENT_LIST}}", event_list_str)
        prompt = prompt.replace("{{CHARACTERS}}", char_section)
        prompt = prompt.replace("{{BIRTHDAY}}", birthday_text)
        prompt = prompt.replace("{{MODE}}", mode)
        prompt = prompt.replace("{{WEATHER}}", weather_section)
        prompt = prompt.replace("{{COUNTDOWN}}", countdown_text)
        prompt = prompt.replace("{{REGION_GUIDANCE}}", region_guidance)

    return prompt


# ─── Image Generation ───────────────────────────────────────────

def _load_reference_image(img_url):
    """Return (bytes, mime_type) for one character reference image, or None.

    /api/upload stores character photos on disk and returns a site-relative
    path such as "/uploads/abc.jpg". requests cannot fetch that ("No scheme
    supplied"), so read those straight off disk. Anything carrying a scheme is
    still fetched over HTTP.
    """
    if img_url.startswith(("http://", "https://")):
        resp = requests.get(img_url, timeout=15)
        if resp.status_code != 200:
            print(f"  Reference image {img_url} returned HTTP {resp.status_code}")
            return None
        return resp.content, resp.headers.get("Content-Type", "image/png")

    local_path = os.path.join("data", img_url.lstrip("/"))
    if not os.path.exists(local_path):
        print(f"  Reference image not found on disk: {local_path}")
        return None
    mime = mimetypes.guess_type(local_path)[0] or "image/png"
    with open(local_path, "rb") as fh:
        return fh.read(), mime


def generate_image_via_openrouter(prompt, api_key, model, reference_image_urls=None):
    """Call OpenRouter's Image API to generate an image."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": "3:2",
    }

    if reference_image_urls:
        payload["input_references"] = [
            {"type": "image_url", "image_url": {"url": url}}
            for url in reference_image_urls
        ]
        print(f"Passing {len(reference_image_urls)} reference images to the model")

    for attempt in range(3):
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/images",
                headers=headers,
                json=payload,
                timeout=240,
            )
            if response.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if response.status_code != 200:
                print(f"Image API error {response.status_code}: {response.text[:300]}")
            response.raise_for_status()
            result = response.json()
            images = result.get("data", [])
            if images and images[0].get("b64_json"):
                return base64.b64decode(images[0]["b64_json"])
        except Exception as e:
            print(f"Image generation error (attempt {attempt+1}): {e}")

    return None


def generate_image_via_google_ai(prompt, api_key, model="gemini-3-pro-image", reference_image_urls=None,
                                 image_size=None, aspect_ratio=None):
    """Call Google AI Studio (Gemini API) to generate an image.

    Uses the generateContent endpoint with responseModalities=["IMAGE", "TEXT"].
    """
    if not model:
        # An empty image_model in config.json builds ".../models/:generateContent",
        # which Google answers with a bare 404 and no message.
        print("  \u274c No image model configured — set 'image_model' in data/config.json")
        return None

    if "image" not in model and "nano-banana" not in model:
        # Only image models (*-image, gemini-nano-banana-*) can return an IMAGE
        # part. A text model answers 200 with a text-only candidate, which used
        # to surface as three rounds of "No image in Gemini response" and a
        # bare 500.
        print(f"  \u274c '{model}' is a text model and cannot generate images — "
              f"set 'image_model' to an image model (e.g. gemini-3-pro-image). "
              f"Text models belong in 'text_model'.")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    # Build content parts
    parts = []

    # Add reference images if provided
    if reference_image_urls:
        for img_url in reference_image_urls:
            try:
                loaded = _load_reference_image(img_url)
                if not loaded:
                    continue
                content, content_type = loaded
                parts.append({
                    "inline_data": {
                        "mime_type": content_type,
                        "data": base64.b64encode(content).decode("utf-8"),
                    }
                })
            except Exception as e:
                print(f"Failed to load reference image {img_url}: {e}")
        if parts:
            print(f"Passing {len(parts)} reference images to Gemini")

    # Add the text prompt
    parts.append({"text": prompt})

    generation_config = {
        "responseModalities": ["IMAGE", "TEXT"],
    }

    # Optional output sizing. Gemini 3 image models default to 1K; an 800x480
    # e-ink panel is only 0.38 MP, so requesting a smaller size costs less and
    # loses nothing once resize_and_dither has run. Values are "512", "1K",
    # "2K", "4K" (uppercase K is required). Only sent when configured, so the
    # default request shape is unchanged.
    image_config = {}
    if image_size:
        image_config["imageSize"] = image_size
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if image_config:
        generation_config["imageConfig"] = image_config
        print(f"  Requesting imageConfig: {image_config}")

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": generation_config,
    }

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=240)
            if response.status_code == 429:
                # Without this the whole function can return None having printed
                # nothing at all, surfacing as a bare 500 with no explanation.
                print(f"  Gemini 429 for {model} (attempt {attempt + 1}/3): "
                      f"{response.text[:300]}")
                time.sleep(5 * (attempt + 1))
                continue
            if response.status_code != 200:
                print(f"Gemini API error {response.status_code}: {response.text[:300]}")
            response.raise_for_status()

            result = response.json()
            # Extract image from response
            candidates = result.get("candidates", [])
            for candidate in candidates:
                content = candidate.get("content", {})
                for part in content.get("parts", []):
                    blob = part.get("inlineData") or part.get("inline_data")
                    if blob:
                        data = base64.b64decode(blob["data"])
                        try:
                            print(f"  Gemini returned {Image.open(io.BytesIO(data)).size} "
                                  f"({len(data) // 1024} KB)")
                        except Exception:
                            pass
                        return data

            print(f"No image in Gemini response (attempt {attempt+1}): "
                  f"{_describe_imageless_response(result)}")
        except Exception as e:
            print(f"Gemini image generation error (attempt {attempt+1}): {e}")

    return None


def _describe_imageless_response(result):
    """Summarise why a 200 response carried no image, for the log line."""
    bits = []
    feedback = result.get("promptFeedback", {})
    if feedback.get("blockReason"):
        bits.append(f"blockReason={feedback['blockReason']}")
    for candidate in result.get("candidates", []):
        if candidate.get("finishReason"):
            bits.append(f"finishReason={candidate['finishReason']}")
        text = " ".join(
            part["text"] for part in candidate.get("content", {}).get("parts", [])
            if part.get("text")
        ).strip()
        if text:
            bits.append(f"text={text[:200]!r}")
    if not result.get("candidates"):
        bits.append("no candidates")
    return ", ".join(bits) or "empty response"


# ─── Image Processing ───────────────────────────────────────────

def resize_and_dither(img_bytes):
    """Resize and apply Floyd-Steinberg dithering for the 6-color e-ink palette."""
    img = Image.open(io.BytesIO(img_bytes))

    # Center crop to display aspect ratio
    target_ratio = DISPLAY_WIDTH / DISPLAY_HEIGHT
    img_ratio = img.width / img.height

    if img_ratio > target_ratio:
        new_width = int(img.height * target_ratio)
        left = (img.width - new_width) // 2
        img = img.crop((left, 0, left + new_width, img.height))
    elif img_ratio < target_ratio:
        new_height = int(img.width / target_ratio)
        top = (img.height - new_height) // 2
        img = img.crop((0, top, img.width, top + new_height))

    img = img.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS)

    # Save full-color version
    full_color_buf = io.BytesIO()
    img.save(full_color_buf, format="PNG")
    full_color_bytes = full_color_buf.getvalue()

    # Apply Floyd-Steinberg dithering
    pixels = np.array(img.convert("RGB"), dtype=np.float64)
    h, w, _ = pixels.shape

    for y in range(h):
        for x in range(w):
            old_pixel = pixels[y, x].copy()
            distances = np.sqrt(np.sum((EINK_PALETTE - old_pixel) ** 2, axis=1))
            new_pixel = EINK_PALETTE[np.argmin(distances)]
            pixels[y, x] = new_pixel
            error = old_pixel - new_pixel

            if x + 1 < w:
                pixels[y, x + 1] += error * 7 / 16
            if y + 1 < h:
                if x - 1 >= 0:
                    pixels[y + 1, x - 1] += error * 3 / 16
                pixels[y + 1, x] += error * 5 / 16
                if x + 1 < w:
                    pixels[y + 1, x + 1] += error * 1 / 16

    pixels = np.clip(pixels, 0, 255).astype(np.uint8)
    dithered = Image.fromarray(pixels)

    dithered_buf = io.BytesIO()
    dithered.save(dithered_buf, format="PNG")
    dithered_bytes = dithered_buf.getvalue()

    return full_color_bytes, dithered_bytes


# ─── Battery Indicator ──────────────────────────────────────────

BATTERY_LINE_WIDTH = 1
BATTERY_LINE_COLOR = (0, 0, 0)  # Black, an exact palette entry so it survives raw packing


def parse_battery_percentage(value):
    """Parse the firmware's X-Battery-Percentage header; None if absent or bogus."""
    try:
        pct = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return pct if 0 <= pct <= 100 else None


def draw_battery_line(img, pct):
    """Burn a battery gauge into the right edge of img, in place.

    A BATTERY_LINE_WIDTH-wide line rises from the bottom edge: full height at
    100%, nothing at 0%.
    """
    from PIL import ImageDraw

    height = round(img.height * pct / 100)
    if height <= 0:
        return img
    ImageDraw.Draw(img).rectangle(
        [img.width - BATTERY_LINE_WIDTH, img.height - height, img.width - 1, img.height - 1],
        fill=BATTERY_LINE_COLOR,
    )
    return img


# ─── Helper: Run pipeline for a single user ─────────────────────

def _generate_for_device(config: dict, force: bool = False):
    """Run the full image generation pipeline using a local config dictionary.

    Args:
        config: Dictionary containing user settings (api_key, ical_url, etc.)
        force: If True, skip the hash check and always regenerate.
    """
    
    api_key = config.get("openrouter_api_key", "") or config.get("api_key", "")
    api_provider = config.get("api_provider", "google")
    ical_url = config.get("ical_url", "")
    timezone = config.get("timezone", DEFAULT_TIMEZONE)
    latitude = config.get("latitude")
    longitude = config.get("longitude")
    temp_unit = config.get("temp_unit", "celsius")

    model = config.get("image_model", "google/gemini-3-pro-image")
    text_model = config.get("text_model", DEFAULT_TEXT_MODEL)
    characters_enabled = config.get("characters_enabled", True)
    calendar_id = config.get("calendar_id", "primary")
    aesthetic = config.get("aesthetic", "whimsical")

    # ─── API Key Verification ───────────────────────────────────
    if not api_key:
        print(f"  ❌ Missing API key in config")
        return None

    print(f"  🔑 Using API provider: {api_provider}")

    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    hour = now.hour
    today = now.date()

    # ─── Fetch today's events (iCal) ────────────────────────────
    today_events = []
    birthdays = []
    
    if ical_url:
        today_events = fetch_events_ical(ical_url, timezone=timezone, target_date=today)
        print(f"  📅 iCal: {len(today_events)} today")
    else:
        print("  ⚠️ No iCal URL provided")

    # ─── Smart mode determination ───────────────────────────────
    mode, banner_text, events = _determine_mode_and_events(
        hour, today_events, timezone
    )
    print(f"  Mode: {mode}, Banner: '{banner_text}', Events: {len(events)}")

    # ─── Fetch weather ──────────────────────────────────────────
    weather = None
    weather_summary = ""
    if latitude and longitude:
        weather = fetch_weather(latitude, longitude, temp_unit=temp_unit)
        if weather:
            weather_summary = f"{weather['emoji']} {weather['condition']}"
            print(f"  Weather: {weather['temp']}{weather['unit_symbol']} {weather['condition']}")

    # ─── Change detection ───────────────────────────────────────
    generation_hash = _compute_generation_hash(mode, banner_text, events, weather_summary, weather=weather)

    if not force:
        status_dict = config.get("status", {})
        last_hash = status_dict.get("last_generation_hash", "")
        if last_hash == generation_hash:
            return {"skipped": True, "hash": generation_hash}

    print(f"  🎨 Changes detected (hash={generation_hash}), generating new image...")

    # ─── Load characters ────────────────────────────────────────
    characters = config.get("characters", [])
    
    # Filter characters by device selection
    selected_chars = config.get("selected_characters", [])
    if selected_chars and len(selected_chars) > 0:
        characters = [c for c in characters if c.get("id") in selected_chars]

    prompt_template = config.get("prompt_template", "")

    # ─── Humanize events via Gemini ────────────────────────────────
    # Use a lightweight Gemini text call to rewrite raw calendar entries
    # (e.g. "9:00am Tavi Library Bag") into friendly human language
    # (e.g. "📚 9am — Tavi, remember library bag!") before the image prompt.
    events = humanize_events_via_gemini(
        events, api_key, api_provider=api_provider, characters=characters,
        text_model=text_model,
    )

    # ─── Location lookup ─────────────────────────────────────────
    location_name = config.get("location_name", "")
    if not location_name and latitude and longitude:
        location_name = _reverse_geocode_location(latitude, longitude)
        if location_name:
            config["location_name"] = location_name
            print(f"  📍 Location: {location_name} (cached)")
    elif location_name:
        print(f"  📍 Location: {location_name} (cached)")

    # ─── Scene description via text model ───────────────────────
    # Instead of just "winter scene" (which draws snow in Sydney),
    # use the text model to generate a realistic, location-aware description.
    scene_description = ""
    if weather:
        raw_season = get_season(now.month, latitude)
        scene_description = describe_scene_weather_via_gemini(
            weather, raw_season, timezone, api_key,
            api_provider=api_provider,
            location_name=location_name,
            events=events,
            text_model=text_model,
        )

    important_events = []

    # ─── Fetch widget data via Gemini text model ─────────────────
    layout_placements = config.get("layout_placements", {})
    widget_configs = config.get("widget_configs", {})
    widget_data = {}
    needs_widget_data = any(w in layout_placements for w in ["stocks", "sports", "news", "history"])
    if needs_widget_data:
        stocks_symbols = widget_configs.get("stocks", {}).get("symbols") or (
            [widget_configs.get("stocks", {}).get("symbol")] if widget_configs.get("stocks", {}).get("symbol") else []
        )
        sports_team = widget_configs.get("sports", {}).get("team")
        print("  📊 Fetching/generating widget data via Gemini...")
        widget_data = fetch_widget_data_via_gemini(
            api_key, api_provider=api_provider, text_model=text_model,
            stocks_symbols=stocks_symbols, sports_team=sports_team
        )

    # ─── Fetch email widget data (optional) ──────────────────────
    if "email" in layout_placements:
        max_emails = widget_configs.get("email", {}).get("max_emails", 5)
        email_data = fetch_email_widget_data(
            api_key, api_provider=api_provider, text_model=text_model,
            max_emails=max_emails
        )
        widget_data.update(email_data)

    # ─── Build prompt ───────────────────────────────────────────
    prompt = build_prompt(
        events, characters, prompt_template,
        timezone=timezone, mode=mode,
        banner_text=banner_text,
        characters_enabled=characters_enabled,
        routine_weekday=config.get("default_routine_weekday", DEFAULT_ROUTINE_WEEKDAY),
        routine_weekend=config.get("default_routine_weekend", DEFAULT_ROUTINE_WEEKEND),
        weather=weather,
        birthdays=birthdays,
        aesthetic=aesthetic,
        scene_description=scene_description,
        important_events=important_events,
        location_name=location_name,
        layout_placements=layout_placements,
        widget_configs=widget_configs,
        widget_data=widget_data,
        latitude=latitude,
    )

    # Collect reference images — only for the characters actually in the scene,
    # so a large library doesn't attach dozens of photos to every generation.
    reference_urls = []
    if characters_enabled:
        cast, _ = resolve_scene_characters(events, characters)
        cast_names = ", ".join(c.get("name", "?") for c in cast) or "(nobody)"
        print(f"  🎭 Today's cast: {cast_names}")
        for char in cast:
            if char.get("imageUrl"):
                reference_urls.append(char["imageUrl"])

    # ─── Generate image (route to correct provider) ──────────────
    refs = reference_urls if reference_urls else None

    if api_provider == "openrouter":
        # OpenRouter models need the 'google/' prefix
        or_model = model if "/" in model else f"google/{model}"
        print(f"  Using OpenRouter: {or_model}")
        img_bytes = generate_image_via_openrouter(prompt, api_key, or_model, reference_image_urls=refs)
    else:
        # Google AI Studio (default)
        # Strip 'google/' prefix if present
        gemini_model = model.replace("google/", "") if model.startswith("google/") else model
        print(f"  Using Google AI Studio: {gemini_model}")
        img_bytes = generate_image_via_google_ai(
            prompt, api_key, gemini_model, reference_image_urls=refs,
            image_size=config.get("image_size") or None,
            aspect_ratio=config.get("image_aspect_ratio") or None,
        )

    if not img_bytes:
        return {"success": False, "error": "Image generation failed"}

    # Resize & dither
    full_color_bytes, dithered_bytes = resize_and_dither(img_bytes)

    # ─── Save locally ───────────────────────────────────────────
    os.makedirs("data/images", exist_ok=True)
    
    latest_path = "data/images/latest_display.png"
    dithered_path = "data/images/latest_display_dithered.png"
    
    with open(latest_path, "wb") as f:
        f.write(full_color_bytes)
        
    with open(dithered_path, "wb") as f:
        f.write(dithered_bytes)

    # ─── Update device status (with hash for next comparison) ───
    status_data = {
        "last_generated": now.isoformat(),
        "last_prompt": prompt,
        "last_mode": mode,
        "last_banner": banner_text,
        "events_count": len(events),
        "image_url": f"/images/latest_display.png",
        "dithered_url": f"/images/latest_display_dithered.png",
        "last_generation_hash": generation_hash,
    }
    if weather:
        status_data["last_weather"] = f"{weather['emoji']} {weather['temp']}{weather['unit_symbol']} {weather['condition']}"

    config["status"] = status_data

    return {
        "success": True,
        "image_url": status_data["image_url"],
        "dithered_url": status_data["dithered_url"],
        "events_count": len(events),
        "mode": mode,
        "banner": banner_text,
        "hash": generation_hash,
        "prompt_preview": prompt[:500],
    }




import json

CONFIG_FILE = "data/config.json"

import threading
config_lock = threading.Lock()

def load_config():
    with config_lock:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    content = f.read().strip()
                    if not content:
                        return {}
                    return json.loads(content)
            except Exception as e:
                print(f"⚠️ Error loading config: {e}")
                return {}
        return {}

def save_config(config_data):
    with config_lock:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        temp_file = CONFIG_FILE + ".tmp"
        try:
            with open(temp_file, "w") as f:
                json.dump(config_data, f, indent=4)
            os.replace(temp_file, CONFIG_FILE)
        except Exception as e:
            print(f"⚠️ Error saving config: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass


def _persist_generation_state(config):
    """Write back only the keys a generation run mutates.

    _generate_for_device works on a snapshot taken before a run that can last
    several minutes. Saving that whole snapshot back would revert any setting
    changed in the dashboard meanwhile, and silently drop keys added during the
    run — which is how image_model can vanish from config.json entirely.
    """
    latest = load_config() or config
    for key in ("status", "location_name"):
        if key in config:
            latest[key] = config[key]
    save_config(latest)


@app.get("/api/server-info")
def get_server_info(request: Request):
    """Return the server's local network IP for device configuration."""
    import socket
    try:
        # Connect to an external address to find the local network IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"
    
    port = request.url.port or 8000
    return {
        "local_ip": local_ip,
        "port": port,
        "display_url": f"http://{local_ip}:{port}/api/display",
    }

@app.post("/api/upload")
async def upload_file(request: Request):
    """Upload a file (character image) and return its local URL."""
    from fastapi.responses import JSONResponse
    import uuid
    
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")
    
    upload_dir = "data/uploads"
    os.makedirs(upload_dir, exist_ok=True)
    
    ext = os.path.splitext(file.filename)[1] or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(upload_dir, filename)
    
    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)
    
    return {"url": f"/uploads/{filename}"}

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config")
def update_config(config: dict):
    # merge with existing
    existing = load_config()
    # A model <select> holding a value that matches none of its options renders
    # blank, and the dashboard's auto-save then posts "" over a working model.
    # An empty model name is never meaningful, so never let one clobber a good.
    for key in ("image_model", "text_model"):
        if key in config and not str(config[key] or "").strip() and existing.get(key):
            print(f"⚠️ Ignoring empty {key} in config update "
                  f"(keeping {existing[key]!r})")
            config.pop(key)
    existing.update(config)
    save_config(existing)
    return {"status": "success"}

# ─── Email OAuth Endpoints ──────────────────────────────────────

@app.get("/api/email/status")
def email_status():
    """Check if Gmail email integration is available and authorised."""
    if not GMAIL_AVAILABLE:
        return {"available": False, "reason": "dependencies_not_installed"}
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        return {"available": True, "configured": False, "reason": "credentials_file_missing"}
    creds = _get_gmail_credentials()
    if creds:
        return {"available": True, "configured": True, "authorised": True}
    else:
        return {"available": True, "configured": True, "authorised": False}


@app.get("/api/email/auth-url")
def email_auth_url(request: Request):
    """Generate a Google OAuth URL for the user to authorise Gmail access."""
    if not GMAIL_AVAILABLE:
        raise HTTPException(status_code=400, detail="Email dependencies not installed. Run: pip install -r requirements-email.txt")
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        raise HTTPException(status_code=400, detail="Gmail credentials file not found. See EMAIL_SETUP.md for instructions.")

    try:
        # Determine redirect URI from the request
        host = request.headers.get("host", "localhost:8000")
        scheme = request.headers.get("x-forwarded-proto", "http")
        redirect_uri = f"{scheme}://{host}/api/email/callback"

        flow = Flow.from_client_secrets_file(
            GMAIL_CREDENTIALS_FILE,
            scopes=GMAIL_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return {"auth_url": auth_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create auth URL: {e}")


@app.get("/api/email/callback")
def email_callback(request: Request, code: str = None, error: str = None):
    """OAuth callback that exchanges the authorization code for tokens."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(f"""
            <html><body style="font-family:system-ui;text-align:center;padding:60px;">
            <h2>❌ Gmail Authorization Failed</h2>
            <p>{error}</p>
            <p><a href="/">Return to Glanceboard</a></p>
            </body></html>
        """)
    if not code:
        return HTMLResponse("""
            <html><body style="font-family:system-ui;text-align:center;padding:60px;">
            <h2>❌ No authorization code received</h2>
            <p><a href="/">Return to Glanceboard</a></p>
            </body></html>
        """)

    try:
        host = request.headers.get("host", "localhost:8000")
        scheme = request.headers.get("x-forwarded-proto", "http")
        redirect_uri = f"{scheme}://{host}/api/email/callback"

        flow = Flow.from_client_secrets_file(
            GMAIL_CREDENTIALS_FILE,
            scopes=GMAIL_SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        os.makedirs(os.path.dirname(GMAIL_TOKEN_FILE), exist_ok=True)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

        print("  ✅ Gmail OAuth completed successfully")
        return HTMLResponse("""
            <html><body style="font-family:system-ui;text-align:center;padding:60px;">
            <h2>✅ Gmail Connected!</h2>
            <p>You can close this tab and return to Glanceboard.</p>
            <script>setTimeout(() => window.close(), 2000);</script>
            </body></html>
        """)
    except Exception as e:
        print(f"  ❌ Gmail OAuth failed: {e}")
        return HTMLResponse(f"""
            <html><body style="font-family:system-ui;text-align:center;padding:60px;">
            <h2>❌ Gmail Authorization Failed</h2>
            <p>{e}</p>
            <p><a href="/">Return to Glanceboard</a></p>
            </body></html>
        """)


@app.post("/api/email/disconnect")
def email_disconnect():
    """Remove stored Gmail tokens to disconnect email integration."""
    try:
        if os.path.exists(GMAIL_TOKEN_FILE):
            os.remove(GMAIL_TOKEN_FILE)
            print("  📧 Gmail token removed")
        return {"status": "disconnected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disconnect: {e}")

@app.post("/api/generate")
def generate_now(force: bool = False):
    config = load_config()
    if not config:
        raise HTTPException(status_code=400, detail="Not configured")
        
    result = _generate_for_device(config, force=force)
    
    if result and result.get("success"):
        # save updated status
        _persist_generation_state(config)
        return result
    elif result and result.get("skipped"):
        return result
    else:
        raise HTTPException(status_code=500, detail="Generation failed")

@app.get("/api/status")
def get_status():
    config = load_config()
    status = config.get("status", {})
    return status

# ─── Device-facing endpoints (for PhotoPainter / e-ink display) ──

def _load_display_image(request: Request):
    """Latest display image as RGB, with the battery line burned in if enabled.

    The PhotoPainter firmware reports its charge in X-Battery-Percentage on
    every fetch, so the line reflects the battery at the moment of download.
    """
    dithered = "data/images/latest_display_dithered.png"
    original = "data/images/latest_display.png"

    # Prefer dithered (optimised for e-ink), fall back to original
    if os.path.exists(dithered):
        image_path = dithered
    elif os.path.exists(original):
        image_path = original
    else:
        raise HTTPException(status_code=404, detail="No image generated yet")

    img = Image.open(image_path).convert("RGB")
    if load_config().get("battery_indicator") is True:
        pct = parse_battery_percentage(request.headers.get("x-battery-percentage"))
        if pct is not None:
            draw_battery_line(img, pct)
    return img


@app.get("/api/display")
def get_display_image(request: Request, format: str = "png"):
    """
    Returns the latest display image for the e-ink device.
    The PhotoPainter custom firmware should poll this URL.
    
    Query params:
      - format: "png" (default) or "bmp"
    
    Usage: Point your PhotoPainter firmware at:
      http://<your-server>:8000/api/display
    """
    img = _load_display_image(request)
    buf = io.BytesIO()

    if format == "bmp":
        # Convert to BMP for firmware that requires it
        try:
            img.save(buf, "BMP")
            with open("data/images/latest_display.bmp", "wb") as f:
                f.write(buf.getvalue())
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"BMP conversion failed: {e}")
        return Response(content=buf.getvalue(), media_type="image/bmp",
                        headers={"Content-Disposition": 'attachment; filename="display.bmp"'})

    img.save(buf, "PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Content-Disposition": 'attachment; filename="display.png"'})

@app.get("/api/display/raw")
def get_display_raw(request: Request):
    """Raw packed 4bpp framebuffer for the PhotoPainter firmware.

    The Glanceboard firmware decodes nothing: display_show_image() memcpy's the
    HTTP response body straight into the panel framebuffer, so a PNG or BMP
    renders as noise. It expects exactly DISPLAY_WIDTH * DISPLAY_HEIGHT / 2
    bytes (192000 for 800x480) of 4bpp data — two pixels per byte, even x in
    the high nibble, odd x in the low nibble, row-major.
    """
    img = _load_display_image(request)
    if img.size != (DISPLAY_WIDTH, DISPLAY_HEIGHT):
        img = img.resize((DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.LANCZOS)

    # Snap every pixel to the nearest palette entry, then translate our palette
    # index into the firmware's colour code.
    # int32, not int16: squared channel differences reach 255**2 = 65025,
    # which overflows int16 and silently mismatches colours.
    pixels = np.asarray(img, dtype=np.int32)
    palette = EINK_PALETTE.astype(np.int32)
    distances = ((pixels[:, :, None, :] - palette[None, None, :, :]) ** 2).sum(axis=3)
    nibbles = EINK_PALETTE_NIBBLES[np.argmin(distances, axis=2)]

    packed = ((nibbles[:, 0::2] << 4) | nibbles[:, 1::2]).astype(np.uint8)
    data = packed.tobytes()

    expected = DISPLAY_WIDTH * DISPLAY_HEIGHT // 2
    if len(data) != expected:
        raise HTTPException(
            status_code=500,
            detail=f"Packed {len(data)} bytes, expected {expected}",
        )

    return Response(content=data, media_type="application/octet-stream")


@app.get("/api/display/check")
def check_display_update():
    """
    Lightweight check for the device to see if a new image is available.
    Returns the last_generated timestamp so the device can skip re-downloading.
    """
    config = load_config()
    status = config.get("status", {})
    return {
        "last_generated": status.get("last_generated"),
        "has_image": os.path.exists("data/images/latest_display_dithered.png") or 
                     os.path.exists("data/images/latest_display.png"),
    }

@app.get("/api/preview")
def preview_prompt():
    config = load_config()
    if not config:
        raise HTTPException(status_code=400, detail="Not configured")

    ical_url = config.get("ical_url", "")
    timezone = config.get("timezone", DEFAULT_TIMEZONE)
    latitude = config.get("latitude")
    longitude = config.get("longitude")
    temp_unit = config.get("temp_unit", "celsius")

    characters_enabled = config.get("characters_enabled", True)

    tz = ZoneInfo(timezone)
    hour = datetime.now(tz).hour
    today = datetime.now(tz).date()

    today_events = []
    if ical_url:
        today_events = fetch_events_ical(ical_url, timezone=timezone, target_date=today)

    mode, banner_text, events = _determine_mode_and_events(
        hour, today_events, timezone
    )

    weather = None
    if latitude and longitude:
        weather = fetch_weather(latitude, longitude, temp_unit=temp_unit)

    characters = config.get("characters", [])
    selected_chars = config.get("selected_characters", [])
    if selected_chars and len(selected_chars) > 0:
        characters = [c for c in characters if c.get("id") in selected_chars]

    prompt_template = config.get("prompt_template", "")

    layout_placements = config.get("layout_placements", {})
    widget_configs = config.get("widget_configs", {})
    widget_data = {}
    
    api_key = config.get("openrouter_api_key", "") or config.get("api_key", "")
    text_model = config.get("text_model", DEFAULT_TEXT_MODEL)
    api_provider = config.get("api_provider", "google")
    
    needs_widget_data = any(w in layout_placements for w in ["stocks", "sports", "news", "history"])
    if needs_widget_data and api_key:
        stocks_symbols = widget_configs.get("stocks", {}).get("symbols") or (
            [widget_configs.get("stocks", {}).get("symbol")] if widget_configs.get("stocks", {}).get("symbol") else []
        )
        sports_team = widget_configs.get("sports", {}).get("team")
        widget_data = fetch_widget_data_via_gemini(
            api_key, api_provider=api_provider, text_model=text_model,
            stocks_symbols=stocks_symbols, sports_team=sports_team
        )

    # ─── Fetch email widget data (optional) ──────────────────────
    if "email" in layout_placements and api_key:
        max_emails = widget_configs.get("email", {}).get("max_emails", 5)
        email_data = fetch_email_widget_data(
            api_key, api_provider=api_provider, text_model=text_model,
            max_emails=max_emails
        )
        widget_data.update(email_data)

    prompt = build_prompt(
        events, characters, prompt_template,
        timezone=timezone, mode=mode,
        banner_text=banner_text,
        characters_enabled=characters_enabled,
        routine_weekday=config.get("default_routine_weekday", DEFAULT_ROUTINE_WEEKDAY),
        routine_weekend=config.get("default_routine_weekend", DEFAULT_ROUTINE_WEEKEND),
        weather=weather,
        birthdays=[],
        layout_placements=layout_placements,
        widget_configs=widget_configs,
        widget_data=widget_data,
        latitude=latitude,
    )

    scene_cast, _ = resolve_scene_characters(events, characters) if characters_enabled else ([], {})

    return {
        "prompt": prompt,
        "events": events,
        "characters_count": len(characters),
        "scene_characters": [c.get("name", "") for c in scene_cast],
        "weather": weather,
    }


# ─── Scheduler ──────────────────────────────────────────────────

from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager

def scheduled_task():
    print(f"Running scheduled check at {datetime.now().isoformat()}")
    config = load_config()
    if not config:
        return
        
    generation_schedule = config.get("generation_schedule", [4, 10, 14, 18])
    tz_str = config.get("timezone", DEFAULT_TIMEZONE)
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
    user_now = datetime.now(tz)
    current_hour = user_now.hour
    today_str = user_now.strftime("%Y-%m-%d")
    
    if current_hour not in generation_schedule:
        return
        
    slot_key = f"{today_str}_{current_hour}"
    status_dict = config.get("status", {})
    completed_slots = status_dict.get("completed_slots", [])
    
    # reset completed slots on new day
    if completed_slots and not completed_slots[0].startswith(today_str):
        completed_slots = []
        
    if slot_key in completed_slots:
        return
        
    print(f"Generating for scheduled slot: {slot_key}")
    try:
        result = _generate_for_device(config, force=False)
        if result and (result.get("success") or result.get("skipped")):
            completed_slots.append(slot_key)
            status_dict["completed_slots"] = completed_slots
            config["status"] = status_dict
            _persist_generation_state(config)
            print(f"Scheduled generation success or skipped")
    except Exception as e:
        print(f"Scheduled generation failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = BackgroundScheduler()
    # check every 15 minutes
    scheduler.add_job(scheduled_task, 'cron', minute='*/15')
    scheduler.start()
    yield
    scheduler.shutdown()

app.router.lifespan_context = lifespan

# ─── Static Files ───────────────────────────────────────────────
import os
os.makedirs("data/images", exist_ok=True)
os.makedirs("data/uploads", exist_ok=True)
app.mount("/images", StaticFiles(directory="data/images"), name="images")
app.mount("/uploads", StaticFiles(directory="data/uploads"), name="uploads")
# Mount the web application
# Note: we need to build the frontend first
try:
    app.mount("/", StaticFiles(directory="../web/dist", html=True), name="static")
except Exception:
    pass  # Frontend not built yet — that's fine for dev

# ─── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
