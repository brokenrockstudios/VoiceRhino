"""Entry point of the frozen VoiceRhino.exe (packaging/voicerhino.spec)."""

import sys

from voicerhino.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
