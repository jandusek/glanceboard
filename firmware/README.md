<!--
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the Apache License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# ESP32-S3 PhotoPainter — Firmware & Setup

The Waveshare ESP32-S3 PhotoPainter is a 7.3" colour e-ink display in a wooden frame with built-in WiFi. Glanceboard drives it with the community [esp32-photoframe](https://github.com/aitjcize/esp32-photoframe) firmware, which handles WiFi setup, scheduled image fetching, and dithered rendering — no SD card required.

## Flashing the Firmware

> ⚠️ **You must replace the stock firmware.** The Waveshare firmware that ships on the device cannot fetch images from a URL.

### Requirements
- **Google Chrome or Microsoft Edge** (Web Serial API required)
- **USB-C cable** connected to your computer

### Steps

1. Open the [esp32-photoframe web flasher](https://aitjcize.github.io/esp32-photoframe/#flash) in **Chrome or Edge**
2. Connect your PhotoPainter to your computer via USB-C
3. Click **Install** and select the USB serial device
4. Wait for the flash to complete (~2 minutes)
5. Unplug and replug the USB-C cable to reboot

If the device isn't detected, hold the **BOOT** button and press **PWR** to enter download mode, then try again.

## WiFi Setup

On first boot the device creates its own WiFi network and shows a QR code on the e-ink display.

1. **Scan the QR code** with your phone, or connect manually to the `PhotoFrame - XXXXXX` network and open `http://192.168.4.1`
2. Enter your **WiFi network name** and **password**
3. Save — the device reboots and joins your network

The firmware also supports SD-card provisioning (a `wifi.txt` file with the SSID and password, deleted after it is read) and setup through the iOS/Android companion app.

Once on your network, the display is reachable at [photoframe.local](http://photoframe.local) or its IP address from any browser on the same network.

## Pointing the Display at Glanceboard

On a computer connected to the same WiFi network:

1. Go to [photoframe.local](http://photoframe.local) and scroll down to **Settings**
2. On the **General** tab, set the **timezone** — rotation schedules follow the device's timezone, not your browser's
3. Open the **Auto Rotate** tab and enable **Auto-Rotate**
4. Set **Rotation Mode** to **URL - Fetch image from URL**
5. Paste your **Display Image URL** into the **Image URL** field. Copy it from the Glanceboard dashboard under **Settings → E-Ink Display Setup → 📋 Copy URL** — it looks like `http://YOUR_SERVER_IP:8000/images/latest_display.bmp`
6. Set a schedule (see below) and click **Save**

If your server requires authentication, the same panel has an **Access Token** field (sent as `Authorization: Bearer`) plus custom header name/value fields.

## Rotation Schedules

The Auto Rotate tab accepts up to 7 schedule rules. Each rule has a day selector — `EVERY DAY`, `WEEKDAYS`, `WEEKENDS`, or `CUSTOM` — and one of two timing modes:

| Mode | Use for | Example |
|------|---------|---------|
| **Throughout the day** | A repeating interval inside an active window | Every 2 hours, 07:00–23:00 |
| **At specific times** | Fixed times of day | 06:00 every morning |

An **Advanced (cron)** field exposes the underlying rules directly. They use a simplified 3-field format — `minute hour day-of-week` — and the next rotation is the earliest time matching any rule:

```
0 6 *              → 06:00 every day
0 7-23/2 *         → every 2 hours between 07:00 and 23:00
*/30 8-22 1-5      → every 30 min, 08:00–22:00, weekdays only
0 6 *  +  0 18 *   → 06:00 and 18:00 daily (two rules)
```

There is no separate quiet-hours setting — bound the active hours in the rules themselves to avoid overnight refreshes.

After saving, **Upcoming rotations** lists the next few wake times so you can confirm the schedule. Those times are shown in your browser's timezone while the device follows its own, so set the timezone first if the two differ.

> **Align the server with the display.** Glanceboard generates images on the hours listed in `generation_schedule` (default `[4, 10, 14, 18]`, in your configured timezone). Make sure a generation slot falls *before* each display refresh — for a 06:00 rotation, keep the 4am slot enabled.

## Power & Reachability

| Mode | Behaviour |
|------|-----------|
| **Deep sleep** (default) | ~10μA asleep, weeks to months on battery. Web UI reachable only while awake. |
| **Always-on** | ~40–80mA with auto light sleep, days to weeks on battery. Web UI always reachable. |

The PhotoPainter's AXP2101 PMIC detects USB power, so a USB-powered display stays awake automatically even with deep sleep enabled.

### Buttons

**While in deep sleep:**

| Button | Function |
|--------|----------|
| **BOOT** | Wake the device and start the web UI — it stays awake |
| **KEY** | Wake, rotate to the next image, then return to sleep |

## Troubleshooting

### Display stays black after flashing
- Unplug and replug the USB-C cable — e-paper displays retain their old image until the firmware boots and refreshes

### Can't find the `PhotoFrame - XXXXXX` WiFi network
- Make sure the flash completed successfully
- Try unplugging and replugging the device
- The setup network may take 10–15 seconds to appear after boot

### Can't reach photoframe.local
- Try the device's IP address directly — mDNS is unreliable on some networks
- If the device is on battery with deep sleep enabled, press **BOOT** to wake it first

### Image not updating
- Check that the image URL is correct and reachable from the device's network — a `localhost` address won't work; use the server's LAN IP
- Verify the Glanceboard server is running
- Check **Upcoming rotations** on the Auto Rotate tab, and confirm the device's timezone is set correctly
- Confirm a server generation slot runs before the display's refresh time
