# Muse on Apple Watch

Push-to-talk from your wrist to **Muse from Meta**: your own agent, with its memory
and tools. Hold the button on the watch, talk, let go, and hear Muse answer.

```
Apple Watch app ──HTTP──▶ Mac bridge (paired as a Muse gadget) ──Noise──▶ your Muse
  hold to talk            musebridge serve                        realtime voice call
  hear the reply  ◀─────  streams reply audio back  ◀───────────
```

Muse has no public agent API. The way in is Meta's open-source
[Muse Gadgets SDK](https://github.com/facebookincubator/muse-gadget-sdk) (Apache-2.0),
which lets hobbyist devices talk to your Muse. It only supports Linux and ESP32, so
`bridge/` ports its one Linux-only piece, Bluetooth pairing, to macOS CoreBluetooth
and reuses the rest unchanged. Your Mac becomes the "gadget", and the watch talks to
the Mac.

- **`bridge/`** (Python): pairs the Mac with Muse once, then keeps a Muse voice call
  open and relays watch turns into it. Unlike the SDK's `musegadget run`, it gives
  Muse **no** shell or file access to your Mac.
- **`watch/`** (SwiftUI): the watchOS app, a watch-face complication and a
  "Talk to Muse" App Intent for Siri, Shortcuts and the Ultra's Action Button.

> Not affiliated with or endorsed by Meta. "Muse" is Meta's trademark. This uses the
> Gadgets SDK's community (developer) mode, which Muse labels as an unverified device.

## What you need

- A Mac with Bluetooth that can stay on, plugged in, while you use this (a MacBook works
  with the lid open).
- An Apple Watch paired to an iPhone, on watchOS 10 or later.
- The Muse app on that iPhone, signed in to your Muse account.
- Xcode (free, Mac App Store) and an Apple ID. A free Apple ID works, but apps it
  signs stop opening after 7 days and need reinstalling; a paid developer account
  ($99/year) doesn't have that limit.
- [Homebrew](https://brew.sh), Python 3.10+ and [uv](https://docs.astral.sh/uv/)
  (`brew install uv`).
- Optional, to use it away from home: [Tailscale](https://tailscale.com) on the Mac
  and the iPhone.

Plan on about an hour the first time, most of it Xcode downloading.

## Step 1: Get a Muse SDK token and turn on Developer mode

1. Go to [gadgets.muse.ai](https://gadgets.muse.ai), sign in with your Muse account,
   and create a token under **Settings > SDK tokens**. It starts with `mgst_`. Treat it
   like a password.
2. In the Muse app on your iPhone, turn on **Settings > Devices > Developer mode**.

## Step 2: Install the bridge on the Mac

```sh
git clone https://github.com/taichman/muse-watch.git
cd muse-watch/bridge
uv venv --python 3.12
uv pip install -e .
```

## Step 3: Pair the Mac with Muse (once)

Run this in **Terminal.app**, not inside an editor's or AI tool's sandboxed shell,
which can't reach Bluetooth:

```sh
.venv/bin/musebridge pair --sdk-token mgst_…
```

1. If macOS asks, allow Bluetooth for Terminal. If it never asks and the log warns
   that Bluetooth hasn't responded, enable Terminal under **System Settings > Privacy &
   Security > Bluetooth** and run it again.
2. Wait for `advertising as MuseGadgetXXXXXX`. Pairing stays open for 10 minutes.
3. In the Muse app: **Settings > Devices > Add Device**. Pick **Muse Gadget**.
4. Accept the community-device warning. If asked about Wi-Fi, pick **Use current
   connection**.
5. Terminal prints `Paired.`; the Muse app shows the gadget as **Connected**.

`musebridge info` shows the pairing; `musebridge unpair` forgets it. The token is
saved, so later runs don't need `--sdk-token`. Bluetooth is only used for this step.

## Step 4: Test the bridge without the watch

```sh
.venv/bin/musebridge -v serve
```

In a second Terminal tab:

```sh
cd muse-watch/bridge
say -o question.wav --file-format=WAVE --data-format=LEI16@24000 "Hi Muse, can you hear me?"
.venv/bin/musebridge say question.wav --out reply.wav && afplay reply.wav
```

If you hear Muse answer, the Mac side works. Stop `serve` with Ctrl-C.

## Step 5: Run the bridge in the background

```sh
.venv/bin/musebridge service install
```

This installs a launchd agent: the bridge starts at login, restarts if it crashes, and
keeps the Mac from idle-sleeping while it's on power. It does **not** stop a laptop
from sleeping when the lid closes.

- `musebridge service status` shows whether it's running.
- `musebridge service uninstall` removes it.
- The log is at `~/Library/Logs/MuseBridge/serve.log`.

## Step 6: Choose the address the watch will use

The watch always goes through your iPhone's connection (or its own Wi-Fi), so it needs
an address the iPhone can reach:

- **At home only:** the Mac's LAN IP, for example `http://192.168.1.20:8787`. An IP
  is more reliable than `yourmac.local` from the watch.
- **Anywhere (recommended):** install Tailscale on the Mac and the iPhone, sign both
  in to the same tailnet, and use the Mac's Tailscale IP, for example
  `http://100.x.y.z:8787` (shown in the Tailscale menu on the Mac). The watch's traffic goes
  through the iPhone's Tailscale connection, so this works away from home too.

Print the secret the watch needs:

```sh
.venv/bin/musebridge secret
```

## Step 7: Set up Xcode

1. Install Xcode from the Mac App Store, then in Terminal:

   ```sh
   sudo xcodebuild -license accept
   sudo xcodebuild -runFirstLaunch
   xcodebuild -downloadPlatform watchOS       # several GB
   brew install xcodegen
   ```

2. Open Xcode, go to **Settings > Accounts**, click **+**, and sign in with your Apple ID.

## Step 8: Get your iPhone and watch ready for development

1. Connect the iPhone to the Mac with a cable, unlock it, and tap **Trust**.
2. In Xcode, open **Window > Devices and Simulators**. If asked, turn on
   **Settings > Privacy & Security > Developer Mode** on the iPhone (it restarts).
3. Keep the watch unlocked and near the phone. After a few minutes it appears under
   the iPhone in that window. Then turn on **Settings > Privacy & Security >
   Developer Mode** on the watch (it restarts). The option only appears after Xcode
   has seen the watch.
4. The first time, Xcode spends 5–15 minutes "preparing" the watch. Wait for that
   to finish.

## Step 9: Configure, build and install the watch app

```sh
cd muse-watch/watch
cp Secrets.xcconfig.example Secrets.xcconfig
```

Edit `Secrets.xcconfig`:

| Setting | Value |
|---|---|
| `DEVELOPMENT_TEAM` | Your team ID: Xcode > Settings > Accounts, select your team. Free accounts show "(Personal Team)". |
| `MUSE_BUNDLE_ID` | Any reverse-DNS ID of your own, for example `com.yourname.musewatch` |
| `MUSE_BRIDGE_URL` | The address from step 6. Keep the `$()`: `http:/$()/100.x.y.z:8787` (xcconfig treats `//` as a comment) |
| `MUSE_BRIDGE_SECRET` | The secret from `musebridge secret` |

Then generate the project and open it:

```sh
xcodegen
open MuseWatch.xcodeproj
```

At the top of Xcode, pick the **MuseWatch** scheme and your watch, then press ▶.

If the watch says the developer isn't trusted, go to **Settings > General > VPN &
Device Management** on the watch and trust your Apple ID.

## Step 10: Use it

- Open **Muse** on the watch and allow the microphone.
- **Hold** the button, talk, and release to send. Or **tap** once, talk, then pause
  or tap again to send.
- Pressing while Muse is talking interrupts it and starts your next turn.
- **Complication:** long-press the watch face, tap **Edit**, choose a complication
  slot, and pick **Muse > Talk to Muse**. It opens the app ready to talk. If Muse
  isn't listed yet, open the app once, wait a minute, or restart the watch.
- **Siri, Shortcuts, Action Button:** "Talk to Muse" starts listening hands-free.
- The app's **Settings** screen checks the connection and lets you change the address.

You can now unplug the iPhone from the Mac. With a free Apple ID, plug it back in and
press ▶ again every 7 days.

## More than one person on the same Mac

Each person talks to their own Muse, so each gets a **profile**: their own gadget
identity, Muse pairing, watch secret, port and log. Each person does steps 1 and 3
from their own Muse account:

```sh
.venv/bin/musebridge --profile alex pair --sdk-token mgst_…   # their token and Muse app
.venv/bin/musebridge --profile alex service install --port 8788
.venv/bin/musebridge --profile alex secret --port 8788
```

Install the same watch app on their watch (step 8 with their iPhone), with their URL
(port `8788`) and secret. You can either set those in `Secrets.xcconfig` before
building their copy, or override them on the command line:

```sh
xcodebuild -project MuseWatch.xcodeproj -scheme MuseWatch -destination 'name=<their watch>' \
  -allowProvisioningUpdates MUSE_BRIDGE_URL='http:/$()/100.x.y.z:8788' MUSE_BRIDGE_SECRET=<secret> build
```

Their log is at `~/Library/Logs/MuseBridge/serve-alex.log`.

## Troubleshooting

**The Muse app says "No devices found".** macOS advertises less than Linux: it drops
the manufacturer data and fits only 31 bytes. So the bridge advertises the
`MuseGadget…` name alone, which is what the app looks for. Make sure pairing is
running in Terminal.app and Bluetooth is allowed for Terminal. To see what the Mac
sends, scan with nRF Connect on the iPhone. `MUSEBRIDGE_ADVERTISE=both` adds the
service UUID back, but that pushes the name out.

**The watch is stuck on "Muse is thinking".**
- Check `~/Library/Logs/MuseBridge/serve.log`. A line like `heard 1.7 s from the watch
  (loudest RMS 1200; held=False…)` means your audio arrived.
- No such line means the watch can't reach the Mac: check the address, and that
  Tailscale is on on both the Mac and the iPhone.
- `no reply from Muse within 60 s` means Muse stayed silent. The bridge then hangs up
  that call, so the next press starts a fresh one.

**The app crashes.** Pull the crash report from the watch:

```sh
xcrun devicectl list devices
xcrun devicectl device info files --device <watch id> --domain-type systemCrashLogs
```

**The app stops opening after a week.** That's the free Apple ID's 7-day signing.
Reconnect the iPhone and press ▶ in Xcode again.

**The watch doesn't show up in Xcode.** Keep the iPhone connected by cable and
unlocked, the watch unlocked and close by, and Wi-Fi and Bluetooth on for all three.

## How it works

- **Pairing:** the Mac runs the SDK's real setup protocol over a CoreBluetooth
  peripheral (`bridge/src/musebridge/mac_ble.py`). The Muse app provisions a device
  token over Bluetooth, and from then on everything goes over the internet.
- **Voice:** `musebridge serve` opens the SDK's realtime voice call to your Muse
  (PCM16, 24 kHz mono) on the first turn and hangs up after 5 idle minutes. Each watch
  turn streams microphone audio in, and Muse's spoken reply streams back as it's
  generated. The bridge waits through short pauses, so a "let me check…" lead-in
  doesn't cut off the real answer.
- **Watch networking:** watchOS restricts WebSockets, so a turn is one HTTP request:
  a streamed upload body (the microphone) and a streamed response (the reply).

Bridge API (all `/v1` routes need `Authorization: Bearer <secret>`):

| Route | |
|---|---|
| `POST /v1/talk` | body: PCM16LE mono 24 kHz, streamed while talking; response: Muse's reply, same format, streamed. 204 if Muse stayed silent. |
| `GET /v1/status` | `{"paired", "call_active"}` |
| `POST /v1/hangup` | end the open voice call |

Run the bridge's tests with `.venv/bin/python -m pytest` after
`uv pip install -e '.[test]'`.

## Security notes

- The secret (in `~/Library/Application Support/MuseBridge/bridge_secret`) is the only
  thing protecting your Muse from anyone who can reach the bridge's port. Keep the
  bridge on your LAN or tailnet. Don't port-forward it.
- Muse credentials and the SDK token live in the same folder with owner-only
  permissions. `musebridge unpair` removes the pairing; you can also remove the
  gadget in the Muse app and revoke the token at gadgets.muse.ai.
- The bridge registers no commands, so Muse can't run anything on your Mac.

## Limits

- No transcripts on the watch: the voice API streams audio only.
- One voice call per Muse: starting voice in the Muse app replaces the watch's call,
  and the other way round.
- Muse occasionally doesn't answer a turn. The bridge recovers on the next press.

## License

Apache-2.0, like the Muse Gadgets SDK it builds on.
