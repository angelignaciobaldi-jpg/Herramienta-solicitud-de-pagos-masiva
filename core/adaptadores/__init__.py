"""Adaptadores de ingesta.

Los tres —Excel, CFDI y captura manual— producen exactamente el mismo par
`(Solicitud, [Partida])`. El motor RPA no sabe de dónde vino un registro, y por
eso agregar una fuente nueva no obliga a tocar la automatización.
"""
