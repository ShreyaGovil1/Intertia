"""Compatibility wrapper exposing the settings from `setting.config`.
This keeps existing imports like `from config.config import ...` working.
"""
from setting.config import *

__all__ = [
    'JWT_SECRET', 'JWT_ALGORITHM', 'ACCESS_TOKEN_EXPIRE_HOURS',
    'MAX_RUNNING_SPEED_MPS', 'MIN_ACCURACY_M', 'MIN_POLYGON_AREA_M2', 'MAX_CLOSE_DISTANCE_M',
    'MONGO_URL', 'DB_NAME', 'CORS_ORIGINS',
    'H3_RESOLUTION', 'MIN_HEX_CLAIM_COUNT'
]
