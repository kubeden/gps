# GamepadSpeak for WoW Forever

Press a controller button, talk, press it again. Your words are typed into
chat and sent, and the chat box closes. No keyboard, no pause in play.

Two parts:

- `addon/GamepadSpeak` runs inside WoW Forever (the `_forever_` client).
  It owns the settings, shows a small "Recording / Transcribing" indicator,
  opens the chat box at the right moment, and closes it after the text is sent.
- `helper/` is a small Python program that runs next to the game on macOS,
  Windows, or Linux. It watches the same controller button, records the mic,
  transcribes locally with Whisper, then types the text into the game and
  presses Enter.

A WoW addon cannot hear the microphone or run speech recognition, which is why
the helper exists. The game never sees anything except ordinary key presses,
and no audio leaves your PC.

## Install

macOS / Linux:

```
./install.sh
```

Windows (PowerShell):

```
.\install.ps1
```

That copies the addon into `Interface/AddOns` of WoW Forever and prepares the
helper's Python environment. Re-run it after editing the addon. (It is a copy
rather than a symlink on purpose: the client did not load saved variables for
a symlinked addon folder.)
Set `WOW_DIR` first if your install is somewhere else. The helper needs Python
3.10 to 3.13; [uv](https://docs.astral.sh/uv/) is used when present, otherwise
a plain venv is created on first run.

## First-time setup

1. Start WoW Forever with the controller connected. Enable GamepadSpeak in the
   addon list (tick "Load out of date AddOns" if the beta build number moved).
2. In game: `/gps setup`, then press the controller button you want as the
   trigger. Pick one with no game action: Create, the touchpad click, or a spare
   D-pad direction. The addon saves it and reloads the UI.
3. Start the helper:

   ```
   ./run-helper.sh            # macOS / Linux
   .\run-helper.ps1           # Windows
   ```

   The first run downloads the Whisper model (about 150 MB for `base`).
   On macOS the terminal you run it from needs Microphone and Accessibility
   permission (System Settings > Privacy & Security). On Linux, keystroke
   injection needs X11 (or XWayland) and `xdotool` for the foreground check.
4. Check everything lines up:

   ```
   ./run-helper.sh --check
   ```

## Using it

- Press the trigger: a short high beep, and the game shows "Recording".
- Say your message.
- Press the trigger again: a two-note beep, the game shows "Transcribing" and
  opens the chat box. Within a moment the text is typed and sent, and the box
  closes.

The message goes to whatever channel your chat box last used. Pin it with
`/gps channel party` (or say, raid, guild, officer, instance) and go back to
the sticky behavior with `/gps channel sticky`.

Other commands: `/gps status`, `/gps test hello there` (exercises the send path
without the helper), `/gps api` (lists which chat functions the client has),
`/gps reset`.

## Beta client bug: saved variables never load

WoW Forever beta build 69913 (interface 16001) writes every addon's saved
variables to disk but never reads them back, so all addon settings reset on
every login and reload. This is a client bug, tracked by the community
([forum thread](https://us.forums.blizzard.com/en/wow/t/savedvariables-never-load-in-the-beta-%E2%80%94-all-addon-settings-reset-on-login-69913/2354798),
[bug report](https://github.com/ClassicWoWCommunity/forever-bugs/issues/34)).

GamepadSpeak works around it the way [WickKeeper](https://github.com/Wicksmods/WickKeeper)
does: it mirrors its settings into one account macro named `GPSpeak`, which is
stored on Blizzard's server and survives. Don't edit or delete that macro. The
WTF file is still written on every reload, which is what the helper reads.
Macros can arrive a moment after the first login of a session; the addon
restores as soon as they do and says so in chat.

## How the pieces talk

- Addon → helper: the addon writes `GamepadSpeakDB` to
  `WTF/Account/<account>/SavedVariables/GamepadSpeak.lua`. WoW only flushes
  that file on `/reload` or logout, so setup reloads for you. The helper polls
  the file every two seconds and rebinds when it changes.
- Controller: the helper uses SDL's game controller API, the same library WoW
  Forever uses, so the `PAD*` names in the addon map one-to-one to SDL buttons
  (PAD1 = A/Cross, PADSOCIAL = Create/View, PADBACK = touchpad click, and so
  on). An unrecognized pad can still be used with `--raw-button N`.
- Helper → game: after transcription the helper presses Enter (the game's own
  Open Chat binding), types the text, presses Enter to send, then types
  `/click InputFunctionBindingButton_PAD2 LeftButton 1` and Enter. That is
  exactly what the gamepad Back button does: Blizzard routes Circle/B through
  an override binding that clicks that named button, and `/click` is a secure
  slash command. Every step is a real key press handled by Blizzard code.
- Why it's done this way: in WoW Forever's gamepad style the chat box keeps
  focus after a send, and any attempt by addon code to open or clear chat
  focus runs into a protected gamepad call. That taint spreads into the
  gamepad binding stack and can freeze the client. SecureHandler snippets
  would be the textbook answer, but this beta client cannot compile them
  (`loadstring_untainted` is missing, another known client bug). So the addon
  never touches chat focus itself; it only computes the close command from
  Blizzard's key constant and stores it for the helper.
- Safety: the helper only types when a World of Warcraft window is in the
  foreground (where the platform lets it check). Otherwise it logs the
  transcript and plays an error beep.

## Helper options

```
--language bg          speech language (default: auto-detect)
--model small          Whisper model: tiny, base, small, medium, large-v3 (default: base)
--device cuda          run Whisper on an NVIDIA GPU (default: auto)
--button PADSOCIAL     override the trigger from the addon
--raw-button 4         raw joystick button index for unmapped pads
--input-device NAME    pick a specific microphone
--open-key ENTER       key that opens chat before typing: ENTER (default), a binding, or none
--close-command CMD    slash command typed after sending (default auto = from the addon; none to skip)
--close-key none       extra key pressed after sending, e.g. ESCAPE (default none)
--silent               no beeps
--max-seconds 60       auto-stop a forgotten recording
--any-app              type even if WoW is not in the foreground (testing)
--wow-dir PATH         the _forever_ folder if not in the default place
--check                print status and exit
```

`base` is a good default for English on any recent CPU. For Bulgarian or other
languages, `small` with `--language bg` is noticeably more accurate.

## Things to verify in the first live test

These depend on WoW Forever behavior that can't be confirmed outside the game:

1. The addon sees the trigger press while the game's own gamepad UI is active
   (it relies on `OnGamePadButtonDown` with input propagation on).
2. The helper still receives controller input while WoW holds the pad. SDL is
   started with background events allowed; if nothing arrives, the pad is being
   opened exclusively and `--raw-button` over the joystick API is the fallback.
3. Synthetic key events reach the chat box and don't flip the UI out of gamepad
   style (`InputDeviceInterfaceStyle`). If they do, try `--open-key none` with
   `/gps open on` so only the text and Enter are injected.

## Layout

```
addon/GamepadSpeak/          GamepadSpeak.toc, GamepadSpeak.lua, Bindings.xml
helper/gamepadspeak_helper.py  controller, mic, Whisper, keystrokes, settings watcher
helper/pyproject.toml        dependencies (pygame-ce, sounddevice, faster-whisper, pynput)
install.sh / install.ps1     put the addon in WoW + prepare the helper env
run-helper.sh / run-helper.ps1  run the helper in the foreground with logs
```
