"""Constantes structurelles (non configurables) du format audio."""


RATE = 16_000                      # audio archivé : WAV 16 kHz mono 16 bits
FRAME = RATE // 50                 # un paquet Discord = 20 ms = 320 échantillons
FRAME_BYTES = FRAME * 2
