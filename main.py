"""Limitador - punto de entrada.

Uso:
    python main.py                 # interfaz gráfica (pide UAC automáticamente)
    python main.py --cli --down 5  # modo consola limitando a 5 MB/s
    python main.py --cli --check   # solo diagnóstico
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from limitador.app import main

if __name__ == "__main__":
    sys.exit(main())
