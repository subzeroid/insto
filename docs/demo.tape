# VHS tape for the README / docs demo GIF.
# Regenerate with: `brew install vhs && vhs docs/demo.tape` (from the repo root).
# Requires `insto` on PATH (`uv tool install --editable .` for dev) and a
# valid HIKERAPI_TOKEN — `insto setup` once. Each render hits the live
# /sys/balance + /info endpoints, ~5 requests per run.

Output docs/demo.gif

Set Shell "zsh"
Set FontSize 14
Set Width 1000
Set Height 720
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

# Quiet shell preamble — kept Visible so the viewer sees the launch.
Type "insto"
Sleep 200ms
Enter

# Welcome banner + live balance — give it time to fetch /sys/balance.
Sleep 4500ms

# Slash popup: the moment '/' is typed, every command appears in the menu.
Type "/"
Sleep 2000ms

# Narrow it down — popup filters live as you type.
Type "in"
Sleep 1500ms

# Clear and run a real lookup with inline target (does not mutate session state).
Ctrl+U
Sleep 200ms
Type "/info instagram"
Sleep 400ms
Enter
Sleep 4500ms

# Fresh balance from /sys/balance.
Type "/quota"
Sleep 200ms
Enter
Sleep 2500ms

# Help screen — full command surface.
Type "/help"
Sleep 200ms
Enter
Sleep 4000ms

Type "/exit"
Sleep 200ms
Enter
Sleep 600ms
