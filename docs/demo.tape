# VHS tape for the README / docs demo GIF.
# Regenerate with: `brew install vhs && vhs docs/demo.tape` (from the repo root).
# Requires `insto` on PATH (`uv tool install --editable .` for dev) and a
# valid HIKERAPI_TOKEN — `insto setup` once. Each render hits the live
# /sys/balance + /info endpoints, ~6 requests per run.

Output docs/demo.gif

Set Shell "zsh"
Set FontSize 14
Set Width 1100
Set Height 760
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

# Force the default-bundled theme so the gif always shows aiograpi
# regardless of the recording machine's saved config.
Hide
Type "export INSTO_THEME=aiograpi"
Enter
Show

Type "insto"
Sleep 200ms
Enter

# Welcome banner + live HikerAPI balance — give it time to fetch /sys/balance.
Sleep 4500ms

# Slash popup: typing '/' opens the menu with command names + signatures.
Type "/"
Sleep 1800ms

# Narrow live as we type. The popup keeps showing matches.
Type "in"
Sleep 1500ms

# Run a real lookup with inline target (does not mutate session state).
Ctrl+U
Sleep 200ms
Type "/info instagram"
Sleep 400ms
Enter
Sleep 4500ms

# /theme — argument completion: <Tab> after the space lists every palette.
Type "/theme "
Sleep 1500ms

# Pick instagram and switch live.
Ctrl+U
Sleep 200ms
Type "/theme instagram"
Sleep 400ms
Enter
Sleep 1500ms

# Fresh balance from /sys/balance.
Type "/quota"
Sleep 200ms
Enter
Sleep 2200ms

Type "/exit"
Sleep 200ms
Enter
Sleep 600ms
