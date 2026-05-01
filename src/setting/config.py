import os
from datetime import timedelta

JWT_SECRET = os.environ.get('JWT_SECRET', 'intertia-secret-key-change-in-production')
JWT_ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_HOURS = 24

# Database / infra
MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
DB_NAME = os.environ.get('DB_NAME', 'test_database')

# CORS
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://localhost:8000,http://localhost:8001').split(',')

# Anti-cheat and geometries
MAX_RUNNING_SPEED_MPS = 12.0
MIN_ACCURACY_M = 50.0
MIN_POLYGON_AREA_M2 = 100.0
MAX_CLOSE_DISTANCE_M = 30.0

__all__ = [
    'JWT_SECRET', 'JWT_ALGORITHM', 'ACCESS_TOKEN_EXPIRE_HOURS',
    'MONGO_URL', 'DB_NAME', 'CORS_ORIGINS',
    'MAX_RUNNING_SPEED_MPS', 'MIN_ACCURACY_M', 'MIN_POLYGON_AREA_M2', 'MAX_CLOSE_DISTANCE_M'
]
