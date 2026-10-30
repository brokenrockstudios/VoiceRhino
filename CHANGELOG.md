# Changelog

What changed in each VoiceRhino release. Download releases from the
[Releases page](https://github.com/brokenrockstudios/VoiceRhino/releases).

## 0.1.1 (2026-10-29)

A clearer first launch: it's now always obvious whether VoiceRhino is still getting ready or
already listening.

- **Loading is shown the whole way through.** The caption windows say what's happening while the
  speech model downloads, unpacks and loads, and that captions start when it's ready. Before,
  the windows went blank for up to a minute after the download finished (while the model was
  unpacked), which looked as if nothing was working.
- **Loading messages stay up** until captions start or an error appears, instead of fading
  after a few seconds while the download is still going.
- **The first-run notification says it's setting up**: it says the speech model is downloading
  (about 640 MB, first time only), instead of "VoiceRhino is running".
- **"Ready!" on the very first run**: once it's listening, the windows say so ("Captions
  appear here when someone talks") until the first caption shows, so an empty window doesn't
  look broken.
- **The "very quiet" warning no longer lingers over the game.** A caption window shows it
  only along with captions, so it clears when they do; the history window still keeps it up.
  Each window can also hide it: Settings › Windows › _Show audio warnings_, or right-click
  the window while unlocked.
- **Text opacity**: Settings › Windows has a _Text opacity_ slider next to _Background
  opacity_, so the game can show through the captions. The text, its outline and the speaker
  names fade together, down to 10%.
- **Source numbers in the history window**: with several sources (e.g. Discord and a browser),
  Settings › Windows › _Show source number_ puts 1, 2, … in a column in front of each line, next
  to the source's name. Sources are numbered in the order of Settings › Audio, which now has
  _Move up_ and _Move down_.
- **See what VoiceRhino costs**: Settings › General › _Resources_ shows its own CPU (in cores and
  % of your CPU), RAM and GPU use right now, with 1- and 5-minute CPU averages. A history window
  can show the same as one line at the top (Settings › Windows › _Show VoiceRhino's CPU and
  memory use_), to check whether it gets in the way of a game. It counts the speech models and
  the Hy-MT2 translation helper too, like Task Manager.
- **Check for updates** (Settings › General): asks GitHub for the latest release and links to
  its download page. Only when you click it; VoiceRhino never checks by itself.
- **Outline color** for caption windows, next to the outline width (black by default).
- Settings › Windows reads better: _Text opacity_ sits with the text settings, before the
  background ones; _Move to screen_ moved to the bottom; the outline, which history windows
  don't draw, only shows for caption windows; the tick boxes are grouped under _Options_; and
  the explanations moved into tooltips (hover a setting), so the tab takes far less room.
- README: screenshots of the caption windows and the Settings tabs.

## 0.1.0 (2026-10-29)

First public release.

- Live captions on Windows 10 and 11, recognized on your own PC (CPU by default, no GPU needed).
- Click-through caption windows over games (Ctrl+Alt+C locks and unlocks them) and a history
  window for the last minute.
- Captions from any output device, a microphone, or a single app such as Discord.
- Several speech models, including multilingual ones, plus optional word-by-word live text.
- Speaker colors by voice, or Discord names.
- Optional translation into English, also on your PC.
- No telemetry, and nothing anyone says is saved to disk.
