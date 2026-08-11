# A recording of what WCC actually returns, made with vhs
# (https://github.com/charmbracelet/vhs):
#
#   vhs docs/demo/demo.tape
#
# Scripted rather than hand-recorded on purpose. A GIF of a terminal is a claim
# about what the software does, and one that can be regenerated from a file is a
# claim someone else can check — the same reason every number in this repository
# ships with the command that produced it.
#
# The command below is real and unedited. There is no narration inside the
# recording: a first attempt typed shell comments for the camera and zsh
# answered "command not found: #" three times, which is exactly the kind of
# staged-then-broken detail a demo should not have. The commentary lives beside
# the image in the README, where it cannot pretend to be terminal output.

Output docs/demo/demo.gif

Set Shell zsh
Set FontSize 16
Set Width 1200
Set Height 470
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Hide
Type "clear"
Enter
Sleep 500ms
Show

Type "uv run python docs/demo/inspect_one.py"
Sleep 700ms
Enter
Sleep 15s
