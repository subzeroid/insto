# VHS tape for the README / docs demo GIF.
# Regenerate with: `brew install vhs && vhs docs/demo.tape` (from the repo root).
# Requires `insto` on PATH (`uv tool install --editable .` for dev) and a
# valid HIKERAPI_TOKEN — `insto setup` once. Each render hits the live
# /info, /posts, fans likers+comments — ~14 requests per run.
#
# After VHS renders, run the 2.5x speedup pass so docs/demo.gif is
# under 1 MB and ~12 s long (raw render is ~30 s, dominated by the
# /fans backend wait):
#
#   ffmpeg -y -i docs/demo.gif -vf \
#     "setpts=PTS/2.5,palettegen=stats_mode=diff" /tmp/palette.png
#   ffmpeg -y -i docs/demo.gif -i /tmp/palette.png -filter_complex \
#     "[0:v]setpts=PTS/2.5[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" \
#     -loop 0 docs/demo.gif

Output docs/demo.gif

Set Shell "zsh"
Set FontSize 13
Set Width 1180
Set Height 880
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

# Force the default-bundled theme so the gif always shows aiograpi
# regardless of the recording machine's saved config.
Hide
Type "export INSTO_THEME=aiograpi"
Enter
Show

Type "insto --no-progress"
Sleep 200ms
Enter

# Welcome banner + live HikerAPI balance.
Sleep 4500ms

# 1) /info ferrari — rich profile panel with avatar link + created date.
Type "/info ferrari"
Sleep 200ms
Enter
Sleep 4000ms

# 2) /where ferrari --limit 20 — 🔥 geo fingerprint: anchor + centroid + bar chart.
Type "/where ferrari --limit 20"
Sleep 200ms
Enter
Sleep 4500ms

# 3) /fans nasa --limit 5 — 🔥 superfan ranking with ❤️ + 💬 breakdown.
# /fans makes 2N backend calls (5 likers + 5 comments here = 10), each
# ~1-2s — give it 14s to render the table fully.
Type "/fans nasa --limit 5"
Sleep 200ms
Enter
Sleep 14000ms

Type "/quit"
Sleep 200ms
Enter
Sleep 600ms
