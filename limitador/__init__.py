"""Limitador: limitador de ancho de banda para Windows (por aplicación y por equipo).

Módulos
-------
``engine``    captura WinDivert + hilos de inyección
``shaper``    limitación de tasa con presupuesto de retardo (AQM)
``classify``  lectura rápida de las cabeceras IP/TCP/UDP de cada paquete
``procmap``   atribución puerto local -> proceso
``filters``   construcción y validación de filtros WinDivert
``driver``    ciclo de vida del driver (instalación estable, reparación, diagnóstico)
``rules``     configuración y reglas por aplicación
``gui``       interfaz Tkinter + icono de bandeja
``cli``       modo consola con medición
"""

from .rules import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION", "__version__"]
__version__ = APP_VERSION
